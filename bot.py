import asyncio
import os
import random
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from threading import Lock, Thread

from dotenv import load_dotenv, find_dotenv
from flask import Flask, render_template_string, request, redirect, url_for
from twitchio import Scopes, eventsub
from twitchio.ext import commands


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_uptime(delta: timedelta) -> str:
    total_seconds = max(int(delta.total_seconds()), 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}h {minutes}m {seconds}s"


def strip_oauth_prefix(token: str) -> str:
    token = token.strip()
    if token.lower().startswith("oauth:"):
        return token.split(":", 1)[1].strip()
    return token


def validate_environment() -> None:
    env_path = find_dotenv()
    if not env_path:
        raise RuntimeError(
            "Unable to locate a .env file. Copy .env.example to .env in the project root and set TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, TWITCH_BOT_ID, TWITCH_NICK, and TWITCH_CHANNEL."
        )
    load_dotenv(env_path)

    missing = [
        key for key in (
            "TWITCH_CLIENT_ID",
            "TWITCH_CLIENT_SECRET",
            "TWITCH_BOT_ID",
            "TWITCH_NICK",
            "TWITCH_CHANNEL",
        ) if not os.getenv(key)
    ]
    if missing:
        raise RuntimeError(
            "Missing environment variables: "
            + ", ".join(missing)
            + ". Please add them to your .env file."
        )


validate_environment()

TWITCH_CLIENT_ID: str = os.getenv("TWITCH_CLIENT_ID") or ""
TWITCH_CLIENT_SECRET: str = os.getenv("TWITCH_CLIENT_SECRET") or ""
TWITCH_BOT_ID: str = os.getenv("TWITCH_BOT_ID") or ""
BOT_TOKEN: str = strip_oauth_prefix(os.getenv("TWITCH_TOKEN") or "")
BOT_REFRESH_TOKEN: str = (os.getenv("TWITCH_REFRESH_TOKEN") or "").strip()
BOT_NICK: str = os.getenv("TWITCH_NICK") or ""
CHANNEL: str = (os.getenv("TWITCH_CHANNEL") or "").lstrip("#").strip()
COMMAND_PREFIX: str = os.getenv("PREFIX", "!") or "!"

DB_PATH = os.path.join(os.path.dirname(__file__), "bot.db")
_db_lock = Lock()
bot: "CowBot | None" = None

app = Flask(__name__)

