import os
import json
import random
import asyncio
import time
from datetime import datetime, timezone

import aiohttp
from dotenv import load_dotenv, find_dotenv
from twitchio import Scopes, eventsub
from twitchio.exceptions import HTTPException
from twitchio.ext import commands

import store
from api import create_api_app, start_api_server


def strip_oauth_prefix(token: str) -> str:
    token = token.strip()
    if token.lower().startswith("oauth:"):
        return token.split(":", 1)[1].strip()
    return token


def env_value(key: str) -> str:
    value = (os.getenv(key) or "").strip()
    if not value or value.lower().startswith("your_"):
        return ""
    return value


def load_environment() -> None:
    env_path = find_dotenv()
    if env_path:
        load_dotenv(env_path, override=True)

    missing = [
        key for key in (
            "TWITCH_CLIENT_ID",
            "TWITCH_CLIENT_SECRET",
            "TWITCH_NICK",
            "TWITCH_CHANNEL",
        ) if not env_value(key)
    ]
    if missing:
        raise RuntimeError(
            "Missing environment variables: "
            + ", ".join(missing)
            + ". On Railway, add them under this service's Variables. "
            ".env is not copied into the container."
        )


def fetch_twitch_user_id(login: str) -> str | None:
    import json
    import urllib.parse
    import urllib.request

    token_body = urllib.parse.urlencode({
        "client_id": TWITCH_CLIENT_ID,
        "client_secret": TWITCH_CLIENT_SECRET,
        "grant_type": "client_credentials",
    }).encode()
    token_request = urllib.request.Request("https://id.twitch.tv/oauth2/token", data=token_body, method="POST")
    with urllib.request.urlopen(token_request) as response:
        access_token = json.loads(response.read())["access_token"]

    user_request = urllib.request.Request(
        "https://api.twitch.tv/helix/users?login=" + urllib.parse.quote(login.lower()),
        headers={
            "Client-ID": TWITCH_CLIENT_ID,
            "Authorization": f"Bearer {access_token}",
        },
    )
    with urllib.request.urlopen(user_request) as response:
        users = json.loads(response.read()).get("data") or []
    if not users:
        return None
    return str(users[0]["id"])


def upsert_env_value(path: str, key: str, value: str) -> None:
    lines: list[str] = []
    found = False
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if line.startswith(f"{key}="):
                lines.append(f"{key}={value}\n")
                found = True
            else:
                lines.append(line)
    if not found:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"{key}={value}\n")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.writelines(lines)


def persist_twitch_tokens(access_token: str, refresh_token: str) -> None:
    os.environ["TWITCH_TOKEN"] = access_token
    os.environ["TWITCH_REFRESH_TOKEN"] = refresh_token
    store.set_twitch_tokens(access_token, refresh_token)
    env_path = find_dotenv()
    if not env_path or not os.path.isfile(env_path):
        print("Saved new Twitch tokens to the database.")
        return
    try:
        upsert_env_value(env_path, "TWITCH_TOKEN", access_token)
        upsert_env_value(env_path, "TWITCH_REFRESH_TOKEN", refresh_token)
        print(f"Saved new Twitch tokens to {env_path}")
    except OSError as exc:
        print(f"Could not write Twitch tokens to .env ({exc}). They are stored in the database.")


def resolve_bot_id() -> str:
    configured = env_value("TWITCH_BOT_ID")
    if configured.isdigit():
        return configured
    user_id = fetch_twitch_user_id(BOT_NICK)
    if not user_id:
        raise RuntimeError(
            f"Could not find Twitch user '{BOT_NICK}'. Confirm the SimplyCowBot account exists, "
            "the username is exact, and the email is verified. Then set TWITCH_BOT_ID to that account's numeric user ID."
        )
    print(f"Resolved TWITCH_BOT_ID for {BOT_NICK}: {user_id}")
    return user_id


load_environment()

