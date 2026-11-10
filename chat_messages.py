"""Plain-text presentation shared by chat commands and dashboard announcements."""
import re

LIMIT = 440
ICONS = {"shop": "🛍", "giveaway": "🎁", "winner": "🏆", "winners": "🏆", "reroll": "🏆", "profile": "👑", "boost": "⚡",
         "points": "🪙", "gambling": "🎲", "queue": "📋", "poll": "📊", "raffle": "🎟"}
REWARDS = {"challenge": "🎯", "title": "👑", "shoutout": "📣", "gift": "🎁", "cosmetic": "✨"}


def clean(value):
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", str(value))).strip()


def size(value):
    return len(value.encode("utf-16-le")) // 2


def clip(value, limit):
    value = clean(value)
    if size(value) <= limit:
        return value
    result, count = [], 0
    for char in value:
        width = 2 if ord(char) > 0xffff else 1
        if count + width > limit - 1:
            break
        result.append(char)
        count += width
    return "".join(result).rstrip() + "…"


def line(topic, *parts):
    label = f"{ICONS.get(topic, '•')} {topic.upper()}"
    return clip(" • ".join([label] + [clean(part) for part in parts if part is not None and clean(part)]), LIMIT)


def groups(rows, budget=290, max_items=3, separator=" | "):
    pages, current = [], []
    for row in rows:
        row = clip(row, budget)
        if current and (len(current) >= max_items or size(separator.join(current + [row])) > budget):
            pages.append(current)
            current = []
        current.append(row)
    if current:
        pages.append(current)
    return pages


def mention(user):
    return "@" + clean(user).lstrip("@").lower()


def giveaway_open(name, prefix, count):
    return line("giveaway", f"OPEN — {clip(name, 160)}", f"Free entry → {prefix}giveaway", f"{count} winner{'s' if count != 1 else ''}")


def giveaway_cancelled(name):
    return line("giveaway", f"CANCELLED — {clip(name, 180)}", "No winner selected")


def giveaway_results(name, winners, *, reroll=False, replaced=None, selected=None):
    if reroll and replaced:
        return [line("reroll", clip(name, 120), f"New winner: {mention(selected or winners[0])}", f"Replaces {mention(replaced)}")]
    names = [mention(user) for user in winners]
    pages = groups(names, budget=260, max_items=25, separator=" · ")
    messages = []
    for index, page in enumerate(pages, 1):
        topic = "reroll" if reroll else "winner" if len(names) == 1 else "winners"
        page_label = f" [{index}/{len(pages)}]" if len(pages) > 1 else ""
        messages.append(line(topic, f"{clip(name, 110)}{page_label}", " · ".join(page), "Congratulations!"))
    return messages
