import ast
import copy
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import chat_messages as chat
import shop
import store
from api import create_api_app


def inline(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as done:
        return done.value
    finally:
        coroutine.close()
    raise AssertionError("Unexpected background work")


class ChatPresentationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = patch.object(store, "DB_PATH", str(Path(self.directory.name) / "test.db"))
        self.database.start()
        store.init_db()
        store.set_setting("command_prefixes", "!")

    def tearDown(self):
        self.database.stop()
        self.directory.cleanup()

    def assert_safe(self, messages):
        for message in messages:
            self.assertLessEqual(chat.size(message), chat.LIMIT)
            self.assertNotIn("\n", message)
            self.assertNotIn("\r", message)

    def command(self, name):
        tree = ast.parse(Path("bot.py").read_text(encoding="utf-8"))
        component = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "CowCommands")
        function = copy.deepcopy(next(node for node in component.body if isinstance(node, ast.AsyncFunctionDef) and node.name == name))
        function.decorator_list = []
        namespace = {"store": store, "shop": shop, "chat": chat, "require_command": AsyncMock(return_value=True),
                     "get_author_name": lambda ctx: "viewer", "get_author_mention": lambda ctx: "@viewer",
                     "is_mod_or_broadcaster": lambda ctx: True}
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), function], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), "bot.py", "exec"), namespace)
        return namespace[name]

    def test_shop_pagination_details_and_prefix_leave_balances_unchanged(self):
        first = shop.catalog_messages()[0]
        second = shop.catalog_messages("2")[0]
        self.assertIn("1/2", first)
        self.assertIn("2/2", second)
        self.assertIn("!shop page 2", first)
        self.assertIn("!buy <code>", first)
        self.assertIn("challenge", first)
        self.assertIn("gold-glow", second)
        detail = shop.catalog_messages("shoutout")[0]
        self.assertIn("Approval required", detail)
        self.assertIn("!buy shoutout <message>", detail)
        self.assert_safe([first, second, detail])
        self.assertEqual(store.get_points("viewer"), 100)
        self.assertEqual(shop.snapshot()["total"], 0)
        for query in ("0", "99999999999999999999", "page nope", "does-not-exist"):
            self.assert_safe(shop.catalog_messages(query))

    def test_large_emoji_catalog_preserves_every_code_across_pages(self):
        codes = []
        for index in range(20):
            code = f"reward-{index}-" + "x" * 20
            ok, error = shop.save_item({"slug": code, "name": "🐮" * 60, "category": "shoutout", "price": 1000000})
            self.assertTrue(ok, error)
            codes.append(code)
        messages = []
        for page in range(1, 50):
            message = shop.catalog_messages(f"page {page}")[0]
            if "Choose a page" in message:
                break
            messages.append(message)
        self.assert_safe(messages)
        for code in codes:
            self.assertIn(f"[{code}]", " ".join(messages))

    def test_numeric_item_codes_have_details_and_explicit_page_navigation(self):
        self.assertTrue(shop.save_item({"slug": "2", "name": "Second reward", "category": "shoutout", "price": 100})[0])
        self.assertIn("!buy 2 <message>", shop.catalog_messages("2")[0])
        self.assertIn("2/", shop.catalog_messages("page 2")[0])

    def test_winner_messages_keep_all_25_mentions_and_bound_long_names(self):
        winners = [f"viewer{index:02d}" + "x" * 17 for index in range(25)]
        messages = chat.giveaway_results("🐮" * 200, winners)
        self.assertGreater(len(messages), 1)
        self.assert_safe(messages)
        for winner in winners:
            self.assertIn("@" + winner, " ".join(messages))
        reroll = chat.giveaway_results("Friday loot", winners, reroll=True, replaced="oldviewer", selected="newviewer")
        self.assertIn("@newviewer", reroll[0])
        self.assertIn("@oldviewer", reroll[0])
        self.assertNotIn("@" + winners[0], reroll[0])
        self.assert_safe([chat.giveaway_open("A\nlong\tname" * 100, "!", 25), *reroll])

    def test_chat_and_dashboard_share_giveaway_wording_and_pin_behavior(self):
        announce = AsyncMock()
        app = create_api_app(get_status=lambda: {}, announce=announce)
        route = next(r for r in app.router.routes() if r.resource.canonical == "/api/giveaway")

        def call(payload):
            with patch.dict(os.environ, {"API_SECRET": ""}):
                return inline(route.handler(SimpleNamespace(headers={}, json=AsyncMock(return_value=payload))))

        self.assertEqual(call({"action": "start", "name": "Friday loot", "count": 2}).status, 200)
        api_message = announce.await_args.args[0]
        self.assertTrue(announce.await_args.kwargs["pin_giveaway"])
        store.cancel_giveaway()
        ctx = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id="start-id")), reply=AsyncMock())
        component = SimpleNamespace(bot=SimpleNamespace(pin_giveaway_message=AsyncMock(), unpin_giveaway_message=AsyncMock()))
        command = self.command("giveaway")
        inline(command(component, ctx, "start", name="Friday loot 2"))
        self.assertEqual(ctx.send.await_args.args[0], api_message)
        component.bot.pin_giveaway_message.assert_awaited_once_with("start-id")
        inline(command(component, ctx))
        self.assertIn("You're in!", ctx.reply.await_args.args[0])
        inline(command(component, ctx))
        self.assertIn("already entered", ctx.reply.await_args.args[0])
        # Winner pagination must still unpin exactly once, with every winner announced.
        store.cancel_giveaway()
        store.start_giveaway("Big draw", 25)
        for index in range(25):
            store.enter_giveaway(f"viewer{index:02d}" + "x" * 17)
        announce.reset_mock()
        self.assertEqual(call({"action": "end"}).status, 200)
        self.assertGreater(announce.await_count, 1)
        self.assertEqual(sum(call.kwargs.get("unpin_giveaway", False) for call in announce.await_args_list), 1)
        messages = [call.args[0] for call in announce.await_args_list]
        self.assert_safe(messages)
        for index in range(25):
            self.assertIn(f"@viewer{index:02d}" + "x" * 17, " ".join(messages))

    def test_profile_and_purchase_show_human_readable_cosmetic_time(self):
        store.change_points("viewer", 9900)
        self.assertTrue(shop.buy("viewer", "gold-glow")[0])
        ctx = SimpleNamespace(send=AsyncMock())
        inline(self.command("profile")(None, ctx))
        message = ctx.send.await_args.args[0]
        self.assertIn("gold", message)
        self.assertIn("left", message)
        self.assertNotIn("+00:00", message)
        self.assert_safe([message])
        store.change_points("other", 9900)
        ok, result = shop.buy("other", "gold-glow")
        self.assertTrue(ok, result)
        self.assertIn("30 min added", result)
        self.assertNotIn("+00:00", result)
