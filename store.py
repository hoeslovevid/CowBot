import json
import os
import random
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from threading import Lock


def resolve_db_path() -> str:
    volume = (os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "").strip().rstrip("/\\")
    configured = (os.getenv("DB_PATH") or "").strip()
    if volume:
        volume_abs = os.path.abspath(volume)
        if configured:
            configured_abs = os.path.abspath(configured)
            if configured_abs == volume_abs or configured_abs.startswith(volume_abs + os.sep):
                return configured
        return os.path.join(volume, "bot.db")
    if configured:
        return configured
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.db")


DB_PATH = resolve_db_path()
_db_lock = Lock()
_db_path_logged = False


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


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if column not in _column_names(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


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
    global _db_path_logged
    if not _db_path_logged:
        _db_path_logged = True
        volume = (os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "").strip()
        print(f"Database | {os.path.abspath(DB_PATH)}")
        if os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID"):
            if volume:
                print(f"Database volume | {volume}")
            else:
                print(
                    "No Railway volume is mounted. Giveaways, points, and quotes will reset on redeploy. "
                    "Add a Volume to this service with mount path /data, and set DB_PATH=/data/bot.db."
                )
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS custom_commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                response TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """
        )
        _ensure_column(conn, "custom_commands", "aliases", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "custom_commands", "cooldown_seconds", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "custom_commands", "use_count", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "custom_commands", "last_used_at", "TEXT")
        if not conn.execute("SELECT 1 FROM config WHERE key = 'command_prefixes'").fetchone():
            env_prefixes = parse_prefixes(os.getenv("PREFIX") or "?,!") or ("?", "!")
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
        if get_config_value(conn, "pending_giveaway_winner"):
            return False, "A winner is being drawn right now."
        if get_config_value(conn, "active_giveaway"):
            return False, "A giveaway is already running."
        conn.execute("DELETE FROM giveaway_entries WHERE giveaway_name = ?", (giveaway_name,))
        upsert_config(conn, "active_giveaway", giveaway_name)
        upsert_config(conn, "giveaway_excluded_winners", "")
        upsert_config(conn, "pending_giveaway_reroll", "")
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


def get_giveaway_entries(giveaway_name: str | None = None) -> list[str]:
    name = (
        giveaway_name
        or get_active_giveaway()
        or get_setting("pending_giveaway_name", "")
        or get_last_giveaway_name()
    )
    if not name:
        return []
    with db_session() as conn:
        rows = conn.execute(
            "SELECT user FROM giveaway_entries WHERE giveaway_name = ? ORDER BY user",
            (name,),
        ).fetchall()
    return [row["user"] for row in rows]


def get_pending_giveaway() -> tuple[str | None, str | None]:
    winner = get_setting("pending_giveaway_winner", "")
    name = get_setting("pending_giveaway_name", "")
    if winner and name:
        return winner, name
    return None, None


def get_last_giveaway_name() -> str | None:
    value = get_setting("last_giveaway_name", "")
    return value or None


def _excluded_winners(conn: sqlite3.Connection, extra: str | None = None) -> set[str]:
    names = {normalize_user(part) for part in get_config_value(conn, "giveaway_excluded_winners").split(",") if part.strip()}
    if extra:
        names.add(normalize_user(extra))
    names.discard("")
    names.discard("unknown")
    return names


def _record_giveaway_winner(conn: sqlite3.Connection, name: str, winner: str) -> None:
    upsert_config(conn, "last_giveaway_winner", winner)
    upsert_config(conn, "last_giveaway_name", name)
    excluded = _excluded_winners(conn, winner)
    upsert_config(conn, "giveaway_excluded_winners", ",".join(sorted(excluded)))


def draw_giveaway() -> tuple[str | None, str | None, list[str]]:
    pending_winner, pending_name = get_pending_giveaway()
    if pending_winner and pending_name:
        return pending_winner, pending_name, get_giveaway_entries(pending_name)

    with db_session() as conn:
        giveaway_name = get_config_value(conn, "active_giveaway")
        if not giveaway_name:
            return None, "No giveaway is currently active.", []
        rows = conn.execute(
            "SELECT user FROM giveaway_entries WHERE giveaway_name = ?",
            (giveaway_name,),
        ).fetchall()
        if not rows:
            return None, f"No entries for giveaway '{giveaway_name}'.", []
        winner = random.choice(rows)["user"]
        entries = [row["user"] for row in rows]
        upsert_config(conn, "active_giveaway", "")
        upsert_config(conn, "pending_giveaway_winner", winner)
        upsert_config(conn, "pending_giveaway_name", giveaway_name)
        upsert_config(conn, "pending_giveaway_reroll", "")
        upsert_config(conn, "last_giveaway_name", giveaway_name)
    publish_overlay_spin(winner, giveaway_name, entries, reroll=False)
    return winner, giveaway_name, entries


def reroll_giveaway() -> tuple[str | None, str | None, list[str]]:
    with db_session() as conn:
        pending_winner = get_config_value(conn, "pending_giveaway_winner")
        if get_config_value(conn, "active_giveaway") and not pending_winner:
            return None, "Pick a winner first, then you can reroll.", []
        giveaway_name = (
            get_config_value(conn, "pending_giveaway_name")
            or get_config_value(conn, "last_giveaway_name")
        )
        if not giveaway_name:
            return None, "There is no giveaway to reroll.", []
        rows = conn.execute(
            "SELECT user FROM giveaway_entries WHERE giveaway_name = ?",
            (giveaway_name,),
        ).fetchall()
        entries = [row["user"] for row in rows]
        excluded = _excluded_winners(conn, pending_winner or get_config_value(conn, "last_giveaway_winner"))
        remaining = [user for user in entries if normalize_user(user) not in excluded]
        if not remaining:
            return None, "No other entries left to reroll.", entries
        winner = random.choice(remaining)
        upsert_config(conn, "active_giveaway", "")
        upsert_config(conn, "pending_giveaway_winner", winner)
        upsert_config(conn, "pending_giveaway_name", giveaway_name)
        upsert_config(conn, "pending_giveaway_reroll", "1")
        upsert_config(conn, "last_giveaway_name", giveaway_name)
    publish_overlay_spin(winner, giveaway_name, remaining, reroll=True)
    return winner, giveaway_name, remaining


def complete_giveaway_draw() -> tuple[str | None, str | None, bool]:
    with db_session() as conn:
        winner = get_config_value(conn, "pending_giveaway_winner")
        name = get_config_value(conn, "pending_giveaway_name")
        is_reroll = get_config_value(conn, "pending_giveaway_reroll") == "1"
        if not winner or not name:
            return None, "No giveaway draw is waiting to finish.", False
        _record_giveaway_winner(conn, name, winner)
        upsert_config(conn, "pending_giveaway_winner", "")
        upsert_config(conn, "pending_giveaway_name", "")
        upsert_config(conn, "pending_giveaway_reroll", "")
    return winner, name, is_reroll


def finish_giveaway() -> tuple[str | None, str | None]:
    pending_winner, pending_name = get_pending_giveaway()
    if pending_winner and pending_name:
        winner, name, _is_reroll = complete_giveaway_draw()
        return winner, name
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
        entries = [row["user"] for row in rows]
        _record_giveaway_winner(conn, giveaway_name, winner)
    publish_overlay_spin(winner, giveaway_name, entries, reroll=False)
    return winner, giveaway_name


def cancel_giveaway() -> tuple[bool, str | None]:
    with db_session() as conn:
        active = get_config_value(conn, "active_giveaway")
        pending_name = get_config_value(conn, "pending_giveaway_name")
        name = active or pending_name
        if not name:
            return False, "No giveaway is currently active."
        conn.execute("DELETE FROM giveaway_entries WHERE giveaway_name = ?", (name,))
        upsert_config(conn, "active_giveaway", "")
        upsert_config(conn, "pending_giveaway_winner", "")
        upsert_config(conn, "pending_giveaway_name", "")
        upsert_config(conn, "pending_giveaway_reroll", "")
        upsert_config(conn, "overlay_giveaway_spin", "")
    return True, name


def publish_overlay_spin(winner: str, name: str, entries: list[str], *, reroll: bool = False) -> dict:
    payload = {
        "id": uuid.uuid4().hex,
        "winner": winner,
        "name": name,
        "entries": list(entries),
        "reroll": reroll,
        "created_at": utc_now().isoformat(),
    }
    set_setting("overlay_giveaway_spin", json.dumps(payload))
    return payload


def get_overlay_spin() -> dict | None:
    raw = get_setting("overlay_giveaway_spin", "")
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or not payload.get("id") or not payload.get("winner"):
        return None
    return payload


def get_active_giveaway() -> str | None:
    value = get_setting("active_giveaway", "")
    return value or None


def current_giveaway_state() -> dict:
    active = get_active_giveaway()
    pending_winner, pending_name = get_pending_giveaway()
    name = active or pending_name
    if not name:
        return {
            "name": None,
            "open": False,
            "drawing": False,
            "entries": [],
            "count": 0,
        }
    entries = get_giveaway_entries(name)
    return {
        "name": name,
        "open": bool(active),
        "drawing": bool(pending_winner),
        "entries": entries,
        "count": len(entries),
    }


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
    if stored:
        return stored
    return parse_prefixes(os.getenv("PREFIX") or "?,!") or ("?", "!")


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


FEATURE_MODULES = {
    "economy": {
        "label": "Economy",
        "blurb": "Points, daily, gamble, roulette, transfer, and the leaderboard",
        "off_message": "Economy commands are currently disabled.",
    },
    "giveaway": {
        "label": "Giveaways",
        "blurb": "Free-entry giveaways and the winner wheel",
        "off_message": "Giveaways are currently disabled.",
    },
    "poll": {
        "label": "Polls",
        "blurb": "Chat polls and voting",
        "off_message": "Polls are currently disabled.",
    },
    "raffle": {
        "label": "Raffles",
        "blurb": "Point-entry raffles",
        "off_message": "Raffles are currently disabled.",
    },
    "quotes": {
        "label": "Quotes",
        "blurb": "Quote lookup and adding quotes",
        "off_message": "Quotes are currently disabled.",
    },
    "custom_commands": {
        "label": "Custom commands",
        "blurb": "Replies you create, with aliases, cooldowns, and placeholders",
        "off_message": "Custom commands are currently disabled.",
    },
    "scheduled_messages": {
        "label": "Scheduled messages",
        "blurb": "Repeating chat lines while the stream is live",
        "off_message": "Scheduled messages are currently disabled.",
    },
}


def is_feature_enabled(name: str) -> bool:
    if name not in FEATURE_MODULES:
        return True
    return get_setting(f"feature_{name}", "1") != "0"


def feature_off_message(name: str) -> str:
    meta = FEATURE_MODULES.get(name) or {}
    return str(meta.get("off_message") or "That module is currently disabled.")


def get_feature_flags() -> dict:
    return {
        key: {
            "enabled": is_feature_enabled(key),
            "label": meta["label"],
            "blurb": meta["blurb"],
            "off_message": meta["off_message"],
        }
        for key, meta in FEATURE_MODULES.items()
    }


def set_feature_flags(flags: dict) -> None:
    for key in FEATURE_MODULES:
        if key not in flags:
            continue
        raw = str(flags.get(key, "")).strip().lower()
        enabled = raw in {"1", "true", "on", "yes"}
        set_setting(f"feature_{key}", "1" if enabled else "0")


BUILTIN_COMMANDS = {
    "ping": {"blurb": "Check that the bot is responding", "module": None},
    "uptime": {"blurb": "How long the bot has been online", "module": None},
    "lurk": {"blurb": "Announce that you're lurking", "module": None},
    "points": {"blurb": "Check a chatter's points", "module": "economy"},
    "daily": {"blurb": "Claim the daily reward", "module": "economy"},
    "gamble": {"blurb": "Coin-flip wager", "module": "economy"},
    "roulette": {"blurb": "Roulette wager", "module": "economy"},
    "transfer": {"blurb": "Send points to another chatter", "module": "economy"},
    "leaderboard": {"blurb": "Top points in chat", "module": "economy"},
    "giveaway": {"blurb": "Join or run free-entry giveaways", "module": "giveaway"},
    "poll": {"blurb": "Start, vote, and end polls", "module": "poll"},
    "raffle": {"blurb": "Join or run point-entry raffles", "module": "raffle"},
    "quote": {"blurb": "Look up or add quotes", "module": "quotes"},
}

DEFAULT_LURK_MESSAGE = (
    "{user} steps back into the shadows. Pay no mind to those who lurk in the shadows."
)


def is_command_enabled(name: str) -> bool:
    if name not in BUILTIN_COMMANDS:
        return True
    return get_setting(f"command_{name}", "1") != "0"


def is_command_available(name: str) -> bool:
    meta = BUILTIN_COMMANDS.get(name)
    if not meta:
        return True
    module = meta.get("module")
    if module and not is_feature_enabled(module):
        return False
    return is_command_enabled(name)


def command_unavailable_message(name: str) -> str:
    meta = BUILTIN_COMMANDS.get(name) or {}
    module = meta.get("module")
    if module and not is_feature_enabled(module):
        return feature_off_message(module)
    return f"The {primary_prefix()}{name} command is currently disabled."


def _command_entry(name: str, *, enabled: bool) -> dict:
    meta = BUILTIN_COMMANDS[name]
    return {
        "name": name,
        "label": name,
        "blurb": meta["blurb"],
        "module": meta["module"],
        "enabled": enabled,
    }


def get_command_groups(*, live: bool = True) -> list[dict]:
    by_module: dict[str | None, list[str]] = {}
    for name, meta in BUILTIN_COMMANDS.items():
        by_module.setdefault(meta["module"], []).append(name)

    groups = [{
        "key": "core",
        "label": "Core",
        "module": None,
        "module_enabled": True,
        "commands": [
            _command_entry(name, enabled=is_command_enabled(name) if live else True)
            for name in by_module.get(None, [])
        ],
    }]
    for mod_key, mod in FEATURE_MODULES.items():
        names = by_module.get(mod_key)
        if not names:
            continue
        groups.append({
            "key": mod_key,
            "label": mod["label"],
            "module": mod_key,
            "module_enabled": is_feature_enabled(mod_key) if live else True,
            "commands": [
                _command_entry(name, enabled=is_command_enabled(name) if live else True)
                for name in names
            ],
        })
    return groups


def set_command_flags(flags: dict) -> None:
    for name in BUILTIN_COMMANDS:
        if name not in flags:
            continue
        raw = str(flags.get(name, "")).strip().lower()
        enabled = raw in {"1", "true", "on", "yes"}
        set_setting(f"command_{name}", "1" if enabled else "0")


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


RESERVED_COMMANDS = frozenset(BUILTIN_COMMANDS)
CUSTOM_COMMAND_COLUMNS = (
    "id, name, response, enabled, created_at, aliases, cooldown_seconds, use_count, last_used_at"
)
MAX_COMMAND_ALIASES = 8
MAX_COMMAND_COOLDOWN = 3600


def normalize_command_name(raw: str | None) -> str:
    name = (raw or "").strip().lower()
    for prefix in get_command_prefixes():
        if name.startswith(prefix):
            name = name[len(prefix):].lstrip()
            break
    name = name.split()[0] if name else ""
    cleaned = "".join(ch for ch in name if ch.isalnum() or ch in "_-")
    return cleaned[:32]


def parse_command_aliases(raw: str | None) -> list[str]:
    names: list[str] = []
    for part in str(raw or "").replace(";", ",").split(","):
        name = normalize_command_name(part)
        if name and name not in names:
            names.append(name)
        if len(names) >= MAX_COMMAND_ALIASES:
            break
    return names


def format_command_aliases(aliases: list[str]) -> str:
    return ",".join(aliases)


def _custom_command_row(row: sqlite3.Row) -> dict:
    aliases = parse_command_aliases(row["aliases"] if "aliases" in row.keys() else "")
    cooldown = row["cooldown_seconds"] if "cooldown_seconds" in row.keys() else 0
    use_count = row["use_count"] if "use_count" in row.keys() else 0
    last_used = row["last_used_at"] if "last_used_at" in row.keys() else None
    return {
        "id": row["id"],
        "name": row["name"],
        "response": row["response"],
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
        "aliases": aliases,
        "aliases_text": ", ".join(aliases),
        "cooldown_seconds": int(cooldown or 0),
        "use_count": int(use_count or 0),
        "last_used_at": last_used,
    }


def list_custom_commands() -> list[dict]:
    with db_session() as conn:
        rows = conn.execute(
            f"SELECT {CUSTOM_COMMAND_COLUMNS} FROM custom_commands ORDER BY name"
        ).fetchall()
    return [_custom_command_row(row) for row in rows]


def get_custom_command(name: str) -> dict | None:
    command_name = normalize_command_name(name)
    if not command_name:
        return None
    for command in list_custom_commands():
        if not command["enabled"]:
            continue
        if command["name"] == command_name or command_name in command["aliases"]:
            return command
    return None


def _taken_command_names(exclude_id: int | None = None) -> set[str]:
    taken = set(RESERVED_COMMANDS)
    for command in list_custom_commands():
        if exclude_id is not None and command["id"] == exclude_id:
            continue
        taken.add(command["name"])
        taken.update(command["aliases"])
    return taken


def upsert_custom_command(
    name: str,
    response: str,
    aliases: str | None = None,
    cooldown_seconds: int | str | None = 0,
) -> tuple[bool, str | None]:
    command_name = normalize_command_name(name)
    if not command_name:
        return False, "Command name cannot be empty."
    if command_name in RESERVED_COMMANDS:
        return False, f"'{command_name}' is a built-in command and cannot be replaced."
    text = (response or "").strip()
    if not text:
        return False, "Command response cannot be empty."
    if len(text) > 500:
        return False, "Twitch messages cannot exceed 500 characters."
    alias_names = [alias for alias in parse_command_aliases(aliases) if alias != command_name]
    cooldown = parse_non_negative_int(str(cooldown_seconds if cooldown_seconds is not None else 0), 0)
    if cooldown > MAX_COMMAND_COOLDOWN:
        return False, "Cooldown cannot be more than 1 hour."
    existing = None
    for command in list_custom_commands():
        if command["name"] == command_name:
            existing = command
            break
    taken = _taken_command_names(existing["id"] if existing else None)
    if existing is None and command_name in taken:
        return False, f"'{command_name}' is already used as a command or alias."
    for alias in alias_names:
        if alias in RESERVED_COMMANDS:
            return False, f"'{alias}' is a built-in command and cannot be an alias."
        if alias in taken:
            return False, f"'{alias}' is already used as a command or alias."
    with db_session() as conn:
        conn.execute(
            """
            INSERT INTO custom_commands (
                name, response, enabled, created_at, aliases, cooldown_seconds
            )
            VALUES (?, ?, 1, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                response = excluded.response,
                enabled = 1,
                aliases = excluded.aliases,
                cooldown_seconds = excluded.cooldown_seconds
            """,
            (
                command_name,
                text,
                utc_now().isoformat(),
                format_command_aliases(alias_names),
                cooldown,
            ),
        )
    return True, command_name


def delete_custom_command(command_id: int) -> bool:
    with db_session() as conn:
        cursor = conn.execute("DELETE FROM custom_commands WHERE id = ?", (command_id,))
        return cursor.rowcount > 0


def set_custom_command_enabled(command_id: int, enabled: bool) -> bool:
    with db_session() as conn:
        cursor = conn.execute(
            "UPDATE custom_commands SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, command_id),
        )
        return cursor.rowcount > 0


def get_custom_command_by_id(command_id: int) -> dict | None:
    with db_session() as conn:
        row = conn.execute(
            f"SELECT {CUSTOM_COMMAND_COLUMNS} FROM custom_commands WHERE id = ?",
            (command_id,),
        ).fetchone()
    return _custom_command_row(row) if row else None


def use_custom_command(name: str, *, bypass_cooldown: bool = False) -> dict | None:
    command = get_custom_command(name)
    if not command:
        return None
    if command["cooldown_seconds"] > 0 and not bypass_cooldown and command["last_used_at"]:
        elapsed = (utc_now() - parse_iso(command["last_used_at"])).total_seconds()
        if elapsed < command["cooldown_seconds"]:
            return None
    with db_session() as conn:
        conn.execute(
            """
            UPDATE custom_commands
            SET use_count = use_count + 1, last_used_at = ?
            WHERE id = ?
            """,
            (utc_now().isoformat(), command["id"]),
        )
        row = conn.execute(
            f"SELECT {CUSTOM_COMMAND_COLUMNS} FROM custom_commands WHERE id = ?",
            (command["id"],),
        ).fetchone()
    return _custom_command_row(row) if row else None


def render_custom_command(
    response: str,
    *,
    user: str,
    channel: str = "",
    points: int = 0,
    count: int = 0,
    target: str | None = None,
) -> str:
    text = (
        (response or "")
        .replace("{user}", user)
        .replace("{prefix}", primary_prefix())
        .replace("{channel}", channel or "")
        .replace("{points}", str(points))
        .replace("{count}", str(count))
        .replace("{target}", (target or user).lstrip("@") or user)
    ).strip()
    return text[:500]


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
        "prefixes": ", ".join(prefixes),
        "primary_prefix": prefixes[0],
        "lurk_message": get_lurk_message(),
    }


def get_lurk_message() -> str:
    text = get_setting("lurk_message", DEFAULT_LURK_MESSAGE).strip()
    return text or DEFAULT_LURK_MESSAGE


def set_lurk_message(raw: str) -> tuple[bool, str | None]:
    text = " ".join(str(raw or "").split())
    if not text:
        return False, "Lurk message cannot be empty."
    if len(text) > 500:
        return False, "Lurk message must be 500 characters or less."
    set_setting("lurk_message", text)
    return True, None


def render_lurk_message(mention: str) -> str:
    return (
        get_lurk_message()
        .replace("{user}", mention)
        .replace("{prefix}", primary_prefix())
    )[:500]


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


def dashboard_snapshot(uptime: str, bot_name: str, channel: str, connected: bool, stream_live: bool = False) -> dict:
    poll_name = get_active_poll_name()
    raffle_name = get_active_raffle_name()
    poll_results = [
        {"option": row["option"], "votes": row["votes"]}
        for row in (get_poll_options(poll_name) if poll_name else [])
    ]
    giveaway = current_giveaway_state()
    return {
        "ok": True,
        "connected": connected,
        "stream_live": stream_live,
        "bot_name": bot_name,
        "channel": channel,
        "uptime": uptime,
        "active_poll": poll_name,
        "poll_question": get_poll_question(poll_name) if poll_name else None,
        "poll_results": poll_results,
        "active_raffle": raffle_name,
        "raffle_cost": get_raffle_cost(raffle_name) if raffle_name else None,
        "active_giveaway": giveaway["name"],
        "giveaway_open": giveaway["open"],
        "giveaway_drawing": giveaway["drawing"],
        "giveaway_entries": giveaway["entries"],
        "giveaway_entry_count": giveaway["count"],
        "last_giveaway_winner": get_last_giveaway_winner(),
        "leaderboard": [
            {"user": row["user"], "points": row["points"]}
            for row in get_leaderboard(10)
        ],
        "settings": get_dashboard_settings(),
        "features": get_feature_flags(),
        "builtin_command_groups": get_command_groups(),
        "scheduled_messages": list_scheduled_messages(),
        "custom_commands": list_custom_commands(),
    }
