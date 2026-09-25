from __future__ import annotations

import asyncio
import os

import pytest

from family_safety_bot.ms_family import MicrosoftFamilyApi, _FAMILY_URL

pytestmark = pytest.mark.live


def _configured_child_id() -> str | None:
    override = os.environ.get("LIVE_MS_FAMILY_CHILD_ID", "").strip()
    if override:
        return override

    index = 1
    while True:
        child_id = os.environ.get(f"CHILD_{index}_MS_ID", "").strip()
        if child_id:
            return child_id
        if f"CHILD_{index}_PHONE" not in os.environ:
            return None
        index += 1


def _require_live_ms_family_env() -> tuple[str, str]:
    if os.environ.get("LIVE_TEST") != "1":
        pytest.skip("Set LIVE_TEST=1 to run Microsoft Family Safety live tests")

    email = os.environ.get("ADMIN_1_MS_EMAIL", "").strip()
    child_id = _configured_child_id()
    missing = [
        key
        for key, value in (
            ("ADMIN_1_MS_EMAIL", email),
            ("LIVE_MS_FAMILY_CHILD_ID or CHILD_1_MS_ID", child_id),
        )
        if not value
    ]
    if missing:
        pytest.skip(f"Missing live Microsoft Family Safety env: {', '.join(missing)}")

    assert child_id is not None
    return email, child_id


async def _assert_readonly_family_api_canary(email: str, child_id: str) -> None:
    api = MicrosoftFamilyApi(email, session_path="data/ms-family-session.json")
    try:
        await api.ensure_authenticated()
        client = api._require_client()

        family_response = await client.get(_FAMILY_URL)
        assert api._is_authenticated_family_response(family_response)

        verification_token = api._extract_request_verification_token(family_response.text)
        assert verification_token

        relationship_jwt = await api._fetch_relationship_jwt(
            child_id=child_id,
            verification_token=verification_token,
            referer_url=str(family_response.url),
        )
        assert relationship_jwt, "Configured child id was not present in the Family Safety roster"
    finally:
        await api.aclose()


def test_ms_family_live_authentication_and_roster_are_readable() -> None:
    email, child_id = _require_live_ms_family_env()

    asyncio.run(_assert_readonly_family_api_canary(email, child_id))
