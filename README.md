# Playtime Bot

Signal group bot for managing children’s playtime with rule-based decisions and Microsoft Family Safety API calls.

## Overview

The bot is built around a research-informed playtime economy. Profiles define named time banks with maximum balances and either weekly additions or automatic recovery after a completed break. A child needs enough time in every bank to play. Activity rewards go to the profile's first bank.

To keep play sessions balanced, the bot enforces a cap on the maximum length of continuous play. After reaching the continuous-play limit, a child needs to take a break before earning more playtime. Configurable blackout periods can create daily no-play windows, full rest days, or a predictable rhythm for when playtime is available.

Day to day, Playtime Bot runs in a shared Signal group. Children request playtime with messages like `30m`; the bot checks every active bank, blackout windows, active sessions, and break rules, then grants screen time through Microsoft Family Safety when the request is allowed. Admins can approve activity claims, adjust named balances, switch profiles, end sessions, or block grants directly from the group.

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

Rule profiles are created through admin commands, not environment settings. A fresh installation will not grant playtime until an admin defines and assigns a profile. Profiles contain one or more named banks; every bank must fund a playtime request. The first bank is where activity rewards and bank commands go by default.

6. Start the full stack:

```bash
docker compose up -d
docker compose logs -f playtime-bot
```

Then define and assign a profile in Signal. This example recreates a long-term earned-time bank plus a 30-hour weekly allowance:

```text
profile define normal {
  "banks": {
    "earned": {"weekly_addition": "14h", "max_balance": "42h"},
    "weekly": {"weekly_addition": "30h", "max_balance": "30h"},
    "recovery": {"recovery_rate": 3.0, "max_balance": "3h"}
  },
  "blackouts": [
    {"days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"], "start": "00:00", "end": "08:00"},
    {"days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"], "start": "20:30", "end": "24:00"}
  ]
}
profile use normal Alice
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
