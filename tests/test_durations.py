from family_safety_bot.durations import parse_activity_claim, parse_duration_minutes, parse_signed_duration_minutes


def test_parse_duration_minutes_accepts_min_designator() -> None:
    assert parse_duration_minutes("30min") == 30


def test_parse_duration_minutes_accepts_compound_min_designator() -> None:
    assert parse_duration_minutes("1h 30min") == 90


def test_parse_signed_duration_minutes_accepts_min_designator() -> None:
    assert parse_signed_duration_minutes("-15min") == -15


def test_parse_duration_minutes_accepts_multiplier_prefix_with_compound_duration() -> None:
    assert parse_duration_minutes("3x1h15m") == 225


def test_parse_duration_minutes_accepts_multiplier_prefix_with_spaced_compound_duration() -> None:
    assert parse_duration_minutes("3 x 1h 15 min") == 225


def test_parse_duration_minutes_accepts_multiplier_prefix_with_minutes() -> None:
    assert parse_duration_minutes("3x100m") == 300


def test_parse_signed_duration_minutes_accepts_multiplier_prefix() -> None:
    assert parse_signed_duration_minutes("-3x15min") == -45


def test_parse_signed_duration_minutes_accepts_positive_multiplier_prefix() -> None:
    assert parse_signed_duration_minutes("+3x2h") == 360


def test_parse_signed_duration_minutes_accepts_equals_prefix() -> None:
    assert parse_signed_duration_minutes("=2h") == 120


def test_parse_duration_minutes_accepts_plus_prefixed_multiplier() -> None:
    assert parse_duration_minutes("+3x45m") == 135


def test_parse_duration_minutes_rejects_multiplier_without_duration() -> None:
    assert parse_duration_minutes("3x") is None


def test_parse_duration_minutes_rejects_trailing_text() -> None:
    assert parse_duration_minutes("30m played guitar") is None


def test_parse_duration_minutes_rejects_invalid_separator() -> None:
    assert parse_duration_minutes("1h then 30m") is None


def test_parse_activity_claim_basic() -> None:
    assert parse_activity_claim("30m played games") == 30


def test_parse_activity_claim_compound_duration() -> None:
    assert parse_activity_claim("1h30m cleaned room") == 90


def test_parse_activity_claim_spaced_compound_duration() -> None:
    assert parse_activity_claim("1h 30m read a book") == 90


def test_parse_activity_claim_multiplier_duration() -> None:
    assert parse_activity_claim("3x1h played games") == 180


def test_parse_activity_claim_no_description_returns_none() -> None:
    assert parse_activity_claim("30m") is None


def test_parse_activity_claim_no_duration_returns_none() -> None:
    assert parse_activity_claim("played games") is None


def test_parse_activity_claim_plain_duration_with_trailing_space_returns_none() -> None:
    assert parse_activity_claim("30m  ") is None


def test_parse_activity_claim_multiword_description() -> None:
    assert parse_activity_claim("45m cleaned my room and did homework") == 45
