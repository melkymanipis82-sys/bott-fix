# FBWatchBot — Telegram + Web UID Monitor

English-only version for Render. The service runs the Telegram bot and the static web dashboard from the same Python process.

## Render environment variables

- `BOT_TOKEN` — Telegram bot token
- `OWNER_IDS` — comma-separated Telegram admin IDs
- `USER_IDS` — optional comma-separated Telegram user IDs
- `CHECK_INTERVAL_SEC` — status polling interval in seconds (default: 300)

## Telegram commands

- `/start`
- `/help`
- `/myid`
- `/add` or `/add`
- `/list` or `/list`
- `/remove <uid>` or `/remove <uid>`
- `/grant <user_id> [user|admin]`
- `/revoke <user_id>`
- `/who`

English commands are recommended. Legacy aliases are kept so existing users do not have to change immediately.

## Web endpoints

- `/` — hacker-style dashboard
- `/healthz` — Render health endpoint
- `/api/uids` — tracked UID status data
- `/api/check?uid=<uid-or-facebook-url>` — live UID check

## Important fix

The previous `guard()` implementation declared `_decorator` with `async def`. That made `@guard()` produce a coroutine instead of a callable decorator. Python-Telegram-Bot then raised:

`TypeError: 'coroutine' object is not callable`

The fixed version uses a normal synchronous decorator around the async callback.


## Security note
The bot token is configured in the deployment code for this build. Do not publish this repository publicly. If the token has been exposed, revoke it with BotFather and replace it with a new token.
