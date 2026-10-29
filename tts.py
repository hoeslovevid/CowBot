"""Persistent TTS queue and offline WAV synthesis. No external speech service."""

import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import uuid
import wave
from pathlib import Path

import store

STREAMER_SCOPES = ("bits:read", "channel:read:redemptions")
DEFAULTS = {
    "enabled": False, "bits_enabled": False, "points_enabled": False,
    "reward_id": "", "min_bits": 100, "max_chars": 300, "cooldown": 15,
    "approval_required": True, "read_username": True,
    "voice": "default", "rate": 0, "volume": 80, "blocked_terms": "",
}
ACTIVE = ("pending", "queued", "playing")
TERMINAL = ("completed", "skipped", "rejected", "failed")


def get_streamer_tokens():
    return store.get_setting("tts_streamer_token", ""), store.get_setting("tts_streamer_refresh", "")


def set_streamer_tokens(access, refresh, user_id="", login=""):
    with store.db_session() as conn:
        for key, value in {"tts_streamer_token": access, "tts_streamer_refresh": refresh,
                           "tts_streamer_id": user_id, "tts_streamer_login": login}.items():
            if value:
                store.upsert_config(conn, key, str(value))


def clean_text(text):
    return " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", str(text or "")).split())


def validate_settings(payload, current=None):
    if not isinstance(payload, dict):
        raise ValueError("Settings must be an object.")
    result = {**DEFAULTS, **(current or {})}
    for key in ("enabled", "bits_enabled", "points_enabled", "approval_required", "read_username"):
        if key in payload:
            value = payload[key]
            if value not in (True, False, 0, 1, "0", "1", "true", "false"):
                raise ValueError(f"Invalid value for {key}.")
            result[key] = value in (True, 1, "1", "true")
    for key, low, high in (("min_bits", 1, 100000), ("max_chars", 20, 500),
                           ("cooldown", 0, 3600), ("rate", -5, 5), ("volume", 0, 100)):
        if key in payload:
            try:
                value = int(str(payload[key]))
            except (ValueError, TypeError):
                raise ValueError(f"Enter a whole number for {key}.")
            if not low <= value <= high:
                raise ValueError(f"{key} must be between {low} and {high}.")
            result[key] = value
    for key, limit in (("reward_id", 100), ("blocked_terms", 2000)):
        if key in payload:
            result[key] = str(payload[key] or "").strip()
            if len(result[key]) > limit:
                raise ValueError(f"{key} is too long.")
    if "voice" in payload:
        if payload["voice"] not in ("default", "female", "male"):
            raise ValueError("Choose one of the available voices.")
        result["voice"] = payload["voice"]
    if result["points_enabled"] and not result["reward_id"]:
        raise ValueError("Choose a Channel Point reward before enabling its trigger.")
    return result


