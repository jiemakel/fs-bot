from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

_LOCALES_DIR = Path(__file__).with_name("locales")


class I18n:
    @staticmethod
    def _available_languages() -> list[str]:
        return sorted(path.stem for path in _LOCALES_DIR.glob("*.json"))

    @classmethod
    def _load_catalog(cls, language: str) -> dict[str, Any]:
        locale_path = _LOCALES_DIR / f"{language}.json"
        if not locale_path.exists():
            available = ", ".join(cls._available_languages())
            raise ValueError(f"Unsupported BOT_LANGUAGE {language!r}. Available locales: {available}")
        with locale_path.open("r", encoding="utf-8") as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict):
            raise ValueError(f"Locale file {locale_path.name} must contain a JSON object")
        return loaded

    def __init__(self, language: str = "en") -> None:
        self.language = language.strip().lower() or "en"
        self._data = self._load_catalog(self.language)

    def command_aliases(self, key: str) -> list[str]:
        aliases = cast(list[str], self._data["commands"][key])
        return [alias.lower() for alias in aliases]

    def msg(self, key: str, **kwargs: Any) -> str:
        template = cast(str, self._data["messages"].get(key, key))
        return template.format(**kwargs)
