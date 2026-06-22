# Playtime Bot

Signal group bot for managing children’s playtime with rule-based decisions and Microsoft Family Safety API calls.

## What It Does

- Supports multiple children and admins
- Tracks per-child playtime bank, active sessions, and accrued playtime
- Applies blackout periods and accrued-playtime/session limits
- Automatically grants time through Microsoft Family Safety when rules pass
- Sends all responses to one Signal group for shared visibility

## Important Behavior

- Grant requests are approved only if rules pass.
- Local state (bank/session/accrued-playtime tracking) is updated only after a successful Microsoft API grant.
- If Microsoft grant/authentication fails, the request is rejected and local state is unchanged.
- Ending a session (`end`) first attempts an immediate Microsoft block; if block fails, session is not ended locally.

## Requirements

- Python 3.12+
- `uv` (recommended) or `pip`
- Running `signal-cli-rest-api` (json-rpc mode)
- Microsoft Family account credentials for live API use

## Quick Start

1. Install dependencies:

```bash
uv sync
```

2. Configure environment variables (see full reference below).
3. Run:

```bash
uv run python main.py
```

## Configuration

### Signal

- `SIGNAL_SERVICE` e.g. `127.0.0.1:8080`
- `PHONE_NUMBER` bot account phone number
- `SIGNAL_GROUP_ID` target Signal group ID

### Admins (indexed)

- `ADMIN_1_PHONE`, `ADMIN_2_PHONE`, ...

### Children (indexed)

- `CHILD_1_PHONE`, `CHILD_2_PHONE`, ...
- `CHILD_1_MS_ID`, `CHILD_2_MS_ID`, ...
- `CHILD_1_NAME`, `CHILD_2_NAME`, ... (required)

### Microsoft Family

- `MS_FAMILY_EMAIL`
- `MS_FAMILY_PASSWORD`

### Rule Settings

- `WEEKLY_ADDITION_TIME` default `14h`
- `MAX_BANK_TIME` default `42h`
- `ACCRUED_PLAYTIME_MAX_TIME` default `3h`
- `BREAK_RECOVERY_RATE` default `3.0`

Duration format examples:

- `14h`
- `840m`
- `840min`
- `13h+30m` (also `13h30m`)
- `3x1h15m` (also `3 x 1h 15 min`)

### Blackouts

- `BLACKOUT_PERIOD_MON` ... `BLACKOUT_PERIOD_SUN`
- Format: `HH:MM-HH:MM,HH:MM-HH:MM`
- Example: `BLACKOUT_PERIOD_MON="00:00-08:00,20:30-24:00"`
- Use `00:00-24:00` to block the whole day
- Invalid blackout entries now fail startup instead of being ignored

### Other

- `TZ` default `Europe/Helsinki`
- `DATA_DIR` default `./data`
- `BOT_LANGUAGE` exact locale name, e.g. `en` or `fi` (default `en`)

## Commands (In the Group)

### Child

- `<duration>` request playtime (`30m`, `30min`, `1.5h`, `1h 30m`, `3x1h15m`)
- `status` show status for all children
- `end` end own active session
- `?` show help

### Admin

- `status` show status for all children
- `request <name> <duration>` run child request flow as test command
- `<name> <duration>` add to bank
- `<name> +<duration>` add to bank explicitly
- `<name> -<duration>` remove from bank
- `<name> =<duration>` set bank directly
- `break <name> <duration>` set accrued playtime directly
- `end <name>` end child session
- `rollover <name>` force weekly rollover
- `profile list` list profiles
- `profile show <name>` show profile settings
- `profile define <name> KEY=VALUE ...` create or replace a profile
- `profile use <profile> <child>` assign a profile to one child

`<name>` is case-insensitive.

## Scheduling

- Weekly rollover job runs Monday at `00:01` in configured timezone.

## Data Storage

Under `DATA_DIR`:

- `playtime.sqlite3` playtime/bank/session/accrued-playtime state
- `signalbot.sqlite3` SignalBot internal state

## Development

Run tests:

```bash
uv run pytest
```

## Project Layout

- `main.py` entrypoint
- `family_safety_bot/config.py` env config parsing
- `family_safety_bot/watcher.py` Signal command handling
- `family_safety_bot/rules.py` rule engine
- `family_safety_bot/storage.py` SQLite persistence
- `family_safety_bot/ms_family.py` Microsoft Family API client
