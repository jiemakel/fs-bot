# Playtime Bot

Signal group bot for managing children’s playtime with rule-based decisions and Microsoft Family Safety API calls.

## Design

The bot is built around a research-informed playtime economy. Each child starts the week with a base allowance of playtime. From there, they can earn additional time through real-world activity — for example, going outside for an hour might grant three hours of playtime as a reward. This encourages healthy habits while giving children agency over how they spend their leisure time.

To keep play sessions balanced, the bot enforces a cap on the maximum length of continuous play. Research shows that regular breaks help maintain mental focus and emotional balance, so after reaching the continuous-play limit, a child needs to take a short break before earning more playtime. This prevents long unbroken sessions while still allowing flexibility in how breaks are taken.

Weeks are structured through configurable blackout periods set by an admin. These can be used to create daily no-play windows (e.g., a three-hour break in the afternoon), designate complete rest days, or simply define when playtime is available (such as starting at 08:00 and ending at 21:00). Blackout periods give families a predictable rhythm without requiring constant manual intervention.

Once the rules are in place, the bot handles most day-to-day interactions automatically. Admins are only asked to approve activity-granted playtime bonuses. That said, admins can always interject — manually granting or blocking playtime, adjusting limits, or overriding the bot's decisions whenever the situation calls for it.

## What It Does

- Supports multiple children and admins, each configured with phone numbers and Microsoft Family IDs
- Tracks per-child playtime bank, active sessions, accrued playtime debt, and weekly pace metrics
- Applies blackout periods, accrued-playtime caps, and grant-block modes through configurable rule profiles
- Grants screen time through Microsoft Family Safety when rules pass, with smart partial grants when a full request can't be fulfilled
- Lets children submit activity claims (e.g., `30m cleaned my room`) for admin approval, turning chores into earned playtime
- Notifies the group when sessions expire, breaks complete, or weekly rollovers happen
- Sends all messages to a single Signal group for shared family visibility
- Supports English and Finnish localization

## How It Works

A child sends a duration request (e.g., `30m` or `1h`) in the Signal group. The bot evaluates the request against the rule engine — checking blackout periods, accrued playtime limits, and bank balance. If the request passes, the bot calls the Microsoft Family Safety API to grant screen time on the child's device. Only on a successful API call does the bot update its local state (bank balance, session tracking, accrued playtime).

If a request can't be fully granted but some time is available, the bot performs a **partial grant** — giving what it can and explaining which caps applied (bank, accrued playtime, or upcoming blackout). This avoids hard denials and teaches children to work within available limits.

When a child finishes playing, they can send `end` to complete the session early. The bot will block screen time on the device and refund any unused time back to the bank.

## Key Features

**Playtime Economy** — Children earn a weekly allowance and can gain more through activity claims. A child sends `30m played guitar` to submit a claim, which an admin reviews and approves with `ack`. This turns chores and healthy activities into earned playtime without constant admin overhead.

**Smart Break System** — After reaching the continuous-play cap, accrued playtime recovers during breaks at a configurable rate (default 3×). If a child interrupts a break, they get a 1-minute grace window to stop and resume recovery — preventing accidental resets from quick check-ins.

**Rule Profiles** — Different children can have different rules. Create named profiles with `profile define` and assign them with `profile use`. Each profile can have its own weekly allowance, bank cap, accrued playtime limits, recovery rate, and blackout schedule.

**Pace Tracking** — The status display shows how a child is pacing their weekly allowance relative to remaining playable time, with a pace ratio that indicates whether they're spending faster or slower than the week allows.

**Grant Block Mode** — Admins can toggle a block mode (`block <name>`) that denies all playtime requests and immediately ends any active session. Useful for enforcing unexpected breaks or bedtime.

**Automatic Notifications** — The bot posts to the group when sessions naturally expire, when break recovery completes, and during weekly rollovers (Monday 00:01). Everyone stays informed without asking.

**Docker Support** — Full `docker-compose.yml` included, bundling the bot and `signal-cli-rest-api` in a single stack for easy deployment.

## Important Behavior

- Grant requests are approved only if rules pass.
- Local state (bank/session/accrued-playtime tracking) is updated only after a successful Microsoft API grant.
- If Microsoft grant/authentication fails, the request is rejected and local state is unchanged.
- Ending a session (`end`) first attempts an immediate Microsoft block; if block fails, the session is not ended locally.
- Activity claims are pending until an admin runs `ack` — they don't grant time automatically.
- When an admin manually adjusts a child's bank balance, pending claims are auto-cleared to avoid double-counting.
- Microsoft API authentication is handled automatically, with retry on session expiry.

## Requirements

- Python 3.12+
- `uv` (recommended) or `pip`
- Running `signal-cli-rest-api` (json-rpc mode)
- Microsoft Family account credentials for live API use

## Docker Deployment

The project includes a `docker-compose.yml` that bundles the bot and `signal-cli-rest-api` together:

```bash
# Configure environment variables in a `.env` file
docker compose up -d
```

All data (databases, Signal state, attachments) is persisted in the `./data` directory.

## Quick Start (Local)

1. Install dependencies:

```bash
uv sync
```

2. Configure environment variables in a `.env` file (see `.env.example` as a template). For a complete list of settings and options, refer to the [Reference Guide](REFERENCE.md).

3. Run the application:

```bash
uv run python main.py
```

## Documentation

For comprehensive documentation, see the [Configuration and Commands Reference](REFERENCE.md).

## Development

Run tests to verify local changes:

```bash
uv run pytest
```