class SpeechEngine:
    def __init__(self, directory=None):
        namespace = hashlib.sha256(os.path.abspath(store.DB_PATH).encode()).hexdigest()[:12]
        self.directory = Path(directory or (Path(tempfile.gettempdir()) / f"cowbot-tts-{namespace}"))
        self.directory.mkdir(parents=True, exist_ok=True)
        self.executable = shutil.which("espeak-ng") or shutil.which("espeak")
        self.windows = os.name == "nt"

    def status(self):
        return {"available": bool(self.executable or self.windows),
                "name": "Windows speech" if self.windows else "eSpeak NG",
                "error": "" if self.executable or self.windows else "Install espeak-ng on the bot host, or rebuild the Docker image."}

    def path(self, job_id):
        if not re.fullmatch(r"[a-f0-9]{32}", str(job_id)):
            raise ValueError("Invalid speech ID.")
        return self.directory / f"{job_id}.wav"

    def synthesize(self, job_id, text, settings):
        path = self.path(job_id)
        if self.windows:
            # The fixed script receives untrusted text only as JSON over stdin.
            script = """$ErrorActionPreference='Stop'
[Console]::InputEncoding=[System.Text.Encoding]::UTF8
$data=[Console]::In.ReadToEnd() | ConvertFrom-Json
Add-Type -AssemblyName System.Speech
$speaker=New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
  if ($data.voice -eq 'female') { $speaker.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::Female) }
  if ($data.voice -eq 'male') { $speaker.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::Male) }
  $speaker.Rate=[int]$data.rate
  $speaker.SetOutputToWaveFile([string]$data.path)
  $speaker.Speak([string]$data.text)
} finally { $speaker.Dispose() }
"""
            command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                       base64.b64encode(script.encode("utf-16le")).decode()]
            content = json.dumps({"text": text, "path": str(path), "voice": settings["voice"], "rate": settings["rate"]})
        elif self.executable:
            voice = {"default": "en-us", "female": "en-us+f3", "male": "en-us+m3"}[settings["voice"]]
            command = [self.executable, "-v", voice, "-s", str(175 + settings["rate"] * 15), "-w", str(path), "--stdin"]
            content = text
        else:
            raise RuntimeError(self.status()["error"])
        try:
            subprocess.run(command, input=content.encode("utf-8"), capture_output=True, timeout=40,
                           check=True, creationflags=subprocess.CREATE_NO_WINDOW if self.windows else 0)
            with wave.open(str(path), "rb") as audio:
                if audio.getnframes() < 1 or audio.getframerate() < 1:
                    raise RuntimeError("The speech engine produced empty audio.")
                duration = audio.getnframes() / audio.getframerate()
            if duration > 120:
                raise RuntimeError("Speech exceeds the two-minute playback limit.")
            return path, duration
        except (subprocess.SubprocessError, OSError, wave.Error, RuntimeError) as exc:
            path.unlink(missing_ok=True)
            raise RuntimeError("Could not generate speech. Check the speech engine on the bot host.") from exc


