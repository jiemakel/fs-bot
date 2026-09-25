import asyncio
import stat
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from family_safety_bot.ms_family import MicrosoftFamilyApi


def test_session_cookies_are_persisted_with_owner_only_permissions(tmp_path: Path) -> None:
    session_path = tmp_path / "ms-family-session.json"
    writer = MicrosoftFamilyApi("parent@example.com", session_path=session_path)
    writer._client = httpx.AsyncClient()
    writer._require_client().cookies.set("MSPAuth", "opaque", domain=".live.com", path="/")

    writer._save_session_cookies()

    assert stat.S_IMODE(session_path.stat().st_mode) == 0o600
    reader = MicrosoftFamilyApi("parent@example.com", session_path=session_path)
    reader._client = httpx.AsyncClient()
    assert reader._load_session_cookies()
    assert reader._require_client().cookies.get("MSPAuth", domain=".live.com", path="/") == "opaque"

    asyncio.run(writer.aclose())
    asyncio.run(reader.aclose())


def test_interactive_authorization_url_replaces_silent_prompt() -> None:
    original = (
        "https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize"
        "?client_id=example&state=opaque&prompt=none&scope=openid%20profile"
    )

    updated = MicrosoftFamilyApi._interactive_authorization_url(original)
    query = parse_qs(urlsplit(updated).query)

    assert query["prompt"] == ["login"]
    assert query["client_id"] == ["example"]
    assert query["state"] == ["opaque"]
    assert query["scope"] == ["openid profile"]


def test_meta_refresh_redirect_preserves_html_encoded_query_separator() -> None:
    html = (
        '<meta http-equiv="refresh" '
        'content="0; URL=https://account.microsoft.com/auth/complete?state=opaque&amp;source=login"/>'
    )

    redirect = MicrosoftFamilyApi._extract_client_side_redirect_url(html)

    assert redirect == "https://account.microsoft.com/auth/complete?state=opaque&source=login"


def test_additional_verification_login_page_has_specific_error() -> None:
    response = httpx.Response(
        200,
        request=httpx.Request("GET", "https://login.live.com/ppsecure/post.srf"),
        text="Help us protect your account",
    )

    with pytest.raises(ValueError, match="additional verification"):
        MicrosoftFamilyApi._raise_on_login_error(response)


def test_login_error_uses_structured_account_error() -> None:
    response = httpx.Response(
        200,
        request=httpx.Request("GET", "https://login.live.com/login.srf"),
        text=(
            '<script>var ServerData = {"fHasError":true,'
            '"sErrTxt":"We couldn\'t find your account"};</script>'
        ),
    )

    with pytest.raises(ValueError, match="Account doesn't exist"):
        MicrosoftFamilyApi._raise_on_login_error(response)


def test_visible_login_form_is_not_treated_as_auto_submit() -> None:
    html = (
        '<form action="https://login.live.com/ppsecure/post.srf" method="post">'
        '<input type="hidden" name="PPFT" value="opaque">'
        '</form>'
    )

    assert MicrosoftFamilyApi._parse_auto_submit_form(html) is None


def test_script_submitted_handoff_form_is_parsed() -> None:
    html = (
        '<form id="handoff" action="https://account.microsoft.com/auth" method="post">'
        '<input type="hidden" name="token" value="opaque">'
        '</form><script>document.getElementById("handoff").submit();</script>'
    )

    assert MicrosoftFamilyApi._parse_auto_submit_form(html) == (
        "https://account.microsoft.com/auth",
        "post",
        {"token": "opaque"},
    )


def test_server_flow_token_takes_precedence_over_page_fallback() -> None:
    page = '<input type="hidden" name="PPFT" value="stale-page-token">'
    server_data = {
        "sFT": '<input type="hidden" name="PPFT" value="current&amp;token">'
    }

    assert MicrosoftFamilyApi._extract_ppft(page, server_data) == "current&token"


