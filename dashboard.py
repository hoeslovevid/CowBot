import logging
import os

import requests
from dotenv import find_dotenv, load_dotenv
from flask import Flask, flash, jsonify, redirect, render_template, request, url_for

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

EMPTY_STATUS = {
    "ok": False,
    "connected": False,
    "bot_name": "CowBot",
    "channel": "offline",
    "uptime": "Waiting for bot",
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
    "last_giveaway_winner": None,
    "leaderboard": [],
    "settings": {
        "daily_min": 25,
        "daily_max": 100,
        "starting_points": 100,
        "default_raffle_cost": 50,
        "prefixes": "?,!",
        "primary_prefix": "?",
    },
    "scheduled_messages": [],
}


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


@app.route("/health", methods=["GET", "HEAD"])
def health():
    return {"ok": True, "service": "dashboard"}


@app.route("/api/status")
def proxy_status():
    return fetch_status()


@app.route("/")
def dashboard():
    return render_template("dashboard.html", status=fetch_status())


@app.route("/update-settings", methods=["POST"])
def update_settings():
    success, error = post_bot("/api/settings", {
        "daily_min": request.form.get("daily_min"),
        "daily_max": request.form.get("daily_max"),
        "starting_points": request.form.get("starting_points"),
        "default_raffle_cost": request.form.get("default_raffle_cost"),
        "prefixes": request.form.get("prefixes"),
    })
    flash(error or "Settings saved.", "error" if error else "success")
    return redirect(url_for("dashboard"))


@app.route("/poll", methods=["POST"])
def manage_poll():
    action = request.form.get("action")
    if action == "start":
        success, error = post_bot("/api/poll", {
            "action": "start",
            "name": request.form.get("poll_name"),
            "question": request.form.get("poll_question"),
            "options": request.form.get("poll_options"),
        })
        flash(error or "Poll started in chat.", "error" if error else "success")
    else:
        success, error = post_bot("/api/poll", {"action": "end"})
        flash(error or "Poll ended and results posted to chat.", "error" if error else "success")
    return redirect(url_for("dashboard"))


@app.route("/raffle", methods=["POST"])
def manage_raffle():
    action = request.form.get("action")
    if action == "start":
        success, error = post_bot("/api/raffle", {
            "action": "start",
            "name": request.form.get("raffle_name"),
            "cost": request.form.get("raffle_cost"),
        })
        flash(error or "Raffle started in chat.", "error" if error else "success")
    else:
        success, error = post_bot("/api/raffle", {"action": "end"})
        flash(error or "Raffle ended and winner posted to chat.", "error" if error else "success")
    return redirect(url_for("dashboard"))


@app.route("/giveaway", methods=["POST"])
def manage_giveaway():
    action = request.form.get("action")
    if action == "start":
        success, error = post_bot("/api/giveaway", {
            "action": "start",
            "name": request.form.get("giveaway_name"),
        })
        flash(error or "Giveaway started in chat.", "error" if error else "success")
    else:
        success, error = post_bot("/api/giveaway", {"action": "end"})
        flash(error or "Giveaway ended and winner posted to chat.", "error" if error else "success")
    return redirect(url_for("dashboard"))


@app.route("/giveaway/draw", methods=["POST"])
def draw_giveaway():
    success, data = post_bot_data("/api/giveaway", {"action": "draw"})
    return jsonify(data), 200 if success else 400


@app.route("/giveaway/complete", methods=["POST"])
def complete_giveaway():
    success, data = post_bot_data("/api/giveaway", {"action": "complete"})
    return jsonify(data), 200 if success else 400


@app.route("/giveaway/reroll", methods=["POST"])
def reroll_giveaway():
    success, data = post_bot_data("/api/giveaway", {"action": "reroll"})
    return jsonify(data), 200 if success else 400


@app.route("/overlay/giveaway")
def giveaway_overlay():
    return render_template("giveaway_overlay.html")


@app.route("/overlay/giveaway/state")
def giveaway_overlay_state():
    try:
        response = requests.get(
            f"{BOT_API_URL}/api/giveaway/overlay",
            headers=bot_headers(),
            timeout=3,
        )
        data = response.json() if response.content else {}
        return jsonify(data if isinstance(data, dict) else {"ok": True, "spin": None}), response.status_code
    except (requests.RequestException, ValueError):
        return jsonify({"ok": True, "spin": None})


@app.route("/quote", methods=["POST"])
def manage_quote():
    success, error = post_bot("/api/quote", {
        "text": request.form.get("quote_text"),
        "author": request.form.get("quote_author") or "Unknown",
    })
    flash(error or "Quote added.", "error" if error else "success")
    return redirect(url_for("dashboard"))


@app.route("/schedule", methods=["POST"])
def manage_schedule():
    action = request.form.get("action")
    if action == "create":
        success, error = post_bot("/api/schedule", {
            "action": "create",
            "message": request.form.get("schedule_message"),
            "interval_minutes": request.form.get("interval_minutes"),
        })
        flash(error or "Scheduled message added.", "error" if error else "success")
    else:
        success, error = post_bot("/api/schedule", {
            "action": action,
            "id": request.form.get("id"),
        })
        if error:
            flash(error, "error")
        elif action == "send":
            flash("Message sent to chat.", "success")
        elif action == "delete":
            flash("Scheduled message removed.", "success")
        else:
            flash("Scheduled message updated.", "success")
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    from waitress import serve

    print(f"Dashboard listening on 0.0.0.0:{PORT}")
    serve(app, host="0.0.0.0", port=PORT, threads=8, channel_timeout=20, ident="cowbot")
