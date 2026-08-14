# Twitch Giveaway and Gambling Bot

A simple Twitch chat bot for giveaways, points, gambling, roulette, and user commands.

## Setup

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Create a `.env` file in the project root with:
   ```text
   TWITCH_CLIENT_ID=your_twitch_client_id
   TWITCH_CLIENT_SECRET=your_twitch_client_secret
   TWITCH_BOT_ID=your_bot_user_id
   TWITCH_TOKEN=your_user_access_token_here
   TWITCH_REFRESH_TOKEN=your_refresh_token_here
   TWITCH_NICK=your_bot_username
   TWITCH_CHANNEL=channel_name
   PREFIX=!
   ```

   The bot validates this file at startup and will error clearly if the file is missing or any of the required variables are not set.

   TwitchIO 3 uses EventSub instead of IRC. `TWITCH_TOKEN` must be a **user access token** for the bot account (not an `oauth:` IRC token) with `user:read:chat`, `user:write:chat`, and `user:bot`. A refresh token is strongly recommended so the bot can stay logged in. If tokens are missing or expired, start the bot and authorize it at `http://localhost:4343/oauth`.

3. Run the bot:
   ```bash
   python bot.py
   ```

4. Open the dashboard in your browser at:
   ```text
   http://localhost:5000
   ```

## Commands

- `!points` - Shows your current points.
- `!points <user>` - Shows another user’s points.
- `!daily` - Claim a daily reward once per day.
- `!gamble <amount|all>` - Gamble points with a ~50/50 win chance.
- `!roulette <amount>` - Play roulette with a chance to win 35x.
- `!giveaway start <name>` - Start a giveaway (`mod` or broadcaster should run this).
- `!giveaway enter` - Enter the currently active giveaway.
- `!giveaway end` - End the giveaway and choose a winner.
- `!transfer <user> <amount>` - Send points to another user.

## Notes

- The bot uses an SQLite database at `bot.db` to save points and giveaway entries.
- Make sure your bot account is added to the Twitch chat and has moderator or VIP permissions if necessary.
- Customize the bot logic in `bot.py` to add more features.
