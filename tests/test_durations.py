import pytest

from family_safety_bot.durations import parse_activity_claim, parse_duration_minutes, parse_signed_duration_minutes


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("30min", 30),
        ("1h 30min", 90),
        ("3x1h15m", 225),
        ("3 x 1h 15 min", 225),
        ("3x100m", 300),
        ("+3x45m", 135),
        ("3x", None),
        ("30m played guitar", None),
        ("1h then 30m", None),
    ],
)
def test_parse_duration_minutes(text: str, expected: int | None) -> None:
    assert parse_duration_minutes(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("-15min", -15),
        ("-3x15min", -45),
        ("+3x2h", 360),
        ("=2h", 120),
    ],
)
def test_parse_signed_duration_minutes(text: str, expected: int) -> None:
    assert parse_signed_duration_minutes(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("30m played games", 30),
        ("1h30m cleaned room", 90),
        ("1h 30m read a book", 90),
        ("3x1h played games", 180),
        ("45m cleaned my room and did homework", 45),
        ("30m", None),
        ("played games", None),
        ("30m  ", None),
    ],
)
def test_parse_activity_claim(text: str, expected: int | None) -> None:
    assert parse_activity_claim(text) == expected
