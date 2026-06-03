from __future__ import annotations


def format_duration(minutes: int) -> str:
    """Format minutes as a compact human-readable duration."""
    if minutes <= 0:
        return "0m"
    hours, remaining_minutes = divmod(minutes, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if remaining_minutes:
        parts.append(f"{remaining_minutes}m")
    return " ".join(parts)
