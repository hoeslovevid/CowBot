import ast
import copy
import json
import os
import random
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import dashboard
import chat_messages as chat
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


class CooldownTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = patch.object(store, "DB_PATH", str(Path(self.directory.name) / "test.db"))
        self.database.start()
        store.init_db()
        self.now = store.utc_now()
        self.clock = patch.object(store, "utc_now", side_effect=lambda: self.now)
        self.clock.start()
        store.change_points("viewer", 9900)

    def tearDown(self):
        self.clock.stop()
        self.database.stop()
        self.directory.cleanup()

    def test_defaults_and_boost_cooldown_shared_between_types(self):
        self.assertEqual(store.economy_cooldown_seconds("boost"), 60)
        self.assertEqual(store.economy_cooldown_seconds("gambling"), 30)
        self.assertTrue(store.buy_boost("viewer", "watch")[0])
        state = store.boost_overlay_state()
        for kind in ("watch", "luck"):
            ok, message = store.buy_boost("@VIEWER", kind)
            self.assertFalse(ok)
            self.assertIn("60 seconds", message)
        self.assertEqual(store.get_points("viewer"), 5000)
        self.assertEqual(store.boost_overlay_state(), state)
        self.assertTrue(store.buy_boost("other", "luck")[0])
        self.now += timedelta(seconds=59.5)
        self.assertIn("1 second", store.buy_boost("viewer", "luck")[1])
        self.now += timedelta(seconds=.5)
        self.assertTrue(store.buy_boost("viewer", "luck")[0])

    def test_invalid_attempts_do_not_start_cooldown(self):
        self.assertFalse(store.buy_boost("viewer", "invalid")[0])
        store.set_setting("boost_watch_percentage", "0")
        self.assertFalse(store.buy_boost("viewer", "watch")[0])
        self.assertTrue(store.buy_boost("viewer", "luck")[0])
        store.change_points("broke", -100)
        self.assertFalse(store.buy_boost("broke", "watch")[0])
        self.assertFalse(store.buy_boost("broke", "luck")[0])
        store.change_points("broke", 100)
        self.assertTrue(store.buy_boost("broke", "luck")[0])
        for amount in (0, -1, 1000000):
            self.assertFalse(store.try_gambling_wager("viewer", amount)[0])
        self.assertTrue(store.try_gambling_wager("viewer", 10)[0])

    def test_gambling_cooldown_blocks_slots_without_charging_and_expires(self):
        self.assertTrue(store.try_gambling_wager("viewer", 10)[0])
        self.assertTrue(store.try_gambling_wager("other", 10)[0])
        before = store.get_points("viewer")
        with patch.object(store, "spin_slot_reel") as spin:
            ok, error, total, reels, payout = store.play_slots("VIEWER", 10)
            self.assertFalse(ok)
            self.assertIn("30 seconds", error)
            self.assertEqual(total, before)
            spin.assert_not_called()
        self.now += timedelta(seconds=30)
        self.assertTrue(store.play_slots("viewer", 10)[0])
        self.assertFalse(store.try_gambling_wager("viewer", 10)[0])

    def test_live_settings_changes_and_disabling_cooldowns(self):
        self.assertTrue(store.buy_boost("viewer", "watch")[0])
        self.assertTrue(store.try_gambling_wager("viewer", 10)[0])
        store.set_setting("boost_cooldown_seconds", "5")
        store.set_setting("gambling_cooldown_seconds", "5")
        self.now += timedelta(seconds=5)
        self.assertTrue(store.buy_boost("viewer", "luck")[0])
        self.assertTrue(store.try_gambling_wager("viewer", 10)[0])
        store.set_setting("boost_cooldown_seconds", "0")
        store.set_setting("gambling_cooldown_seconds", "0")
        self.assertTrue(store.buy_boost("viewer", "watch")[0])
        self.assertTrue(store.try_gambling_wager("viewer", 10)[0])
        # init_db is safe on an existing database and preserves cooldown history.
        store.init_db()
        store.set_setting("gambling_cooldown_seconds", "30")
        self.assertFalse(store.try_gambling_wager("viewer", 10)[0])

    def test_all_chat_gambling_commands_share_the_same_cooldown(self):
        tree = ast.parse(Path("bot.py").read_text(encoding="utf-8"))
        namespace = {"store": store, "random": random, "chat": chat, "get_author_name": lambda ctx: ctx.username,
                     "require_command": AsyncMock(return_value=True)}
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name in {"gamble", "roulette", "slots"}:
                function = copy.deepcopy(node)
                function.decorator_list = []
                module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), function], type_ignores=[])
                exec(compile(ast.fix_missing_locations(module), "bot.py", "exec"), namespace)
        ctx = SimpleNamespace(username="viewer", send=AsyncMock())
        for first in ("gamble", "roulette", "slots"):
            self.now += timedelta(seconds=30)
            inline(namespace[first](None, ctx, "10"))
            balance = store.get_points("viewer")
            for blocked in ("gamble", "roulette", "slots"):
                inline(namespace[blocked](None, ctx, "10"))
                self.assertIn("cooldown", ctx.send.await_args.args[0])
                self.assertEqual(store.get_points("viewer"), balance)

    def test_settings_round_trip_validation_and_dashboard(self):
        app = create_api_app(get_status=lambda: {}, announce=AsyncMock())
        route = next(r for r in app.router.routes() if r.resource.canonical == "/api/settings")

        def save(payload):
            return inline(route.handler(SimpleNamespace(headers={}, json=AsyncMock(return_value=payload))))

        with patch.dict(os.environ, {"API_SECRET": ""}):
            response = save({"boost_cooldown_seconds": "120", "gambling_cooldown_seconds": "15"})
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.text)["settings"]["boost_cooldown_seconds"], 120)
            for value in ("-1", "1.5", "86401", "", None):
                self.assertEqual(save({"boost_cooldown_seconds": value}).status, 400)
                self.assertEqual(store.economy_cooldown_seconds("boost"), 120)
            self.assertEqual(save({}).status, 200)
            self.assertEqual(store.economy_cooldown_seconds("gambling"), 15)
        client = dashboard.app.test_client()
        with patch.object(dashboard, "fetch_status", return_value=copy.deepcopy(dashboard.EMPTY_STATUS)):
            response = client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'name="boost_cooldown_seconds"', response.data)
            self.assertIn(b'name="gambling_cooldown_seconds"', response.data)
        with patch.object(dashboard, "post_bot", return_value=(True, None)) as post:
            client.post("/update-settings", data={"boost_cooldown_seconds": "120", "gambling_cooldown_seconds": "15"})
            self.assertEqual(post.call_args.args[1]["boost_cooldown_seconds"], "120")
            self.assertEqual(post.call_args.args[1]["gambling_cooldown_seconds"], "15")
