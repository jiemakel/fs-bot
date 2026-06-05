from __future__ import annotations

import re

_DURATION_TOKEN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(h|min|m)")
_DURATION_MULTIPLIER_RE = re.compile(r"^\+?(\d+)\s*x\s*(.+)$")


def parse_duration_minutes(value: str) -> int | None:
    """Parse duration text into minutes.

    Supports decimal values and units h/m/min, including compound forms
    like ``1h30m``, ``1h30min``, ``1h+30m``, and multiplier forms like
    ``3x1h15m`` or ``3 x 1h 15 min``.
    """
    text = value.strip().lower()
    if not text:
        return None

    multiplier = 1
    multiplier_match = _DURATION_MULTIPLIER_RE.match(text)
    if multiplier_match:
        multiplier = int(multiplier_match.group(1))
        text = multiplier_match.group(2).strip()
        if not text:
            return None

    total_minutes = 0.0
    pos = 0
    seen = False
    for match in _DURATION_TOKEN_RE.finditer(text):
        separator = text[pos:match.start()]
        if separator and separator.strip(" +"):
            return None
        amount = float(match.group(1))
        unit = match.group(2)
        total_minutes += amount * 60 if unit == "h" else amount
        pos = match.end()
        seen = True

    if not seen or (text[pos:] and text[pos:].strip(" +")):
        return None
    return int(round(total_minutes * multiplier))


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
        minutes = parse_duration_minutes(candidate)
        if minutes is not None and minutes > 0:
            best_minutes = minutes

    return best_minutes


def parse_signed_duration_minutes(value: str) -> int | None:
    """Parse duration text with an optional leading sign.

    Examples: ``30m``, ``30min``, ``+30m``, ``-1h``, ``=2h``.
    """
    text = value.strip()
    if not text:
        return None

    sign = 1
    if text[0] in "+-=":
        sign = -1 if text[0] == "-" else 1
        text = text[1:].strip()

    minutes = parse_duration_minutes(text)
    if minutes is None:
        return None
    return sign * minutes
