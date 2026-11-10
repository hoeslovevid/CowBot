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
- `?boost watch` / `?boost luck`: buy or extend a boost for everyone. Defaults to 50% of the buyer's current points, rounded up to a whole point (minimum 1). In **Settings → Economy**, change each boost's percentage or turn off its percentage toggle to set a flat point price. A price of 0 disables that boost in the selected mode.
- `?daily`
- `?gamble <amount|all>`
- `?roulette <amount>`
- `?giveaway` to enter, `?giveaway start <name>` / `end` / `reroll` for mods
- `?transfer <user> <amount>`
- `?poll`, `?raffle`, `?quote`, `?leaderboard`, `?uptime`, `?ping`
- Custom commands added from the dashboard, such as `?discord`

## Chat presentation

Shop, giveaway, boost, profile, points, gambling, poll, raffle, and queue replies use consistent category labels and small icons. All command prompts use the configured prefix.

The shop shows one compact page at a time, with up to three items. Use `?shop 2` (or `?shop page 2`) for another page and `?shop shoutout` for an item's description, price, approval requirements, stock, cooldown, and purchase command. Long catalogs are split without omitting item codes. Profiles show cosmetic time remaining, and owned titles are grouped into readable messages. Giveaway winners are split across messages when needed, preserving every mention.

Examples using `!` as the configured prefix:

```text
🎁 GIVEAWAY • OPEN — friday loot • Free entry → !giveaway • 2 winners
🏆 WINNER • friday loot • @luckycow • Congratulations!
```

Messages are plain text with readable separators, rather than Markdown or HTML. Generated messages stay below Twitch's message limit; customized commands and scheduled messages keep their existing wording.

## Points shop

Open **Points shop** in the dashboard to manage the catalog, approval queue, viewer collections, and searchable purchase history. Five starter items are included; you can edit their prices or disable them before making them available to your community. Items have a chat code, price, description, stock (−1 means unlimited), and per-viewer cooldown. Pending requests reserve stock. Purchase receipts retain the original price and reward if you edit an item later.

- **Streamer challenges:** preset items such as `meleeonly` or `pistolonly` work with just `?buy meleeonly`. The description supplies the challenge instructions, falling back to the item name if blank. Turn on **Viewer supplies the challenge** in the item editor when a buyer should instead provide their own challenge text (for example, `?buy challenge No healing next round!`). The starter “Pick a challenge” item keeps this option enabled. Existing custom challenge items default to preset behavior after the update. Approve the request, perform it on stream, then mark it completed. Reject a pending request or cancel an approved challenge to refund the original price and restore reserved stock.
- **Profile titles:** permanent unlocks, automatically equipped on purchase. Viewers can switch between their owned titles. They appear in CowBot profiles and celebration details, rather than Twitch badges.
- **On-screen shout-outs:** messages of up to 140 characters wait for approval before appearing on the celebration overlay. Rejecting them refunds points. The OBS source must already be loaded before approval; it does not replay older announcements after a refresh.
- **Community gifts:** award the configured point amount to each other viewer seen in chat or the watch-time viewer list during the last 15 minutes. The buyer and known bots are excluded. Receipts list every recipient. If nobody is eligible, no purchase is made and no points are spent. Viewer-list availability depends on the bot's existing Twitch permissions; recent chat activity is also tracked.
- **Temporary cosmetics:** mint, gold, violet, or rose styling for a viewer's boost and shop celebrations. Set the duration in minutes. Buying the same active color extends its time; buying another color replaces it. Expired cosmetics return to mint styling.

Chat commands (using your configured prefix):

```text
?shop
?buy challenge No healing next round!
?buy legendairy
?buy shoutout Good luck to the herd!
?buy herd-gift
?buy gold-glow
?profile
?profile viewername
?title Legendairy
?title none
```

Shout-outs, gift celebrations, and cosmetic previews use the existing **Boost celebrations** OBS source (800 × 450). Copy its URL from Settings. Shop data, approvals, titles, and cosmetic expiration times persist in the bot database. Turning off the Shop module or Economy stops new purchases; dashboard review and refunds remain available.

## Boost and gambling cooldowns

In **Settings → Economy**, set **Boost cooldown (seconds)** and **Gambling cooldown (seconds)** from 0 to 86,400. Defaults are 60 seconds for boosts and 30 seconds for gambling; 0 disables that cooldown.

Cooldowns apply per viewer. Watch and Luck purchases/extensions share one boost cooldown; gamble, roulette, and slots share one gambling cooldown. The two groups are independent, so buying a boost does not block gambling. Cooldowns begin only after a successful purchase or accepted wager, apply to wins and losses, and survive restarts. Blocked attempts show the remaining wait without spending points or extending a boost. Duration changes use the viewer's last successful purchase/wager, including uses while the cooldown was disabled.

## Give and remove points

On **Live**, below the leaderboard, **Adjust one viewer** lets you enter a username and an amount, then choose **Give points** or **Remove points**. Removing stops at zero; the result reports the amount actually removed.

Use **Give points to multiple viewers** to award the same amount to up to 100 viewers at once. Separate usernames with commas, spaces, or new lines. Names are case-insensitive, optional `@` prefixes are accepted, and duplicates receive points once. The entire list is validated before any balance changes. Each viewer receives the full entered amount (1 to 1,000,000 points).

## Reset points

On **Live**, below the leaderboard, use **Reset viewer to 0** to clear one viewer's balance, or **Reset everyone's points to 0** to clear all saved balances. Both actions ask for confirmation and cannot be undone. Watch time, daily-claim history, and watch-reward cooldowns are preserved. New viewers still receive the configured starting points, and existing viewers can earn points again normally.

## Boost celebrations

In **Settings → Boost celebrations**, copy the boost browser-source URL into a new OBS **Browser Source**, sized **800 × 450**. Keep **Shutdown source when not visible** unchecked. The source has a transparent background.

Each successful `?boost watch` or `?boost luck` purchase gets an eight-second animated celebration with the buyer's name, boost effect, point cost, and added duration. Extensions celebrate too, and purchases made close together play in order. Beneath the celebration, both active boosts have live countdowns and credit for their latest buyer. Everything hides when boosts expire.

Use **Preview celebration** to see sample boosts without spending points. Load the OBS source before purchases: opening or refreshing the source restores active timers without replaying old celebrations. Recent events survive a bot restart for up to two minutes; older announcements are discarded. This overlay is visual only and does not play sound.

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
