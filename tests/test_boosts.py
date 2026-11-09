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
import store
from api import create_api_app


class BoostTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = patch.object(store, "DB_PATH", str(Path(self.directory.name) / "test.db"))
        self.database.start()
        store.init_db()

    def tearDown(self):
        self.database.stop()
        self.directory.cleanup()

    def test_default_percentage_uses_current_balance_for_each_purchase(self):
        store.change_points("viewer", 900)
        for kind, remaining in (("watch", 500), ("luck", 250), ("watch", 125)):
            with self.subTest(kind=kind, remaining=remaining):
                previous = store.boost_until(kind)
                ok, message = store.buy_boost("viewer", kind)
                self.assertTrue(ok, message)
                self.assertEqual(store.get_points("viewer"), remaining)
                self.assertTrue(store.is_boost_active(kind))
                if previous:
                    self.assertGreater(store.boost_until(kind), previous)

    def test_rounding_and_empty_balance(self):
        store.change_points("viewer", -97)
        self.assertTrue(store.buy_boost("viewer", "watch")[0])
        self.assertEqual(store.get_points("viewer"), 1)
        self.assertTrue(store.buy_boost("viewer", "luck")[0])
        self.assertEqual(store.get_points("viewer"), 0)
        self.assertFalse(store.buy_boost("viewer", "watch")[0])

    def test_custom_percentage_and_flat_pricing_are_independent(self):
        store.set_setting("boost_watch_percentage", "25")
        store.set_setting("boost_luck_cost_mode", "flat")
        store.set_setting("boost_luck_cost", "60")
        self.assertTrue(store.buy_boost("viewer", "watch")[0])
        self.assertEqual(store.get_points("viewer"), 75)
        self.assertTrue(store.buy_boost("viewer", "luck")[0])
        self.assertEqual(store.get_points("viewer"), 15)
        self.assertFalse(store.buy_boost("viewer", "luck")[0])
        self.assertEqual(store.get_points("viewer"), 15)

    def test_zero_disables_selected_mode_only(self):
        store.set_setting("boost_watch_cost", "0")
        self.assertTrue(store.buy_boost("viewer", "watch")[0])
        store.set_setting("boost_watch_percentage", "0")
        self.assertFalse(store.buy_boost("viewer", "watch")[0])
        store.set_setting("boost_luck_cost_mode", "flat")
        store.set_setting("boost_luck_cost", "0")
        self.assertFalse(store.buy_boost("viewer", "luck")[0])

    def test_overview_explains_price(self):
        self.assertIn("50% of your current points", store.boost_overview())
        store.set_setting("boost_luck_cost_mode", "flat")
        self.assertIn("1,000 points", store.boost_overview())

    def test_celebrations_capture_purchases_and_extensions(self):
        self.assertTrue(store.buy_boost("viewer", "watch")[0])
        self.assertTrue(store.buy_boost("other", "watch")[0])
        state = store.boost_overlay_state()
        self.assertEqual(len(state["events"]), 2)
        first, second = state["events"]
        self.assertFalse(first["extended"])
        self.assertTrue(second["extended"])
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["cost"], 50)
        self.assertEqual(state["active"][0]["user"], "other")
        json.dumps(state)  # The HTTP payload must contain serializable timestamps.
        store.change_points("broke", -100)
        self.assertFalse(store.buy_boost("broke", "luck")[0])
        self.assertEqual(len(store.boost_overlay_state()["events"]), 2)
        with patch.object(store, "utc_now", return_value=store.utc_now() + timedelta(seconds=121)):
            self.assertEqual(store.boost_overlay_state()["events"], [])
            self.assertEqual(len(store.boost_overlay_state()["active"]), 1)
        with patch.object(store, "utc_now", return_value=store.utc_now() + timedelta(hours=1)):
            self.assertEqual(store.boost_overlay_state()["active"], [])

    def test_purchase_rolls_back_if_celebration_cannot_be_saved(self):
        original = store.upsert_config

        def fail_event(conn, key, value):
            if key == "boost_celebrations":
                raise RuntimeError("Database write failed")
            original(conn, key, value)

        with patch.object(store, "upsert_config", side_effect=fail_event):
            with self.assertRaises(RuntimeError):
                store.buy_boost("viewer", "watch")
        self.assertEqual(store.get_points("viewer"), 100)
        self.assertFalse(store.is_boost_active("watch"))
        self.assertEqual(store.boost_overlay_state()["events"], [])

    def test_boost_overlay_routes_and_proxy_failure(self):
        client = dashboard.app.test_client()
        self.assertEqual(client.get("/overlay/boosts?preview=1").status_code, 200)
        with patch.object(dashboard.requests, "get", return_value=SimpleNamespace(
                raise_for_status=lambda: None, json=store.boost_overlay_state)):
            response = client.get("/overlay/boosts/state")
            self.assertTrue(response.json["ok"])
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        with patch.object(dashboard.requests, "get", side_effect=dashboard.requests.ConnectionError):
            self.assertEqual(client.get("/overlay/boosts/state").status_code, 503)
        app = create_api_app(get_status=lambda: {}, announce=AsyncMock())
        route = next(r for r in app.router.routes() if r.resource.canonical == "/api/boosts/overlay")
        with patch.dict(os.environ, {"API_SECRET": "private"}):
            for headers, expected in (({}, 401), ({"X-API-Secret": "private"}, 200)):
                coroutine = route.handler(SimpleNamespace(headers=headers))
                try:
                    coroutine.send(None)
                except StopIteration as done:
                    self.assertEqual(done.value.status, expected)
                finally:
                    coroutine.close()

    def settings_request(self, payload):
        app = create_api_app(get_status=lambda: {}, announce=AsyncMock())
        route = next(r for r in app.router.routes() if r.resource.canonical == "/api/settings")
        request = SimpleNamespace(headers={}, json=AsyncMock(return_value=payload))
        with patch.dict(os.environ, {"API_SECRET": ""}):
            coroutine = route.handler(request)
            try:
                coroutine.send(None)
            except StopIteration as done:
                return done.value
            finally:
                coroutine.close()
            raise AssertionError("Unexpected background work in settings handler.")

    def test_api_saves_pricing_and_rejects_invalid_values(self):
        response = self.settings_request({"boost_watch_cost_mode": "flat", "boost_watch_percentage": "35",
                                          "boost_watch_cost": "200", "boost_luck_percentage": "100"})
        self.assertEqual(response.status, 200)
        settings = json.loads(response.text)["settings"]
        self.assertEqual(settings["boost_watch_cost_mode"], "flat")
        self.assertEqual(settings["boost_watch_percentage"], 35)
        self.assertEqual(settings["boost_luck_percentage"], 100)
        self.assertTrue(store.buy_boost("viewer", "luck")[0])
        self.assertEqual(store.get_points("viewer"), 0)
        for payload in ({"boost_watch_cost_mode": "other"}, {"boost_watch_percentage": "101"},
                        {"boost_watch_percentage": "-1"}, {"boost_watch_percentage": "1.5"}):
            with self.subTest(payload=payload):
                self.assertEqual(self.settings_request(payload).status, 400)
                self.assertEqual(store.boost_percentage("watch"), 35)

    def test_dashboard_renders_and_forwards_pricing(self):
        status = copy.deepcopy(dashboard.EMPTY_STATUS)
        status["settings"] = store.get_dashboard_settings()
        with patch.object(dashboard, "fetch_status", return_value=status):
            response = dashboard.app.test_client().get("/")
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'data-boost-percentage-toggle checked', response.data)
            self.assertIn(b'name="boost_watch_percentage"', response.data)
        payload = {"boost_watch_cost_mode": "flat", "boost_watch_percentage": "30",
                   "boost_watch_cost": "250", "boost_luck_cost_mode": "percentage",
                   "boost_luck_percentage": "40"}
        with patch.object(dashboard, "post_bot", return_value=(True, None)) as post:
            dashboard.app.test_client().post("/update-settings", data=payload)
            for key, value in payload.items():
                self.assertEqual(post.call_args.args[1][key], value)


if __name__ == "__main__":
    unittest.main()
