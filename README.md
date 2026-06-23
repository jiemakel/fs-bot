# Playtime Bot

Signal group bot for managing children’s playtime with rule-based decisions and Microsoft Family Safety API calls.

## Overview

The bot is built around a research-informed playtime economy. Each child starts the week with a base allowance of playtime. From there, they can earn additional time through real-world activity, such as going outside for an hour and receiving a larger amount of playtime as a reward. This encourages healthy habits while giving children agency over how they spend their leisure time.

To keep play sessions balanced, the bot enforces a cap on the maximum length of continuous play. After reaching the continuous-play limit, a child needs to take a break before earning more playtime. Configurable blackout periods can create daily no-play windows, full rest days, or a predictable rhythm for when playtime is available.

Day to day, Playtime Bot runs in a shared Signal group. Children request playtime with messages like `30m`; the bot checks bank balance, blackout windows, active sessions, and break rules, then grants screen time through Microsoft Family Safety when the request is allowed. Admins can approve activity claims, adjust balances, switch profiles, end sessions, or block grants directly from the group.

## Docker Compose Setup

You need Docker Compose, a Signal account for the bot, and a Microsoft Family organizer account that can manage the children’s screen time.

1. Create the environment file:

```bash
cp .env.example .env
```

Set `PHONE_NUMBER` in `.env` to the Signal number that will act as the bot, then start only the Signal API:

```bash
docker compose up -d signal-cli-rest-api
```

2. Link the bot Signal account:

Open `http://localhost:8080/v1/qrcodelink?device_name=playtime-bot`, then scan the QR code from Signal under **Settings > Linked devices**. Add the bot account to the family Signal group.

3. Find the Signal group ID:

```bash
curl -s "http://127.0.0.1:8080/v1/groups/+1234567890"
```

Replace the phone number with `PHONE_NUMBER` from `.env`. Find the family group in the response and copy its `id` value into `SIGNAL_GROUP_ID`. It should look like `group.ckRzaEd4VmRzNnJaASA...`.

4. Find each Microsoft child ID:

Sign in at `https://account.microsoft.com/family` with the parent/organizer account used by `MS_FAMILY_EMAIL`. Open browser developer tools, go to the **Network** tab, filter for `roster`, and refresh the Family Safety page. Open the `/family/api/roster` response, find each child in `members`, and copy that member’s `puid` into `CHILD_n_MS_ID`.

5. Finish `.env`:

```env
SIGNAL_SERVICE=signal-cli-rest-api:8080
PHONE_NUMBER=+1234567890
SIGNAL_GROUP_ID=group.ckRzaEd4VmRzNnJaASA...

ADMIN_1_PHONE=+1234567892

CHILD_1_PHONE=+1234567891
CHILD_1_MS_ID=1234567890123456
CHILD_1_NAME=Alice

MS_FAMILY_EMAIL=parent@example.com
MS_FAMILY_PASSWORD=your_password_here

TZ=Europe/Helsinki
DATA_DIR=./data
BOT_LANGUAGE=en
```

Optional rule settings in `.env` control the weekly allowance, max bank, continuous-play cap, recovery rate, and blackout windows:

```env
WEEKLY_ADDITION_TIME=14h
MAX_BANK_TIME=42h
ACCRUED_PLAYTIME_MAX_TIME=3h
BREAK_RECOVERY_RATE=3.0
BLACKOUT_PERIOD_MON=00:00-08:00,20:30-24:00
```

Use `BLACKOUT_PERIOD_TUE` through `BLACKOUT_PERIOD_SUN` for the other days. Use `00:00-24:00` to block a full day.

6. Start the full stack:

```bash
docker compose up -d
docker compose logs -f playtime-bot
```

Bot data is persisted in `./data`; the linked Signal identity is persisted in the `signal-cli-data` Docker volume. The Signal REST API is exposed on local port `8080`; keep it private, because anyone who can reach it can send messages as the linked Signal account.

For the full command list, runtime behavior, and configuration reference, see [REFERENCE.md](REFERENCE.md).

## Local Development

Install dependencies and run the bot directly:

```bash
uv sync
uv run python main.py
```

When running outside Docker, use the host address for Signal:

```env
SIGNAL_SERVICE=127.0.0.1:8080
```

Run tests:

```bash
uv run pytest
```

## Documentation

For additional environment details, command syntax, runtime behavior, scheduling, storage, and source layout, see [REFERENCE.md](REFERENCE.md).
