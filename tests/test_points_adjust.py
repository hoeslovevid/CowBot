import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import dashboard
import store
from api import create_api_app


class PointsAdjustmentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = patch.object(store, "DB_PATH", str(Path(self.directory.name) / "test.db"))
        self.database.start()
        store.init_db()
        store.change_points("viewer", 900)
        self.announce = AsyncMock()

    def tearDown(self):
        self.database.stop()
        self.directory.cleanup()

    def call(self, path, payload, headers=None):
        app = create_api_app(get_status=lambda: {}, announce=self.announce)
        route = next(r for r in app.router.routes() if r.resource.canonical == path)
        coroutine = route.handler(SimpleNamespace(headers=headers or {}, json=AsyncMock(return_value=payload)))
        try:
            coroutine.send(None)
        except StopIteration as done:
            return done.value
        finally:
            coroutine.close()
        raise AssertionError("Unexpected background work")

    def test_bulk_names_are_normalized_and_deduplicated(self):
        ok, error, results = store.adjust_points(" @Viewer, OTHER\nviewer\tthird ", 50)
        self.assertTrue(ok, error)
        self.assertEqual([r["user"] for r in results], ["viewer", "other", "third"])
        self.assertEqual([r["total"] for r in results], [1050, 150, 150])

    def test_invalid_bulk_request_does_not_partially_award(self):
        invalid = ["viewer, invalid-name", "", None, ["viewer", 42], ["viewer", "bad name"],
                   [f"viewer{i}" for i in range(101)]]
        for users in invalid:
            with self.subTest(users=users):
                self.assertFalse(store.adjust_points(users, 50)[0])
                self.assertEqual(store.get_points("viewer"), 1000)
        for amount in (0, 1000001, 1.5, True):
            self.assertFalse(store.adjust_points("viewer", amount)[0])
        self.assertEqual(store.get_points("viewer"), 1000)

    def test_transaction_rolls_back_all_awards_on_write_failure(self):
        original = store.ensure_user_row

        def fail_second(conn, user, starting_points):
            if user == "other":
                raise RuntimeError("Write failed")
            original(conn, user, starting_points)

        with patch.object(store, "ensure_user_row", side_effect=fail_second):
            with self.assertRaises(RuntimeError):
                store.adjust_points("viewer,other", 100)
        self.assertEqual(store.get_points("viewer"), 1000)
        self.assertEqual(store.get_points("other"), 100)

    def test_bulk_api_authorization_validation_and_per_viewer_amount(self):
        with patch.dict(os.environ, {"API_SECRET": "secret"}):
            payload = {"users": "viewer,other,VIEWER", "amount": "25"}
            headers = {"X-API-Secret": "secret"}
            self.assertEqual(self.call("/api/points/bulk", payload).status, 401)
            store.set_setting("feature_economy", "0")
            self.assertEqual(self.call("/api/points/bulk", payload, headers).status, 400)
            store.set_setting("feature_economy", "1")
            response = self.call("/api/points/bulk", payload, headers)
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.text)["count"], 2)
            self.assertEqual(store.get_points("viewer"), 1025)
            self.assertEqual(store.get_points("other"), 125)
            self.announce.assert_awaited_once()
            for amount in ("0", "-5", "1.5", "1000001"):
                self.assertEqual(self.call("/api/points/bulk", {**payload, "amount": amount}, headers).status, 400)
            self.assertEqual(store.get_points("viewer"), 1025)

    def test_remove_exact_amount_and_clamp_to_zero(self):
        with patch.dict(os.environ, {"API_SECRET": ""}):
            response = self.call("/api/points", {"action": "remove", "user": "viewer", "amount": "75"})
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.text)["amount"], -75)
            self.assertEqual(store.get_points("viewer"), 925)
            response = self.call("/api/points", {"action": "remove", "user": "viewer", "amount": "1000"})
            self.assertEqual(json.loads(response.text)["amount"], -925)
            self.assertEqual(store.get_points("viewer"), 0)
            self.assertIn("lost 925 points", self.announce.await_args.args[0])
            response = self.call("/api/points", {"action": "remove", "user": "viewer", "amount": "10"})
            self.assertEqual(json.loads(response.text)["amount"], 0)
            self.assertEqual(store.get_points("viewer"), 0)
            self.assertEqual(self.call("/api/points", {"action": "remove", "user": "viewer,other", "amount": "10"}).status, 400)

    def test_dashboard_renders_and_proxies_bulk_and_remove(self):
        client = dashboard.app.test_client()
        client.environ_base["HTTP_ACCEPT"] = "application/json"
        with patch.object(dashboard, "fetch_status", return_value=copy.deepcopy(dashboard.EMPTY_STATUS)):
            response = client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'id="bulk-points-form"', response.data)
            self.assertIn(b'Points to give or remove', response.data)
        with patch.object(dashboard, "post_bot_data", return_value=(True, {"amount": 50, "count": 2})) as post:
            response = client.post("/points/bulk", json={"points_users": "viewer,other", "points_amount": "50"})
            post.assert_called_once_with("/api/points/bulk", {"users": "viewer,other", "amount": "50"})
            self.assertIn("50 points each to 2 viewers", response.json["message"])
        with patch.object(dashboard, "post_bot_data", return_value=(True, {"user": "viewer", "amount": -25, "total": 0})) as post:
            response = client.post("/points", json={"action": "remove", "points_user": "viewer", "points_amount": "100"})
            self.assertIn("Removed 25 points", response.json["message"])
            self.assertEqual(post.call_args.args[1]["action"], "remove")
        with patch.object(dashboard, "post_bot_data", return_value=(False, {"error": "Invalid names"})):
            response = client.post("/points/bulk", json={"points_users": "bad-name", "points_amount": "50"})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json["error"], "Invalid names")
