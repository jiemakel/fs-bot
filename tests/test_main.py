from __future__ import annotations


def test_main_imports_with_public_signalbot_api() -> None:
    import main

    assert callable(main.main)