dashboard_template = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Twitch Bot Dashboard</title>
  <style>
    body { font-family: Arial, sans-serif; max-width: 960px; margin: auto; padding: 1rem; }
    section { margin-bottom: 2rem; }
    label { display: block; margin: 0.5rem 0 0.2rem; }
    input { width: 100%; padding: 0.5rem; }
    button { margin-top: 1rem; padding: 0.75rem 1.2rem; }
    table { width: 100%; border-collapse: collapse; margin-top: 0.5rem; }
    th, td { border: 1px solid #ccc; padding: 0.4rem 0.6rem; text-align: left; }
  </style>
</head>
<body>
  <h1>Twitch Bot Dashboard</h1>
  <section>
    <h2>Status</h2>
    <p><strong>Bot uptime:</strong> {{ uptime }}</p>
    <p><strong>Active poll:</strong> {{ active_poll or 'None' }}</p>
    {% if active_poll %}
      <p><strong>Poll question:</strong> {{ poll_question }}</p>
      <p><strong>Poll results:</strong> {{ poll_results }}</p>
    {% endif %}
    <p><strong>Active raffle:</strong> {{ active_raffle or 'None' }}</p>
    {% if active_raffle %}
      <p><strong>Raffle cost:</strong> {{ raffle_cost }}</p>
    {% endif %}
    <p><strong>Active giveaway:</strong> {{ active_giveaway or 'None' }}</p>
    {% if last_giveaway_winner %}
      <p><strong>Last giveaway winner:</strong> {{ last_giveaway_winner }}</p>
    {% endif %}
  </section>
  <section>
    <h2>Leaderboard</h2>
    <table>
      <thead><tr><th>User</th><th>Points</th></tr></thead>
      <tbody>
      {% for row in leaderboard %}
        <tr><td>{{ row.user }}</td><td>{{ row.points }}</td></tr>
      {% endfor %}
      </tbody>
    </table>
  </section>
  <section>
    <h2>Settings</h2>
    <form method="post" action="{{ url_for('update_settings') }}">
      <label>Daily reward min:</label>
      <input name="daily_min" value="{{ settings.daily_min }}">
      <label>Daily reward max:</label>
      <input name="daily_max" value="{{ settings.daily_max }}">
      <label>Starting points:</label>
      <input name="starting_points" value="{{ settings.starting_points }}">
      <label>Default raffle cost:</label>
      <input name="default_raffle_cost" value="{{ settings.default_raffle_cost }}">
      <button type="submit">Save settings</button>
    </form>
  </section>
  <section>
    <h2>Poll Management</h2>
    <form method="post" action="{{ url_for('manage_poll') }}">
      <label>Start poll (name | question | option1, option2, ...):</label>
      <input name="start_poll" placeholder="name | question | yes, no">
      <button type="submit" name="action" value="start">Start poll</button>
    </form>
    <form method="post" action="{{ url_for('manage_poll') }}" style="margin-top: 1rem;">
      <button type="submit" name="action" value="end">End active poll</button>
      <button type="submit" name="action" value="status">Refresh poll status</button>
    </form>
  </section>
  <section>
    <h2>Raffle Management</h2>
    <form method="post" action="{{ url_for('manage_raffle') }}">
      <label>Start raffle (name | cost):</label>
      <input name="start_raffle" placeholder="raffle | 50">
      <button type="submit" name="action" value="start">Start raffle</button>
    </form>
    <form method="post" action="{{ url_for('manage_raffle') }}" style="margin-top: 1rem;">
      <button type="submit" name="action" value="end">End active raffle</button>
    </form>
  </section>
  <section>
    <h2>Giveaway Control</h2>
    <form method="post" action="{{ url_for('manage_giveaway') }}">
      <label>Start giveaway (name):</label>
      <input name="start_giveaway" placeholder="giveaway_name">
      <button type="submit" name="action" value="start">Start giveaway</button>
    </form>
    <form method="post" action="{{ url_for('manage_giveaway') }}" style="margin-top: 1rem;">
      <button type="submit" name="action" value="end">End active giveaway</button>
    </form>
  </section>
  <section>
    <h2>Quote Management</h2>
    <form method="post" action="{{ url_for('manage_quote') }}">
      <label>Add quote (quote text | author):</label>
      <input name="new_quote" placeholder="Never give up | Coach">
      <button type="submit">Add quote</button>
    </form>
  </section>
</body>
</html>
"""


def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db_session():
    with _db_lock:
        conn = get_db_connection()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def init_db():
    with db_session() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS points (
                user TEXT PRIMARY KEY,
                points INTEGER NOT NULL DEFAULT 100,
                last_daily TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS giveaway_entries (
                giveaway_name TEXT,
                user TEXT,
                PRIMARY KEY (giveaway_name, user)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS quotes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                author TEXT,
                added_by TEXT NOT NULL,
                added_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS polls (
                name TEXT PRIMARY KEY,
                question TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS poll_options (
                poll_name TEXT,
                option TEXT,
                votes INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (poll_name, option)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS poll_votes (
                poll_name TEXT,
                user TEXT,
                option TEXT,
                PRIMARY KEY (poll_name, user)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS raffles (
                name TEXT PRIMARY KEY,
                entry_cost INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS raffle_entries (
                raffle_name TEXT,
                user TEXT,
                PRIMARY KEY (raffle_name, user)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )


def normalize_user(user_name: str | None) -> str:
    if not user_name:
        return "unknown"
    return user_name.strip().lstrip("@").lower()


def parse_non_negative_int(value: str | None, default: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def get_author_name(ctx: commands.Context) -> str:
    author = getattr(ctx, "author", None) or getattr(ctx, "chatter", None)
    return getattr(author, "name", None) or getattr(author, "display_name", None) or "Unknown"


def is_mod_or_broadcaster(ctx: commands.Context) -> bool:
    chatter = getattr(ctx, "chatter", None) or getattr(ctx, "author", None)
    if chatter is None:
        return False
    return bool(
        getattr(chatter, "moderator", False)
        or getattr(chatter, "broadcaster", False)
        or getattr(chatter, "is_mod", False)
        or getattr(chatter, "is_broadcaster", False)
    )


def ensure_user_row(conn: sqlite3.Connection, user: str, starting_points: int) -> None:
    conn.execute(
        "INSERT INTO points (user, points) VALUES (?, ?) ON CONFLICT(user) DO NOTHING",
        (user, starting_points),
    )


def get_points(user_name: str) -> int:
    user = normalize_user(user_name)
    starting_points = get_dashboard_settings()["starting_points"]
    with db_session() as conn:
        row = conn.execute(
            "SELECT points FROM points WHERE user = ?", (user,)
        ).fetchone()
        if row:
            return row["points"]
        return starting_points


def change_points(user_name: str, amount: int) -> int:
    user = normalize_user(user_name)
    starting_points = get_dashboard_settings()["starting_points"]
    with db_session() as conn:
        ensure_user_row(conn, user, starting_points)
        conn.execute(
            "UPDATE points SET points = MAX(points + ?, 0) WHERE user = ?",
            (amount, user),
        )
        row = conn.execute("SELECT points FROM points WHERE user = ?", (user,)).fetchone()
        return row["points"]


def try_spend_points(user_name: str, amount: int) -> tuple[bool, int]:
    user = normalize_user(user_name)
    starting_points = get_dashboard_settings()["starting_points"]
    with db_session() as conn:
        ensure_user_row(conn, user, starting_points)
        cursor = conn.execute(
            "UPDATE points SET points = points - ? WHERE user = ? AND points >= ?",
            (amount, user, amount),
        )
        row = conn.execute("SELECT points FROM points WHERE user = ?", (user,)).fetchone()
        current = row["points"]
        return cursor.rowcount > 0, current


def add_quote(text: str, author: str, added_by: str | None) -> int | None:
    with db_session() as conn:
        cursor = conn.execute(
            "INSERT INTO quotes (text, author, added_by, added_at) VALUES (?, ?, ?, ?)",
            (text, author, added_by or "Unknown", utc_now().isoformat()),
        )
        return cursor.lastrowid


def get_random_quote():
    with db_session() as conn:
        return conn.execute(
            "SELECT id, text, author FROM quotes ORDER BY RANDOM() LIMIT 1"
        ).fetchone()


def get_leaderboard(limit: int = 10):
    with db_session() as conn:
        return conn.execute(
            "SELECT user, points FROM points ORDER BY points DESC LIMIT ?", (limit,)
        ).fetchall()


def unique_options(options: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for option in options:
        key = option.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(option)
    return unique


def create_poll(name: str, question: str, options: list[str]) -> tuple[bool, str | None]:
    name = normalize_user(name)
    options = unique_options(options)
    if len(options) < 2:
        return False, "A poll needs at least two unique options."
    with db_session() as conn:
        active = conn.execute("SELECT name FROM polls WHERE active = 1 LIMIT 1").fetchone()
        if active:
            return False, f"A poll is already running ({active['name']}). End it first."
        conn.execute("UPDATE polls SET active = 0 WHERE name = ?", (name,))
        conn.execute("DELETE FROM poll_votes WHERE poll_name = ?", (name,))
        conn.execute("DELETE FROM poll_options WHERE poll_name = ?", (name,))
        conn.execute(
            """
            INSERT INTO polls (name, question, active) VALUES (?, ?, 1)
            ON CONFLICT(name) DO UPDATE SET question = excluded.question, active = 1
            """,
            (name, question),
        )
        conn.executemany(
            "INSERT INTO poll_options (poll_name, option, votes) VALUES (?, ?, 0)",
            [(name, option) for option in options],
        )
    return True, None


def get_poll_options(name: str):
    name = normalize_user(name)
    with db_session() as conn:
        return conn.execute(
            "SELECT option, votes FROM poll_options WHERE poll_name = ? ORDER BY votes DESC", (name,)
        ).fetchall()


def match_poll_option(conn: sqlite3.Connection, name: str, option: str) -> str | None:
    rows = conn.execute(
        "SELECT option FROM poll_options WHERE poll_name = ?",
        (name,),
    ).fetchall()
    option_key = option.strip().casefold()
    for row in rows:
        if row["option"].casefold() == option_key:
            return row["option"]
    return None


def vote_poll(name: str, user_name: str, option: str) -> tuple[bool, str | None]:
    name = normalize_user(name)
    user_name = normalize_user(user_name)
    option_value = option.strip()
    with db_session() as conn:
        matched = match_poll_option(conn, name, option_value)
        if not matched:
            return False, "That option is not available for the current poll."

        existing_vote = conn.execute(
            "SELECT option FROM poll_votes WHERE poll_name = ? AND user = ?",
            (name, user_name),
        ).fetchone()
        if existing_vote:
            if existing_vote["option"] == matched:
                return False, "You already voted for that option."
            conn.execute(
                "UPDATE poll_options SET votes = MAX(votes - 1, 0) WHERE poll_name = ? AND option = ?",
                (name, existing_vote["option"]),
            )
            conn.execute(
                "UPDATE poll_votes SET option = ? WHERE poll_name = ? AND user = ?",
                (matched, name, user_name),
            )
        else:
            conn.execute(
                "INSERT INTO poll_votes (poll_name, user, option) VALUES (?, ?, ?)",
                (name, user_name, matched),
            )
        conn.execute(
            "UPDATE poll_options SET votes = votes + 1 WHERE poll_name = ? AND option = ?",
            (name, matched),
        )
    return True, matched


def end_poll(name: str):
    name = normalize_user(name)
    with db_session() as conn:
        conn.execute("UPDATE polls SET active = 0 WHERE name = ?", (name,))
        return conn.execute(
            "SELECT option, votes FROM poll_options WHERE poll_name = ? ORDER BY votes DESC",
            (name,),
        ).fetchall()


def create_raffle(name: str, entry_cost: int) -> tuple[bool, str | None]:
    name = normalize_user(name)
    if entry_cost <= 0:
        return False, "Entry cost must be a positive number."
    with db_session() as conn:
        active = conn.execute("SELECT name FROM raffles WHERE active = 1 LIMIT 1").fetchone()
        if active:
            return False, f"A raffle is already running ({active['name']}). End it first."
        conn.execute(
            """
            INSERT INTO raffles (name, entry_cost, active) VALUES (?, ?, 1)
            ON CONFLICT(name) DO UPDATE SET entry_cost = excluded.entry_cost, active = 1
            """,
            (name, entry_cost),
        )
        conn.execute("DELETE FROM raffle_entries WHERE raffle_name = ?", (name,))
    return True, None


def enter_raffle(name: str, user_name: str) -> tuple[bool, str | None]:
    name = normalize_user(name)
    user_name = normalize_user(user_name)
    starting_points = get_dashboard_settings()["starting_points"]
    with db_session() as conn:
        raffle = conn.execute(
            "SELECT entry_cost FROM raffles WHERE name = ? AND active = 1", (name,)
        ).fetchone()
        if not raffle:
            return False, "There is no active raffle with that name."
        if conn.execute(
            "SELECT 1 FROM raffle_entries WHERE raffle_name = ? AND user = ?",
            (name, user_name),
        ).fetchone():
            return False, "You are already entered in this raffle."

        cost = raffle["entry_cost"]
        ensure_user_row(conn, user_name, starting_points)
        cursor = conn.execute(
            "UPDATE points SET points = points - ? WHERE user = ? AND points >= ?",
            (cost, user_name, cost),
        )
        if cursor.rowcount == 0:
            return False, "You do not have enough points to enter."
        conn.execute(
            "INSERT INTO raffle_entries (raffle_name, user) VALUES (?, ?)",
            (name, user_name),
        )
    return True, None


def end_raffle(name: str):
    name = normalize_user(name)
    with db_session() as conn:
        entries = conn.execute(
            "SELECT user FROM raffle_entries WHERE raffle_name = ?", (name,)
        ).fetchall()
        conn.execute("UPDATE raffles SET active = 0 WHERE name = ?", (name,))
        if not entries:
            return None
        return random.choice(entries)["user"]


def start_giveaway(name: str) -> tuple[bool, str | None]:
    if bot is None:
        return False, "Bot is not running."
    giveaway_name = normalize_user(name)
    if not giveaway_name or giveaway_name == "unknown":
        return False, "Giveaway name cannot be empty."
    with db_session() as conn:
        if bot.active_giveaway:
            return False, "A giveaway is already running."
        conn.execute("DELETE FROM giveaway_entries WHERE giveaway_name = ?", (giveaway_name,))
        bot.active_giveaway = giveaway_name
    return True, giveaway_name


def enter_giveaway(user_name: str) -> tuple[bool, str | None]:
    if bot is None:
        return False, "No giveaway is currently active."
    author_name = normalize_user(user_name)
    with db_session() as conn:
        if not bot.active_giveaway:
            return False, "No giveaway is currently active."
        cursor = conn.execute(
            "INSERT OR IGNORE INTO giveaway_entries (giveaway_name, user) VALUES (?, ?)",
            (bot.active_giveaway, author_name),
        )
        if cursor.rowcount == 0:
            return False, f"{author_name} is already entered in the giveaway."
    return True, author_name


def finish_giveaway() -> tuple[str | None, str | None]:
    if bot is None:
        return None, "No giveaway is currently active."
    with db_session() as conn:
        if not bot.active_giveaway:
            return None, "No giveaway is currently active."
        giveaway_name = bot.active_giveaway
        rows = conn.execute(
            "SELECT user FROM giveaway_entries WHERE giveaway_name = ?",
            (giveaway_name,),
        ).fetchall()
        bot.active_giveaway = None
        if not rows:
            return None, f"No entries for giveaway '{giveaway_name}'."
        winner = random.choice(rows)["user"]
        bot.last_giveaway_winner = winner
        return winner, giveaway_name


def get_active_poll_name():
    with db_session() as conn:
        row = conn.execute("SELECT name FROM polls WHERE active = 1 LIMIT 1").fetchone()
        return row["name"] if row else None


def get_active_raffle_name():
    with db_session() as conn:
        row = conn.execute("SELECT name FROM raffles WHERE active = 1 LIMIT 1").fetchone()
        return row["name"] if row else None


def get_raffle_cost(name: str) -> int | None:
    name = normalize_user(name)
    with db_session() as conn:
        row = conn.execute(
            "SELECT entry_cost FROM raffles WHERE name = ? AND active = 1", (name,)
        ).fetchone()
        return row["entry_cost"] if row else None


def get_setting(key: str, default: str) -> str:
    with db_session() as conn:
        row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str):
    with db_session() as conn:
        conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_dashboard_settings() -> dict:
    daily_min = parse_non_negative_int(get_setting("daily_min", "25"), 25)
    daily_max = parse_non_negative_int(get_setting("daily_max", "100"), 100)
    if daily_min > daily_max:
        daily_min, daily_max = daily_max, daily_min
    return {
        "daily_min": daily_min,
        "daily_max": daily_max,
        "starting_points": parse_non_negative_int(get_setting("starting_points", "100"), 100),
        "default_raffle_cost": max(parse_non_negative_int(get_setting("default_raffle_cost", "50"), 50), 1),
    }


def get_poll_question(name: str) -> str | None:
    name = normalize_user(name)
    with db_session() as conn:
        row = conn.execute("SELECT question FROM polls WHERE name = ?", (name,)).fetchone()
        return row["question"] if row else None


def announce_from_dashboard(message: str) -> None:
    if bot is None or bot.loop is None:
        return
    asyncio.run_coroutine_threadsafe(bot.send_channel_message(message), bot.loop)


def try_claim_daily(user_name: str) -> tuple[bool, int, int]:
    user = normalize_user(user_name)
    settings = get_dashboard_settings()
    earned = random.randint(settings["daily_min"], settings["daily_max"])
    now = utc_now().isoformat()
    with db_session() as conn:
        ensure_user_row(conn, user, settings["starting_points"])
        row = conn.execute(
            "SELECT last_daily FROM points WHERE user = ?",
            (user,),
        ).fetchone()
        if row and row["last_daily"]:
            last = parse_iso(row["last_daily"])
            if utc_now() - last < timedelta(hours=24):
                current = conn.execute(
                    "SELECT points FROM points WHERE user = ?", (user,)
                ).fetchone()
                return False, 0, current["points"]
        conn.execute(
            "UPDATE points SET points = points + ?, last_daily = ? WHERE user = ?",
            (earned, now, user),
        )
        current = conn.execute(
            "SELECT points FROM points WHERE user = ?", (user,)
        ).fetchone()
        return True, earned, current["points"]


@app.route("/", methods=["GET"])
def dashboard():
    settings = get_dashboard_settings()
    poll_name = get_active_poll_name()
    poll_question = get_poll_question(poll_name) if poll_name else None
    poll_results = ", ".join(
        f"{row['option']}({row['votes']})"
        for row in get_poll_options(poll_name)
    ) if poll_name else ""
    raffle_name = get_active_raffle_name()
    raffle_cost = get_raffle_cost(raffle_name) if raffle_name else None
    active_giveaway = bot.active_giveaway if bot else None
    last_giveaway_winner = bot.last_giveaway_winner if bot else None
    leaderboard = get_leaderboard(10)
    uptime = format_uptime(utc_now() - bot.start_time) if bot else "Unknown"
    return render_template_string(
        dashboard_template,
        uptime=uptime,
        active_poll=poll_name,
        poll_question=poll_question,
        poll_results=poll_results,
        active_raffle=raffle_name,
        raffle_cost=raffle_cost,
        active_giveaway=active_giveaway,
        last_giveaway_winner=last_giveaway_winner,
        leaderboard=leaderboard,
        settings=settings,
    )


@app.route("/update-settings", methods=["POST"])
def update_settings():
    daily_min = parse_non_negative_int(request.form.get("daily_min"), 25)
    daily_max = parse_non_negative_int(request.form.get("daily_max"), 100)
    if daily_min > daily_max:
        daily_min, daily_max = daily_max, daily_min
    starting_points = parse_non_negative_int(request.form.get("starting_points"), 100)
    default_raffle_cost = max(parse_non_negative_int(request.form.get("default_raffle_cost"), 50), 1)
    set_setting("daily_min", str(daily_min))
    set_setting("daily_max", str(daily_max))
    set_setting("starting_points", str(starting_points))
    set_setting("default_raffle_cost", str(default_raffle_cost))
    return redirect(url_for("dashboard"))


@app.route("/poll", methods=["POST"])
def manage_poll():
    action = request.form.get("action")
    if action == "start":
        raw = request.form.get("start_poll", "")
        parts = [part.strip() for part in raw.split("|", 2)]
        if len(parts) >= 3:
            name, question, options_text = parts
            options = [opt.strip() for opt in options_text.split(",") if opt.strip()]
            success, error = create_poll(name, question, options)
            if success:
                announce_from_dashboard(f"Poll '{name}' started: {question} Options: {', '.join(unique_options(options))}")
            elif error:
                print(f"Dashboard poll start failed: {error}")
    elif action == "end":
        active_poll = get_active_poll_name()
        if active_poll:
            results = end_poll(active_poll)
            top = ", ".join(f"{row['option']}({row['votes']})" for row in results)
            announce_from_dashboard(f"Poll '{active_poll}' ended. Results: {top}")
    return redirect(url_for("dashboard"))


@app.route("/raffle", methods=["POST"])
def manage_raffle():
    action = request.form.get("action")
    if action == "start":
        raw = request.form.get("start_raffle", "")
        if "|" in raw:
            name, cost_text = [part.strip() for part in raw.split("|", 1)]
            cost = parse_non_negative_int(cost_text, 0)
            if cost > 0:
                success, error = create_raffle(name, cost)
            else:
                success, error = False, "Entry cost must be a positive number."
        elif raw.strip():
            success, error = create_raffle(raw.strip(), get_dashboard_settings()["default_raffle_cost"])
            name = raw.strip()
            cost = get_dashboard_settings()["default_raffle_cost"]
        else:
            success, error = False, None
            name = ""
            cost = 0
        if success:
            announce_from_dashboard(f"Raffle '{name}' started with entry cost {cost} points. Type !raffle enter.")
        elif error:
            print(f"Dashboard raffle start failed: {error}")
    elif action == "end":
        active_raffle = get_active_raffle_name()
        if active_raffle:
            winner = end_raffle(active_raffle)
            if winner:
                announce_from_dashboard(f"Raffle '{active_raffle}' ended! The winner is {winner}.")
            else:
                announce_from_dashboard(f"Raffle '{active_raffle}' ended with no entries.")
    return redirect(url_for("dashboard"))


@app.route("/giveaway", methods=["POST"])
def manage_giveaway():
    action = request.form.get("action")
    if action == "start":
        name = request.form.get("start_giveaway", "").strip()
        success, result = start_giveaway(name)
        if success:
            announce_from_dashboard(f"Giveaway '{result}' started! Type !giveaway enter to join.")
        elif result:
            print(f"Dashboard giveaway start failed: {result}")
    elif action == "end":
        winner, giveaway_name = finish_giveaway()
        if winner and giveaway_name:
            announce_from_dashboard(f"Giveaway '{giveaway_name}' ended! The winner is {winner}.")
        elif giveaway_name and giveaway_name.startswith("No entries"):
            announce_from_dashboard(giveaway_name)
    return redirect(url_for("dashboard"))


@app.route("/quote", methods=["POST"])
def manage_quote():
    raw = request.form.get("new_quote", "").strip()
    if raw:
        if "|" in raw:
            text, author = [part.strip() for part in raw.split("|", 1)]
        else:
            text, author = raw, "Unknown"
        if text:
            add_quote(text, author or "Unknown", "dashboard")
    return redirect(url_for("dashboard"))


def run_dashboard():
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)


class CowBot(commands.Bot):
    def __init__(self):
        super().__init__(
            client_id=TWITCH_CLIENT_ID,
            client_secret=TWITCH_CLIENT_SECRET,
            bot_id=TWITCH_BOT_ID,
            prefix=COMMAND_PREFIX,
            scopes=Scopes(
                user_read_chat=True,
                user_write_chat=True,
                user_bot=True,
            ),
        )
        self.active_giveaway: str | None = None
        self.last_giveaway_winner: str | None = None
        self.start_time = utc_now()
        self.channel_user = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._chat_subscribed = False

    async def setup_hook(self) -> None:
        self.loop = asyncio.get_running_loop()
        init_db()

        if BOT_TOKEN:
            try:
                await self.add_token(BOT_TOKEN, BOT_REFRESH_TOKEN)
            except Exception as exc:
                print(f"Could not add TWITCH_TOKEN from .env: {exc}")
                print("If this token has no refresh token, authorize the bot at http://localhost:4343/oauth")

        users = await self.fetch_users(logins=[CHANNEL.lower()])
        if not users:
            raise RuntimeError(f"Could not find Twitch channel '{CHANNEL}'. Check TWITCH_CHANNEL in your .env file.")
        self.channel_user = users[0]
        await self._subscribe_to_chat()

    async def _subscribe_to_chat(self) -> None:
        if self._chat_subscribed or self.channel_user is None:
            return
        payload = eventsub.ChatMessageSubscription(
            broadcaster_user_id=self.channel_user.id,
            user_id=self.bot_id,
        )
        try:
            await self.subscribe_websocket(payload=payload)
            self._chat_subscribed = True
            print(f"Subscribed to chat for channel | {CHANNEL}")
        except Exception as exc:
            print(f"Chat subscription failed: {exc}")
            print("Authorize the bot account at http://localhost:4343/oauth?scopes=user:read:chat+user:write:chat+user:bot")

    async def event_oauth_authorized(self, payload) -> None:
        await self.add_token(payload["access_token"], payload["refresh_token"])
        await self._subscribe_to_chat()

    async def send_channel_message(self, content: str) -> None:
        if self.channel_user is None:
            return
        try:
            await self.channel_user.send_message(content, sender=self.bot_id, token_for=self.bot_id)
        except Exception as exc:
            print(f"Failed to send channel message: {exc}")

    async def event_ready(self):
        bot_name = getattr(self.user, "name", BOT_NICK) if self.user else BOT_NICK
        print(f"Logged in as | {bot_name}")
        print(f"Connected to channel | {CHANNEL}")

    async def event_command_error(self, payload: commands.CommandErrorPayload) -> None:
        error = payload.exception
        ctx = payload.context
        if isinstance(error, commands.MissingRequiredArgument):
            command_name = getattr(ctx.command, "name", "command")
            await ctx.send(f"Missing argument for !{command_name}.")
            return
        if isinstance(error, commands.BadArgument):
            await ctx.send("Invalid argument.")
            return
        await super().event_command_error(payload)

    @commands.command(name="uptime")
    async def uptime(self, ctx: commands.Context):
        await ctx.send(f"Bot uptime: {format_uptime(utc_now() - self.start_time)}.")

    @commands.command(name="points")
    async def points(self, ctx: commands.Context, *, target: str | None = None):
        target = normalize_user(target or get_author_name(ctx))
        points = get_points(target)
        await ctx.send(f"{target} has {points} points.")

    @commands.command(name="daily")
    async def daily(self, ctx: commands.Context):
        author_name = get_author_name(ctx)
        claimed, earned, new_total = try_claim_daily(author_name)
        if claimed:
            await ctx.send(f"{author_name}, you claimed your daily reward and earned {earned} points! Total: {new_total}.")
        else:
            await ctx.send(f"{author_name}, you already claimed your daily reward. Come back tomorrow.")

    @commands.command(name="gamble")
    async def gamble(self, ctx: commands.Context, amount: str):
        author_name = get_author_name(ctx)
        user = normalize_user(author_name)
        current = get_points(user)
        if amount.lower() == "all":
            amount_value = current
        else:
            if not amount.isdigit():
                await ctx.send("Usage: !gamble <amount|all>")
                return
            amount_value = int(amount)

        if amount_value <= 0 or amount_value > current:
            await ctx.send(f"{author_name}, invalid amount. You have {current} points.")
            return

        win = random.choice([True, False])
        if win:
            spent, remaining = try_spend_points(user, amount_value)
            if not spent:
                await ctx.send(f"{author_name}, invalid amount. You have {remaining} points.")
                return
            new_total = change_points(user, amount_value * 2)
            await ctx.send(f"{author_name} won {amount_value} points! Total: {new_total}.")
        else:
            spent, new_total = try_spend_points(user, amount_value)
            if not spent:
                await ctx.send(f"{author_name}, invalid amount. You have {new_total} points.")
                return
            await ctx.send(f"{author_name} lost {amount_value} points. Total: {new_total}.")

    @commands.command(name="roulette")
    async def roulette(self, ctx: commands.Context, amount: str):
        author_name = get_author_name(ctx)
        user = normalize_user(author_name)
        if not amount.isdigit():
            await ctx.send("Usage: !roulette <amount>")
            return
        wager = int(amount)
        current = get_points(user)
        if wager <= 0 or wager > current:
            await ctx.send(f"{author_name}, invalid wager. You have {current} points.")
            return

        spent, remaining = try_spend_points(user, wager)
        if not spent:
            await ctx.send(f"{author_name}, invalid wager. You have {remaining} points.")
            return

        number = random.randint(0, 36)
        choice = random.randint(0, 36)
        if number == choice:
            payout = wager * 36
            new_total = change_points(user, payout)
            await ctx.send(f"{author_name} hit {number}! You win {payout} points! Total: {new_total}.")
        else:
            await ctx.send(f"{author_name} spun {number} and lost {wager} points. Total: {remaining}.")

    @commands.command(name="giveaway")
    async def giveaway(self, ctx: commands.Context, action: str, *, name: str | None = None):
        action = action.lower()
        if action == "start":
            if not is_mod_or_broadcaster(ctx):
                await ctx.send("Only mods and the broadcaster can start giveaways.")
                return
            if not name:
                await ctx.send("Usage: !giveaway start <name>")
                return
            success, result = start_giveaway(name)
            if not success:
                await ctx.send(result or "Could not start giveaway.")
                return
            await ctx.send(f"Giveaway '{name}' started! Type !giveaway enter to join.")
        elif action == "enter":
            success, result = enter_giveaway(get_author_name(ctx))
            if not success:
                await ctx.send(result or "Could not enter giveaway.")
                return
            await ctx.send(f"{result} entered the giveaway '{self.active_giveaway}'.")
        elif action == "end":
            if not is_mod_or_broadcaster(ctx):
                await ctx.send("Only mods and the broadcaster can end giveaways.")
                return
            winner, giveaway_name = finish_giveaway()
            if winner and giveaway_name:
                await ctx.send(f"Giveaway '{giveaway_name}' ended! The winner is {winner}.")
            else:
                await ctx.send(giveaway_name or "No giveaway is currently active.")
        else:
            await ctx.send("Giveaway commands: !giveaway start <name>, !giveaway enter, !giveaway end")

    @commands.command(name="quote")
    async def quote(self, ctx: commands.Context, *, text: str | None = None):
        if not text:
            quote = get_random_quote()
            if not quote:
                await ctx.send("No quotes have been added yet.")
                return
            author_text = f" — {quote['author']}" if quote["author"] else ""
            await ctx.send(f"Quote #{quote['id']}: {quote['text']}{author_text}")
            return

        if not is_mod_or_broadcaster(ctx):
            await ctx.send("Only mods and the broadcaster can add quotes.")
            return

        quote_text = text.strip()
        if quote_text.lower().startswith("add "):
            quote_text = quote_text[4:].strip()

        if "|" not in quote_text:
            await ctx.send("Usage: !quote add <quote text> | <author>")
            return

        raw_quote, author = map(str.strip, quote_text.split("|", 1))
        if not raw_quote:
            await ctx.send("Quote text cannot be empty.")
            return

        quote_id = add_quote(raw_quote, author or "Unknown", get_author_name(ctx))
        await ctx.send(f"Quote #{quote_id} added.")

    @commands.command(name="leaderboard")
    async def leaderboard(self, ctx: commands.Context):
        rows = get_leaderboard(5)
        if not rows:
            await ctx.send("No leaderboard entries yet.")
            return
        leaderboard = ", ".join(f"{row['user']}({row['points']})" for row in rows)
        await ctx.send(f"Top points: {leaderboard}")

    @commands.command(name="poll")
    async def poll(self, ctx: commands.Context, action: str, *, args: str | None = None):
        action = action.lower()
        if action == "start":
            if not is_mod_or_broadcaster(ctx):
                await ctx.send("Only mods and the broadcaster can start polls.")
                return
            if not args or "|" not in args:
                await ctx.send("Usage: !poll start <poll name> | <question> | <option1>, <option2>, ...")
                return
            parts = [part.strip() for part in args.split("|")]
            if len(parts) < 3:
                await ctx.send("Usage: !poll start <poll name> | <question> | <option1>, <option2>, ...")
                return
            name = parts[0]
            question = parts[1]
            options = [opt.strip() for opt in parts[2].split(",") if opt.strip()]
            success, error = create_poll(name, question, options)
            if not success:
                await ctx.send(error or "Could not start poll.")
                return
            await ctx.send(f"Poll '{name}' started: {question} Options: {', '.join(unique_options(options))}")
        elif action == "vote":
            active_poll = get_active_poll_name()
            if not active_poll:
                await ctx.send("There is no active poll.")
                return
            if not args:
                await ctx.send("Usage: !poll vote <option>")
                return
            author_name = get_author_name(ctx)
            success, result = vote_poll(active_poll, author_name, args)
            if not success:
                await ctx.send(result or "Could not register your vote.")
                return
            await ctx.send(f"{author_name} voted for {result} in poll '{active_poll}'.")
        elif action == "status":
            active_poll = get_active_poll_name()
            if not active_poll:
                await ctx.send("There is no active poll.")
                return
            question = get_poll_question(active_poll)
            options = get_poll_options(active_poll)
            results = ", ".join(f"{row['option']}({row['votes']})" for row in options)
            await ctx.send(f"Poll '{active_poll}': {question} Results: {results}")
        elif action == "end":
            if not is_mod_or_broadcaster(ctx):
                await ctx.send("Only mods and the broadcaster can end polls.")
                return
            active_poll = get_active_poll_name()
            if not active_poll:
                await ctx.send("There is no active poll.")
                return
            results = end_poll(active_poll)
            if not results:
                await ctx.send(f"Poll '{active_poll}' ended with no votes.")
                return
            top = ", ".join(f"{row['option']}({row['votes']})" for row in results)
            await ctx.send(f"Poll '{active_poll}' ended. Results: {top}")
        else:
            await ctx.send("Poll commands: !poll start <name> | <question> | <options>, !poll vote <option>, !poll end")

    @commands.command(name="raffle")
    async def raffle(self, ctx: commands.Context, action: str, *, args: str | None = None):
        action = action.lower()
        if action == "start":
            if not is_mod_or_broadcaster(ctx):
                await ctx.send("Only mods and the broadcaster can start raffles.")
                return
            if not args:
                await ctx.send("Usage: !raffle start <name> | <cost>")
                return
            if "|" in args:
                name, cost_text = [part.strip() for part in args.split("|", 1)]
                cost = parse_non_negative_int(cost_text, 0)
                if cost <= 0:
                    await ctx.send("Entry cost must be a positive number.")
                    return
            else:
                name = args.strip()
                if not name:
                    await ctx.send("Usage: !raffle start <name> | <cost>")
                    return
                cost = get_dashboard_settings()["default_raffle_cost"]
            success, error = create_raffle(name, cost)
            if not success:
                await ctx.send(error or "Could not start raffle.")
                return
            await ctx.send(f"Raffle '{name}' started with entry cost {cost} points. Type !raffle enter.")
        elif action == "enter":
            active_raffle = get_active_raffle_name()
            if not active_raffle:
                await ctx.send("There is no active raffle.")
                return
            author_name = get_author_name(ctx)
            success, error = enter_raffle(active_raffle, author_name)
            if not success:
                await ctx.send(error or "Failed to enter raffle.")
                return
            cost = get_raffle_cost(active_raffle)
            await ctx.send(f"{author_name} entered raffle '{active_raffle}' for {cost} points.")
        elif action == "end":
            if not is_mod_or_broadcaster(ctx):
                await ctx.send("Only mods and the broadcaster can end raffles.")
                return
            active_raffle = get_active_raffle_name()
            if not active_raffle:
                await ctx.send("There is no active raffle.")
                return
            winner = end_raffle(active_raffle)
            if not winner:
                await ctx.send(f"Raffle '{active_raffle}' ended with no entries.")
                return
            await ctx.send(f"Raffle '{active_raffle}' ended! The winner is {winner}.")
        else:
            await ctx.send("Raffle commands: !raffle start <name> | <cost>, !raffle enter, !raffle end")

    @commands.command(name="transfer")
    async def transfer(self, ctx: commands.Context, target: str, amount: str):
        author_name = get_author_name(ctx)
        from_user = normalize_user(author_name)
        to_user = normalize_user(target)
        if from_user == to_user:
            await ctx.send("You cannot transfer points to yourself.")
            return
        if not amount.isdigit() or amount == "0":
            await ctx.send("Usage: !transfer <user> <amount>")
            return
        amount_value = int(amount)
        spent, current = try_spend_points(from_user, amount_value)
        if not spent:
            await ctx.send(f"{author_name}, invalid amount. You have {current} points.")
            return
        change_points(to_user, amount_value)
        await ctx.send(f"{author_name} transferred {amount_value} points to {to_user}.")


if __name__ == "__main__":
    init_db()
    bot = CowBot()
    dashboard_thread = Thread(target=run_dashboard, daemon=True)
    dashboard_thread.start()
    bot.run()
