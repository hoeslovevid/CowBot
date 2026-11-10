"""Persistent points shop. Purchases and fulfillment share the points transaction."""
import json
import re
import uuid
from datetime import timedelta

import store

CATEGORIES = {
    "challenge": "Streamer challenge", "title": "Profile title", "shoutout": "On-screen shout-out",
    "gift": "Community gift", "cosmetic": "Temporary cosmetic",
}
COLORS = {"mint", "gold", "violet", "rose"}


def init_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS shop_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
        category TEXT NOT NULL, description TEXT NOT NULL, price INTEGER NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1, stock INTEGER NOT NULL DEFAULT -1,
        cooldown INTEGER NOT NULL DEFAULT 60, duration INTEGER NOT NULL DEFAULT 30,
        value TEXT NOT NULL DEFAULT '', gift_points INTEGER NOT NULL DEFAULT 25)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS shop_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT NOT NULL, item_id INTEGER NOT NULL,
        item TEXT NOT NULL, message TEXT NOT NULL, status TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, result TEXT NOT NULL DEFAULT '')""")
    store._ensure_column(conn, "shop_items", "version", "INTEGER NOT NULL DEFAULT 0")
    conn.execute("CREATE INDEX IF NOT EXISTS shop_orders_user_item ON shop_orders(user, item_id, created_at)")
    conn.execute("""CREATE TABLE IF NOT EXISTS shop_titles (
        user TEXT NOT NULL, title TEXT NOT NULL COLLATE NOCASE, acquired_at TEXT NOT NULL, PRIMARY KEY(user,title))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS shop_profiles (
        user TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', color TEXT NOT NULL DEFAULT 'mint',
        cosmetic_until TEXT NOT NULL DEFAULT '')""")
    if store.get_config_value(conn, "shop_seeded"):
        return
    defaults = [
        ("challenge", "Pick a challenge", "challenge", "Request a challenge for the streamer. Approval required.", 500, ""),
        ("legendairy", "Legendairy title", "title", "Unlock and equip the Legendairy profile title.", 1500, "Legendairy"),
        ("shoutout", "On-screen shout-out", "shoutout", "Your short message on stream after approval.", 500, ""),
        ("herd-gift", "Gift the herd", "gift", "Give 25 points to each other recently present viewer.", 1000, ""),
        ("gold-glow", "Golden celebrations", "cosmetic", "Gold celebration styling for 30 minutes.", 750, "gold"),
    ]
    for item in defaults:
        conn.execute("INSERT OR IGNORE INTO shop_items(slug,name,category,description,price,value) VALUES (?,?,?,?,?,?)", item)
    store.upsert_config(conn, "shop_seeded", "1")


def item_dict(row):
    return dict(row)


def list_items(available_only=False):
    with store.db_session() as conn:
        rows = conn.execute("SELECT * FROM shop_items ORDER BY id").fetchall()
    return [item_dict(row) for row in rows if not available_only or (row["enabled"] and row["stock"] != 0)]


def save_item(data):
    try:
        slug = str(data.get("slug", "")).strip().lower()
        name = str(data.get("name", "")).strip()
        description = str(data.get("description", "")).strip()
        category = data.get("category")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", slug):
            raise ValueError("Use a shop code of 1–32 letters, numbers, or hyphens.")
        if not 1 <= len(name) <= 60 or len(description) > 240 or category not in CATEGORIES:
            raise ValueError("Choose a category, a name (up to 60 characters), and a description (up to 240).")
        def number(key, default, low, high):
            raw = str(data.get(key, default))
            if not re.fullmatch(r"-?[0-9]{1,7}", raw) or not low <= int(raw) <= high:
                raise ValueError(f"{key.replace('_', ' ').capitalize()} must be a whole number from {low} to {high}.")
            return int(raw)
        price = number("price", 500, 1, 1000000)
        stock = number("stock", -1, -1, 1000000)
        cooldown = number("cooldown", 60, 0, 86400)
        duration = number("duration", 30, 1, 1440)
        gift = number("gift_points", 25, 1, 1000000)
        value = str(data.get("value", "")).strip()
        if category == "title" and (not 1 <= len(value) <= 32 or any(ord(c) < 32 for c in value)):
            raise ValueError("Enter a profile title of 1–32 characters.")
        if category == "cosmetic" and value not in COLORS:
            raise ValueError("Choose a cosmetic color: mint, gold, violet, or rose.")
        enabled = int(str(data.get("enabled", "1")).lower() in {"1", "true", "on"})
        item_id = number("id", 0, 0, 9999999)
        version = number("version", 0, 0, 9999999)
        with store.db_session() as conn:
            if conn.execute("SELECT 1 FROM shop_items WHERE slug = ? AND id != ?", (slug, item_id)).fetchone():
                raise ValueError("That shop code is already in use.")
            fields = (slug, name, category, description, price, enabled, stock, cooldown, duration, value, gift)
            if item_id:
                changed = conn.execute("""UPDATE shop_items SET slug=?,name=?,category=?,description=?,price=?,enabled=?,
                    stock=?,cooldown=?,duration=?,value=?,gift_points=?,version=version+1 WHERE id=? AND version=?""", (*fields, item_id, version))
                if not changed.rowcount:
                    raise ValueError("Item changed while you were editing. Refresh the shop and reopen it to avoid overwriting sales.")
            else:
                conn.execute("""INSERT INTO shop_items(slug,name,category,description,price,enabled,stock,cooldown,duration,value,gift_points)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""", fields)
        return True, "Shop item saved."
    except (ValueError, TypeError) as exc:
        return False, str(exc)


def _profile(conn, user, now):
    row = conn.execute("SELECT * FROM shop_profiles WHERE user=?", (user,)).fetchone()
    profile = dict(row) if row else {"user": user, "title": "", "color": "mint", "cosmetic_until": ""}
    if not profile["cosmetic_until"] or store.parse_iso(profile["cosmetic_until"]) <= now:
        profile["color"], profile["cosmetic_until"] = "mint", ""
    profile["titles"] = [r["title"] for r in conn.execute("SELECT title FROM shop_titles WHERE user=? ORDER BY acquired_at", (user,))]
    return profile


def profile(user_name):
    user = store.normalize_user(user_name)
    with store.db_session() as conn:
        return _profile(conn, user, store.utc_now())


def equip_title(user_name, title):
    user = store.normalize_user(user_name)
    with store.db_session() as conn:
        owned = _profile(conn, user, store.utc_now())["titles"]
        chosen = next((t for t in owned if t.casefold() == title.strip().casefold()), None)
        if title.strip().lower() == "none":
            chosen = ""
        if chosen is None:
            return False, "You do not own that title. Check your profile for unlocked titles."
        conn.execute("INSERT OR IGNORE INTO shop_profiles(user) VALUES (?)", (user,))
        conn.execute("UPDATE shop_profiles SET title=? WHERE user=?", (chosen, user))
    return True, f"Title equipped: {chosen or 'none'}."


def mark_present(users):
    starting = store.parse_non_negative_int(store.get_setting("starting_points", "100"), 100)
    now = store.utc_now().isoformat()
    with store.db_session() as conn:
        for name in users:
            user = store.normalize_user(name)
            if user in store.WATCH_POINT_BOTS or user in {"", "unknown"}:
                continue
            store.ensure_user_row(conn, user, starting)
            conn.execute("UPDATE points SET shop_seen_at=? WHERE user=?", (now, user))


def _publish(conn, order, item, summary, now):
    event = {"id": uuid.uuid4().hex, "kind": "shop", "label": item["name"], "user": order["user"],
             "summary": summary, "detail": f"Shop reward · {item['price']:,} points", "cost": item["price"],
             "created_at": now.isoformat(), "until": (now + timedelta(seconds=120)).isoformat(),
             "color": _profile(conn, order["user"], now)["color"],
             "title": _profile(conn, order["user"], now)["title"]}
    events = store._boost_events(store.get_config_value(conn, "boost_celebrations") or "[]", now)
    store.upsert_config(conn, "boost_celebrations", json.dumps((events + [event])[-100:]))


def _fulfill(conn, order, item, now):
    user = order["user"]
    category = item["category"]
    conn.execute("INSERT OR IGNORE INTO shop_profiles(user) VALUES (?)", (user,))
    result = item["name"]
    if category == "title":
        conn.execute("INSERT INTO shop_titles(user,title,acquired_at) VALUES (?,?,?)", (user, item["value"], now.isoformat()))
        conn.execute("UPDATE shop_profiles SET title=? WHERE user=?", (item["value"], user))
        result = f"Title unlocked and equipped: {item['value']}"
    elif category == "cosmetic":
        existing = _profile(conn, user, now)
        start = store.parse_iso(existing["cosmetic_until"]) if existing["color"] == item["value"] and existing["cosmetic_until"] else now
        until = start + timedelta(minutes=item["duration"])
        conn.execute("UPDATE shop_profiles SET color=?,cosmetic_until=? WHERE user=?", (item["value"], until.isoformat(), user))
        result = f"{item['value'].capitalize()} celebrations until {until.isoformat()}"
        _publish(conn, order, item, f"{item['value'].capitalize()} celebrations unlocked for {item['duration']} minutes!", now)
    elif category == "gift":
        recipients = conn.execute("SELECT user FROM points WHERE shop_seen_at >= ? AND user != ?",
                                  ((now - timedelta(minutes=15)).isoformat(), user)).fetchall()
        recipients = [r["user"] for r in recipients if r["user"] not in store.WATCH_POINT_BOTS]
        if not recipients:
            raise ValueError("No other recently present viewers to gift. No points spent.")
        for recipient in recipients:
            conn.execute("UPDATE points SET points=points+? WHERE user=?", (item["gift_points"], recipient))
        result = json.dumps({"points_each": item["gift_points"], "recipients": recipients})
        _publish(conn, order, item, f"Gifted {item['gift_points']:,} points each to {len(recipients)} viewers!", now)
    elif category == "shoutout":
        result = order["message"]
        _publish(conn, order, item, result, now)
    elif category == "challenge":
        result = "Challenge completed by the streamer."
    return result


def buy(user_name, slug, message=""):
    if not store.is_feature_enabled("economy") or not store.is_feature_enabled("shop"):
        return False, "The shop is currently disabled."
    user = store.normalize_user(user_name)
    if not re.fullmatch(r"[a-z0-9_]{1,25}", user) or user == "unknown":
        return False, "Enter a valid Twitch username."
    message = " ".join(str(message).split())
    starting = store.parse_non_negative_int(store.get_setting("starting_points", "100"), 100)
    now = store.utc_now()
    try:
        with store.db_session() as conn:
            row = conn.execute("SELECT * FROM shop_items WHERE slug=? AND enabled=1", (str(slug).lower(),)).fetchone()
            if not row:
                raise ValueError("Item not found. Use the shop command to see available item codes.")
            item = item_dict(row)
            if item["stock"] == 0:
                raise ValueError("That item is sold out.")
            if item["category"] in {"challenge", "shoutout"} and not 1 <= len(message) <= 140:
                raise ValueError("Include your challenge or shout-out message (1–140 characters).")
            if item["category"] == "title" and conn.execute("SELECT 1 FROM shop_titles WHERE user=? AND title=?", (user, item["value"])).fetchone():
                raise ValueError("You already own that title. Use the title command to equip it.")
            last = conn.execute("SELECT created_at FROM shop_orders WHERE user=? AND item_id=? AND status != 'rejected' ORDER BY id DESC LIMIT 1", (user, item["id"])).fetchone()
            wait = store._cooldown_remaining(last["created_at"] if last else None, item["cooldown"], now)
            if wait:
                raise ValueError(f"That shop item is on cooldown. Try again in {wait} seconds.")
            store.ensure_user_row(conn, user, starting)
            balance = conn.execute("SELECT points FROM points WHERE user=?", (user,)).fetchone()["points"]
            if balance < item["price"]:
                raise ValueError(f"{item['name']} costs {item['price']:,} points. You have {balance:,}.")
            conn.execute("UPDATE points SET points=points-? WHERE user=?", (item["price"], user))
            if item["stock"] > 0:
                conn.execute("UPDATE shop_items SET stock=stock-1,version=version+1 WHERE id=?", (item["id"],))
            pending = item["category"] in {"challenge", "shoutout"}
            cursor = conn.execute("INSERT INTO shop_orders(user,item_id,item,message,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                                  (user, item["id"], json.dumps(item), message, "pending" if pending else "completed", now.isoformat(), now.isoformat()))
            order = {"id": cursor.lastrowid, "user": user, "message": message}
            result = "Awaiting streamer approval." if pending else _fulfill(conn, order, item, now)
            conn.execute("UPDATE shop_orders SET result=? WHERE id=?", (result, order["id"]))
        if pending:
            return True, f"{user} bought {item['name']} for {item['price']:,} points. Order #{order['id']} is awaiting approval; rejected requests are refunded."
        return True, f"{user} bought {item['name']} for {item['price']:,} points. " + ("Gift delivered!" if item["category"] == "gift" else result)
    except ValueError as exc:
        return False, str(exc)


def review(order_id, action):
    if action not in {"approve", "reject", "complete"}:
        return False, "Unknown shop action."
    now = store.utc_now()
    try:
        with store.db_session() as conn:
            row = conn.execute("SELECT * FROM shop_orders WHERE id=?", (order_id,)).fetchone()
            if not row:
                raise ValueError("Purchase not found.")
            order = dict(row)
            item = json.loads(order["item"])
            if action == "complete":
                if order["status"] != "approved" or item["category"] != "challenge":
                    raise ValueError("Only an approved challenge can be completed.")
                status, result = "completed", _fulfill(conn, order, item, now)
            else:
                cancellable_challenge = action == "reject" and order["status"] == "approved" and item["category"] == "challenge"
                if order["status"] != "pending" and not cancellable_challenge:
                    raise ValueError("That purchase has already been reviewed.")
                if action == "reject":
                    conn.execute("UPDATE points SET points=points+? WHERE user=?", (item["price"], order["user"]))
                    if item["stock"] > 0:
                        conn.execute("UPDATE shop_items SET stock=stock+1,version=version+1 WHERE id=? AND stock>=0", (order["item_id"],))
                    status, result = "rejected", f"Refunded {item['price']} points."
                elif item["category"] == "challenge":
                    status, result = "approved", "Ready for the streamer to perform."
                else:
                    status, result = "completed", _fulfill(conn, order, item, now)
            conn.execute("UPDATE shop_orders SET status=?,result=?,updated_at=? WHERE id=?", (status, result, now.isoformat(), order_id))
        return True, f"Purchase #{order_id}: {result}"
    except ValueError as exc:
        return False, str(exc)


def snapshot(user_filter="", offset=0):
    with store.db_session() as conn:
        where, args = ("WHERE user=?", [store.normalize_user(user_filter)]) if user_filter else ("", [])
        total = conn.execute(f"SELECT COUNT(*) FROM shop_orders {where}", args).fetchone()[0]
        orders = []
        for row in conn.execute(f"SELECT * FROM shop_orders {where} ORDER BY id DESC LIMIT 50 OFFSET ?", (*args, offset)):
            order = dict(row)
            order["item"] = json.loads(order["item"])
            orders.append(order)
        pending = []
        for row in conn.execute("SELECT * FROM shop_orders WHERE status IN ('pending','approved') ORDER BY id"):
            order = dict(row)
            order["item"] = json.loads(order["item"])
            pending.append(order)
        profiles = [_profile(conn, r["user"], store.utc_now()) for r in conn.execute("SELECT user FROM shop_profiles ORDER BY user")]
    return {"ok": True, "items": list_items(), "orders": orders, "pending": pending, "total": total,
            "profiles": profiles, "categories": CATEGORIES, "offset": offset}


def catalog_messages():
    if not store.is_feature_enabled("shop") or not store.is_feature_enabled("economy"):
        return ["The shop is currently disabled."]
    items = list_items(True)
    prefix = store.primary_prefix()
    if not items:
        return ["The shop has no available items right now."]
    lines = [f"{r['slug']}: {r['name']} ({r['price']:,} points)" for r in items]
    messages, current = [], f"Shop — {prefix}buy <code> [message]: "
    for line in lines:
        if len(current) + len(line) + 3 > 440:
            messages.append(current)
            current = "Shop: "
        current += (" | " if not current.endswith(": ") else "") + line
    messages.append(current)
    return messages
