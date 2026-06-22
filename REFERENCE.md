# Configuration and Commands Reference

This document contains the detailed configuration options, available group commands, data storage layout, and source directory structure for the Playtime Bot.

## Configuration

Configure the application by setting environment variables (typically via a `.env` file). See `.env.example` for a starting template.

### Signal

- `SIGNAL_SERVICE` - Connection address for `signal-cli-rest-api` JSON-RPC service (e.g. `127.0.0.1:8080`).
- `PHONE_NUMBER` - Signal phone number of the bot account.
- `SIGNAL_GROUP_ID` - The target Signal group ID where the bot listens and posts.

### Admins (Indexed)

Admins are configured with contiguous indexed environment variables starting at 1:
- `ADMIN_1_PHONE`, `ADMIN_2_PHONE`, ...

### Children (Indexed)

Each child must have matching contiguous index variables configuring their phone number, Microsoft ID, and name:
- `CHILD_1_PHONE`, `CHILD_2_PHONE`, ...
- `CHILD_1_MS_ID`, `CHILD_2_MS_ID`, ...
- `CHILD_1_NAME`, `CHILD_2_NAME`, ... (required, case-insensitive)

### Microsoft Family

Credentials for the parent account to invoke the Microsoft Family Safety API:
- `MS_FAMILY_EMAIL`
- `MS_FAMILY_PASSWORD`

### Rule Settings

Default rule settings for the default profile:
- `WEEKLY_ADDITION_TIME` - Playtime added to bank weekly. Default: `14h`.
- `MAX_BANK_TIME` - Maximum playtime that can accumulate in the bank. Default: `42h`.
- `ACCRUED_PLAYTIME_MAX_TIME` - Maximum continuous/accrued playtime before a break is required. Default: `3h`.
- `BREAK_RECOVERY_RATE` - Rate at which break/non-play time recovers the accrued playtime balance. Default: `3.0` (3x recovery speed).

#### Duration Format Examples:
- `14h`
- `840m`
- `840min`
- `13h+30m` (also `13h30m`)
- `3x1h15m` (also `3 x 1h 15 min`)

### Blackouts

- `BLACKOUT_PERIOD_MON` ... `BLACKOUT_PERIOD_SUN`
- Format: `HH:MM-HH:MM,HH:MM-HH:MM`
- Example: `BLACKOUT_PERIOD_MON="00:00-08:00,20:30-24:00"`
- Use `00:00-24:00` to block the whole day.
- Note: Invalid blackout entries fail startup immediately instead of being ignored.

### Other Settings

- `TZ` - System timezone. Default: `Europe/Helsinki`.
- `DATA_DIR` - Directory where databases and caches are stored. Default: `./data`.
- `BOT_LANGUAGE` - Locale code for message text. Default: `en` (supported: `en`, `fi`).

---

## Commands (In the Group)

### Child Commands

- `<duration>` - Request playtime (e.g. `30m`, `30min`, `1.5h`, `1h 30m`, `3x1h15m`).
- `<duration> <description>` - Submit an activity claim for admin approval (e.g. `30m cleaned room`).
- `status` - Show status for all children.
- `end` - End own active session early (applies block on MS Family and refunds remaining time to bank).
- `?` - Show child commands help.

### Admin Commands

- `status` - Show status for all children.
- `request [name] <duration>` - Run child request evaluation flow as a test command.
- `[name] <duration>` - Add/remove time from child's bank:
  - `[name] +<duration>` (explicit add)
  - `[name] -<duration>` (explicit remove)
  - `[name] =<duration>` (set bank directly)
- `end [name]` - End child session.
- `rollover [name]` - Force weekly rollover.
- `block [name]` - Enable grant block mode and end any active session.
- `unblock [name]` - Disable grant block mode.
- `claims [name]` - List pending activity claims.
- `ack [name]` - Grant all pending activity claims to the bank.
- `profile list` - List configured rule profiles.
- `profile show <name>` - Show details of a rule profile.
- `profile define <name> KEY=VALUE ...` - Create/replace a rule profile.
- `profile use <profile> <child>` - Assign a profile to one child.

*Note: `<name>` is case-insensitive. For commands using `[name]`, omitting the name applies the command to all children.*

---

## Scheduling

- Weekly rollover runs Monday at `00:01` in the configured timezone.
- Recovery/session completion notifications are checked every minute.

---

## Data Storage

Under the directory specified by `DATA_DIR`:

- `playtime.sqlite3` - Playtime, bank balances, active session logs, and accrued playtime state.
- `signalbot.sqlite3` - Internal SignalBot framework state.

---

## Project Layout

- [main.py](main.py) - App entrypoint
- [family_safety_bot/config.py](family_safety_bot/config.py) - Env config parsing and validation
- [family_safety_bot/watcher.py](family_safety_bot/watcher.py) - Signal command handling and scheduler
- [family_safety_bot/rules.py](family_safety_bot/rules.py) - Playtime and break rule evaluation engine
- [family_safety_bot/storage.py](family_safety_bot/storage.py) - SQLite persistence layer
- [family_safety_bot/ms_family.py](family_safety_bot/ms_family.py) - Microsoft Family Safety screen time API client
