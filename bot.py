import os
import random
import asyncio
import time

from dotenv import load_dotenv, find_dotenv
from twitchio import Scopes, eventsub
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
    env_path = find_dotenv() or os.path.join(os.getcwd(), ".env")
    upsert_env_value(env_path, "TWITCH_TOKEN", access_token)
    upsert_env_value(env_path, "TWITCH_REFRESH_TOKEN", refresh_token)
    os.environ["TWITCH_TOKEN"] = access_token
    os.environ["TWITCH_REFRESH_TOKEN"] = refresh_token
    print(f"Saved new Twitch tokens to {env_path}")


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

    @commands.command(name="lurk")
    async def lurk(self, ctx: commands.Context):
        if not await require_command(ctx, "lurk"):
            return
        await ctx.send(store.render_lurk_message(get_author_mention(ctx)))

    @commands.command(name="points")
    async def points(self, ctx: commands.Context, *, target: str | None = None):
        if not await require_command(ctx, "points"):
            return
        target = store.normalize_user(target or get_author_name(ctx))
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
        if amount.lower() == "all":
            amount_value = current
        else:
            if not amount.isdigit():
                await ctx.send(f"Usage: {store.primary_prefix()}gamble <amount|all>")
                return
            amount_value = int(amount)

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
        if not amount.isdigit():
            await ctx.send(f"Usage: {store.primary_prefix()}roulette <amount>")
            return
        wager = int(amount)
        current = store.get_points(user)
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
            await ctx.send(f"Giveaway '{name}' started! Type {store.primary_prefix()}giveaway to join.{extra}")
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
            ),
        )
        self.start_time = store.utc_now()
        self.channel_user = None
        self._chat_subscribed = False
        self._stream_live = False
        self._live_checked_at = 0.0
        self._live_status_logged = False

    async def setup_hook(self) -> None:
        store.init_db()
        api_app = create_api_app(get_status=self.api_status, announce=self.send_channel_message)
        await start_api_server(api_app)

        if BOT_TOKEN:
            try:
                await self.add_token(BOT_TOKEN, BOT_REFRESH_TOKEN)
            except Exception as exc:
                print(f"Saved Twitch token is invalid: {exc}")
                print(
                    "Open http://localhost:4343/oauth while logged into SimpleCowBot to authorize again. "
                    "The Twitch app redirect URL must be http://localhost:4343/oauth/callback"
                )
        else:
            print(
                "TWITCH_TOKEN is missing. Open http://localhost:4343/oauth while logged into SimpleCowBot. "
                "The Twitch app redirect URL must be http://localhost:4343/oauth/callback"
            )

        users = await self.fetch_users(logins=[CHANNEL.lower()])
        if not users:
            raise RuntimeError(f"Could not find Twitch channel '{CHANNEL}'. Check TWITCH_CHANNEL in your .env file.")
        self.channel_user = users[0]
        await self._subscribe_to_chat()
        await self.add_component(CowCommands(self))
        names = ", ".join(sorted({cmd.name for cmd in self.unique_commands}))
        print(f"Loaded commands | {names}")
        self._scheduler_task = asyncio.create_task(self._run_scheduled_messages())
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

    async def refresh_stream_live(self) -> bool:
        if self.channel_user is None:
            self._stream_live = False
            return False
        now = time.monotonic()
        if self._live_checked_at and now - self._live_checked_at < 45:
            return self._stream_live
        previous = self._stream_live
        try:
            live = False
            async for _stream in self.fetch_streams(
                user_ids=[self.channel_user.id],
                first=1,
                max_results=1,
            ):
                live = True
                break
            self._stream_live = live
        except Exception as exc:
            print(f"Stream live check failed: {exc}")
        self._live_checked_at = now
        if not self._live_status_logged or previous != self._stream_live:
            print(f"Channel stream | {'live' if self._stream_live else 'offline'}")
            self._live_status_logged = True
        return self._stream_live

    async def _subscribe_to_chat(self) -> None:
        if self._chat_subscribed or self.channel_user is None:
            return
        payload = eventsub.ChatMessageSubscription(
            broadcaster_user_id=self.channel_user.id,
            user_id=self.bot_id,
        )
        try:
            await self.subscribe_websocket(payload=payload, as_bot=True, token_for=self.bot_id)
            self._chat_subscribed = True
            print(f"Subscribed to chat for channel | {CHANNEL}")
        except Exception as exc:
            print(f"Chat subscription failed: {exc}")
            print(
                "Chat needs TWITCH_TOKEN for SimpleCowBot with scopes user:read:chat, user:write:chat, user:bot. "
                "Copy TWITCH_TOKEN and TWITCH_REFRESH_TOKEN onto this Railway service. "
                "In Cows_Are_Every_Where chat, /mod SimpleCowBot."
            )

    async def _keep_chat_subscribed(self) -> None:
        while True:
            await asyncio.sleep(20)
            if not self._chat_subscribed:
                print("Retrying chat subscription...")
                await self._subscribe_to_chat()

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

    async def send_channel_message(self, content: str) -> None:
        if self.channel_user is None:
            return
        try:
            await self.channel_user.send_message(content, sender=self.bot_id, token_for=self.bot_id)
        except Exception as exc:
            print(f"Failed to send channel message: {exc}")

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

    async def event_ready(self):
        bot_name = getattr(self.user, "name", BOT_NICK) if self.user else BOT_NICK
        prefixes = " ".join(store.get_command_prefixes())
        print(f"Logged in as | {bot_name}")
        print(f"Connected to channel | {CHANNEL}")
        print(f"Command prefixes | {prefixes}")
        print("Scheduled messages post while the stream is live. Prefixes can be changed from the dashboard.")
        await self.refresh_stream_live()

    async def event_message(self, payload) -> None:
        chatter = getattr(payload.chatter, "name", None) or "unknown"
        text = getattr(payload, "text", "") or ""
        print(f"Chat | {chatter}: {text}")
        if any(text.startswith(prefix) for prefix in store.get_command_prefixes()):
            print(f"Command attempt | {chatter}: {text}")
        chatter_id = str(getattr(payload.chatter, "id", "") or "")
        if chatter_id and chatter_id == str(self.bot_id):
            return
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
    bot.run()
