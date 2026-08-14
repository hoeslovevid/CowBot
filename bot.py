import os
import random
import asyncio

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
        load_dotenv(env_path)

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

    async def setup_hook(self) -> None:
        store.init_db()
        api_app = create_api_app(get_status=self.api_status, announce=self.send_channel_message)
        await start_api_server(api_app)

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
        self._scheduler_task = asyncio.create_task(self._run_scheduled_messages())

    def get_context(self, payload, *, cls=None):
        return super().get_context(payload, cls=cls or CowContext)

    def api_status(self) -> dict:
        bot_name = getattr(self.user, "name", BOT_NICK) if self.user else BOT_NICK
        return store.dashboard_snapshot(
            uptime=store.format_uptime(store.utc_now() - self.start_time),
            bot_name=bot_name or BOT_NICK,
            channel=CHANNEL,
            connected=self.channel_user is not None,
        )

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

    async def _run_scheduled_messages(self) -> None:
        while True:
            try:
                if self.channel_user is not None:
                    for row in store.due_scheduled_messages():
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
        print("Scheduled messages and prefixes can be changed from the dashboard.")

    async def event_message(self, payload) -> None:
        chatter = getattr(payload.chatter, "name", None) or "unknown"
        text = getattr(payload, "text", "") or ""
        print(f"Chat | {chatter}: {text}")
        chatter_id = str(getattr(payload.chatter, "id", "") or "")
        if chatter_id and chatter_id == str(self.bot_id):
            return
        await self.process_commands(payload)

    async def event_command_error(self, payload: commands.CommandErrorPayload) -> None:
        error = payload.exception
        ctx = payload.context
        if isinstance(error, commands.CommandNotFound):
            invoked = getattr(ctx, "invoked_with", None) or getattr(ctx, "_invoked_with", "unknown")
            print(f"Unknown command | {invoked}")
            return
        if isinstance(error, commands.MissingRequiredArgument):
            command_name = getattr(ctx.command, "name", "command")
            await ctx.send(f"Missing argument for {store.primary_prefix()}{command_name}.")
            return
        if isinstance(error, commands.BadArgument):
            await ctx.send("Invalid argument.")
            return
        await super().event_command_error(payload)

    @commands.command(name="uptime")
    async def uptime(self, ctx: commands.Context):
        await ctx.send(f"Bot uptime: {store.format_uptime(store.utc_now() - self.start_time)}.")

    @commands.command(name="ping")
    async def ping(self, ctx: commands.Context):
        await ctx.send("Pong")

    @commands.command(name="points")
    async def points(self, ctx: commands.Context, *, target: str | None = None):
        target = store.normalize_user(target or get_author_name(ctx))
        points = store.get_points(target)
        await ctx.send(f"{target} has {points} points.")

    @commands.command(name="daily")
    async def daily(self, ctx: commands.Context):
        author_name = get_author_name(ctx)
        claimed, earned, new_total = store.try_claim_daily(author_name)
        if claimed:
            await ctx.send(f"{author_name}, you claimed your daily reward and earned {earned} points! Total: {new_total}.")
        else:
            await ctx.send(f"{author_name}, you already claimed your daily reward. Come back tomorrow.")

    @commands.command(name="gamble")
    async def gamble(self, ctx: commands.Context, amount: str):
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
    async def giveaway(self, ctx: commands.Context, action: str, *, name: str | None = None):
        action = action.lower()
        if action == "start":
            if not is_mod_or_broadcaster(ctx):
                await ctx.send("Only mods and the broadcaster can start giveaways.")
                return
            if not name:
                await ctx.send(f"Usage: {store.primary_prefix()}giveaway start <name>")
                return
            success, result = store.start_giveaway(name)
            if not success:
                await ctx.send(result or "Could not start giveaway.")
                return
            await ctx.send(f"Giveaway '{name}' started! Type {store.primary_prefix()}giveaway enter to join.")
        elif action == "enter":
            success, result = store.enter_giveaway(get_author_name(ctx))
            if not success:
                await ctx.send(result or "Could not enter giveaway.")
                return
            await ctx.send(f"{result} entered the giveaway '{store.get_active_giveaway()}'.")
        elif action == "end":
            if not is_mod_or_broadcaster(ctx):
                await ctx.send("Only mods and the broadcaster can end giveaways.")
                return
            winner, giveaway_name = store.finish_giveaway()
            if winner and giveaway_name:
                await ctx.send(f"Giveaway '{giveaway_name}' ended! The winner is {winner}.")
            else:
                await ctx.send(giveaway_name or "No giveaway is currently active.")
        else:
            await ctx.send(
                f"Giveaway commands: {store.primary_prefix()}giveaway start <name>, "
                f"{store.primary_prefix()}giveaway enter, {store.primary_prefix()}giveaway end"
            )

    @commands.command(name="quote")
    async def quote(self, ctx: commands.Context, *, text: str | None = None):
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
        rows = store.get_leaderboard(5)
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
    async def raffle(self, ctx: commands.Context, action: str, *, args: str | None = None):
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


if __name__ == "__main__":
    store.init_db()
    bot = CowBot()
    bot.run()
