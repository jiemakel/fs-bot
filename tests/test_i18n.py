from __future__ import annotations

from family_safety_bot.i18n import I18n


def test_i18n_loads_requested_locale_exactly() -> None:
    i18n = I18n("fi")

    assert "tila" in i18n.command_aliases("status")
    # Key must resolve to a translated string, not fall back to the key name
    assert i18n.msg("watcher.status_child_block_mode_enabled") != "watcher.status_child_block_mode_enabled"
