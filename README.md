# CowBot

Twitch chat bot for points, giveaways, polls, raffles, and quotes, plus a web control room. Chat and the dashboard can run in one process; the site talks to the bot over a local HTTP API.

## Local Docker

1. Copy `.env.example` to `.env` and fill in your Twitch credentials plus `API_SECRET`.
2. Start both services:

   ```bash
   docker compose up --build
   ```

3. Open the dashboard at [http://localhost:5000](http://localhost:5000).

The `web` container calls the `bot` container at `http://bot:8080`. Starting or ending a poll, raffle, or giveaway from the site posts to the bot API, which updates the database and sends the message in Twitch chat.

To authorize the bot account locally, also open [http://localhost:4343/oauth](http://localhost:4343/oauth) after the bot is running.

## Without Docker

Run the two processes separately from the project root:

```bash
python bot.py
python dashboard.py
```

Or run both together:

```bash
python start.py
```

`dashboard.py` expects `BOT_API_URL=http://127.0.0.1:8080`.

## Railway

Use **one service** from this GitHub repo. `python start.py` runs the Twitch bot and the dashboard together.

- Generate a public domain (this is the dashboard)
- Add a volume mounted at `/data`. Without it, giveaways, points, and quotes reset on every redeploy.
- Variables:

  ```text
  APP_ROLE=all
  TWITCH_CLIENT_ID
  TWITCH_CLIENT_SECRET
  TWITCH_BOT_ID
  TWITCH_TOKEN
  TWITCH_REFRESH_TOKEN
  TWITCH_NICK
  TWITCH_CHANNEL
  PREFIX=?
  API_SECRET
  FLASK_SECRET_KEY
  DB_PATH=/data/bot.db
  ```

The public `PORT` serves the dashboard. The bot API uses `127.0.0.1:8090` when `PORT` is `8080`, so the two do not collide. You can leave `BOT_API_URL` unset.

If you still have a second Railway service from the old split setup, you can delete it after this service is healthy.

## Chat commands

Prefixes are set in the dashboard. Defaults are `?` (and whatever you add, such as `!`). `/` will not work; Twitch keeps that for its own commands.

- `?points` / `?points <user>`
- `?daily`
- `?gamble <amount|all>`
- `?roulette <amount>`
- `?giveaway` to enter, `?giveaway start <name>` / `end` / `reroll` for mods
- `?transfer <user> <amount>`
- `?poll`, `?raffle`, `?quote`, `?leaderboard`, `?uptime`, `?ping`
- Custom commands added from the dashboard, such as `?discord`
