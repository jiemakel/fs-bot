from __future__ import annotations


def format_duration(minutes: int) -> str:
    if minutes <= 0:
        return "0m"
    parts = [f"{h}h" if (h := minutes // 60) else ""]
    if m := minutes % 60:
        parts.append(f"{m}m")
    return " ".join(parts).strip()