def test_passwordless_authenticator_challenge_announces_polls_and_completes() -> None:
    requests: list[tuple[str, dict[str, Any]]] = []

    class FakeClient:
        async def post(self, url: str, **kwargs: Any) -> httpx.Response:
            requests.append((url, kwargs))
            if "GetCredentialType" in url:
                return httpx.Response(
                    200,
                    request=httpx.Request("POST", url),
                    json={
                        "Username": "Canonical@Example.COM",
                        "Display": "Canonical@example.com",
                        "Credentials": {
                            "HasRemoteNGC": 1,
                            "RemoteNgcParams": {
                                "SessionIdentifier": "lookup-secret",
                                "DefaultType": 1,
                            },
                        },
                    },
                )
            if "GetOneTimeCode.srf" in url:
                return httpx.Response(
                    200,
                    request=httpx.Request("POST", url),
                    json={"State": 201, "FlowToken": "completion-secret", "DisplaySignForUI": "44"},
                )
            if "GetSessionState.srf" in url:
                return httpx.Response(
                    200,
                    request=httpx.Request("POST", url),
                    json={"AuthorizationState": 2, "SessionState": 2},
                )
            return httpx.Response(302, request=httpx.Request("POST", url))

    api = MicrosoftFamilyApi("parent@example.com")
    api._client = cast(httpx.AsyncClient, FakeClient())
    server_data = {
        "urlGetCredentialType": "https://login.live.com/GetCredentialType.srf?uaid=request-id",
        "urlPostMsa": "https://login.live.com/ppsecure/post.srf?existing=1",
        "urlSessionState": "https://login.live.com/GetSessionState.srf?id=38936",
        "sSiteId": "38936",
        "sClientId": "client-id",
        "iRequestLCID": 1033,
        "sCanaryToken": "canary-secret",
        "sCtx": "context-secret",
        "iPollingTimeout": 60,
        "iPollingInterval": 1,
    }
    announced: list[tuple[str, int]] = []

    async def run() -> httpx.Response:
        source = httpx.Response(
            200,
            request=httpx.Request("GET", "https://login.live.com/oauth20_authorize.srf?mkt=en-US"),
            text='<script>var ServerData = {};</script>',
        )
        return await api._complete_passwordless_challenge(
            source,
            server_data,
            "initial-flow-token",
            lambda display_id, timeout: _record_challenge(announced, display_id, timeout),
        )

    result = asyncio.run(run())
    assert result.status_code == 302
    assert announced == [("44", 60)]

    credential_url, credential_request = requests[0]
    assert credential_url.startswith("https://login.live.com/GetCredentialType.srf")
    assert credential_request["json"]["username"] == "parent@example.com"

    otc_url, otc_request = requests[1]
    assert otc_url.startswith("https://login.live.com/GetOneTimeCode.srf")
    assert otc_request["data"]["purpose"] == "eOTT_RemoteNGC"

    poll_url, poll_request = requests[2]
    poll_query = parse_qs(urlsplit(poll_url).query)
    assert poll_query["slk"] == ["lookup-secret"]
    assert poll_query["slkt"] == ["NGC"]
    assert poll_request["json"] == {"DeviceCode": "lookup-secret"}

    completion_url, completion_request = requests[3]
    completion_query = parse_qs(urlsplit(completion_url).query)
    assert completion_query == {"existing": ["1"]}
    completion_data = completion_request["data"]
    assert completion_data["PPFT"] == "completion-secret"
    assert completion_data["psRNGCSLK"] == "lookup-secret"
    assert completion_data["psRNGCEntropy"] == "44"
    assert completion_data["canary"] == "canary-secret"
    assert completion_data["ctx"] == "context-secret"
    assert completion_data["login"] == "canonical@example.com"
    assert completion_data["loginfmt"] == "Canonical@example.com"
    assert completion_data["type"] == "21"
    assert completion_data["LoginOptions"] == "3"
    assert str(result.url).startswith(
        "https://login.live.com/ppsecure/post.srf"
    )


async def _record_challenge(
    announced: list[tuple[str, int]],
    display_id: str,
    timeout: int,
) -> None:
    announced.append((display_id, timeout))
