# CowBot

Twitch chat bot for points, giveaways, polls, raffles, and quotes, plus a web control room. The website and bot run as two services and talk over an internal HTTP API.

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

`dashboard.py` expects `BOT_API_URL=http://127.0.0.1:8080`.

## Railway

Railway does not run Compose files. Create **two services** from the same GitHub repo.

### 1. Bot service

- Source: this repo
- Config file: `railway.bot.toml` (set `RAILWAY_CONFIG_FILE=railway.bot.toml` if asked)
- Start command: `python bot.py`
- Add a volume mounted at `/data`
- Variables:

  ```text
  TWITCH_CLIENT_ID
  TWITCH_CLIENT_SECRET
  TWITCH_BOT_ID
  TWITCH_TOKEN
  TWITCH_REFRESH_TOKEN
  TWITCH_NICK
  TWITCH_CHANNEL
  PREFIX=?
  API_SECRET
  DB_PATH=/data/bot.db
  BOT_API_HOST=0.0.0.0
  ```

Do not generate a public domain for the bot service. Other services reach it at `http://bot.railway.internal:$PORT`.

### 2. Web service

- Source: this repo
- Uses `railway.toml`
- Start command: `python dashboard.py`
- Generate a public domain
- Variables:

  ```text
  BOT_API_URL=http://${{bot.RAILWAY_PRIVATE_DOMAIN}}:${{bot.PORT}}
  API_SECRET=${{bot.API_SECRET}}
  FLASK_SECRET_KEY=a-long-random-string
  ```

Rename the Railway services to `bot` and `web` so the private DNS matches, or change `BOT_API_URL` to your bot service name.

## Chat commands

- `?points` / `?points <user>`
- `?daily`
- `?gamble <amount|all>`
- `?roulette <amount>`
- `?giveaway start <name>` / `enter` / `end`
- `?transfer <user> <amount>`
- `?poll`, `?raffle`, `?quote`, `?leaderboard`, `?uptime`
