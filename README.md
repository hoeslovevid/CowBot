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

Railway does not run Compose files. Create **two services** from the same GitHub repo. Both use `python start.py`; the process is chosen by `APP_ROLE` (or the Railway service name).

### 1. Bot service

- Source: this repo
- Rename the service to `bot`
- Add a volume mounted at `/data`
- Do not generate a public domain
- Variables:

  ```text
  APP_ROLE=bot
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

Other services reach it at `http://bot.railway.internal:$PORT`.

### 2. Web service

- Source: this repo
- Rename the service to `web`
- Generate a public domain
- Variables:

  ```text
  APP_ROLE=web
  BOT_API_URL=http://${{bot.RAILWAY_PRIVATE_DOMAIN}}:${{bot.PORT}}
  API_SECRET=${{bot.API_SECRET}}
  FLASK_SECRET_KEY=a-long-random-string
  ```

## Chat commands

- `?points` / `?points <user>`
- `?daily`
- `?gamble <amount|all>`
- `?roulette <amount>`
- `?giveaway start <name>` / `enter` / `end`
- `?transfer <user> <amount>`
- `?poll`, `?raffle`, `?quote`, `?leaderboard`, `?uptime`, `?ping`
