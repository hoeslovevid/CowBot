import ast
import copy
import json
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import dashboard
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


class ShopTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = patch.object(store, "DB_PATH", str(Path(self.directory.name) / "test.db"))
        self.database.start()
        store.init_db()
        self.now = store.utc_now()
        self.clock = patch.object(store, "utc_now", side_effect=lambda: self.now)
        self.clock.start()
        store.change_points("buyer", 9900)

    def tearDown(self):
        self.clock.stop()
        self.database.stop()
        self.directory.cleanup()

    def item(self, slug):
        return next(item for item in shop.list_items() if item["slug"] == slug)

    def edit(self, original_slug, **changes):
        ok, message = shop.save_item({**self.item(original_slug), **changes})
        self.assertTrue(ok, message)

    def test_seeds_five_types_once_and_preserves_item_edits(self):
        self.assertEqual({i["category"] for i in shop.list_items()}, set(shop.CATEGORIES))
        self.edit("challenge", slug="custom-challenge", enabled=0)
        store.init_db()
        self.assertEqual(len(shop.list_items()), 5)
        self.assertFalse(self.item("custom-challenge")["enabled"])

    def test_challenge_queue_approval_and_completion(self):
        ok, message = shop.buy("buyer", "challenge", "No healing next round")
        self.assertTrue(ok, message)
        state = shop.snapshot()
        order = state["pending"][0]
        self.assertEqual(order["status"], "pending")
        self.assertEqual(store.get_points("buyer"), 9500)
        self.assertFalse(shop.review(order["id"], "complete")[0])
        self.assertTrue(shop.review(order["id"], "approve")[0])
        self.assertEqual(shop.snapshot()["pending"][0]["status"], "approved")
        self.assertTrue(shop.review(order["id"], "complete")[0])
        self.assertEqual(shop.snapshot()["pending"], [])
        self.assertEqual(shop.snapshot()["orders"][0]["status"], "completed")
        self.assertFalse(shop.review(order["id"], "approve")[0])

    def test_rejection_refunds_original_price_restores_stock_exactly_once(self):
        self.edit("challenge", stock=1)
        self.assertTrue(shop.buy("buyer", "challenge", "Try a challenge")[0])
        order = shop.snapshot()["pending"][0]
        self.assertEqual(self.item("challenge")["stock"], 0)
        self.assertFalse(shop.buy("other", "challenge", "Another challenge")[0])
        self.edit("challenge", price=999)
        self.assertTrue(shop.review(order["id"], "reject")[0])
        self.assertEqual(store.get_points("buyer"), 10000)
        self.assertEqual(self.item("challenge")["stock"], 1)
        self.assertFalse(shop.review(order["id"], "reject")[0])
        self.assertEqual(store.get_points("buyer"), 10000)
        self.assertTrue(shop.buy("buyer", "challenge", "A corrected request")[0])

    def test_approved_challenge_can_be_cancelled_and_refunded(self):
        self.assertTrue(shop.buy("buyer", "challenge", "A challenge")[0])
        order_id = shop.snapshot()["pending"][0]["id"]
        self.assertTrue(shop.review(order_id, "approve")[0])
        self.assertTrue(shop.review(order_id, "reject")[0])
        self.assertEqual(store.get_points("buyer"), 10000)
        self.assertFalse(shop.review(order_id, "complete")[0])
        self.assertFalse(shop.review(order_id, "reject")[0])

    def test_title_ownership_equipping_and_no_duplicate_charge(self):
        self.assertTrue(shop.buy("buyer", "legendairy")[0])
        self.assertFalse(shop.buy("buyer", "legendairy")[0])
        self.assertEqual(store.get_points("buyer"), 8500)
        self.assertFalse(shop.equip_title("other", "Legendairy")[0])
        self.assertTrue(shop.equip_title("buyer", "none")[0])
        self.assertEqual(shop.profile("buyer")["title"], "")
        self.assertTrue(shop.equip_title("buyer", "legendairy")[0])
        self.assertEqual(shop.profile("buyer")["title"], "Legendairy")

    def test_stale_editor_cannot_overwrite_stock_changed_by_purchase(self):
        self.edit("challenge", stock=1)
        old_editor = self.item("challenge")
        self.assertTrue(shop.buy("buyer", "challenge", "Try this")[0])
        ok, error = shop.save_item({**old_editor, "name": "A new name"})
        self.assertFalse(ok)
        self.assertIn("Refresh", error)
        self.assertEqual(self.item("challenge")["stock"], 0)

    def test_shoutout_is_never_published_before_approval(self):
        message = '<script>alert("hello")</script>'
        self.assertTrue(shop.buy("buyer", "shoutout", message)[0])
        self.assertEqual(store.boost_overlay_state()["events"], [])
        order = shop.snapshot()["pending"][0]
        self.assertTrue(shop.review(order["id"], "approve")[0])
        events = store.boost_overlay_state()["events"]
        self.assertEqual(events[0]["summary"], message)
        self.assertEqual(events[0]["kind"], "shop")
        self.assertFalse(shop.review(order["id"], "approve")[0])
        self.assertEqual(len(store.boost_overlay_state()["events"]), 1)

    def test_community_gift_targets_recent_viewers_and_excludes_buyer_and_bots(self):
        shop.mark_present(["buyer", "viewer", "oldviewer", "nightbot"])
        self.now += timedelta(minutes=16)
        shop.mark_present(["buyer", "viewer"])
        self.assertTrue(shop.buy("buyer", "herd-gift")[0])
        self.assertEqual(store.get_points("buyer"), 9000)
        self.assertEqual(store.get_points("viewer"), 125)
        self.assertEqual(store.get_points("oldviewer"), 100)
        order = shop.snapshot()["orders"][0]
        self.assertEqual(json.loads(order["result"])["recipients"], ["viewer"])

    def test_gift_without_recipients_and_invalid_buys_leave_everything_unchanged(self):
        self.edit("herd-gift", stock=1)
        for slug, message in (("herd-gift", ""), ("challenge", ""), ("shoutout", "x" * 141), ("missing", "")):
            self.assertFalse(shop.buy("buyer", slug, message)[0])
        self.assertEqual(store.get_points("buyer"), 10000)
        self.assertEqual(shop.snapshot()["orders"], [])
        self.assertEqual(self.item("herd-gift")["stock"], 1)
        self.assertFalse(shop.buy("poor", "legendairy")[0])
        self.edit("challenge", enabled=0)
        self.assertFalse(shop.buy("buyer", "challenge", "test")[0])
        self.assertFalse(shop.buy("buyer", "challenge", "test")[0])
        store.set_setting("feature_shop", "0")
        self.assertFalse(shop.buy("buyer", "gold-glow")[0])

    def test_cosmetics_expire_extend_and_style_boosts_and_shop_events(self):
        self.assertTrue(shop.buy("buyer", "gold-glow")[0])
        initial = shop.profile("buyer")
        self.assertEqual(initial["color"], "gold")
        self.assertEqual(store.boost_overlay_state()["events"][0]["color"], "gold")
        self.assertTrue(shop.buy("buyer", "legendairy")[0])
        self.assertTrue(store.buy_boost("buyer", "watch")[0])
        boost_event = store.boost_overlay_state()["events"][-1]
        self.assertEqual(boost_event["color"], "gold")
        self.assertEqual(boost_event["title"], "Legendairy")
        self.assertFalse(shop.buy("buyer", "gold-glow")[0])
        self.now += timedelta(seconds=60)
        self.assertTrue(shop.buy("buyer", "gold-glow")[0])
        self.assertEqual(store.parse_iso(shop.profile("buyer")["cosmetic_until"]), self.now - timedelta(seconds=60) + timedelta(minutes=60))
        self.now += timedelta(minutes=61)
        self.assertEqual(shop.profile("buyer")["color"], "mint")
        self.assertEqual(shop.profile("buyer")["cosmetic_until"], "")

    def test_shop_validation_pagination_and_original_item_snapshot(self):
        original = self.item("challenge")
        for changes in ({"price": 0}, {"slug": "bad code"}, {"category": "other"}, {"stock": -2}, {"cooldown": 86401}, {"price": 1.5}):
            self.assertFalse(shop.save_item({**original, **changes})[0])
        self.assertEqual(self.item("challenge"), original)
        self.edit("challenge", cooldown=0)
        # Cheap requests exercise history pagination without exceeding the balance.
        self.edit("challenge", price=1)
        for _ in range(52):
            self.assertTrue(shop.buy("buyer", "challenge", "challenge")[0])
        self.assertEqual(len(shop.snapshot()["orders"]), 50)
        self.assertEqual(len(shop.snapshot(offset=50)["orders"]), 2)
        self.assertEqual(len(shop.snapshot()["pending"]), 52)
        self.assertEqual(shop.snapshot("other")["total"], 0)
        self.edit("challenge", price=999, name="Changed")
        self.assertEqual(shop.snapshot()["orders"][0]["item"]["price"], 1)

    def test_api_auth_dashboard_proxy_and_chat_commands(self):
        app = create_api_app(get_status=lambda: {}, announce=AsyncMock())
        routes = {r.method: r for r in app.router.routes() if r.resource.canonical == "/api/shop"}
        with patch.dict(os.environ, {"API_SECRET": "secret"}):
            request = SimpleNamespace(headers={}, query={})
            self.assertEqual(inline(routes["GET"].handler(request)).status, 401)
            request.headers = {"X-API-Secret": "secret"}
            self.assertEqual(inline(routes["GET"].handler(request)).status, 200)
            request.json = AsyncMock(return_value={"action": "save", **self.item("challenge"), "price": 300})
            self.assertEqual(inline(routes["POST"].handler(request)).status, 200)
        client = dashboard.app.test_client()
        with patch.object(dashboard, "fetch_status", return_value=copy.deepcopy(dashboard.EMPTY_STATUS)):
            response = client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'id="panel-shop"', response.data)
            self.assertIn(b'shop-dashboard.js', response.data)
        with patch.object(dashboard.requests, "get", return_value=SimpleNamespace(raise_for_status=lambda: None, json=shop.snapshot)):
            self.assertEqual(client.get("/shop/state").json["ok"], True)
        with patch.object(dashboard, "post_bot_data", return_value=(True, {"ok": True})) as post:
            self.assertEqual(client.post("/shop", json={"action": "reject", "id": 1}).status_code, 200)
            post.assert_called_once_with("/api/shop", {"action": "reject", "id": 1})
        tree = ast.parse(Path("bot.py").read_text(encoding="utf-8"))
        namespace = {"store": store, "shop": shop, "get_author_name": lambda ctx: "buyer", "require_command": AsyncMock(return_value=True)}
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name in {"shop_command", "buy", "profile", "title"}:
                function = copy.deepcopy(node); function.decorator_list = []
                module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), function], type_ignores=[])
                exec(compile(ast.fix_missing_locations(module), "bot.py", "exec"), namespace)
        ctx = SimpleNamespace(send=AsyncMock())
        inline(namespace["shop_command"](None, ctx))
        self.assertIn("buy", ctx.send.await_args.args[0])
        inline(namespace["buy"](None, ctx, "legendairy"))
        inline(namespace["profile"](None, ctx))
        self.assertIn("Legendairy", ctx.send.await_args.args[0])
        inline(namespace["title"](None, ctx, name="none"))
        self.assertEqual(shop.profile("buyer")["title"], "")
