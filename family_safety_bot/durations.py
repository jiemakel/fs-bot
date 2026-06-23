from __future__ import annotations

import re

_DURATION_TOKEN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(h|min|m)")
_DURATION_MULTIPLIER_RE = re.compile(r"^\+?(\d+)\s*x\s*(.+)$")


def parse_duration_minutes(value: str) -> int | None:
    """Parse h/m/min durations, compounds, and multiplier prefixes into minutes."""
    text = value.strip().lower()
    if not text:
        return None

    multiplier = 1
    if match := _DURATION_MULTIPLIER_RE.match(text):
        multiplier = int(match.group(1))
        text = match.group(2).strip()
        if not text:
            return None

    total = 0.0
    pos = 0
    for match in _DURATION_TOKEN_RE.finditer(text):
        if (sep := text[pos : match.start()]) and sep.strip(" +"):
            return None
        total += float(match.group(1)) * (60 if match.group(2) == "h" else 1)
        pos = match.end()

    if pos == 0 or ((trailing := text[pos:]) and trailing.strip(" +")):
        return None
    return int(round(total * multiplier))


def parse_activity_claim(text: str) -> int | None:
    """Validate and parse an activity claim (duration + non-empty description).

    Returns the parsed minutes if ``text`` starts with a valid positive duration
    followed by at least one more word, or ``None`` otherwise.  The caller is
    responsible for storing the original ``text`` as the claim description.
    """
    tokens = text.strip().split()
    if len(tokens) < 2:
        return None

    best_minutes: int | None = None

    for split_at in range(1, len(tokens)):
        candidate = " ".join(tokens[:split_at])
        if (minutes := parse_duration_minutes(candidate)) is not None and minutes > 0:
            best_minutes = minutes

    return best_minutes


def parse_signed_duration_minutes(value: str) -> int | None:
    """Parse duration text with an optional leading +, -, or = sign."""
    text = value.strip()
    if not text:
        return None
    sign = -1 if text.startswith("-") else 1
    if text[0] in "+-=":
        text = text[1:].strip()
    if (minutes := parse_duration_minutes(text)) is None:
        return None
    return sign * minutes
