import os
import random
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from threading import Lock

DB_PATH = os.getenv("DB_PATH") or os.path.join(os.path.dirname(__file__), "bot.db")
_db_lock = Lock()


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


def get_db_connection():
    db_dir = os.path.dirname(os.path.abspath(DB_PATH))
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduled_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message TEXT NOT NULL,
                interval_minutes INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                last_sent_at TEXT
            )
            """
        )
        if not conn.execute("SELECT 1 FROM config WHERE key = 'command_prefixes'").fetchone():
            env_prefixes = parse_prefixes(os.getenv("PREFIX") or "?") or ("?",)
            upsert_config(conn, "command_prefixes", ",".join(env_prefixes))


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
        return cursor.rowcount > 0, row["points"]


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


def get_config_value(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else ""


def upsert_config(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def start_giveaway(name: str) -> tuple[bool, str | None]:
    giveaway_name = normalize_user(name)
    if not giveaway_name or giveaway_name == "unknown":
        return False, "Giveaway name cannot be empty."
    with db_session() as conn:
        if get_config_value(conn, "active_giveaway"):
            return False, "A giveaway is already running."
        conn.execute("DELETE FROM giveaway_entries WHERE giveaway_name = ?", (giveaway_name,))
        upsert_config(conn, "active_giveaway", giveaway_name)
    return True, giveaway_name


def enter_giveaway(user_name: str) -> tuple[bool, str | None]:
    author_name = normalize_user(user_name)
    with db_session() as conn:
        active = get_config_value(conn, "active_giveaway")
        if not active:
            return False, "No giveaway is currently active."
        cursor = conn.execute(
            "INSERT OR IGNORE INTO giveaway_entries (giveaway_name, user) VALUES (?, ?)",
            (active, author_name),
        )
        if cursor.rowcount == 0:
            return False, f"{author_name} is already entered in the giveaway."
    return True, author_name


def finish_giveaway() -> tuple[str | None, str | None]:
    with db_session() as conn:
        giveaway_name = get_config_value(conn, "active_giveaway")
        if not giveaway_name:
            return None, "No giveaway is currently active."
        rows = conn.execute(
            "SELECT user FROM giveaway_entries WHERE giveaway_name = ?",
            (giveaway_name,),
        ).fetchall()
        upsert_config(conn, "active_giveaway", "")
        if not rows:
            return None, f"No entries for giveaway '{giveaway_name}'."
        winner = random.choice(rows)["user"]
        upsert_config(conn, "last_giveaway_winner", winner)
        return winner, giveaway_name


def get_active_giveaway() -> str | None:
    value = get_setting("active_giveaway", "")
    return value or None


def get_last_giveaway_winner() -> str | None:
    value = get_setting("last_giveaway_winner", "")
    return value or None


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
        upsert_config(conn, key, value)


def parse_prefixes(raw: str | None) -> tuple[str, ...]:
    prefixes: list[str] = []
    for part in str(raw or "").replace(" ", ",").split(","):
        prefix = part.strip()
        if not prefix or prefix == "/" or len(prefix) > 3:
            continue
        if prefix not in prefixes:
            prefixes.append(prefix)
        if len(prefixes) >= 5:
            break
    return tuple(prefixes)


def get_command_prefixes() -> tuple[str, ...]:
    stored = parse_prefixes(get_setting("command_prefixes", ""))
    env_prefixes = parse_prefixes(os.getenv("PREFIX") or "?")
    base = stored or env_prefixes or ("?",)
    return tuple(dict.fromkeys(tuple(base) + ("?", "!")))


def primary_prefix() -> str:
    return get_command_prefixes()[0]


def set_command_prefixes(raw: str) -> tuple[bool, str | None]:
    if "/" in [part.strip() for part in str(raw or "").replace(" ", ",").split(",") if part.strip()]:
        return False, "Twitch intercepts / as its own commands. Use ?, !, or another character."
    prefixes = parse_prefixes(raw)
    if not prefixes:
        return False, "Add at least one prefix, like ? or !"
    set_setting("command_prefixes", ",".join(prefixes))
    return True, None


def list_scheduled_messages() -> list[dict]:
    with db_session() as conn:
        rows = conn.execute(
            """
            SELECT id, message, interval_minutes, enabled, created_at, last_sent_at
            FROM scheduled_messages
            ORDER BY id DESC
            """
        ).fetchall()
    return [
        {
            "id": row["id"],
            "message": row["message"],
            "interval_minutes": row["interval_minutes"],
            "enabled": bool(row["enabled"]),
            "created_at": row["created_at"],
            "last_sent_at": row["last_sent_at"],
        }
        for row in rows
    ]


def add_scheduled_message(message: str, interval_minutes: int) -> tuple[bool, str | None]:
    text = (message or "").strip()
    if not text:
        return False, "Message cannot be empty."
    if len(text) > 500:
        return False, "Twitch messages cannot exceed 500 characters."
    minutes = parse_non_negative_int(str(interval_minutes), 0)
    if minutes < 1:
        return False, "Interval must be at least 1 minute."
    if minutes > 1440:
        return False, "Interval cannot be more than 24 hours."
    with db_session() as conn:
        conn.execute(
            """
            INSERT INTO scheduled_messages (message, interval_minutes, enabled, created_at)
            VALUES (?, ?, 1, ?)
            """,
            (text, minutes, utc_now().isoformat()),
        )
    return True, None


def delete_scheduled_message(message_id: int) -> bool:
    with db_session() as conn:
        cursor = conn.execute("DELETE FROM scheduled_messages WHERE id = ?", (message_id,))
        return cursor.rowcount > 0


def set_scheduled_enabled(message_id: int, enabled: bool) -> bool:
    with db_session() as conn:
        cursor = conn.execute(
            "UPDATE scheduled_messages SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, message_id),
        )
        return cursor.rowcount > 0


def get_scheduled_message(message_id: int) -> dict | None:
    with db_session() as conn:
        row = conn.execute(
            """
            SELECT id, message, interval_minutes, enabled, created_at, last_sent_at
            FROM scheduled_messages
            WHERE id = ?
            """,
            (message_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "message": row["message"],
        "interval_minutes": row["interval_minutes"],
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
        "last_sent_at": row["last_sent_at"],
    }


def due_scheduled_messages() -> list[dict]:
    now = utc_now()
    due: list[dict] = []
    for row in list_scheduled_messages():
        if not row["enabled"]:
            continue
        anchor = parse_iso(row["last_sent_at"] or row["created_at"])
        if now - anchor >= timedelta(minutes=row["interval_minutes"]):
            due.append(row)
    return due


def mark_scheduled_sent(message_id: int) -> None:
    with db_session() as conn:
        conn.execute(
            "UPDATE scheduled_messages SET last_sent_at = ? WHERE id = ?",
            (utc_now().isoformat(), message_id),
        )


def get_dashboard_settings() -> dict:
    daily_min = parse_non_negative_int(get_setting("daily_min", "25"), 25)
    daily_max = parse_non_negative_int(get_setting("daily_max", "100"), 100)
    if daily_min > daily_max:
        daily_min, daily_max = daily_max, daily_min
    prefixes = get_command_prefixes()
    return {
        "daily_min": daily_min,
        "daily_max": daily_max,
        "starting_points": parse_non_negative_int(get_setting("starting_points", "100"), 100),
        "default_raffle_cost": max(parse_non_negative_int(get_setting("default_raffle_cost", "50"), 50), 1),
        "prefixes": ",".join(prefixes),
        "primary_prefix": prefixes[0],
    }


def get_poll_question(name: str) -> str | None:
    name = normalize_user(name)
    with db_session() as conn:
        row = conn.execute("SELECT question FROM polls WHERE name = ?", (name,)).fetchone()
        return row["question"] if row else None


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


def dashboard_snapshot(uptime: str, bot_name: str, channel: str, connected: bool) -> dict:
    poll_name = get_active_poll_name()
    raffle_name = get_active_raffle_name()
    poll_results = [
        {"option": row["option"], "votes": row["votes"]}
        for row in (get_poll_options(poll_name) if poll_name else [])
    ]
    return {
        "ok": True,
        "connected": connected,
        "bot_name": bot_name,
        "channel": channel,
        "uptime": uptime,
        "active_poll": poll_name,
        "poll_question": get_poll_question(poll_name) if poll_name else None,
        "poll_results": poll_results,
        "active_raffle": raffle_name,
        "raffle_cost": get_raffle_cost(raffle_name) if raffle_name else None,
        "active_giveaway": get_active_giveaway(),
        "last_giveaway_winner": get_last_giveaway_winner(),
        "leaderboard": [
            {"user": row["user"], "points": row["points"]}
            for row in get_leaderboard(10)
        ],
        "settings": get_dashboard_settings(),
        "scheduled_messages": list_scheduled_messages(),
    }
