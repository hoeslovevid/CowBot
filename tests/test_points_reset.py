import copy
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import dashboard
import store
from api import create_api_app


class PointsResetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = patch.object(store, "DB_PATH", str(Path(self.directory.name) / "test.db"))
        self.database.start()
        store.init_db()
        store.change_points("viewer", 900)
        store.change_points("other", 200)
        store.add_watch_seconds(["viewer"], 3600)
        self.claim_time = store.utc_now().isoformat()
        with store.db_session() as conn:
            conn.execute("UPDATE points SET last_daily = ?, last_watch_reward = ? WHERE user = 'viewer'",
                         (self.claim_time, self.claim_time))

    def tearDown(self):
        self.database.stop()
        self.directory.cleanup()

    def assert_history_preserved(self):
        self.assertEqual(store.get_watch_seconds("viewer"), 3600)
        with store.db_session() as conn:
            row = conn.execute("SELECT last_daily, last_watch_reward FROM points WHERE user = 'viewer'").fetchone()
            self.assertEqual(row["last_daily"], self.claim_time)
            self.assertEqual(row["last_watch_reward"], self.claim_time)

    def test_single_reset_is_exact_and_preserves_history(self):
        self.assertEqual(store.reset_points("user", " @Viewer "), (True, "viewer", 1))
        self.assertEqual(store.get_points("viewer"), 0)
        self.assertEqual(store.get_points("other"), 300)
        self.assert_history_preserved()
        store.reset_points("user", "viewer")
        self.assertEqual(store.get_points("viewer"), 0)

    def test_new_viewer_reset_does_not_restore_starting_points(self):
        store.reset_points("user", "newviewer")
        self.assertEqual(store.get_points("newviewer"), 0)
        self.assertEqual(store.get_points("unseen"), 100)

    def test_everyone_reset_preserves_rows_history_and_settings(self):
        self.assertEqual(store.reset_points("all"), (True, "everyone", 2))
        self.assertEqual(store.get_points("viewer"), 0)
        self.assertEqual(store.get_points("other"), 0)
        self.assertEqual(store.get_points("unseen"), 100)
        self.assert_history_preserved()
        store.change_points("viewer", 10)
        self.assertEqual(store.get_points("viewer"), 10)

    def test_invalid_targets_cannot_reset_anyone(self):
        for scope, user in (("", None), ("everyone", None), ("user", ""), ("user", "@"),
                            ("user", "bad name"), ("user", "viewer' OR 1=1"), ("user", ["viewer"])):
            with self.subTest(scope=scope, user=user):
                self.assertFalse(store.reset_points(scope, user)[0])
                self.assertEqual(store.get_points("viewer"), 1000)
                self.assertEqual(store.get_points("other"), 300)

    def api_call(self, payload, headers=None):
        app = create_api_app(get_status=lambda: {}, announce=AsyncMock())
        route = next(r for r in app.router.routes() if r.resource.canonical == "/api/points/reset")
        coroutine = route.handler(SimpleNamespace(headers=headers or {}, json=AsyncMock(return_value=payload)))
        try:
            coroutine.send(None)
        except StopIteration as done:
            return done.value
        finally:
            coroutine.close()
        raise AssertionError("Unexpected background work")

    def test_api_requires_auth_module_and_explicit_scope(self):
        with patch.dict(os.environ, {"API_SECRET": "secret"}):
            self.assertEqual(self.api_call({"scope": "all"}).status, 401)
            headers = {"X-API-Secret": "secret"}
            self.assertEqual(self.api_call({}, headers).status, 400)
            self.assertEqual(self.api_call({"scope": "user"}, headers).status, 400)
            store.set_setting("feature_economy", "0")
            self.assertEqual(self.api_call({"scope": "all"}, headers).status, 400)
            self.assertEqual(store.get_points("viewer"), 1000)
            store.set_setting("feature_economy", "1")
            self.assertEqual(self.api_call({"scope": "user", "user": "viewer"}, headers).status, 200)
            self.assertEqual(store.get_points("viewer"), 0)
            self.assertEqual(store.get_points("other"), 300)
            self.assertEqual(self.api_call({"scope": "all"}, headers).status, 200)
            self.assertEqual(store.get_points("other"), 0)

    def test_dashboard_controls_and_proxy(self):
        client = dashboard.app.test_client()
        with patch.object(dashboard, "fetch_status", return_value=copy.deepcopy(dashboard.EMPTY_STATUS)):
            response = client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'id="reset-user-points-form" data-confirm=', response.data)
            self.assertIn(b'id="reset-all-points-form" data-confirm=', response.data)
        for scope in ("user", "all"):
            with patch.object(dashboard, "post_bot_data", return_value=(True, {"scope": scope, "user": "viewer", "count": 2})) as post:
                response = client.post("/points/reset", json={"scope": scope, "points_user": "viewer"},
                                       headers={"Accept": "application/json"})
                self.assertTrue(response.json["ok"])
                post.assert_called_once_with("/api/points/reset", {"scope": scope, "user": "viewer"})
        with patch.object(dashboard, "post_bot_data", return_value=(False, {"error": "Bot unavailable"})):
            response = client.post("/points/reset", json={"scope": "all"}, headers={"Accept": "application/json"})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json["error"], "Bot unavailable")