class TTSQueue:
    def __init__(self, engine=None):
        self.engine = engine or SpeechEngine()
        self.connection = {"authorized": False, "bits": False, "points": False, "error": "Connect your streamer account to listen for redemptions."}
        self.player_seen = 0.0
        with store.db_session() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS tts_jobs (
                id TEXT PRIMARY KEY, event_key TEXT UNIQUE, username TEXT NOT NULL, user_id TEXT NOT NULL,
                text TEXT NOT NULL, source TEXT NOT NULL, bits INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL, created_at REAL NOT NULL, finished_at REAL,
                lease_owner TEXT, lease_until REAL, duration REAL, error TEXT NOT NULL DEFAULT '')""")
            conn.execute("CREATE INDEX IF NOT EXISTS tts_jobs_status ON tts_jobs(status, created_at)")
            if not conn.execute("SELECT 1 FROM config WHERE key='tts_overlay_key'").fetchone():
                store.upsert_config(conn, "tts_overlay_key", secrets.token_urlsafe(32))

    def settings(self):
        try:
            saved = json.loads(store.get_setting("tts_settings", "{}"))
            return {**DEFAULTS, **{key: value for key, value in saved.items() if key in DEFAULTS}}
        except (ValueError, AttributeError):
            return dict(DEFAULTS)

    def save_settings(self, payload):
        settings = validate_settings(payload, self.settings())
        store.set_setting("tts_settings", json.dumps(settings))
        return settings

    def overlay_key(self):
        return store.get_setting("tts_overlay_key", "")

    def _expire(self, conn):
        now = time.time()
        # A disconnected player never causes an automatic repeat of paid speech.
        conn.execute("UPDATE tts_jobs SET status='failed',finished_at=?,error='Player disconnected before completion.' WHERE status='playing' AND lease_until < ?", (now, now))
        conn.execute("DELETE FROM tts_jobs WHERE status NOT IN ('pending','queued','playing') AND created_at < ?", (now - 7 * 86400,))

    def snapshot(self):
        with store.db_session() as conn:
            self._expire(conn)
            queue = [dict(row) for row in conn.execute("SELECT id,username,text,source,bits,status,created_at,error FROM tts_jobs WHERE status IN ('pending','queued','playing') ORDER BY created_at,id LIMIT 100")]
            history = [dict(row) for row in conn.execute("SELECT id,username,text,source,bits,status,created_at,error FROM tts_jobs WHERE status NOT IN ('pending','queued','playing') ORDER BY finished_at DESC LIMIT 20")]
        return {"settings": self.settings(), "paused": store.get_setting("tts_paused", "0") == "1",
                "queue": queue, "history": history, "engine": self.engine.status(),
                "connection": dict(self.connection), "player_connected": time.time() - self.player_seen < 15}

    def enqueue(self, *, event_key, username, text, source, user_id="", bits=0, reward_id="", test=False):
        settings = self.settings()
        if not test:
            if not settings["enabled"]:
                return None
            if source == "bits" and (not settings["bits_enabled"] or bits < settings["min_bits"]):
                return None
            if source == "points" and (not settings["points_enabled"] or reward_id != settings["reward_id"]):
                return None
        text = clean_text(text)
        if not text:
            raise ValueError("Enter a message for TTS.")
        if len(text) > settings["max_chars"]:
            raise ValueError(f"TTS messages may contain up to {settings['max_chars']} characters.")
        terms = [term.strip().casefold() for term in settings["blocked_terms"].splitlines() if term.strip()]
        if any(term in text.casefold() for term in terms):
            raise ValueError("The message contains a blocked term.")
        now = time.time()
        with store.db_session() as conn:
            self._expire(conn)
            existing = conn.execute("SELECT id FROM tts_jobs WHERE event_key=?", (event_key,)).fetchone()
            if existing:
                return existing["id"]
            count = conn.execute("SELECT COUNT(*) FROM tts_jobs WHERE status IN ('pending','queued','playing')").fetchone()[0]
            if count >= 100:
                raise ValueError("The TTS queue is full.")
            if not test and settings["cooldown"] and user_id:
                recent = conn.execute("SELECT 1 FROM tts_jobs WHERE user_id=? AND created_at>? LIMIT 1", (user_id, now - settings["cooldown"])).fetchone()
                if recent:
                    raise ValueError("This viewer's TTS cooldown is still active.")
            job_id = uuid.uuid4().hex
            state = "queued" if test or not settings["approval_required"] else "pending"
            conn.execute("INSERT INTO tts_jobs(id,event_key,username,user_id,text,source,bits,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                         (job_id, event_key, clean_text(username)[:60] or "Anonymous", user_id, text, source, bits, state, now))
        return job_id

    def action(self, action, job_id=None):
        if action in ("pause", "resume"):
            store.set_setting("tts_paused", "1" if action == "pause" else "0")
            return
        with store.db_session() as conn:
            if action == "clear":
                conn.execute("UPDATE tts_jobs SET status='skipped',finished_at=? WHERE status IN ('pending','queued','playing')", (time.time(),))
                return
            row = conn.execute("SELECT * FROM tts_jobs WHERE id=?", (str(job_id or ""),)).fetchone()
            if not row:
                raise ValueError("That speech message was not found.")
            if action == "approve" and row["status"] == "pending":
                state = "queued"
            elif action == "skip" and row["status"] in ACTIVE:
                state = "skipped"
            elif action == "reject" and row["status"] in ("pending", "queued"):
                state = "rejected"
            elif action == "replay" and row["status"] in TERMINAL:
                if conn.execute("SELECT COUNT(*) FROM tts_jobs WHERE status IN ('pending','queued','playing')").fetchone()[0] >= 100:
                    raise ValueError("The TTS queue is full.")
                conn.execute("INSERT INTO tts_jobs(id,event_key,username,user_id,text,source,bits,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, uuid.uuid4().hex, row["username"], row["user_id"], row["text"], "replay", 0, "queued", time.time()))
                return
            else:
                raise ValueError("That action is not available for this message.")
            conn.execute("UPDATE tts_jobs SET status=?,finished_at=? WHERE id=?", (state, None if state == "queued" else time.time(), job_id))

    def record_rejection(self, *, event_key, username, text, source, user_id="", bits=0, reason):
        with store.db_session() as conn:
            conn.execute("INSERT OR IGNORE INTO tts_jobs(id,event_key,username,user_id,text,source,bits,status,created_at,finished_at,error) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (uuid.uuid4().hex, event_key, clean_text(username)[:60], user_id,
                          clean_text(text)[:500], source, bits, "rejected", time.time(), time.time(), str(reason)[:250]))

    def cancel_redemption(self, redemption_id):
        with store.db_session() as conn:
            conn.execute("UPDATE tts_jobs SET status='skipped',finished_at=?,error='Redemption canceled on Twitch.' WHERE event_key=? AND status IN ('pending','queued','playing')",
                         (time.time(), f"points:{redemption_id}"))

    def claim(self, client_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", str(client_id)):
            raise ValueError("Invalid player identity.")
        self.player_seen = time.time()
        settings = self.settings()
        paused = store.get_setting("tts_paused", "0") == "1"
        with store.db_session() as conn:
            self._expire(conn)
            playing = conn.execute("SELECT * FROM tts_jobs WHERE status='playing' LIMIT 1").fetchone()
            if playing:
                # An in-flight claim cannot be stolen by another browser source.
                return None
            if paused:
                return None
            row = conn.execute("SELECT * FROM tts_jobs WHERE status='queued' ORDER BY created_at,id LIMIT 1").fetchone()
            if not row:
                return None
            row = dict(row)
            conn.execute("UPDATE tts_jobs SET status='playing',lease_owner=?,lease_until=? WHERE id=?", (client_id, time.time() + 180, row["id"]))
        try:
            text = f"{row['username']} says: {row['text']}" if settings["read_username"] else row["text"]
            path, duration = self.engine.synthesize(row["id"], text, settings)
            with store.db_session() as conn:
                current = conn.execute("SELECT status FROM tts_jobs WHERE id=?", (row["id"],)).fetchone()
                if not current or current["status"] != "playing":
                    return None
                conn.execute("UPDATE tts_jobs SET duration=?,lease_until=? WHERE id=?", (duration, time.time() + duration + 30, row["id"]))
            return {"id": row["id"], "username": row["username"], "text": row["text"], "duration": duration, "volume": settings["volume"]}
        except Exception as exc:
            with store.db_session() as conn:
                conn.execute("UPDATE tts_jobs SET status='failed',error=?,finished_at=? WHERE id=? AND status='playing'", (str(exc)[:250], time.time(), row["id"]))
            raise

    def player_state(self, client_id=""):
        self.player_seen = time.time()
        with store.db_session() as conn:
            self._expire(conn)
            current = conn.execute("SELECT id,lease_owner FROM tts_jobs WHERE status='playing' LIMIT 1").fetchone()
            if current and current["lease_owner"] == client_id:
                conn.execute("UPDATE tts_jobs SET lease_until=? WHERE id=?", (time.time() + 180, current["id"]))
        return {"paused": store.get_setting("tts_paused", "0") == "1", "volume": self.settings()["volume"],
                "current_id": current["id"] if current and current["lease_owner"] == client_id else None}

    def finish(self, job_id, client_id, failed=False):
        with store.db_session() as conn:
            cursor = conn.execute("UPDATE tts_jobs SET status=?,finished_at=?,error=? WHERE id=? AND lease_owner=? AND status='playing'", ("failed" if failed else "completed", time.time(), "Audio playback failed." if failed else "", job_id, client_id))
        return cursor.rowcount > 0

    def audio_path(self, job_id, client_id):
        with store.db_session() as conn:
            row = conn.execute("SELECT status,lease_owner FROM tts_jobs WHERE id=?", (job_id,)).fetchone()
        if not row or row["status"] != "playing" or row["lease_owner"] != client_id:
            return None
        path = self.engine.path(job_id)
        return path if path.is_file() else None

    def cleanup_audio(self):
        cutoff = time.time() - 86400
        for path in self.engine.directory.glob("*.wav"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass
