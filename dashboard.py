import logging
import os
import secrets
from urllib.parse import urlencode

import requests
from dotenv import find_dotenv, load_dotenv
from flask import Flask, flash, jsonify, make_response, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

import store

env_path = find_dotenv()
if env_path:
    load_dotenv(env_path)

BOT_API_URL = (os.getenv("BOT_API_URL") or "http://127.0.0.1:8080").rstrip("/")
API_SECRET = os.getenv("API_SECRET", "")
PORT = int(os.getenv("PORT") or "5000")

logging.getLogger("werkzeug").setLevel(logging.ERROR)
logging.getLogger("urllib3").setLevel(logging.ERROR)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or os.getenv("API_SECRET") or "cowbot-dev-secret"
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

EMPTY_STATUS = {
    "ok": False,
    "connected": False,
    "bot_name": "CowBot",
    "channel": "offline",
    "uptime": "Waiting for bot",
    "stream_live": False,
    "active_poll": None,
    "poll_question": None,
    "poll_results": [],
    "active_raffle": None,
    "raffle_cost": None,
    "active_giveaway": None,
    "giveaway_open": False,
    "giveaway_drawing": False,
    "giveaway_entries": [],
    "giveaway_entry_count": 0,
    "giveaway_winner_count": 1,
    "giveaway_winners": [],
    "last_giveaway_name": None,
    "last_giveaway_winner": None,
    "leaderboard": [],
    "settings": {
        "daily_min": 25,
        "daily_max": 100,
        "starting_points": 100,
        "default_raffle_cost": 50,
        "watchtime_points": 10,
        "prefixes": "?,!",
        "primary_prefix": "?",
        "lurk_message": store.DEFAULT_LURK_MESSAGE,
    },
    "scheduled_messages": [],
    "custom_commands": [],
    "features": {
        key: {**meta, "enabled": True}
        for key, meta in store.FEATURE_MODULES.items()
    },
    "builtin_command_groups": store.get_command_groups(live=False),
}


def public_origin() -> str:
    configured = (os.getenv("PUBLIC_URL") or os.getenv("RAILWAY_PUBLIC_DOMAIN") or "").strip().rstrip("/")
    if configured:
        if configured.startswith("http://") or configured.startswith("https://"):
            return configured
        return f"https://{configured}"
    proto = (request.headers.get("X-Forwarded-Proto") or request.scheme or "https").split(",")[0].strip()
    host = (request.headers.get("X-Forwarded-Host") or request.host).split(",")[0].strip()
    if "railway.app" in host and proto == "http":
        proto = "https"
    return f"{proto}://{host}"


def overlay_page_url() -> str:
    return f"{public_origin()}{url_for('giveaway_overlay')}"


def oauth_callback_url() -> str:
    return f"{public_origin()}{url_for('twitch_oauth_callback')}"


def bot_headers() -> dict[str, str]:
    headers = {"Accept": "application/json", "X-CowBot-Proxy": "1"}
    if API_SECRET:
        headers["X-API-Secret"] = API_SECRET
    return headers


def fetch_status() -> dict:
    if request.headers.get("X-CowBot-Proxy"):
        status = dict(EMPTY_STATUS)
        status["bot_reachable"] = False
        return status
    try:
        response = requests.get(
            f"{BOT_API_URL}/api/status",
            headers=bot_headers(),
            timeout=3,
        )
        response.raise_for_status()
        payload = response.json()
        payload["bot_reachable"] = True
        features = {
            key: {**meta, "enabled": True}
            for key, meta in store.FEATURE_MODULES.items()
        }
        incoming = payload.get("features") or {}
        for key, meta in features.items():
            row = incoming.get(key)
            if isinstance(row, dict):
                meta["enabled"] = bool(row.get("enabled", True))
        payload["features"] = features
        if not payload.get("builtin_command_groups"):
            payload["builtin_command_groups"] = store.get_command_groups(live=False)
        return payload
    except requests.RequestException:
        status = dict(EMPTY_STATUS)
        status["bot_reachable"] = False
        return status


def post_bot_data(path: str, payload: dict) -> tuple[bool, dict]:
    try:
        response = requests.post(
            f"{BOT_API_URL}{path}",
            json=payload,
            headers=bot_headers(),
            timeout=8,
        )
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.ok and data.get("ok", True):
            return True, data if isinstance(data, dict) else {"ok": True}
        error = data.get("error") if isinstance(data, dict) else None
        return False, {"ok": False, "error": error or f"Bot API returned {response.status_code}"}
    except requests.RequestException as exc:
        return False, {"ok": False, "error": f"Could not reach bot API: {exc}"}


def post_bot(path: str, payload: dict) -> tuple[bool, str | None]:
    success, data = post_bot_data(path, payload)
    if success:
        return True, None
    return False, data.get("error")


def wants_json() -> bool:
    return "application/json" in (request.headers.get("Accept") or "")


def posted() -> dict:
    if request.is_json:
        return request.get_json(silent=True) or {}
    return request.form.to_dict(flat=True)


def finish(success: bool, ok_message: str, error: str | None = None):
    if wants_json():
        payload = {"ok": success}
        if success:
            payload["message"] = ok_message
        else:
            payload["error"] = error or ok_message
        return jsonify(payload), 200 if success else 400
    flash(error or ok_message, "error" if not success else "success")
    return redirect(url_for("dashboard"))


@app.after_request
def cache_static(response):
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=604800, immutable"
    return response


@app.route("/health", methods=["GET", "HEAD"])
def health():
    return {"ok": True, "service": "dashboard"}


@app.route("/api/status")
def proxy_status():
    body = jsonify(fetch_status())
    body.headers["Cache-Control"] = "no-store"
    return body


@app.route("/")
def dashboard():
    return render_template(
        "dashboard.html",
        status=fetch_status(),
        overlay_url=overlay_page_url(),
        oauth_redirect_url=oauth_callback_url(),
    )


@app.route("/oauth")
def twitch_oauth_start():
    client_id = (os.getenv("TWITCH_CLIENT_ID") or "").strip()
    if not client_id or client_id.lower().startswith("your_"):
        return redirect(url_for("dashboard", oauth="config"))
    state = secrets.token_urlsafe(24)
    redirect_uri = oauth_callback_url()
    session["oauth_state"] = state
    session["oauth_redirect"] = redirect_uri
    params = urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(store.BOT_OAUTH_SCOPES),
        "force_verify": "true",
        "state": state,
    })
    return redirect(f"https://id.twitch.tv/oauth2/authorize?{params}")


@app.route("/oauth/callback")
def twitch_oauth_callback():
    if request.args.get("state") != session.get("oauth_state"):
        return redirect(url_for("dashboard", oauth="bad"))
    if request.args.get("error"):
        return redirect(url_for("dashboard", oauth="denied"))
    code = (request.args.get("code") or "").strip()
    redirect_uri = session.get("oauth_redirect") or oauth_callback_url()
    client_id = (os.getenv("TWITCH_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("TWITCH_CLIENT_SECRET") or "").strip()
    if not code or not client_id or not client_secret:
        return redirect(url_for("dashboard", oauth="bad"))
    try:
        token_response = requests.post(
            "https://id.twitch.tv/oauth2/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
            timeout=12,
        )
        payload = token_response.json() if token_response.content else {}
    except (requests.RequestException, ValueError):
        return redirect(url_for("dashboard", oauth="bad"))
    access = str(payload.get("access_token") or "").strip()
    refresh = str(payload.get("refresh_token") or "").strip()
    if not token_response.ok or not access:
        return redirect(url_for("dashboard", oauth="redirect"))
    success, error = post_bot("/api/oauth", {
        "access_token": access,
        "refresh_token": refresh,
    })
    session.pop("oauth_state", None)
    session.pop("oauth_redirect", None)
    if not success:
        return redirect(url_for("dashboard", oauth="bot"))
    return redirect(url_for("dashboard", oauth="ok"))


@app.route("/update-settings", methods=["POST"])
def update_settings():
    data = posted()
    success, error = post_bot("/api/settings", {
        "daily_min": data.get("daily_min"),
        "daily_max": data.get("daily_max"),
        "starting_points": data.get("starting_points"),
        "default_raffle_cost": data.get("default_raffle_cost"),
        "watchtime_points": data.get("watchtime_points"),
        "prefixes": data.get("prefixes"),
    })
    return finish(success, "Economy settings saved.", error)


@app.route("/features", methods=["POST"])
def update_features():
    data = posted()
    flags = {
        key: "1" if str(data.get(key, "")).lower() in {"1", "true", "on", "yes"} else "0"
        for key in store.FEATURE_MODULES
    }
    success, error = post_bot("/api/features", flags)
    return finish(success, "Modules updated.", error)


@app.route("/builtin-commands", methods=["POST"])
def update_builtin_commands():
    data = posted()
    commands = data.get("commands") if isinstance(data.get("commands"), dict) else {
        name: "1" if data.get(name) else "0"
        for name in store.BUILTIN_COMMANDS
        if name in data
    }
    payload = {"commands": commands}
    if "lurk_message" in data:
        payload["lurk_message"] = data.get("lurk_message")
    success, error = post_bot("/api/builtin-commands", payload)
    return finish(success, "Built-in commands updated.", error)


@app.route("/lurk-message", methods=["POST"])
def update_lurk_message():
    data = posted()
    success, error = post_bot("/api/builtin-commands", {
        "lurk_message": data.get("lurk_message"),
    })
    return finish(success, "Lurk message saved.", error)


@app.route("/poll", methods=["POST"])
def manage_poll():
    data = posted()
    action = data.get("action")
    if action == "start":
        success, error = post_bot("/api/poll", {
            "action": "start",
            "name": data.get("poll_name"),
            "question": data.get("poll_question"),
            "options": data.get("poll_options"),
        })
        return finish(success, "Poll started in chat.", error)
    success, error = post_bot("/api/poll", {"action": "end"})
    return finish(success, "Poll ended and results posted to chat.", error)


@app.route("/raffle", methods=["POST"])
def manage_raffle():
    data = posted()
    action = data.get("action")
    if action == "start":
        success, error = post_bot("/api/raffle", {
            "action": "start",
            "name": data.get("raffle_name"),
            "cost": data.get("raffle_cost"),
        })
        return finish(success, "Raffle started in chat.", error)
    success, error = post_bot("/api/raffle", {"action": "end"})
    return finish(success, "Raffle ended and winner posted to chat.", error)


@app.route("/giveaway", methods=["POST"])
def manage_giveaway():
    data = posted()
    action = data.get("action")
    if action == "start":
        success, error = post_bot("/api/giveaway", {
            "action": "start",
            "name": data.get("giveaway_name"),
            "count": data.get("giveaway_winners"),
        })
        return finish(success, "Giveaway started in chat.", error)
    if action == "cancel":
        success, error = post_bot("/api/giveaway", {"action": "cancel"})
        return finish(success, "Giveaway cancelled. No winner was chosen.", error)
    success, error = post_bot("/api/giveaway", {"action": "end"})
    return finish(success, "Giveaway ended and winner posted to chat.", error)


@app.route("/giveaway/draw", methods=["POST"])
def draw_giveaway():
    data = posted()
    success, result = post_bot_data("/api/giveaway", {
        "action": "draw",
        "count": data.get("count") or data.get("giveaway_winners"),
    })
    return jsonify(result), 200 if success else 400


@app.route("/giveaway/complete", methods=["POST"])
def complete_giveaway():
    success, data = post_bot_data("/api/giveaway", {"action": "complete"})
    return jsonify(data), 200 if success else 400


@app.route("/giveaway/reroll", methods=["POST"])
def reroll_giveaway():
    data = posted()
    success, result = post_bot_data("/api/giveaway", {
        "action": "reroll",
        "replace": data.get("replace"),
    })
    return jsonify(result), 200 if success else 400


@app.route("/overlay/giveaway")
def giveaway_overlay():
    resp = make_response(render_template("giveaway_overlay.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/overlay/giveaway/state")
def giveaway_overlay_state():
    try:
        response = requests.get(
            f"{BOT_API_URL}/api/giveaway/overlay",
            headers=bot_headers(),
            timeout=3,
        )
        data = response.json() if response.content else {}
        payload = data if isinstance(data, dict) else {"ok": True, "spin": None}
        payload.setdefault("ok", True)
        if "spin" not in payload:
            payload["spin"] = None
        body = jsonify(payload)
        body.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        return body, 200
    except (requests.RequestException, ValueError):
        body = jsonify({"ok": True, "spin": None})
        body.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        return body


@app.route("/quote", methods=["POST"])
def manage_quote():
    data = posted()
    success, error = post_bot("/api/quote", {
        "text": data.get("quote_text"),
        "author": data.get("quote_author") or "Unknown",
    })
    return finish(success, "Quote added.", error)


@app.route("/schedule", methods=["POST"])
def manage_schedule():
    data = posted()
    action = data.get("action")
    if action == "create":
        success, error = post_bot("/api/schedule", {
            "action": "create",
            "message": data.get("schedule_message"),
            "interval_minutes": data.get("interval_minutes"),
        })
        return finish(success, "Scheduled message added.", error)
    success, error = post_bot("/api/schedule", {
        "action": action,
        "id": data.get("id"),
    })
    if error:
        return finish(False, error, error)
    if action == "send":
        return finish(True, "Message sent to chat.")
    if action == "delete":
        return finish(True, "Scheduled message removed.")
    return finish(True, "Scheduled message updated.")


@app.route("/commands", methods=["POST"])
def manage_commands():
    data = posted()
    action = data.get("action")
    if action in {"create", "update"}:
        success, error = post_bot("/api/commands", {
            "action": action,
            "id": data.get("command_id") or data.get("id"),
            "name": data.get("command_name"),
            "response": data.get("command_response"),
            "aliases": data.get("command_aliases"),
            "cooldown_seconds": data.get("command_cooldown"),
        })
        message = "Custom command updated." if action == "update" else "Custom command saved."
        return finish(success, message, error)
    success, error = post_bot("/api/commands", {
        "action": action,
        "id": data.get("id"),
    })
    if error:
        return finish(False, error, error)
    if action == "delete":
        return finish(True, "Custom command removed.")
    return finish(True, "Custom command updated.")


if __name__ == "__main__":
    from waitress import serve

    print(f"Dashboard listening on 0.0.0.0:{PORT}")
    serve(app, host="0.0.0.0", port=PORT, threads=8, channel_timeout=20, ident="cowbot")
