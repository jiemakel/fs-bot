# Configuration and Commands Reference

The README contains the Docker Compose setup flow, including Signal linking, group ID discovery, Microsoft child ID discovery, and the minimal `.env` needed to start. This file only adds operational details and reference material.

## Environment Details

### Identity Variables

- `PHONE_NUMBER` is the linked Signal account used by the bot.
- `SIGNAL_GROUP_ID` is the one group where the bot listens and posts.
- `SIGNAL_SERVICE` is the address of `signal-cli-rest-api` from the bot process.
- `MS_FAMILY_EMAIL` and `MS_FAMILY_PASSWORD` must belong to a Microsoft Family organizer account that can manage the configured children.
- Microsoft sign-in flows that require MFA or additional proof-up are not supported by the current web client.

### Admins

Admins are configured with contiguous indexed variables starting at 1:

- `ADMIN_1_PHONE`
- `ADMIN_2_PHONE`
- `ADMIN_3_PHONE`

Parsing stops at the first missing index. If `ADMIN_2_PHONE` is missing, `ADMIN_3_PHONE` is ignored.

### Children

Each child index must include all three values:

- `CHILD_n_PHONE` - Signal sender phone number for that child.
- `CHILD_n_MS_ID` - Microsoft Family Safety member `puid`.
- `CHILD_n_NAME` - Display name used in admin commands.

Child names are case-insensitive for commands and must be unique after lowercasing.

### Rule Settings

- `WEEKLY_ADDITION_TIME` - Playtime added during weekly rollover. Default: `14h`.
- `MAX_BANK_TIME` - Maximum banked playtime. Default: `42h`.
- `ACCRUED_PLAYTIME_MAX_TIME` - Continuous/accrued playtime cap before a break is required. Default: `3h`.
- `BREAK_RECOVERY_RATE` - Break recovery multiplier. Default: `3.0`, meaning 1 minute of break recovers 3 minutes of accrued playtime.

Durations accept `h`, `m`, and `min`, including compound and multiplier forms:

- `14h`
- `840m`
- `840min`
- `13h+30m`
- `13h30m`
- `3x1h15m`
- `3 x 1h 15 min`

Duration-only commands must be only a duration. Text after a duration becomes an activity claim, not a playtime request.

### Blackout Settings

Use one variable per weekday:

- `BLACKOUT_PERIOD_MON`
- `BLACKOUT_PERIOD_TUE`
- `BLACKOUT_PERIOD_WED`
- `BLACKOUT_PERIOD_THU`
- `BLACKOUT_PERIOD_FRI`
- `BLACKOUT_PERIOD_SAT`
- `BLACKOUT_PERIOD_SUN`

Format each value as comma-separated `HH:MM-HH:MM` ranges. `24:00` is accepted as an end time, and `00:00-24:00` blocks a full day. Ranges must start before they end; overnight windows should be split across two days. Invalid blackout entries fail startup.

### Other Settings

- `TZ` - Timezone for scheduling and local rule evaluation. Default: `Europe/Helsinki`.
- `DATA_DIR` - Directory for the bot's SQLite databases when running the Python app. Default: `./data`.
- `BOT_LANGUAGE` - Message locale. Supported values: `en`, `fi`.

---

## Commands

### Child Commands

- `<duration>` - Request playtime, for example `30m`, `30min`, `1.5h`, `1h 30m`, or `3x1h15m`.
- `<duration> <description>` - Submit an activity claim for admin approval, for example `30m went for a walk`.
- `status` - Show status for all children.
- `end` - End the child's active session early.
- `?` - Show child help.

### Admin Commands

- `status` - Show status for all children.
- `request [name] <duration>` - Run the child request flow as a test command.
- `[name] <duration>` - Add time to a child's bank.
- `[name] +<duration>` - Add time explicitly.
- `[name] -<duration>` - Remove time.
- `[name] =<duration>` - Set the bank to an exact value.
- `end [name]` - End a child's active session.
- `rollover [name]` - Force weekly rollover.
- `block [name]` - Enable grant block mode and end any active session.
- `unblock [name]` - Disable grant block mode.
- `claims [name]` - List pending activity claims.
- `ack [name]` - Grant all pending activity claims to the bank.
- `profile list` - List rule profiles.
- `profile show <name>` - Show a rule profile.
- `profile define <name> KEY=VALUE ...` - Create or replace a rule profile using env-style values.
- `profile use <profile> <child>` - Assign a profile to one child.

`<name>` is case-insensitive. For commands that accept `[name]`, omitting it applies the command to all children.

### Profile Definition Keys

`profile define` requires:

- `WEEKLY_ADDITION_TIME`
- `MAX_BANK_TIME`
- `ACCRUED_PLAYTIME_MAX_TIME`
- `BREAK_RECOVERY_RATE`
- `BLACKOUT_PERIOD_MON`
- `BLACKOUT_PERIOD_TUE`
- `BLACKOUT_PERIOD_WED`
- `BLACKOUT_PERIOD_THU`
- `BLACKOUT_PERIOD_FRI`
- `BLACKOUT_PERIOD_SAT`
- `BLACKOUT_PERIOD_SUN`

Use an empty value for a day with no blackout periods, for example `BLACKOUT_PERIOD_SAT=`.

---

## Runtime Behavior

- Local state is updated only after a successful Microsoft Family Safety grant.
- If Microsoft grant/authentication fails, the request is rejected and local state is unchanged.
- Ending a session first applies an immediate Microsoft block; if that block fails, the session is not ended locally.
- Partial grants give the maximum currently allowed time when bank, accrued-playtime, or blackout limits prevent the full request.
- Activity claims remain pending until an admin runs `ack` or an explicit bank modification.
- Accrued playtime recovery is granted only when the full required break has completed.
- If a child briefly interrupts recovery, stopping within the grace window preserves the previous recovery progress.

---

## Scheduling

- Weekly rollover runs Monday at `00:01` in `TZ`.
- Recovery completion and natural session expiry checks run once per minute.

---

## Data Storage

Under `DATA_DIR`:

- `playtime.sqlite3` - Bank balances, sessions, claims, profiles, active profile assignments, recovery state, and weekly baselines.
- `signalbot.sqlite3` - SignalBot framework state.

The linked Signal account state belongs to `signal-cli-rest-api`; in the provided Docker Compose file it is stored in the `signal-cli-data` Docker volume.

---

## Project Layout

- [main.py](main.py) - App entrypoint.
- [family_safety_bot/config.py](family_safety_bot/config.py) - Env config parsing and validation.
- [family_safety_bot/watcher.py](family_safety_bot/watcher.py) - Signal command handling and scheduler jobs.
- [family_safety_bot/rules.py](family_safety_bot/rules.py) - Playtime and break rule evaluation engine.
- [family_safety_bot/storage.py](family_safety_bot/storage.py) - SQLite persistence layer.
- [family_safety_bot/ms_family.py](family_safety_bot/ms_family.py) - Microsoft Family Safety web client.
