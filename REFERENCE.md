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

### Rule Profiles

Rule settings are stored in profiles created through Signal admin commands. There are no environment defaults. A profile must be defined and assigned before playtime functionality is enabled for a child.

Each profile has one or more named banks. Every bank must have enough balance for a grant, and the granted amount is deducted from every bank. Each bank defines its own weekly addition and maximum balance. Banks are a JSON object, and its first key is the default target for activity claims and balance commands.

Durations accept `h`, `m`, and `min`, including compound and multiplier forms:

- `14h`
- `840m`
- `840min`
- `13h+30m`
- `13h30m`
- `3x1h15m`
- `3 x 1h 15 min`

Duration-only commands must be only a duration. Text after a duration becomes an activity claim, not a playtime request.

### Blackout Profile Settings

Each item in the profile's `blackouts` array has `days`, `start`, and `end`. `days` accepts `mon` through `sun`, and one item may apply to several days. Times use `HH:MM`; `24:00` is accepted as an end time, and `00:00` to `24:00` blocks a full day. Ranges must start before they end; overnight windows should be split into two entries. Invalid entries reject the entire profile definition.

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
- `[name] [bank] <duration>` - Add time; child and bank are optional, and the first profile bank is the default.
- `[name] [bank] +<duration>` - Add time explicitly.
- `[name] [bank] -<duration>` - Remove time.
- `[name] [bank] =<duration>` - Set a bank to an exact value.
- `end [name]` - End a child's active session.
- `rollover [name]` - Force weekly rollover.
- `block [name]` - Enable grant block mode and end any active session.
- `unblock [name]` - Disable grant block mode.
- `claims [name]` - List pending activity claims.
- `ack [name]` - Grant all pending activity claims to the profile's first bank.
- `profile list` - List rule profiles.
- `profile show <name>` - Show a rule profile.
- `profile define <name> <json>` - Create or replace a rule profile using the JSON structure below.
- `profile use <profile> <child>` - Assign a profile to one child.

`<name>` is case-insensitive. For commands that accept `[name]`, omitting it applies the command to all children.

### Profile Definition JSON

The JSON object requires exactly `banks`, `accrued_playtime_max`, `break_recovery_rate`, and `blackouts`. Each bank requires exactly `weekly_addition` and `max_balance`. Object order is significant only for `banks`: the first bank is the activity/default bank.

```json
{
  "banks": {
    "earned": {"weekly_addition": "14h", "max_balance": "42h"},
    "weekly": {"weekly_addition": "30h", "max_balance": "30h"}
  },
  "accrued_playtime_max": "3h",
  "break_recovery_rate": 3.0,
  "blackouts": [
    {"days": ["mon", "tue", "wed", "thu", "fri"], "start": "00:00", "end": "08:00"},
    {"days": ["sat", "sun"], "start": "00:00", "end": "09:00"}
  ]
}
```

The command accepts whitespace and newlines inside the JSON, so this structure can be pasted directly after `profile define <name>`.

---

## Runtime Behavior

- Local state is updated only after a successful Microsoft Family Safety grant.
- If Microsoft grant/authentication fails, the request is rejected and local state is unchanged.
- Ending a session first applies an immediate Microsoft block; if that block fails, the session is not ended locally.
- Partial grants give the maximum currently allowed time when any bank, accrued-playtime, or blackout limit prevents the full request.
- Every grant deducts from every bank in the active profile.
- Activity rewards and unspecified balance commands target the profile's first bank.
- Ending early returns unused time to every bank debited for that session.
- Bank balances are stored by child and bank name independently of profiles. Switching profiles preserves them even when a balance exceeds the new maximum; a later rollover may clamp the balance to the active profile's maximum.
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

- `playtime.sqlite3` - Bank balances, sessions, claims, profiles, active profile assignments, and recovery state.
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
