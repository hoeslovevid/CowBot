import ast
import copy
import json
import os
import tempfile
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import store
import tts
from api import create_api_app
import dashboard


class FakeEngine:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.fail = False

    def status(self):
        return {"available": True, "name": "Test speech", "error": ""}

    def path(self, job_id):
        return self.directory / f"{job_id}.wav"

    def synthesize(self, job_id, text, settings):
        if self.fail:
            raise RuntimeError("Speech engine unavailable.")
        path = self.path(job_id)
        with wave.open(str(path), "wb") as output:
            output.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            output.writeframes(b"\x00\x00" * 800)
        return path, 0.1


class TTSEnvironment:
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = patch.object(store, "DB_PATH", str(Path(self.directory.name) / "test.db"))
        self.database.start()
        store.init_db()
        self.engine = FakeEngine(self.directory.name)
        self.queue = tts.TTSQueue(self.engine)
        self.queue.save_settings({"enabled": True, "bits_enabled": True, "points_enabled": True,
                                  "reward_id": "tts-reward", "cooldown": 0})
        self.client = "test-player-00000001"

    def tearDown(self):
        self.database.stop()
        self.directory.cleanup()

    def enqueue(self, **kwargs):
        return self.queue.enqueue(**{"event_key": "bits:event-1", "username": "Viewer", "user_id": "42",
                                    "text": "Hello stream", "source": "bits", "bits": 100, **kwargs})


class QueueTests(TTSEnvironment, unittest.TestCase):
    def test_triggers_and_selected_reward(self):
        self.assertIsNone(self.enqueue(bits=99))
        self.assertIsNone(self.enqueue(source="points", reward_id="other-reward"))
        self.assertIsNotNone(self.enqueue(source="points", reward_id="tts-reward"))
        self.queue.save_settings({"enabled": False})
        self.assertIsNone(self.enqueue(event_key="new-event"))
        self.assertIsNotNone(self.enqueue(event_key="test", test=True, source="test"))

    def test_event_deduplication_persists_across_restart(self):
        job = self.enqueue()
        restored = tts.TTSQueue(self.engine)
        self.assertEqual(job, restored.enqueue(event_key="bits:event-1", username="Viewer", user_id="42", text="Hello stream", source="bits", bits=100))
        self.assertEqual(len(restored.snapshot()["queue"]), 1)

    def test_approval_single_player_and_acknowledgment(self):
        job = self.enqueue()
        self.assertIsNone(self.queue.claim(self.client))
        self.queue.action("approve", job)
        claimed = self.queue.claim(self.client)
        self.assertEqual(claimed["id"], job)
        self.assertIsNone(self.queue.claim("second-player-0000001"))
        self.assertIsNone(self.queue.audio_path(job, "second-player-0000001"))
        self.assertTrue(self.queue.audio_path(job, self.client).is_file())
        self.assertFalse(self.queue.finish(job, "second-player-0000001"))
        self.assertTrue(self.queue.finish(job, self.client))
        self.assertFalse(self.queue.finish(job, self.client))
        self.assertEqual(self.queue.snapshot()["history"][0]["status"], "completed")

    def test_pause_heartbeat_skip_clear_and_replay(self):
        self.queue.save_settings({"approval_required": False})
        job = self.enqueue()
        self.queue.action("pause")
        self.assertIsNone(self.queue.claim(self.client))
        self.queue.action("resume")
        self.queue.claim(self.client)
        with store.db_session() as conn:
            conn.execute("UPDATE tts_jobs SET lease_until=? WHERE id=?", (time.time() + 1, job))
        self.queue.action("pause")
        self.assertEqual(self.queue.player_state(self.client)["current_id"], job)
        with store.db_session() as conn:
            expiry = conn.execute("SELECT lease_until FROM tts_jobs WHERE id=?", (job,)).fetchone()[0]
        self.assertGreater(expiry, time.time() + 150)
        self.queue.action("skip", job)
        self.assertIsNone(self.queue.player_state(self.client)["current_id"])
        self.queue.action("replay", job)
        self.assertEqual(self.queue.snapshot()["queue"][0]["source"], "replay")
        self.queue.action("clear")
        self.assertEqual(self.queue.snapshot()["queue"], [])

    def test_failed_synthesis_and_disconnected_player_do_not_block_queue(self):
        self.queue.save_settings({"approval_required": False})
        self.enqueue()
        self.engine.fail = True
        with self.assertRaises(RuntimeError):
            self.queue.claim(self.client)
        self.engine.fail = False
        job = self.enqueue(event_key="bits:event-2")
        self.assertEqual(self.queue.claim(self.client)["id"], job)
        with store.db_session() as conn:
            conn.execute("UPDATE tts_jobs SET lease_until=0 WHERE id=?", (job,))
        self.assertIsNone(self.queue.player_state(self.client)["current_id"])
        self.assertTrue(all(row["status"] == "failed" for row in self.queue.snapshot()["history"]))

    def test_filters_and_cooldown(self):
        self.queue.save_settings({"blocked_terms": "bad phrase", "max_chars": 20, "cooldown": 60})
        with self.assertRaises(ValueError): self.enqueue(text="BAD PHRASE")
        with self.assertRaises(ValueError): self.enqueue(text="a" * 21)
        self.enqueue()
        with self.assertRaises(ValueError): self.enqueue(event_key="second")
        self.assertIsNotNone(self.enqueue())  # A redelivery isn't a second redemption.

    def test_settings_and_audio_path_validation(self):
        for payload in ({"voice": "../voice"}, {"min_bits": 0}, {"rate": 20}, {"volume": "loud"}, {"enabled": "yes"}):
            with self.assertRaises(ValueError): self.queue.save_settings(payload)
        with self.assertRaises(ValueError): self.engine = tts.SpeechEngine(self.directory.name); self.engine.path("../../secret")

    def test_terminal_history_and_secret_not_in_snapshot(self):
        job = self.enqueue()
        self.queue.action("reject", job)
        snapshot = self.queue.snapshot()
        self.assertEqual(snapshot["history"][0]["status"], "rejected")
        self.assertNotIn(self.queue.overlay_key(), json.dumps(snapshot))

    def test_queue_capacity(self):
        for index in range(100): self.enqueue(event_key=f"event-{index}")
        with self.assertRaisesRegex(ValueError, "full"): self.enqueue(event_key="overflow")

    def test_twitch_cancellation_stops_queued_or_playing_speech(self):
        job = self.enqueue(event_key="points:redemption-1", source="points", reward_id="tts-reward")
        self.queue.action("approve", job)
        self.queue.claim(self.client)
        self.queue.cancel_redemption("redemption-1")
        self.assertIsNone(self.queue.player_state(self.client)["current_id"])
        self.assertEqual(self.queue.snapshot()["history"][0]["status"], "skipped")

    def test_twitch_event_handlers_filter_and_deduplicate(self):
        # Extract only event handlers: importing bot.py would load real Twitch credentials.
        tree = ast.parse(Path("bot.py").read_text(encoding="utf-8"))
        bot_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "CowBot")
        selected = {"_queue_tts_event", "event_bits_use", "event_custom_redemption_add", "event_custom_redemption_update"}
        node = ast.ClassDef(name="EventHandlers", bases=[], keywords=[], decorator_list=[],
                            body=[method for method in bot_class.body if getattr(method, "name", "") in selected])
        namespace = {}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), "bot.py", "exec"), namespace)
        handler = namespace["EventHandlers"]()
        handler.tts = self.queue
        handler.channel_user = SimpleNamespace(id="streamer")
        event = SimpleNamespace(type="cheer", broadcaster=SimpleNamespace(id="streamer"),
                                user=SimpleNamespace(id="viewer", name="Viewer", display_name="Viewer"),
                                metadata=SimpleNamespace(message_id="notification-1"), bits=200,
                                fragments=[SimpleNamespace(type="cheermote", text="Cheer200"), SimpleNamespace(type="text", text=" Hello chat")])
        def complete(coroutine):
            try: coroutine.send(None)
            except StopIteration: return
            self.fail("Unexpected asynchronous work in an event ingestion handler.")
        complete(handler.event_bits_use(event))
        complete(handler.event_bits_use(event))
        self.assertEqual(len(self.queue.snapshot()["queue"]), 1)
        self.assertEqual(self.queue.snapshot()["queue"][0]["text"], "Hello chat")
        event.type = "power_up"
        event.metadata.message_id = "different-id"
        complete(handler.event_bits_use(event))
        self.assertEqual(len(self.queue.snapshot()["queue"]), 1)
        event.type = "cheer"
        event.broadcaster.id = "other-streamer"
        complete(handler.event_bits_use(event))
        self.assertEqual(len(self.queue.snapshot()["queue"]), 1)

    def test_espeak_uses_stdin_and_validated_voice(self):
        engine = tts.SpeechEngine(self.directory.name)
        engine.windows = False
        engine.executable = "/usr/bin/espeak-ng"
        def run(command, **kwargs):
            with wave.open(command[command.index("-w") + 1], "wb") as output:
                output.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
                output.writeframes(b"\0\0" * 800)
            self.assertIn("--stdin", command)
            self.assertEqual(kwargs["input"], b"Untrusted $(text) <hello>")
            self.assertNotIn("Untrusted", " ".join(command))
        with patch.object(tts.subprocess, "run", side_effect=run):
            path, duration = engine.synthesize("a" * 32, "Untrusted $(text) <hello>", dict(tts.DEFAULTS, voice="female"))
        self.assertTrue(path.is_file())
        self.assertGreater(duration, 0)


class ApiTests(TTSEnvironment, unittest.TestCase):
    def setUp(self):
        TTSEnvironment.setUp(self)
        self.secret = patch.dict(os.environ, {"API_SECRET": "test-api-secret"})
        self.secret.start()
        self.app = create_api_app(get_status=lambda: {}, announce=AsyncMock(), tts_queue=self.queue)

    def tearDown(self):
        self.secret.stop()
        TTSEnvironment.tearDown(self)

    async def call(self, path, body=None, headers=None, operation=None, query=None, method="POST"):
        route = next(route for route in self.app.router.routes() if route.resource.canonical == path and route.method in (method, "*"))
        request = SimpleNamespace(headers=headers or {}, json=AsyncMock(return_value=body or {}),
                                  match_info={"operation": operation}, query=query or {}, method=method)
        return await route.handler(request)

    def test_api_and_player_authentication(self):
        # Exercise the real handlers without binding a network socket.
        def inline(coroutine):
            try:
                coroutine.send(None)
            except StopIteration as done:
                return done.value
            raise AssertionError("Unexpected background work in a handler test.")
        response = inline(self.call("/api/tts", {"action": "test", "text": "Hello"}))
        self.assertEqual(response.status, 401)
        headers = {"X-API-Secret": "test-api-secret"}
        response = inline(self.call("/api/tts/player/{operation}", headers=headers, operation="claim"))
        self.assertEqual(response.status, 403)
        response = inline(self.call("/api/tts", {"action": "test", "text": "Hello"}, headers))
        self.assertEqual(response.status, 200)
        headers["X-TTS-Key"] = self.queue.overlay_key()
        with patch("api.asyncio.to_thread", new=AsyncMock(side_effect=lambda fn, *args: fn(*args))):
            response = inline(self.call("/api/tts/player/{operation}", {"client": self.client}, headers, operation="claim"))
        self.assertEqual(response.status, 200)
        self.assertIsNotNone(json.loads(response.text)["job"])


class DashboardTests(unittest.TestCase):
    def test_streamer_oauth_rejects_wrong_account_or_missing_permissions(self):
        token_response = SimpleNamespace(content=b"{}", ok=True, json=lambda: {"access_token": "sample-token", "refresh_token": "sample-refresh"})
        for validation in (
            {"login": "wrong_channel", "user_id": "2", "client_id": "test-client", "scopes": list(tts.STREAMER_SCOPES)},
            {"login": "our_channel", "user_id": "1", "client_id": "test-client", "scopes": ["bits:read"]},
            {"login": "our_channel", "user_id": "1", "client_id": "other-client", "scopes": list(tts.STREAMER_SCOPES)},
        ):
            with self.subTest(validation=validation), patch.dict(os.environ, {"TWITCH_CLIENT_ID": "test-client", "TWITCH_CLIENT_SECRET": "test-secret", "TWITCH_CHANNEL": "our_channel"}), \
                    patch.object(dashboard.requests, "post", return_value=token_response), \
                    patch.object(dashboard.requests, "get", return_value=SimpleNamespace(ok=True, json=lambda: validation)), \
                    patch.object(store, "init_db"), patch.object(tts, "set_streamer_tokens") as save, \
                    patch.object(dashboard, "post_bot_data") as apply:
                client = dashboard.app.test_client()
                with client.session_transaction() as session:
                    session["oauth_kind"] = "streamer"
                    session["oauth_state"] = "test-state"
                response = client.get("/oauth/callback?state=test-state&code=test-code")
                self.assertIn("streamer_wrong", response.location)
                save.assert_not_called()
                apply.assert_not_called()

    def test_dashboard_and_overlay_render(self):
        status = dict(copy.deepcopy(dashboard.EMPTY_STATUS), bot_reachable=False)
        with patch.object(dashboard, "fetch_status", return_value=status):
            client = dashboard.app.test_client()
            response = client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'id="panel-tts"', response.data)
            self.assertEqual(client.get("/overlay/tts?key=test").status_code, 200)
            self.assertEqual(client.post("/tts/player/claim", json={}).status_code, 403)
            self.assertEqual(client.get("/tts/player/unknown").status_code, 404)
            with client.get("/static/tts-player.js") as asset:
                self.assertIn("no-cache", asset.headers["Cache-Control"])

    def test_streamer_oauth_requests_separate_scopes(self):
        with patch.dict(os.environ, {"TWITCH_CLIENT_ID": "test-client"}):
            client = dashboard.app.test_client()
            response = client.get("/oauth/streamer")
            self.assertEqual(response.status_code, 302)
            self.assertIn("bits%3Aread", response.location)
            self.assertIn("channel%3Aread%3Aredemptions", response.location)
            with client.session_transaction() as session:
                self.assertEqual(session["oauth_kind"], "streamer")
            response = client.get("/oauth")
            self.assertNotIn("bits%3Aread", response.location)

    def test_player_proxy_keeps_key_out_of_bot_query(self):
        response = SimpleNamespace(content=b'{"ok":true}', status_code=200, headers={"Content-Type": "application/json"})
        with patch.object(dashboard.requests, "request", return_value=response) as proxy:
            client = dashboard.app.test_client()
            result = client.get("/tts/player/state?key=player-secret&client=player-test-123456")
            self.assertEqual(result.status_code, 200)
            self.assertEqual(proxy.call_args.kwargs["headers"]["X-TTS-Key"], "player-secret")
            self.assertNotIn("key", proxy.call_args.kwargs["params"])


if __name__ == "__main__":
    unittest.main()
