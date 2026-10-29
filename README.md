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

## Text to speech

CowBot generates speech on the bot host and plays it through a dedicated OBS browser source. It does not send viewer text to an external speech provider and does not need a TTS API key. Docker/Railway uses **eSpeak NG** (included in the image); local Windows runs use the installed **Windows speech** voices. For Linux without Docker, install `espeak-ng` before starting the bot. The initial voices are basic system voices.

1. Open **Text to speech** in the dashboard and select **Connect streamer**. Authorize the account configured in `TWITCH_CHANNEL`, rather than the separate bot account. This requests `bits:read` and `channel:read:redemptions`. The existing Twitch app redirect URL (`/oauth/callback`) is reused. Streamer tokens are stored separately from bot tokens and refreshed automatically.
2. On Twitch, create a custom Channel Point reward such as **Text to speech**, with **Require Viewer to Enter Text** enabled. In CowBot, click **Load Twitch rewards**, select that reward, and enable the Channel Point trigger. Alternatively, enable **Read Cheer messages** and set a minimum Bits amount. This version handles regular Cheers; custom Power-ups, Extension purchases, and third-party TTS purchases are not triggers.
3. Choose a voice, volume, speed, character limit, per-viewer cooldown, and blocked phrases. **Require moderator approval** is on by default. Enable viewer TTS and save settings. Changing approval settings affects new messages; existing pending messages still need approval.
4. Copy the private **OBS audio source** URL into an OBS **Browser Source**. Enable **Control audio via OBS**. Leave **Shutdown source when not visible** unchecked, and reuse this existing source across scenes. Use **Monitor and Output** in Advanced Audio Properties to hear speech locally as well as on stream. No OBS plugin is required.
5. Click **Queue test speech**. A test bypasses the enable switch and approval so setup can be checked before allowing viewer redemptions, but respects pause and text filters. Speech needs a connected player: use **Open browser player** for a quick local test, then click **Enable audio** if your browser asks. Keep that tab open while listening. For audio on stream, connect the OBS source instead. Each message is claimed by one player, so close the browser player when switching playback to OBS.

If a message stays **queued** with **Waiting for player**, the speech engine is waiting for an audio player to connect. Opening only the dashboard does not start playback. The queue help explains whether playback is waiting for a player, paused, or waiting for moderator approval.

The dashboard supports approval, rejection, pause/resume (including current audio), skip, clear, replay, player health, and recent history. One player claims each message, so two browser sources cannot both speak it. Duplicate Twitch notifications are ignored. If a player disconnects before confirming completion, that message eventually fails rather than automatically repeating; use Replay to retry it. Queued messages persist through bot restarts. Keep the Railway database volume mounted for settings and history persistence.

Rejecting, filtering, clearing, or skipping speech **does not refund Bits or Channel Points**. Manage reward fulfillment/refunds in Twitch. A redemption canceled in Twitch is removed from CowBot's pending/playing queue when Twitch sends its update. Text that exceeds the limit or matches a blocked phrase, and repeats within the cooldown, are rejected and recorded in history. Keep the OBS URL private; it grants access to this speech player. Speech history is retained for seven days (the dashboard shows the latest 20 terminal messages), and generated audio is cleaned up after a day.

Run the queue, API, and dashboard checks with:

```bash
python -m unittest discover -s tests -v
```

With Node.js installed, also check the browser-player state transitions with `node --test tests/tts-player.test.js`.