TWITCH_CLIENT_ID: str = env_value("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET: str = env_value("TWITCH_CLIENT_SECRET")
BOT_TOKEN: str = strip_oauth_prefix(env_value("TWITCH_TOKEN"))
BOT_REFRESH_TOKEN: str = env_value("TWITCH_REFRESH_TOKEN")
BOT_NICK: str = env_value("TWITCH_NICK")
CHANNEL: str = env_value("TWITCH_CHANNEL").lstrip("#")
TWITCH_BOT_ID: str = resolve_bot_id()

bot: "CowBot | None" = None


async def prefixes_for_message(_bot, _message) -> tuple[str, ...]:
    return store.get_command_prefixes()


def get_author_name(ctx: commands.Context) -> str:
    author = getattr(ctx, "author", None) or getattr(ctx, "chatter", None)
    return getattr(author, "name", None) or getattr(author, "display_name", None) or "Unknown"


def get_author_mention(ctx: commands.Context) -> str:
    chatter = getattr(ctx, "chatter", None) or getattr(ctx, "author", None)
    mention = getattr(chatter, "mention", None)
    if mention:
        return mention
    name = get_author_name(ctx)
    return f"@{name}" if name != "Unknown" else name


def get_invoked_argument(ctx: commands.Context, invoked: str) -> str:
    payload = getattr(ctx, "_payload", None) or getattr(ctx, "message", None)
    text = str(getattr(payload, "text", None) or getattr(ctx, "content", "") or "").strip()
    for prefix in store.get_command_prefixes():
        if text.startswith(prefix):
            text = text[len(prefix):].lstrip()
            break
    invoked_name = str(invoked or "").strip()
    if invoked_name and text.lower().startswith(invoked_name.lower()):
        text = text[len(invoked_name):].lstrip()
    if not text:
        return ""
    return text.split()[0].lstrip("@")


def _badge_set_ids(source) -> set[str]:
    badges = getattr(source, "badges", None) or []
    ids: set[str] = set()
    for badge in badges:
        set_id = getattr(badge, "set_id", None)
        if set_id:
            ids.add(str(set_id).lower())
    return ids


def is_mod_or_broadcaster(ctx: commands.Context) -> bool:
    chatter = getattr(ctx, "chatter", None) or getattr(ctx, "author", None)
    if chatter is None:
        return False

    chatter_id = str(getattr(chatter, "id", "") or "")
    chatter_login = str(getattr(chatter, "name", "") or "").lower()
    broadcaster = getattr(ctx, "broadcaster", None)
    broadcaster_id = str(getattr(broadcaster, "id", "") or "") if broadcaster is not None else ""

    if chatter_id and broadcaster_id and chatter_id == broadcaster_id:
        return True
    if chatter_login and chatter_login == CHANNEL.lower():
        return True
    if getattr(chatter, "moderator", False) or getattr(chatter, "broadcaster", False):
        return True

    badge_ids = _badge_set_ids(chatter)
    message = getattr(ctx, "message", None) or getattr(ctx, "_payload", None)
    badge_ids |= _badge_set_ids(message)
    return bool(badge_ids & {"moderator", "broadcaster"})


async def require_command(ctx: commands.Context, name: str) -> bool:
    if store.is_command_available(name):
        return True
    await ctx.send(store.command_unavailable_message(name))
    return False


class CowCommands(commands.Component):
    def __init__(self, bot: "CowBot"):
        self.bot = bot

    @commands.command(name="uptime")
    async def uptime(self, ctx: commands.Context):
        if not await require_command(ctx, "uptime"):
            return
        await ctx.send(f"Bot uptime: {store.format_uptime(store.utc_now() - self.bot.start_time)}.")

    @commands.command(name="ping")
    async def ping(self, ctx: commands.Context):
        if not await require_command(ctx, "ping"):
            return
        await ctx.send("Pong")

    @commands.command(name="help", aliases=["commands"])
    async def help_command(self, ctx: commands.Context):
        if not await require_command(ctx, "help"):
            return
        messages = store.help_whisper_messages()
        chatter = getattr(ctx, "chatter", None) or getattr(ctx, "author", None)
        ok, error = await self.bot.whisper_user(chatter, messages)
        if ok:
            await ctx.reply("Sent you a whisper from SimpleCowBot with the enabled commands.")
            return
        await ctx.reply(error or "I couldn't whisper you the command list.")

    @commands.command(name="lurk")
    async def lurk(self, ctx: commands.Context):
        if not await require_command(ctx, "lurk"):
            return
        await ctx.send(store.render_lurk_message(get_author_mention(ctx)))

    @commands.command(name="unlurk")
    async def unlurk(self, ctx: commands.Context):
        if not await require_command(ctx, "unlurk"):
            return
        await ctx.send(store.render_unlurk_message(get_author_mention(ctx)))

    @commands.command(name="followage")
    async def followage(self, ctx: commands.Context, target: str | None = None):
        if not await require_command(ctx, "followage"):
            return
        if self.bot.channel_user is None:
            await ctx.send("The bot is still connecting to the channel.")
            return

        channel_name = getattr(self.bot.channel_user, "display_name", None) or CHANNEL
        looking_up_other = bool((target or "").strip())
        if looking_up_other:
            login = store.normalize_user(target)
            if not login or login == "unknown":
                await ctx.send(f"Usage: {store.primary_prefix()}followage [user]")
                return
            users = await self.bot.fetch_users(logins=[login])
            if not users:
                await ctx.send(f"Couldn't find Twitch user '{login}'.")
                return
            user = users[0]
            user_id = str(user.id)
            name = getattr(user, "display_name", None) or getattr(user, "name", None) or login
        else:
            chatter = getattr(ctx, "chatter", None) or getattr(ctx, "author", None)
            user_id = str(getattr(chatter, "id", "") or "")
            name = get_author_name(ctx)
            if not user_id:
                users = await self.bot.fetch_users(logins=[store.normalize_user(name)])
                if not users:
                    await ctx.send("Could not look up your Twitch account.")
                    return
                user_id = str(users[0].id)

        if user_id == str(self.bot.channel_user.id):
            if looking_up_other:
                await ctx.send(f"{name} is the streamer.")
            else:
                await ctx.reply(f"{get_author_mention(ctx)} you own this channel.")
            return

        followed_at, error = await self.bot.fetch_channel_follow(user_id)
        if error:
            await ctx.send(error)
            return
        if followed_at is None:
            if looking_up_other:
                await ctx.send(f"{name} is not following {channel_name}.")
            else:
                await ctx.reply(f"{get_author_mention(ctx)} you are not following {channel_name}.")
            return
        duration = store.format_followage(followed_at)
        if looking_up_other:
            await ctx.send(f"{name} has been following for {duration}.")
        else:
            await ctx.reply(f"{get_author_mention(ctx)} you have been following for {duration}.")

    @commands.command(name="streamuptime", aliases=["stream"])
    async def streamuptime(self, ctx: commands.Context):
        if not await require_command(ctx, "streamuptime"):
            return
        stream = await self.bot.fetch_current_stream()
        if stream is None:
            await ctx.send("The stream is offline.")
            return
        started = getattr(stream, "started_at", None)
        if isinstance(started, str):
            started = store.parse_iso(started)
        if started is None:
            await ctx.send("The stream is live, but I couldn't read when it started.")
            return
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        seconds = max(int((store.utc_now() - started).total_seconds()), 0)
        await ctx.send(f"The stream has been live for {store.format_watchtime(seconds)}.")

    @commands.command(name="viewers")
    async def viewers(self, ctx: commands.Context):
        if not await require_command(ctx, "viewers"):
            return
        stream = await self.bot.fetch_current_stream()
        if stream is None:
            await ctx.send("The stream is offline.")
            return
        count = int(getattr(stream, "viewer_count", 0) or 0)
        label = "viewer" if count == 1 else "viewers"
        await ctx.send(f"There {'is' if count == 1 else 'are'} {count:,} {label} right now.")

    @commands.command(name="followcount", aliases=["followers", "follows"])
    async def followcount(self, ctx: commands.Context):
        if not await require_command(ctx, "followcount"):
            return
        total, error = await self.bot.fetch_follower_total()
        if error:
            await ctx.send(error)
            return
        channel_name = getattr(self.bot.channel_user, "display_name", None) or CHANNEL
        label = "follower" if total == 1 else "followers"
        await ctx.send(f"{channel_name} has {total:,} {label}.")

    @commands.command(name="subcount", aliases=["subs", "subscribers"])
    async def subcount(self, ctx: commands.Context):
        if not await require_command(ctx, "subcount"):
            return
        total, error = await self.bot.fetch_subscriber_total()
        if error:
            await ctx.send(error)
            return
        channel_name = getattr(self.bot.channel_user, "display_name", None) or CHANNEL
        label = "subscriber" if total == 1 else "subscribers"
        await ctx.send(f"{channel_name} has {total:,} {label}.")

    @commands.command(name="first")
    async def first(self, ctx: commands.Context):
        if not await require_command(ctx, "first"):
            return
        stream = await self.bot.fetch_current_stream()
        stream_id = getattr(stream, "id", None) if stream is not None else None
        author_name = get_author_name(ctx)
        if stream is not None and self.bot._can_claim_first(author_name):
            chatter = store.try_claim_first(author_name, stream_id)
        else:
            chatter = store.get_first_chatter()
        if stream is None:
            if chatter:
                await ctx.send(f"{store.mention_user(chatter)} was first last stream.")
            else:
                await ctx.send("The stream is offline.")
            return
        if chatter:
            await ctx.send(f"{store.mention_user(chatter)} was first this stream.")
            return
        await ctx.send("Nobody has claimed first yet.")

    @commands.command(name="points")
    async def points(self, ctx: commands.Context, *, rest: str | None = None):
        if not await require_command(ctx, "points"):
            return
        parts = (rest or "").split()
        action = parts[0].lower() if parts else ""
        if action in {"give", "add", "remove", "take"}:
            if not is_mod_or_broadcaster(ctx):
                await ctx.send("Only mods and the broadcaster can give or remove points.")
                return
            if len(parts) < 3 or not parts[2].isdigit():
                await ctx.send(f"Usage: {store.primary_prefix()}points {action} <user> <amount>")
                return
            amount = int(parts[2])
            if action in {"remove", "take"}:
                amount = -amount
            ok, result, total = store.grant_points(parts[1], amount)
            if not ok:
                await ctx.send(result or "Could not update points.")
                return
            user = result
            if amount > 0:
                await ctx.send(f"{store.mention_user(user)} gained {amount} points. Total: {total}.")
            else:
                await ctx.send(f"{store.mention_user(user)} lost {abs(amount)} points. Total: {total}.")
            return
        target = store.normalize_user(rest or get_author_name(ctx))
        points = store.get_points(target)
        await ctx.send(f"{target} has {points} points.")

    @commands.command(name="daily")
    async def daily(self, ctx: commands.Context):
        if not await require_command(ctx, "daily"):
            return
        author_name = get_author_name(ctx)
        claimed, earned, new_total = store.try_claim_daily(author_name)
        if claimed:
            await ctx.send(f"{author_name}, you claimed your daily reward and earned {earned} points! Total: {new_total}.")
        else:
            await ctx.send(f"{author_name}, you already claimed your daily reward. Come back tomorrow.")

    @commands.command(name="gamble")
    async def gamble(self, ctx: commands.Context, amount: str):
        if not await require_command(ctx, "gamble"):
            return
        author_name = get_author_name(ctx)
        user = store.normalize_user(author_name)
        current = store.get_points(user)
        amount_value, parse_error = store.parse_wager(amount, current)
        if amount_value is None:
            await ctx.send(f"{author_name}, {parse_error or f'Usage: {store.primary_prefix()}gamble <amount|percent|all>'}")
            return

        if amount_value <= 0 or amount_value > current:
            await ctx.send(f"{author_name}, invalid amount. You have {current} points.")
            return

        win = random.choice([True, False])
        if win:
            spent, remaining = store.try_spend_points(user, amount_value)
            if not spent:
                await ctx.send(f"{author_name}, invalid amount. You have {remaining} points.")
                return
            new_total = store.change_points(user, amount_value * 2)
            await ctx.send(f"{author_name} won {amount_value} points! Total: {new_total}.")
        else:
            spent, new_total = store.try_spend_points(user, amount_value)
            if not spent:
                await ctx.send(f"{author_name}, invalid amount. You have {new_total} points.")
                return
            await ctx.send(f"{author_name} lost {amount_value} points. Total: {new_total}.")

    @commands.command(name="roulette")
    async def roulette(self, ctx: commands.Context, amount: str):
        if not await require_command(ctx, "roulette"):
            return
        author_name = get_author_name(ctx)
        user = store.normalize_user(author_name)
        current = store.get_points(user)
        wager, parse_error = store.parse_wager(amount, current)
        if wager is None:
            await ctx.send(f"{author_name}, {parse_error or f'Usage: {store.primary_prefix()}roulette <amount|percent|all>'}")
            return
        if wager <= 0 or wager > current:
            await ctx.send(f"{author_name}, invalid wager. You have {current} points.")
            return

        spent, remaining = store.try_spend_points(user, wager)
        if not spent:
            await ctx.send(f"{author_name}, invalid wager. You have {remaining} points.")
            return

        number = random.randint(0, 36)
        choice = random.randint(0, 36)
        if number == choice:
            payout = wager * 36
            new_total = store.change_points(user, payout)
            await ctx.send(f"{author_name} hit {number}! You win {payout} points! Total: {new_total}.")
        else:
            await ctx.send(f"{author_name} spun {number} and lost {wager} points. Total: {remaining}.")

    @commands.command(name="slots")
    async def slots(self, ctx: commands.Context, amount: str):
        if not await require_command(ctx, "slots"):
            return
        author_name = get_author_name(ctx)
        user = store.normalize_user(author_name)
        current = store.get_points(user)
        wager, parse_error = store.parse_wager(amount, current)
        if wager is None:
            await ctx.send(f"{author_name}, {parse_error or f'Usage: {store.primary_prefix()}slots <amount|percent|all>'}")
            return
        if wager <= 0 or wager > current:
            await ctx.send(f"{author_name}, invalid amount. You have {current} points.")
            return
        ok, error, total, reels, payout = store.play_slots(user, wager)
        if not ok:
            await ctx.send(f"{author_name}, {error or 'Could not play slots.'}")
            return
        line = " | ".join(reels)
        if payout >= wager * 10:
            result = f"JACKPOT! Won {payout} points"
        elif payout:
            result = f"won {payout} points"
        else:
            result = f"lost {wager} points"
        await ctx.send(f"{author_name} spun {line} and {result}. Total: {total}.")

    @commands.command(name="giveaway")
    async def giveaway(self, ctx: commands.Context, action: str | None = None, *, name: str | None = None):
        if not await require_command(ctx, "giveaway"):
            return
        if not action:
            action = "enter"
        else:
            action = action.lower()
        if action == "start":
            if not is_mod_or_broadcaster(ctx):
                print(f"Giveaway start denied | {get_author_name(ctx)}")
                await ctx.send("Only mods and the broadcaster can start giveaways.")
                return
            if not name:
                await ctx.send(f"Usage: {store.primary_prefix()}giveaway start <name> [winners]")
                return
            winner_count = 1
            parts = name.rsplit(None, 1)
            if len(parts) == 2 and parts[1].isdigit():
                name, winner_count = parts[0], int(parts[1])
            success, result = store.start_giveaway(name, winner_count)
            if not success:
                await ctx.send(result or "Could not start giveaway.")
                return
            count = store.get_giveaway_winner_count()
            extra = f" Drawing {count} winners." if count > 1 else ""
            sent = await ctx.send(
                f"Giveaway '{name}' started! Type {store.primary_prefix()}giveaway to join.{extra}"
            )
            await self.bot.pin_giveaway_message(getattr(sent, "id", None))
        elif action == "enter":
            mention = get_author_mention(ctx)
            success, result = store.enter_giveaway(get_author_name(ctx))
            if not success:
                if result and "already entered" in result:
                    await ctx.reply(f"{mention} you are already entered in the giveaway.")
                else:
                    await ctx.reply(f"{mention} {result or 'Could not enter giveaway.'}")
                return
            await ctx.reply(f"{mention} you entered the giveaway '{store.get_active_giveaway()}'.")
        elif action == "end":
            if not is_mod_or_broadcaster(ctx):
                await ctx.send("Only mods and the broadcaster can end giveaways.")
                return
            winners, giveaway_name = store.finish_giveaway()
            if winners and giveaway_name:
                await self.bot.unpin_giveaway_message()
                label = "winner is" if len(winners) == 1 else "winners are"
                await ctx.send(f"Giveaway '{giveaway_name}' ended! The {label} {store.format_winners(winners)}.")
            else:
                await ctx.send(giveaway_name or "No giveaway is currently active.")
        elif action == "cancel":
            if not is_mod_or_broadcaster(ctx):
                await ctx.send("Only mods and the broadcaster can cancel giveaways.")
                return
            success, result = store.cancel_giveaway()
            if not success:
                await ctx.send(result or "No giveaway is currently active.")
                return
            await self.bot.unpin_giveaway_message()
            await ctx.send(f"Giveaway '{result}' was cancelled. No winner was chosen.")
        elif action == "reroll":
            if not is_mod_or_broadcaster(ctx):
                await ctx.send("Only mods and the broadcaster can reroll giveaways.")
                return
            winners, giveaway_name, _entries, _replaced = store.reroll_giveaway(name)
            if not winners or not giveaway_name:
                await ctx.send(giveaway_name or "There is no giveaway to reroll.")
                return
            winners, giveaway_name, is_reroll, replaced, drawn_winner = store.complete_giveaway_draw()
            if winners and giveaway_name:
                if is_reroll and replaced:
                    await ctx.send(
                        f"Giveaway '{giveaway_name}' reroll: {store.mention_user(replaced)} is out. "
                        f"The new winner is {store.mention_user(drawn_winner or winners[0])}."
                    )
                else:
                    await ctx.send(
                        f"Giveaway '{giveaway_name}' was rerolled! The new winner is {store.format_winners(winners)}."
                    )
            else:
                await ctx.send(giveaway_name or "Could not reroll the giveaway.")
        else:
            await ctx.send(
                f"Giveaway commands: {store.primary_prefix()}giveaway, "
                f"{store.primary_prefix()}giveaway start <name> [winners], "
                f"{store.primary_prefix()}giveaway end, {store.primary_prefix()}giveaway cancel, "
                f"{store.primary_prefix()}giveaway reroll [user]"
            )

    @commands.command(name="quote")
    async def quote(self, ctx: commands.Context, *, text: str | None = None):
        if not await require_command(ctx, "quote"):
            return
        if not text:
            quote = store.get_random_quote()
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
            await ctx.send(f"Usage: {store.primary_prefix()}quote add <quote text> | <author>")
            return

        raw_quote, author = map(str.strip, quote_text.split("|", 1))
        if not raw_quote:
            await ctx.send("Quote text cannot be empty.")
            return

        quote_id = store.add_quote(raw_quote, author or "Unknown", get_author_name(ctx))
        await ctx.send(f"Quote #{quote_id} added.")

    @commands.command(name="leaderboard")
    async def leaderboard(self, ctx: commands.Context):
        if not await require_command(ctx, "leaderboard"):
            return
        rows = store.get_leaderboard(5)
        if not rows:
            await ctx.send("No leaderboard entries yet.")
            return
        leaderboard = ", ".join(f"{row['user']}({row['points']})" for row in rows)
        await ctx.send(f"Top points: {leaderboard}")

    @commands.command(name="watchtime", aliases=["wt"])
    async def watchtime(self, ctx: commands.Context, target: str | None = None):
        if not await require_command(ctx, "watchtime"):
            return
        looking_up_other = bool((target or "").strip())
        if looking_up_other:
            name = store.normalize_user(target)
            if not name or name == "unknown":
                await ctx.send(f"Usage: {store.primary_prefix()}watchtime [user]")
                return
        else:
            name = store.normalize_user(get_author_name(ctx))

        if name == store.normalize_user(CHANNEL):
            if looking_up_other:
                await ctx.send(f"{name} is the streamer.")
            else:
                await ctx.reply(f"{get_author_mention(ctx)} you're the streamer.")
            return

        total = store.get_watch_seconds(name)
        session = self.bot.session_watch_seconds(name)
        if total <= 0 and session <= 0:
            if looking_up_other:
                await ctx.send(f"{name} has no watch time yet.")
            else:
                await ctx.reply(
                    f"{get_author_mention(ctx)} you have no watch time yet. "
                    "Hang around in chat while the stream is live."
                )
            return
        if total <= 0:
            body = f"been watching for {store.format_watchtime(session)} this stream"
        elif session > 0:
            body = f"watched for {store.format_watchtime(total)} ({store.format_watchtime(session)} this stream)"
        else:
            body = f"watched for {store.format_watchtime(total)}"
        if looking_up_other:
            await ctx.send(f"{name} has {body}.")
        else:
            await ctx.reply(f"{get_author_mention(ctx)} you have {body}.")

    @commands.command(name="poll")
    async def poll(self, ctx: commands.Context, action: str | None = None, *, args: str | None = None):
        if not await require_command(ctx, "poll"):
            return
        if not action:
            await ctx.send(
                f"Poll commands: {store.primary_prefix()}poll start <name> | <question> | <options>, "
                f"{store.primary_prefix()}poll vote <option>, {store.primary_prefix()}poll end"
            )
            return
        action = action.lower()
        if action == "start":
            if not is_mod_or_broadcaster(ctx):
                await ctx.send("Only mods and the broadcaster can start polls.")
                return
            if not args or "|" not in args:
                await ctx.send(
                    f"Usage: {store.primary_prefix()}poll start <poll name> | <question> | <option1>, <option2>, ..."
                )
                return
            parts = [part.strip() for part in args.split("|")]
            if len(parts) < 3:
                await ctx.send(
                    f"Usage: {store.primary_prefix()}poll start <poll name> | <question> | <option1>, <option2>, ..."
                )
                return
            name = parts[0]
            question = parts[1]
            options = [opt.strip() for opt in parts[2].split(",") if opt.strip()]
            success, error = store.create_poll(name, question, options)
            if not success:
                await ctx.send(error or "Could not start poll.")
                return
            await ctx.send(f"Poll '{name}' started: {question} Options: {', '.join(store.unique_options(options))}")
        elif action == "vote":
            active_poll = store.get_active_poll_name()
            if not active_poll:
                await ctx.send("There is no active poll.")
                return
            if not args:
                await ctx.send(f"Usage: {store.primary_prefix()}poll vote <option>")
                return
            author_name = get_author_name(ctx)
            success, result = store.vote_poll(active_poll, author_name, args)
            if not success:
                await ctx.send(result or "Could not register your vote.")
                return
            await ctx.send(f"{author_name} voted for {result} in poll '{active_poll}'.")
        elif action == "status":
            active_poll = store.get_active_poll_name()
            if not active_poll:
                await ctx.send("There is no active poll.")
                return
            question = store.get_poll_question(active_poll)
            options = store.get_poll_options(active_poll)
            results = ", ".join(f"{row['option']}({row['votes']})" for row in options)
            await ctx.send(f"Poll '{active_poll}': {question} Results: {results}")
        elif action == "end":
            if not is_mod_or_broadcaster(ctx):
                await ctx.send("Only mods and the broadcaster can end polls.")
                return
            active_poll = store.get_active_poll_name()
            if not active_poll:
                await ctx.send("There is no active poll.")
                return
            results = store.end_poll(active_poll)
            if not results:
                await ctx.send(f"Poll '{active_poll}' ended with no votes.")
                return
            top = ", ".join(f"{row['option']}({row['votes']})" for row in results)
            await ctx.send(f"Poll '{active_poll}' ended. Results: {top}")
        else:
            await ctx.send(
                f"Poll commands: {store.primary_prefix()}poll start <name> | <question> | <options>, "
                f"{store.primary_prefix()}poll vote <option>, {store.primary_prefix()}poll end"
            )

    @commands.command(name="raffle")
    async def raffle(self, ctx: commands.Context, action: str | None = None, *, args: str | None = None):
        if not await require_command(ctx, "raffle"):
            return
        if not action:
            await ctx.send(
                f"Raffle commands: {store.primary_prefix()}raffle start <name> | <cost>, "
                f"{store.primary_prefix()}raffle enter, {store.primary_prefix()}raffle end"
            )
            return
        action = action.lower()
        if action == "start":
            if not is_mod_or_broadcaster(ctx):
                await ctx.send("Only mods and the broadcaster can start raffles.")
                return
            if not args:
                await ctx.send(f"Usage: {store.primary_prefix()}raffle start <name> | <cost>")
                return
            if "|" in args:
                name, cost_text = [part.strip() for part in args.split("|", 1)]
                cost = store.parse_non_negative_int(cost_text, 0)
                if cost <= 0:
                    await ctx.send("Entry cost must be a positive number.")
                    return
            else:
                name = args.strip()
                if not name:
                    await ctx.send(f"Usage: {store.primary_prefix()}raffle start <name> | <cost>")
                    return
                cost = store.get_dashboard_settings()["default_raffle_cost"]
            success, error = store.create_raffle(name, cost)
            if not success:
                await ctx.send(error or "Could not start raffle.")
                return
            await ctx.send(
                f"Raffle '{name}' started with entry cost {cost} points. Type {store.primary_prefix()}raffle enter."
            )
        elif action == "enter":
            active_raffle = store.get_active_raffle_name()
            if not active_raffle:
                await ctx.send("There is no active raffle.")
                return
            author_name = get_author_name(ctx)
            success, error = store.enter_raffle(active_raffle, author_name)
            if not success:
                await ctx.send(error or "Failed to enter raffle.")
                return
            cost = store.get_raffle_cost(active_raffle)
            await ctx.send(f"{author_name} entered raffle '{active_raffle}' for {cost} points.")
        elif action == "end":
            if not is_mod_or_broadcaster(ctx):
                await ctx.send("Only mods and the broadcaster can end raffles.")
                return
            active_raffle = store.get_active_raffle_name()
            if not active_raffle:
                await ctx.send("There is no active raffle.")
                return
            winner = store.end_raffle(active_raffle)
            if not winner:
                await ctx.send(f"Raffle '{active_raffle}' ended with no entries.")
                return
            await ctx.send(f"Raffle '{active_raffle}' ended! The winner is {winner}.")
        else:
            await ctx.send(
                f"Raffle commands: {store.primary_prefix()}raffle start <name> | <cost>, "
                f"{store.primary_prefix()}raffle enter, {store.primary_prefix()}raffle end"
            )

    @commands.command(name="queue", aliases=["q"])
    async def queue(self, ctx: commands.Context, action: str | None = None, *, args: str | None = None):
        if not await require_command(ctx, "queue"):
            return
        prefix = store.primary_prefix()
        author_name = get_author_name(ctx)
        mention = get_author_mention(ctx)
        action = (action or "").lower()

        if action in {"", "join"}:
            place, total, name = store.get_queue_position(author_name)
            if name and place:
                await ctx.reply(f"{mention} you are #{place} of {total} in '{name}'.")
                return
            success, result, place, total = store.join_queue(author_name)
            if not success:
                await ctx.reply(f"{mention} {result or 'Could not join the queue.'}")
                return
            name = store.current_queue_state()["name"]
            await ctx.reply(f"{mention} you joined '{name}' at #{place} of {total}.")
            return

        if action == "leave":
            success, result = store.leave_queue(author_name)
            if not success:
                await ctx.reply(f"{mention} {result or 'Could not leave the queue.'}")
                return
            await ctx.reply(f"{mention} you left the queue.")
            return

        if action == "list":
            state = store.current_queue_state()
            if not state["name"]:
                await ctx.send("No queue is currently running.")
                return
            entries = state["entries"][:10]
            if not entries:
                await ctx.send(f"Queue '{state['name']}' is empty.")
                return
            names = ", ".join(f"{row['place']}. {row['user']}" for row in entries)
            extra = f" (+{state['count'] - 10} more)" if state["count"] > 10 else ""
            status = "open" if state["open"] else "closed"
            await ctx.send(f"Queue '{state['name']}' ({status}, {state['count']}): {names}{extra}")
            return

        if action == "start":
            if not is_mod_or_broadcaster(ctx):
                await ctx.send("Only mods and the broadcaster can start the queue.")
                return
            if not args:
                await ctx.send(f"Usage: {prefix}queue start <name> [max]")
                return
            cap = 0
            parts = args.rsplit(None, 1)
            if len(parts) == 2 and parts[1].isdigit():
                name, cap = parts[0], int(parts[1])
            else:
                name = args.strip()
            success, result = store.start_queue(name, cap)
            if not success:
                await ctx.send(result or "Could not start queue.")
                return
            parsed_cap = store.current_queue_state()["cap"]
            extra = f" Cap {parsed_cap}." if parsed_cap else ""
            await ctx.send(f"Queue '{result}' is open! Type {prefix}queue to join.{extra}")
            return

        if action in {"next", "pop"}:
            if not is_mod_or_broadcaster(ctx):
                await ctx.send("Only mods and the broadcaster can call the next person.")
                return
            user, error, remaining = store.next_queue()
            if error:
                await ctx.send(error)
                return
            name = store.current_queue_state()["name"]
            leftover = f"{remaining} remaining." if remaining else "Queue is empty."
            await ctx.send(f"{store.mention_user(user)} you're up for {name}! {leftover}")
            return

        if action == "close":
            if not is_mod_or_broadcaster(ctx):
                await ctx.send("Only mods and the broadcaster can close the queue.")
                return
            success, result = store.close_queue()
            if not success:
                await ctx.send(result or "Could not close the queue.")
                return
            await ctx.send(f"Queue '{result}' is closed. No more joins.")
            return

        if action == "open":
            if not is_mod_or_broadcaster(ctx):
                await ctx.send("Only mods and the broadcaster can reopen the queue.")
                return
            success, result = store.open_queue()
            if not success:
                await ctx.send(result or "Could not reopen the queue.")
                return
            await ctx.send(f"Queue '{result}' is open again. Type {prefix}queue to join.")
            return

        if action in {"remove", "skip"}:
            if not is_mod_or_broadcaster(ctx):
                await ctx.send("Only mods and the broadcaster can remove people from the queue.")
                return
            target = (args or "").strip()
            if not target:
                await ctx.send(f"Usage: {prefix}queue remove <user>")
                return
            success, result = store.remove_from_queue(target)
            if not success:
                await ctx.send(result or "Could not remove that user.")
                return
            await ctx.send(f"{store.mention_user(result)} was removed from the queue.")
            return

        if action == "clear":
            if not is_mod_or_broadcaster(ctx):
                await ctx.send("Only mods and the broadcaster can clear the queue.")
                return
            success, result = store.clear_queue()
            if not success:
                await ctx.send(result or "Could not clear the queue.")
                return
            await ctx.send(f"Queue '{result}' was cleared.")
            return

        await ctx.send(
            f"Queue commands: {prefix}queue, {prefix}queue leave, {prefix}queue list. "
            f"Mods: {prefix}queue start <name> [max], {prefix}queue next, {prefix}queue close, {prefix}queue clear."
        )

    @commands.command(name="transfer")
    async def transfer(self, ctx: commands.Context, target: str, amount: str):
        if not await require_command(ctx, "transfer"):
            return
        author_name = get_author_name(ctx)
        from_user = store.normalize_user(author_name)
        to_user = store.normalize_user(target)
        if from_user == to_user:
            await ctx.send("You cannot transfer points to yourself.")
            return
        if not amount.isdigit() or amount == "0":
            await ctx.send(f"Usage: {store.primary_prefix()}transfer <user> <amount>")
            return
        amount_value = int(amount)
        spent, current = store.try_spend_points(from_user, amount_value)
        if not spent:
            await ctx.send(f"{author_name}, invalid amount. You have {current} points.")
            return
        store.change_points(to_user, amount_value)
        await ctx.send(f"{author_name} transferred {amount_value} points to {to_user}.")



class CowContext(commands.Context):
    async def send(self, content: str, *, me: bool = False):
        message = (f"/me {content}" if me else content).strip()
        try:
            return await self.channel.send_message(
                sender=self.bot.bot_id,
                message=message,
                token_for=self.bot.bot_id,
            )
        except Exception as exc:
            print(f"Failed to send chat reply: {exc}")
            raise

    async def reply(self, content: str, *, me: bool = False):
        message = (f"/me {content}" if me else content).strip()
        try:
            return await self.channel.send_message(
                sender=self.bot.bot_id,
                message=message,
                token_for=self.bot.bot_id,
                reply_to_message_id=getattr(self._payload, "id", None),
            )
        except Exception as exc:
            print(f"Failed to send chat reply: {exc}")
            return await self.send(content, me=me)


class CowBot(commands.Bot):
    def __init__(self):
        super().__init__(
            client_id=TWITCH_CLIENT_ID,
            client_secret=TWITCH_CLIENT_SECRET,
            bot_id=TWITCH_BOT_ID,
            prefix=prefixes_for_message,
            scopes=Scopes(
                user_read_chat=True,
                user_write_chat=True,
                user_bot=True,
                moderator_read_chatters=True,
                moderator_manage_chat_messages=True,
                moderator_read_followers=True,
                user_manage_whispers=True,
            ),
        )
        self.start_time = store.utc_now()
        self.channel_user = None
        self._chat_subscribed = False
        self._chat_subscribe_lock = asyncio.Lock()
        self._recent_message_ids: dict[str, float] = {}
        self._stream_live = False
        self._current_stream = None
        self._live_checked_at = 0.0
        self._live_status_logged = False
        self._watch_chat_seen: dict[str, float] = {}
        self._watch_session_started: dict[str, datetime] = {}
        self._watch_interval_seconds = store.DEFAULT_WATCHTIME_MINUTES * 60
        self._watch_scope_warned = False
        self._pin_scope_warned = False
        self._follow_scope_warned = False
        self._sub_scope_warned = False
        self._whisper_scope_warned = False

    async def setup_hook(self) -> None:
        store.init_db()

        async def apply_tokens(access: str, refresh: str) -> None:
            persist_twitch_tokens(access, refresh)
            self._watch_scope_warned = False
            self._pin_scope_warned = False
            self._follow_scope_warned = False
            self._sub_scope_warned = False
            self._whisper_scope_warned = False
            await self.add_token(access, refresh)
            await self._subscribe_to_chat()
            print("SimpleCowBot token updated. Watch points can now read chatters if the new scopes were granted.")

        api_app = create_api_app(
            get_status=self.api_status,
            announce=self.send_channel_message,
            apply_tokens=apply_tokens,
        )
        await start_api_server(api_app)

        stored_access, stored_refresh = store.get_twitch_tokens()
        token = stored_access or BOT_TOKEN
        refresh = stored_refresh or BOT_REFRESH_TOKEN
        if token:
            try:
                await self.add_token(token, refresh)
            except Exception as exc:
                print(f"Saved Twitch token is invalid: {exc}")
                print("Open the dashboard Settings tab and click Authorize SimpleCowBot.")
        else:
            print("TWITCH_TOKEN is missing. Open the dashboard Settings tab and click Authorize SimpleCowBot.")

        users = await self.fetch_users(logins=[CHANNEL.lower()])
        if not users:
            raise RuntimeError(f"Could not find Twitch channel '{CHANNEL}'. Check TWITCH_CHANNEL in your .env file.")
        self.channel_user = users[0]
        await self._subscribe_to_chat()
        await self.add_component(CowCommands(self))
        names = ", ".join(sorted({cmd.name for cmd in self.unique_commands}))
        print(f"Loaded commands | {names}")
        self._scheduler_task = asyncio.create_task(self._run_scheduled_messages())
        self._watch_points_task = asyncio.create_task(self._run_watch_points())
        self._subscribe_task = asyncio.create_task(self._keep_chat_subscribed())

    def get_context(self, payload, *, cls=None):
        return super().get_context(payload, cls=cls or CowContext)

    def api_status(self) -> dict:
        bot_name = getattr(self.user, "name", BOT_NICK) if self.user else BOT_NICK
        return store.dashboard_snapshot(
            uptime=store.format_uptime(store.utc_now() - self.start_time),
            bot_name=bot_name or BOT_NICK,
            channel=CHANNEL,
            connected=self.channel_user is not None,
            stream_live=self._stream_live,
        )

    async def refresh_stream_live(self, *, force: bool = False) -> bool:
        if self.channel_user is None:
            self._stream_live = False
            self._current_stream = None
            return False
        now = time.monotonic()
        if not force and self._live_checked_at and now - self._live_checked_at < 45:
            return self._stream_live
        previous = self._stream_live
        try:
            stream = await self.channel_user.fetch_stream()
        except Exception as exc:
            print(f"Stream live check failed: {exc}")
        else:
            self._current_stream = stream
            self._stream_live = stream is not None
            if stream is not None:
                store.note_live_stream(str(stream.id))
        self._live_checked_at = now
        if not self._live_status_logged or previous != self._stream_live:
            print(f"Channel stream | {'live' if self._stream_live else 'offline'}")
            self._live_status_logged = True
        return self._stream_live

    async def fetch_current_stream(self):
        await self.refresh_stream_live(force=True)
        return self._current_stream

    def _chat_subscription_ids(self) -> list[str]:
        try:
            subs = self.websocket_subscriptions()
        except Exception:
            return []
        ids: list[str] = []
        for sub_id, sub in subs.items():
            kind = getattr(sub.type, "value", str(sub.type))
            if str(kind) == "channel.chat.message":
                ids.append(sub_id)
        return ids

    def _has_chat_subscription(self) -> bool:
        return bool(self._chat_subscription_ids())

    async def _prune_dead_eventsub_sockets(self) -> None:
        sockets = getattr(self, "_websockets", None) or {}
        for mapping in sockets.values():
            for session_id, websocket in list(mapping.items()):
                if getattr(websocket, "connected", False):
                    continue
                try:
                    await websocket.close()
                except Exception:
                    pass
                mapping.pop(session_id, None)

    async def _drop_extra_chat_subscriptions(self) -> None:
        ids = self._chat_subscription_ids()
        for extra_id in ids[1:]:
            try:
                await self.delete_websocket_subscription(extra_id, force=True)
                print(f"Removed extra chat EventSub | {extra_id}")
            except Exception as exc:
                print(f"Could not remove extra chat EventSub | {extra_id}: {exc}")

    async def _reset_eventsub_sockets(self) -> None:
        sockets = getattr(self, "_websockets", None) or {}
        for mapping in list(sockets.values()):
            for websocket in list(mapping.values()):
                try:
                    await websocket.close()
                except Exception:
                    pass
            mapping.clear()
        self._chat_subscribed = False

    async def _subscribe_to_chat(self) -> None:
        if self.channel_user is None:
            return
        async with self._chat_subscribe_lock:
            await self._prune_dead_eventsub_sockets()
            await self._drop_extra_chat_subscriptions()
            if self._has_chat_subscription():
                self._chat_subscribed = True
                return
            payload = eventsub.ChatMessageSubscription(
                broadcaster_user_id=self.channel_user.id,
                user_id=self.bot_id,
            )
            try:
                await self.subscribe_websocket(payload=payload, as_bot=True, token_for=self.bot_id)
                await self._drop_extra_chat_subscriptions()
                self._chat_subscribed = True
                print(f"Subscribed to chat for channel | {CHANNEL}")
            except HTTPException as exc:
                extra = f"{exc} {getattr(exc, 'extra', '')}".lower()
                if exc.status == 409:
                    self._chat_subscribed = True
                    return
                if exc.status == 400 and "session" in extra:
                    print("Chat EventSub session expired. Opening a new websocket...")
                    await self._reset_eventsub_sockets()
                    try:
                        await self.subscribe_websocket(payload=payload, as_bot=True, token_for=self.bot_id)
                        await self._drop_extra_chat_subscriptions()
                        self._chat_subscribed = True
                        print(f"Subscribed to chat for channel | {CHANNEL}")
                    except Exception as retry_exc:
                        print(f"Chat resubscribe failed: {retry_exc}")
                    return
                print(f"Chat subscription failed: {exc}")
                print(
                    "Chat needs a SimpleCowBot token with scopes user:read:chat, user:write:chat, user:bot, moderator:read:chatters. "
                    "Open the dashboard Settings tab and click Authorize SimpleCowBot. "
                    "In Cows_Are_Every_Where chat, /mod SimpleCowBot."
                )
            except Exception as exc:
                print(f"Chat subscription failed: {exc}")
                print(
                    "Chat needs a SimpleCowBot token with scopes user:read:chat, user:write:chat, user:bot, moderator:read:chatters. "
                    "Open the dashboard Settings tab and click Authorize SimpleCowBot. "
                    "In Cows_Are_Every_Where chat, /mod SimpleCowBot."
                )

    async def _keep_chat_subscribed(self) -> None:
        while True:
            await asyncio.sleep(15)
            if self.channel_user is None:
                continue
            await self._prune_dead_eventsub_sockets()
            if self._has_chat_subscription():
                await self._drop_extra_chat_subscriptions()
                self._chat_subscribed = True
                continue
            if self._chat_subscribed:
                print("Chat EventSub dropped. Retrying subscription...")
            else:
                print("Retrying chat subscription...")
            self._chat_subscribed = False
            await self._subscribe_to_chat()

    async def event_websocket_closed(self, payload) -> None:
        self._chat_subscribed = False
        print("EventSub websocket closed. Will resubscribe to chat.")

    async def event_subscription_revoked(self, payload) -> None:
        self._chat_subscribed = False
        print("Chat EventSub subscription revoked. Will resubscribe.")

    async def event_oauth_authorized(self, payload) -> None:
        access = payload["access_token"] if isinstance(payload, dict) else payload.access_token
        refresh = payload["refresh_token"] if isinstance(payload, dict) else payload.refresh_token
        await self.add_token(access, refresh)
        persist_twitch_tokens(access, refresh)
        print("SimpleCowBot authorized. Copy TWITCH_TOKEN and TWITCH_REFRESH_TOKEN onto Railway too.")
        if self.channel_user is None:
            users = await self.fetch_users(logins=[CHANNEL.lower()])
            if users:
                self.channel_user = users[0]
        await self._subscribe_to_chat()

    def _bot_access_token(self) -> str:
        stored, _refresh = store.get_twitch_tokens()
        return stored or os.getenv("TWITCH_TOKEN") or BOT_TOKEN

    async def fetch_channel_follow(self, user_id: str) -> tuple[datetime | None, str | None]:
        if self.channel_user is None:
            return None, "The bot is still connecting to the channel."
        try:
            result = await self.channel_user.fetch_followers(
                user=user_id,
                first=1,
                max_results=1,
                token_for=self.bot_id,
            )
            async for row in result.followers:
                followed_at = getattr(row, "followed_at", None)
                return followed_at, None
            return None, None
        except HTTPException as exc:
            if exc.status in {401, 403}:
                if not self._follow_scope_warned:
                    print(f"Followage lookup failed: {exc}")
                    print(
                        "Followage needs moderator:read:followers. "
                        "Open the dashboard Settings tab and click Authorize SimpleCowBot again, "
                        "and keep SimpleCowBot modded in the channel."
                    )
                    self._follow_scope_warned = True
                return None, (
                    "Followage needs a SimpleCowBot token with follower access. "
                    "Authorize SimpleCowBot from the dashboard and keep it modded."
                )
            print(f"Followage lookup failed: {exc}")
            return None, "Could not look up followage right now."
        except Exception as exc:
            print(f"Followage lookup failed: {exc}")
            return None, "Could not look up followage right now."

    async def fetch_follower_total(self) -> tuple[int | None, str | None]:
        if self.channel_user is None:
            return None, "The bot is still connecting to the channel."
        try:
            result = await self.channel_user.fetch_followers(
                first=1,
                max_results=1,
                token_for=self.bot_id,
            )
            return int(result.total), None
        except HTTPException as exc:
            if exc.status in {401, 403}:
                if not self._follow_scope_warned:
                    print(f"Follower count lookup failed: {exc}")
                    print(
                        "Follower count needs moderator:read:followers. "
                        "Open the dashboard Settings tab and click Authorize SimpleCowBot again, "
                        "and keep SimpleCowBot modded in the channel."
                    )
                    self._follow_scope_warned = True
                return None, (
                    "Follower count needs a SimpleCowBot token with follower access. "
                    "Authorize SimpleCowBot from the dashboard and keep it modded."
                )
            print(f"Follower count lookup failed: {exc}")
            return None, "Could not look up follower count right now."
        except Exception as exc:
            print(f"Follower count lookup failed: {exc}")
            return None, "Could not look up follower count right now."

    async def fetch_subscriber_total(self) -> tuple[int | None, str | None]:
        if self.channel_user is None:
            return None, "The bot is still connecting to the channel."
        try:
            result = await self.channel_user.fetch_broadcaster_subscriptions(
                first=1,
                max_results=1,
            )
            total = getattr(result, "total", None)
            if total is None:
                return None, "Twitch didn't return a subscriber count."
            return int(total), None
        except HTTPException as exc:
            if exc.status in {401, 403}:
                if not self._sub_scope_warned:
                    print(f"Subscriber count lookup failed: {exc}")
                    print(
                        "Twitch only shares subscriber count with the streamer's own login. "
                        "SimpleCowBot cannot read channel:read:subscriptions for this channel."
                    )
                    self._sub_scope_warned = True
                return None, (
                    "Twitch only shares subscriber count with the streamer's own login. "
                    "SimpleCowBot can't read it."
                )
            print(f"Subscriber count lookup failed: {exc}")
            return None, "Could not look up subscriber count right now."
        except Exception as exc:
            print(f"Subscriber count lookup failed: {exc}")
            return None, "Could not look up subscriber count right now."

    def _helix_error_message(self, status: int, body: str) -> str:
        text = (body or "").strip()
        try:
            data = json.loads(text) if text else {}
        except json.JSONDecodeError:
            data = {}
        if isinstance(data, dict):
            message = str(data.get("message") or "").strip()
            if message:
                return message
        return text or f"HTTP {status}"

    async def whisper_user(self, to_user, messages: list[str]) -> tuple[bool, str | None]:
        token = strip_oauth_prefix(self._bot_access_token())
        from_id = str(self.bot_id or "")
        to_id = str(getattr(to_user, "id", "") or "")
        if not to_id:
            login = store.normalize_user(getattr(to_user, "name", None))
            if login and login != "unknown":
                try:
                    users = await self.fetch_users(logins=[login])
                except Exception:
                    users = []
                if users:
                    to_id = str(users[0].id)
        if not token or not from_id:
            return False, "The bot is still connecting."
        if not to_id:
            return False, "Couldn't find your Twitch account to whisper."
        if from_id == to_id:
            return False, "I can't whisper myself."
        chunks = [str(message).strip()[:store.WHISPER_MAX_CHARS] for message in messages if str(message).strip()]
        if not chunks:
            return False, "No commands are enabled right now."
        headers = {
            "Authorization": f"Bearer {token}",
            "Client-Id": TWITCH_CLIENT_ID,
            "Content-Type": "application/json",
        }
        try:
            async with aiohttp.ClientSession() as session:
                for index, message in enumerate(chunks):
                    if index:
                        await asyncio.sleep(0.4)
                    async with session.post(
                        "https://api.twitch.tv/helix/whispers",
                        params={"from_user_id": from_id, "to_user_id": to_id},
                        headers=headers,
                        json={"message": message},
                    ) as resp:
                        body = await resp.text()
                        if resp.status in {200, 204}:
                            continue
                        detail = self._helix_error_message(resp.status, body)
                        print(f"Help whisper failed: {resp.status} {detail}")
                        lower = detail.lower()
                        if resp.status == 429:
                            return False, "Twitch is rate-limiting whispers right now. Try again in a bit."
                        if "phone" in lower:
                            return False, (
                                "SimpleCowBot needs a verified phone number on Twitch before it can send whispers."
                            )
                        if resp.status == 400:
                            return False, (
                                "Your Twitch privacy settings are blocking whispers from SimpleCowBot. "
                                "Turn off Block whispers from strangers, or follow SimpleCowBot, then try again."
                            )
                        if resp.status in {401, 403}:
                            if not self._whisper_scope_warned:
                                print(
                                    "Help whispers need user:manage:whispers. "
                                    "Open the dashboard Settings tab and click Authorize SimpleCowBot again. "
                                    "SimpleCowBot also needs a verified phone number on Twitch."
                                )
                                self._whisper_scope_warned = True
                            return False, (
                                "I couldn't whisper you. Authorize SimpleCowBot from the dashboard again, "
                                "and make sure SimpleCowBot has a verified phone number on Twitch."
                            )
                        return False, (
                            "I couldn't whisper you. Allow whispers from SimpleCowBot in Twitch privacy settings, then try again."
                        )
            self._whisper_scope_warned = False
            return True, None
        except Exception as exc:
            print(f"Help whisper failed: {exc}")
            return False, (
                "I couldn't whisper you. Allow whispers from SimpleCowBot in Twitch privacy settings, then try again."
            )

    async def _chat_pin_request(self, method: str, message_id: str) -> int:
        token = strip_oauth_prefix(self._bot_access_token())
        if not token or self.channel_user is None or not message_id:
            return 0
        params = {
            "broadcaster_id": str(self.channel_user.id),
            "moderator_id": str(self.bot_id),
            "message_id": message_id,
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Client-Id": TWITCH_CLIENT_ID,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.request(
                    method,
                    "https://api.twitch.tv/helix/chat/pins",
                    params=params,
                    headers=headers,
                ) as resp:
                    if resp.status >= 400 and resp.status not in {404, 409}:
                        body = await resp.text()
                        print(f"Giveaway pin {method} failed: {resp.status} {body}")
                        if resp.status == 401 and not self._pin_scope_warned:
                            print(
                                "Pinning giveaways needs moderator:manage:chat_messages. "
                                "Open the dashboard Settings tab and click Authorize SimpleCowBot again."
                            )
                            self._pin_scope_warned = True
                    return resp.status
        except Exception as exc:
            print(f"Giveaway pin {method} failed: {exc}")
            return 0

    async def pin_giveaway_message(self, message_id: str | None) -> None:
        await self.unpin_giveaway_message()
        pin_id = str(message_id or "").strip()
        if not pin_id:
            return
        status = await self._chat_pin_request("PUT", pin_id)
        if status in {204, 409}:
            store.set_giveaway_pin_id(pin_id)
            print(f"Giveaway message pinned | {pin_id}")

    async def unpin_giveaway_message(self) -> None:
        pin_id = store.get_giveaway_pin_id()
        if pin_id:
            await self._chat_pin_request("DELETE", pin_id)
        store.set_giveaway_pin_id("")

    async def send_channel_message(
        self,
        content: str,
        *,
        pin_giveaway: bool = False,
        unpin_giveaway: bool = False,
    ) -> None:
        if unpin_giveaway:
            await self.unpin_giveaway_message()
        if self.channel_user is None:
            return
        sent = None
        try:
            sent = await self.channel_user.send_message(content, sender=self.bot_id, token_for=self.bot_id)
        except Exception as exc:
            print(f"Failed to send channel message: {exc}")
            return
        if pin_giveaway:
            await self.pin_giveaway_message(getattr(sent, "id", None))

    async def _run_scheduled_messages(self) -> None:
        while True:
            try:
                live = await self.refresh_stream_live()
                if (
                    live
                    and self.channel_user is not None
                    and store.is_feature_enabled("scheduled_messages")
                ):
                    due = store.due_scheduled_messages()
                    if due:
                        row = due[0]
                        try:
                            await self.channel_user.send_message(
                                row["message"],
                                sender=self.bot_id,
                                token_for=self.bot_id,
                            )
                            store.mark_scheduled_sent(row["id"])
                            print(f"Scheduled message sent | #{row['id']}")
                        except Exception as exc:
                            print(f"Failed to send scheduled message #{row['id']}: {exc}")
            except Exception as exc:
                print(f"Scheduled message error: {exc}")
            await asyncio.sleep(15)

    def _note_watcher(self, user_name: str | None) -> None:
        name = store.normalize_user(user_name)
        if not name or name == "unknown":
            return
        now = time.monotonic()
        self._watch_chat_seen[name] = now
        if self._stream_live:
            self._mark_session_watcher(name)
        stale = now - (self._watch_interval_seconds * 3)
        if len(self._watch_chat_seen) > 4000:
            self._watch_chat_seen = {
                user: seen for user, seen in self._watch_chat_seen.items() if seen >= stale
            }

    def _watch_skip_names(self) -> set[str]:
        names = {store.normalize_user(BOT_NICK), store.normalize_user(CHANNEL)}
        bot_user = getattr(self.user, "name", None) if self.user else None
        if bot_user:
            names.add(store.normalize_user(bot_user))
        names.discard("")
        names.discard("unknown")
        return names

    def _can_claim_first(self, user_name: str | None) -> bool:
        name = store.normalize_user(user_name)
        if not name or name == "unknown":
            return False
        if name in self._watch_skip_names():
            return False
        if name in store.WATCH_POINT_BOTS:
            return False
        return True

    def _mark_session_watcher(self, user_name: str | None) -> None:
        name = store.normalize_user(user_name)
        if not name or name == "unknown" or name in self._watch_skip_names():
            return
        if name not in self._watch_session_started:
            self._watch_session_started[name] = store.utc_now()

    def session_watch_seconds(self, user_name: str) -> int:
        name = store.normalize_user(user_name)
        started = self._watch_session_started.get(name)
        if not started or not self._stream_live:
            return 0
        return max(int((store.utc_now() - started).total_seconds()), 0)

    async def _current_watchers(self) -> set[str] | None:
        if self.channel_user is None:
            return None
        try:
            chatters = await self.channel_user.fetch_chatters(
                moderator=self.bot_id,
                first=1000,
                max_results=5000,
            )
            names: set[str] = set()
            async for user in chatters.users:
                login = store.normalize_user(getattr(user, "name", None))
                if login and login != "unknown":
                    names.add(login)
            self._watch_scope_warned = False
            return names
        except Exception as exc:
            if not self._watch_scope_warned:
                print(f"Watch points chatters lookup failed: {exc}")
                print(
                    "Open the dashboard Settings tab and click Authorize SimpleCowBot "
                    "while logged into SimpleCowBot. Keep SimpleCowBot modded in the channel."
                )
                self._watch_scope_warned = True
            cutoff = time.monotonic() - self._watch_interval_seconds
            fallback = {user for user, seen in self._watch_chat_seen.items() if seen >= cutoff}
            return fallback or None

    async def _run_watch_points(self) -> None:
        present: set[str] = set()
        last_tick = 0.0
        while True:
            try:
                live = await self.refresh_stream_live()
                amount = store.get_watchtime_points()
                interval = store.get_watch_points_seconds()
                self._watch_interval_seconds = interval
                if (
                    not live
                    or self.channel_user is None
                    or not store.is_feature_enabled("economy")
                ):
                    present = set()
                    last_tick = 0.0
                    self._watch_session_started.clear()
                    await asyncio.sleep(15)
                    continue
                now = time.monotonic()
                if last_tick and now - last_tick < interval:
                    await asyncio.sleep(15)
                    continue
                watchers = await self._current_watchers()
                if watchers is None:
                    await asyncio.sleep(15)
                    continue
                for name in watchers:
                    self._mark_session_watcher(name)
                if last_tick and present:
                    elapsed = min(int(now - last_tick), interval * 2)
                    store.add_watch_seconds(
                        watchers & present,
                        elapsed,
                        skip=self._watch_skip_names(),
                    )
                    awarded = store.award_watch_points(
                        watchers & present,
                        amount,
                        skip=self._watch_skip_names(),
                    )
                    if awarded:
                        print(f"Watch points | {awarded} viewers +{amount}")
                present = watchers
                last_tick = now
            except Exception as exc:
                print(f"Watch points error: {exc}")
            await asyncio.sleep(15)

    async def event_ready(self):
        bot_name = getattr(self.user, "name", BOT_NICK) if self.user else BOT_NICK
        prefixes = " ".join(store.get_command_prefixes())
        print(f"Logged in as | {bot_name}")
        print(f"Connected to channel | {CHANNEL}")
        print(f"Command prefixes | {prefixes}")
        print("Scheduled messages post while the stream is live. Prefixes can be changed from the dashboard.")
        await self.refresh_stream_live()

    def _already_handled_message(self, message_id: str) -> bool:
        now = time.monotonic()
        self._recent_message_ids = {
            key: seen for key, seen in self._recent_message_ids.items() if now - seen < 30
        }
        if message_id in self._recent_message_ids:
            return True
        self._recent_message_ids[message_id] = now
        return False

    async def event_message(self, payload) -> None:
        chatter = getattr(payload.chatter, "name", None) or "unknown"
        text = getattr(payload, "text", "") or ""
        print(f"Chat | {chatter}: {text}")
        if any(text.startswith(prefix) for prefix in store.get_command_prefixes()):
            print(f"Command attempt | {chatter}: {text}")
        chatter_id = str(getattr(payload.chatter, "id", "") or "")
        if chatter_id and chatter_id == str(self.bot_id):
            return
        if getattr(payload, "source_broadcaster", None) is not None:
            return
        message_id = str(getattr(payload, "id", "") or "")
        if message_id and self._already_handled_message(message_id):
            return
        self._note_watcher(chatter)
        if self._stream_live and self._can_claim_first(chatter) and self._current_stream is not None:
            stream_id = getattr(self._current_stream, "id", None)
            if stream_id:
                store.try_claim_first(chatter, stream_id)
        await self.process_commands(payload)

    async def event_command_error(self, payload: commands.CommandErrorPayload) -> None:
        error = payload.exception
        ctx = payload.context
        if isinstance(error, commands.CommandNotFound):
            invoked = getattr(ctx, "invoked_with", None) or getattr(ctx, "_invoked_with", "unknown")
            if store.get_custom_command(str(invoked)):
                if not store.is_feature_enabled("custom_commands"):
                    await ctx.send(store.feature_off_message("custom_commands"))
                    return
                custom = store.use_custom_command(
                    str(invoked),
                    bypass_cooldown=is_mod_or_broadcaster(ctx),
                )
                if not custom:
                    return
                user = get_author_name(ctx)
                reply = store.render_custom_command(
                    custom["response"],
                    user=user,
                    channel=CHANNEL,
                    points=store.get_points(user),
                    count=custom["use_count"],
                    target=get_invoked_argument(ctx, str(invoked)) or user,
                )
                if reply:
                    await ctx.send(reply)
                return
            print(f"Unknown command | {invoked}")
            return
        if isinstance(error, commands.MissingRequiredArgument):
            command_name = getattr(ctx.command, "name", "command")
            param = getattr(error, "param", None)
            param_name = getattr(param, "name", None)
            if param_name:
                await ctx.send(f"Usage: {store.primary_prefix()}{command_name} <{param_name}>")
            else:
                await ctx.send(f"Usage: {store.primary_prefix()}{command_name}")
            return
        if isinstance(error, commands.BadArgument):
            await ctx.send("Invalid argument.")
            return
        await super().event_command_error(payload)



if __name__ == "__main__":
    store.init_db()
    bot = CowBot()
    bot.run(with_adapter=False)
