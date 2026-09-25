from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from family_safety_bot.formatting import format_duration

logger = logging.getLogger(__name__)
MfaChallengeHandler = Callable[[str, int], Awaitable[None]]


class AuthenticationRequiredError(RuntimeError):
    """Raised when interactive authentication is needed but not authorized."""

_FAMILY_URL = "https://account.microsoft.com/family/home"
_SCREEN_TIME_ENDPOINT = "https://account.microsoft.com/family/api/screen-time-request"
_ROSTER_ENDPOINT = "https://account.microsoft.com/family/api/roster"
_SCREEN_TIME_OVERRIDE_ENDPOINT = "https://account.microsoft.com/family/api/device-limits/screentime-time-override"

_WEB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/119.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

class MicrosoftFamilyApi:
    """Client for Microsoft Family Safety web endpoints."""

    def __init__(
        self,
        email: str,
        session_path: str | Path | None = None,
    ) -> None:
        self._email = email
        self._session_path = Path(session_path) if session_path else None
        self._client: httpx.AsyncClient | None = None
        self._authenticated = False
        self._last_status_code: int | None = None

    @property
    def last_status_code(self) -> int | None:
        return self._last_status_code

    async def aclose(self) -> None:
        if self._client:
            await self._client.aclose()
        self._client = None
        self._authenticated = False

    async def ensure_authenticated(
        self,
        force: bool = False,
        mfa_challenge_handler: MfaChallengeHandler | None = None,
    ) -> None:
        if force:
            await self.aclose()
            self._discard_session_file()
        elif await self.has_valid_session():
            return

        await self._authenticate_web(mfa_challenge_handler)

    async def has_valid_session(self) -> bool:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers=_WEB_HEADERS,
                timeout=httpx.Timeout(30.0),
                follow_redirects=True,
            )

        client = self._require_client()
        if not self._authenticated and not self._load_session_cookies():
            return False

        check = await client.get(_FAMILY_URL)
        if self._is_authenticated_family_response(check):
            self._authenticated = True
            return True

        logger.info("Existing Family Safety session appears expired")
        self._authenticated = False
        client.cookies.clear()
        self._discard_session_file()
        return False

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("MicrosoftFamilyApi client is not initialized")
        return self._client

    async def _authenticate_web(
        self,
        mfa_challenge_handler: MfaChallengeHandler | None = None,
    ) -> None:
        logger.info("Authenticating with Microsoft Family Safety web portal...")
        if self._client is not None:
            await self._client.aclose()
        self._client = httpx.AsyncClient(
            headers=_WEB_HEADERS,
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
        )
        client = self._require_client()

        if self._load_session_cookies():
            cached_response = await client.get(_FAMILY_URL)
            if self._is_authenticated_family_response(cached_response):
                logger.info("Restored Microsoft Family Safety session from disk")
                self._authenticated = True
                return
            logger.info("Stored Microsoft Family Safety session has expired")
            client.cookies.clear()
            self._discard_session_file()

        family_entry_response = await client.get(_FAMILY_URL, follow_redirects=False)
        authorization_url = family_entry_response.headers.get("location")
        if not authorization_url:
            raise ValueError(
                "Authentication flow did not reach Microsoft authorization endpoint. "
                f"Family Safety returned status {family_entry_response.status_code}."
            )
        authorization_url = self._interactive_authorization_url(
            urljoin(str(family_entry_response.url), authorization_url)
        )
        login_page_response = await client.get(authorization_url)
        login_host = login_page_response.url.host.lower() if login_page_response.url.host else ""
        if login_host not in {"login.live.com", "login.microsoftonline.com"}:
            raise ValueError(
                "Authentication flow did not reach Microsoft login page. "
                f"Final URL was: {login_page_response.url}"
            )

        server_data = self._extract_server_data(login_page_response.text)
        ppft = self._extract_ppft(login_page_response.text, server_data)
        if not ppft:
            raise ValueError("Could not find PPFT token on login page")

        if mfa_challenge_handler is None:
            raise AuthenticationRequiredError(
                "Microsoft Authenticator approval is required, but no challenge handler "
                "was provided"
            )
        submit_response = await self._complete_passwordless_challenge(
            login_page_response,
            server_data or {},
            ppft,
            mfa_challenge_handler,
        )

        self._raise_on_login_error(submit_response)
        await self._complete_sign_in_handoffs(submit_response)

        family_response = await client.get(_FAMILY_URL)
        if not self._is_authenticated_family_response(family_response):
            raise ValueError("Authentication failed: Family Safety session was not established")

        logger.info("Successfully authenticated with Microsoft Family Safety web portal")
        self._authenticated = True
        self._save_session_cookies()

    def _load_session_cookies(self) -> bool:
        if not self._session_path or not self._session_path.exists():
            return False
        try:
            records = json.loads(self._session_path.read_text(encoding="utf-8"))
            if not isinstance(records, list):
                raise ValueError("cookie file is not a list")
            client = self._require_client()
            for record in records:
                if not isinstance(record, dict):
                    continue
                client.cookies.set(
                    str(record["name"]),
                    str(record["value"]),
                    domain=str(record["domain"]),
                    path=str(record.get("path") or "/"),
                )
            return bool(records)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("Ignoring invalid Microsoft session file %s: %s", self._session_path, exc)
            return False

    def _save_session_cookies(self) -> None:
        if not self._session_path:
            return
        records = [
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
            }
            for cookie in self._require_client().cookies.jar
        ]
        self._session_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._session_path.with_suffix(self._session_path.suffix + ".tmp")
        temporary.write_text(json.dumps(records, separators=(",", ":")), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(self._session_path)

    def _discard_session_file(self) -> None:
        if not self._session_path:
            return
        try:
            self._session_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not remove Microsoft session file: %s", exc)

    async def _complete_passwordless_challenge(
        self,
        login_response: httpx.Response,
        server_data: dict,
        flow_token: str,
        challenge_handler: MfaChallengeHandler,
    ) -> httpx.Response:
        client = self._require_client()
        credential_url = server_data.get("urlGetCredentialType")
        if not isinstance(credential_url, str) or not credential_url:
            raise ValueError("Microsoft login page did not provide a credential discovery endpoint")

        credential_response = await client.post(
            urljoin(str(login_response.url), credential_url),
            json={
                "checkPhones": False,
                "country": str(server_data.get("country") or ""),
                "federationFlags": int(server_data.get("iGctFederationFlags") or 0),
                "flowToken": flow_token,
                "forceotclogin": False,
                "isCookieBannerShown": bool(server_data.get("fShowCookieBanner")),
                "isExternalFederationDisallowed": bool(
                    server_data.get("fIsExternalFederationDisallowed")
                ),
                "isFederationDisabled": bool(server_data.get("fIsFedDisabled")),
                "isFidoSupported": bool(server_data.get("fIsFidoSupported")),
                "isOtherIdpSupported": True,
                "isReactLoginRequest": True,
                "isRemoteConnectSupported": bool(server_data.get("fRemoteConnectEnabled")),
                "isRemoteNGCSupported": True,
                "isSignup": False,
                "originalRequest": str(server_data.get("sCtx") or ""),
                "otclogindisallowed": bool(server_data.get("fIsOtcLoginDisabled")),
                "uaid": self._query_value(credential_url, "uaid"),
                "username": self._email,
            },
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Referer": str(login_response.url),
            },
        )
        credential_response.raise_for_status()
        credential_data = credential_response.json()
        credentials = credential_data.get("Credentials") or {}
        remote = credentials.get("RemoteNgcParams") or {}
        session_key = remote.get("SessionIdentifier")
        if not credentials.get("HasRemoteNGC") or not isinstance(session_key, str) or not session_key:
            raise ValueError("Microsoft Authenticator passwordless sign-in is unavailable for this account")

        one_time_code_url = server_data.get("urlGetOneTimeCode")
        if not isinstance(one_time_code_url, str) or not one_time_code_url:
            parsed_login_url = urlsplit(str(login_response.url))
            endpoint_query: dict[str, str] = {}
            login_query = dict(parse_qsl(parsed_login_url.query, keep_blank_values=True))
            if market := login_query.get("mkt"):
                endpoint_query["mkt"] = market
            if locale_id := server_data.get("iRequestLCID") or login_query.get("lc"):
                endpoint_query["lcid"] = str(locale_id)
            for field, parameter in (
                ("sSiteId", "id"),
                ("sClientId", "client_id"),
                ("sForwardedClientId", "fci"),
                ("sNoPaBubbleVersion", "nopa"),
            ):
                if value := server_data.get(field):
                    endpoint_query[parameter] = str(value)
            one_time_code_url = urlunsplit(
                parsed_login_url._replace(path="/GetOneTimeCode.srf", query=urlencode(endpoint_query))
            )
        uaid = self._query_value(credential_url, "uaid")
        otc_response = await client.post(
            urljoin(str(login_response.url), one_time_code_url),
            data={
                "login": self._email,
                "flowtoken": session_key,
                "purpose": "eOTT_RemoteNGC",
                "channel": "PushNotifications",
                "SAPId": "",
                "ChallengeViewSupported": str(server_data.get("iUXMode") or 0),
                "uaid": uaid,
                "canaryFlowToken": flow_token,
            },
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": str(login_response.url),
            },
        )
        otc_response.raise_for_status()
        otc_data = otc_response.json()
        completion_token = str(otc_data.get("flowToken") or otc_data.get("FlowToken") or "")
        if not completion_token:
            raise ValueError("Microsoft did not return an Authenticator flow token")
        display_id = str(
            otc_data.get("displaySignForUI")
            or otc_data.get("DisplaySignForUI")
            or remote.get("Entropy")
            or "unknown"
        )
        timeout_seconds = max(1, int(server_data.get("iPollingTimeout") or 60))
        polling_interval = max(1, int(server_data.get("iPollingInterval") or 1))
        await challenge_handler(display_id, timeout_seconds)

        session_state_url = server_data.get("urlSessionState")
        if not isinstance(session_state_url, str) or not session_state_url:
            raise ValueError("Microsoft login page did not provide an Authenticator status endpoint")
        await self._poll_authenticator(
            urljoin(str(login_response.url), session_state_url),
            session_key,
            timeout_seconds,
            polling_interval,
            "NGC",
            str(login_response.url),
        )

        post_url = server_data.get("urlPostMsa") or server_data.get("urlPost")
        if not isinstance(post_url, str) or not post_url:
            raise ValueError("Microsoft login page did not provide a sign-in completion endpoint")
        username = str(credential_data.get("Username") or self._email)
        display_username = str(credential_data.get("Display") or username)
        completion_fields = {
            "slk": session_key,
            "uaid": uaid,
            "ps": "4",
            "psRNGCDefaultType": str(remote.get("DefaultType") or ""),
            "psRNGCEntropy": display_id,
            "psRNGCSLK": session_key,
            "canary": str(server_data.get("sCanary") or server_data.get("sCanaryToken") or ""),
            "ctx": str(server_data.get("sCtx") or ""),
            "hpgrequestid": "",
            "PPFT": completion_token,
            "PPSX": str(server_data.get("sRandomBlob") or ""),
            "NewUser": "1",
            "FoundMSAs": str(server_data.get("sFoundMSAs") or ""),
            "fspost": "1" if server_data.get("fPOST_ForceSignin") else "0",
            "i21": "0",
            "CookieDisclosure": "1" if server_data.get("fShowCookieBanner") else "0",
            "IsFidoSupported": "1" if server_data.get("fIsFidoSupported") else "0",
            "isSignupPost": "0",
            "isRecoveryAttemptPost": "0",
            "i13": "0",
            "login": username.strip().lower(),
            "loginfmt": display_username,
            "type": "21",
            "LoginOptions": "3",
            "lrt": "",
            "lrtPartition": "",
            "hisRegion": "",
            "hisScaleUnit": "",
            "cpr": "0",
        }
        return await client.post(
            urljoin(str(login_response.url), post_url),
            data=completion_fields,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": f"{login_response.url.scheme}://{login_response.url.host}",
                "Referer": str(login_response.url),
            },
            follow_redirects=False,
        )

    async def _poll_authenticator(
        self,
        poll_url: str,
        session_key: str,
        timeout_seconds: int,
        polling_interval: int,
        session_key_type: str | None,
        referer: str,
    ) -> None:
        client = self._require_client()
        poll_url = self._session_approval_poll_url(poll_url, session_key, session_key_type)
        attempts = max(1, (timeout_seconds + polling_interval - 1) // polling_interval)
        last_state: dict = {}
        for attempt in range(attempts):
            response = await client.post(
                poll_url,
                json={"DeviceCode": session_key},
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": referer,
                },
            )
            response.raise_for_status()
            state = {str(key).lower(): value for key, value in response.json().items()}
            if state != last_state:
                logger.info("Microsoft Authenticator state: %s", state)
                last_state = state
            authorization_state = state.get("authorizationstate")
            if authorization_state == 2:
                return
            if authorization_state == 1:
                raise ValueError("Microsoft Authenticator request was denied")
            if authorization_state == 6:
                detail = state.get("code")
                raise ValueError(
                    "Microsoft Authenticator request failed" + (f" ({detail})" if detail else "")
                )
            if authorization_state not in (0, 7):
                raise ValueError(
                    f"Microsoft returned an invalid Authenticator state ({authorization_state!r})"
                )
            if attempt + 1 < attempts:
                await asyncio.sleep(polling_interval)
        raise ValueError(f"Microsoft Authenticator approval timed out (last state: {last_state})")

    @staticmethod
    def _query_value(url: str, name: str) -> str:
        return dict(parse_qsl(urlsplit(url).query, keep_blank_values=True)).get(name, "")

    @staticmethod
    def _interactive_authorization_url(url: str) -> str:
        """Request an actual login page instead of OAuth's silent-login callback."""
        parsed = urlsplit(url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["prompt"] = "login"
        return urlunsplit(parsed._replace(query=urlencode(query)))

    @staticmethod
    def _session_approval_poll_url(
        url: str,
        session_key: str,
        session_key_type: str | None = None,
    ) -> str:
        parsed = urlsplit(url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["slk"] = session_key
        if session_key_type:
            query["slkt"] = session_key_type
        return urlunsplit(parsed._replace(query=urlencode(query)))

    async def _complete_sign_in_handoffs(self, response: httpx.Response) -> httpx.Response:
        current = response
        client = self._require_client()

        server_data = self._extract_server_data(current.text)
        if isinstance(server_data, dict):
            url_post = server_data.get("urlPost")
            ppft = self._extract_ppft(current.text, server_data)
            if isinstance(url_post, str) and url_post and ppft:
                # Accept the "stay signed in" continuation so Microsoft issues the
                # persistent cookies saved after the Family session resolves.
                current = await client.post(
                    url_post,
                    data={
                        "PPFT": ppft,
                        "type": "28",
                        "login": self._email,
                        "loginfmt": self._email,
                        "LoginOptions": "1",
                        "ctx": str(server_data.get("sCtx") or ""),
                        "hpgrequestid": str(server_data.get("sessionId") or ""),
                        "canary": str(
                            server_data.get("sCanary")
                            or server_data.get("sCanaryToken")
                            or ""
                        ),
                    },
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Origin": "https://login.live.com",
                        "Referer": str(response.url),
                    },
                    follow_redirects=False,
                )
                self._raise_on_login_error(current)

        return await self._resolve_sign_in_navigation(current)

    async def _resolve_sign_in_navigation(self, response: httpx.Response) -> httpx.Response:
        current = response
        client = self._require_client()

        for _ in range(12):
            if 300 <= current.status_code < 400:
                location = current.headers.get("location")
                if not location:
                    break
                current = await client.get(urljoin(str(current.url), location), follow_redirects=False)
                continue

            parsed_form = self._parse_auto_submit_form(current.text)
            if parsed_form:
                action, method, fields = parsed_form
                target = urljoin(str(current.url), action)
                if method.lower() == "post":
                    current = await client.post(
                        target,
                        data=fields,
                        headers=self._form_navigation_headers(str(current.url), target),
                        follow_redirects=False,
                    )
                else:
                    current = await client.get(target, params=fields, follow_redirects=False)
                continue

            next_url = self._extract_client_side_redirect_url(current.text)
            if next_url:
                current = await client.get(urljoin(str(current.url), next_url), follow_redirects=False)
                continue

            break

        return current

    @staticmethod
    def _form_navigation_headers(source_url: str, target_url: str) -> dict[str, str]:
        source = urlsplit(source_url)
        target = urlsplit(target_url)
        source_origin = f"{source.scheme}://{source.netloc}"
        target_origin = f"{target.scheme}://{target.netloc}"
        referer = source_url if source_origin == target_origin else source_origin + "/"
        if source_origin == target_origin:
            fetch_site = "same-origin"
        elif MicrosoftFamilyApi._registrable_site(source.hostname) == MicrosoftFamilyApi._registrable_site(target.hostname):
            fetch_site = "same-site"
        else:
            fetch_site = "cross-site"
        return {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": source_origin,
            "Referer": referer,
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": fetch_site,
        }

    @staticmethod
    def _registrable_site(hostname: str | None) -> str:
        labels = (hostname or "").split(".")
        return ".".join(labels[-2:]) if len(labels) >= 2 else (hostname or "")

    async def _ensure_ready_client(self) -> httpx.AsyncClient:
        if not self._client or not self._authenticated:
            await self.ensure_authenticated()
        return self._require_client()

    async def grant_screen_time(self, child_id: str, minutes: int) -> bool:
        await self._ensure_ready_client()
        return await self._grant_web(child_id, minutes)

    async def block_screen_time(self, child_id: str) -> bool:
        await self._ensure_ready_client()
        return await self._block_web(child_id)

    async def _grant_web(self, child_id: str, minutes: int) -> bool:
        logger.info(
            "WEB: Granting %s to child %s via Family Safety API",
            format_duration(minutes),
            child_id,
        )
        self._last_status_code = None
        client = self._require_client()

        family_response = await client.get(_FAMILY_URL)
        if not self._is_authenticated_family_response(family_response):
            self._last_status_code = 401
            logger.error("Session is not authenticated before grant.")
            return False

        verification_token = self._extract_request_verification_token(family_response.text)
        if not verification_token:
            self._last_status_code = 400
            logger.error("Could not extract __RequestVerificationToken from family page before grant.")
            return False
        duration = f"{minutes // 60:02d}:{minutes % 60:02d}:00"
        payload = {"childId": child_id, "duration": duration}
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "X-AMC-JsonMode": "CamelCase",
            "Origin": "https://account.microsoft.com",
            "Referer": str(family_response.url),
            "__RequestVerificationToken": verification_token,
        }

        try:
            grant_response = await client.post(
                _SCREEN_TIME_ENDPOINT,
                json=payload,
                headers=headers,
            )
        except httpx.HTTPError as e:
            logger.exception("Network error during time grant: %s", e)
            return False

        return self._handle_api_response(
            grant_response,
            "grant",
            format_duration(minutes),
            child_id,
        )

    async def _block_web(self, child_id: str) -> bool:
        logger.info("WEB: Blocking child %s on windows via Family Safety API", child_id)
        self._last_status_code = None
        client = self._require_client()

        family_response = await client.get(_FAMILY_URL)
        if not self._is_authenticated_family_response(family_response):
            self._last_status_code = 401
            logger.error("Session is not authenticated before block.")
            return False

        verification_token = self._extract_request_verification_token(family_response.text)
        if not verification_token:
            self._last_status_code = 400
            logger.error("Could not extract __RequestVerificationToken from family page before block.")
            return False
        relationship_jwt = await self._fetch_relationship_jwt(
            child_id=child_id,
            verification_token=verification_token,
            referer_url=str(family_response.url),
        )
        if not relationship_jwt:
            self._last_status_code = 400
            logger.error("Could not fetch relationship JWT for child %s from roster.", child_id)
            return False
        block_until_utc = self._block_until_utc_string(1)
        payload = {
            "childId": child_id,
            "platformType": "windows",
            "timeOverride": "blockUntil",
            "dateTime": block_until_utc,
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "__RequestVerificationToken": verification_token,
            "X-JwtFamilyRelationshipToken": relationship_jwt,
        }

        try:
            block_response = await client.post(
                _SCREEN_TIME_OVERRIDE_ENDPOINT,
                json=payload,
                headers=headers,
            )
        except httpx.HTTPError as e:
            logger.exception("Network error during time block: %s", e)
            return False

        return self._handle_api_response(block_response, "block", child_id=child_id)

    async def _fetch_relationship_jwt(
        self,
        *,
        child_id: str,
        verification_token: str,
        referer_url: str,
    ) -> str | None:
        client = self._require_client()
        headers = {
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "X-AMC-JsonMode": "CamelCase",
            "Referer": referer_url,
            "__RequestVerificationToken": verification_token,
        }

        try:
            roster_response = await client.get(_ROSTER_ENDPOINT, headers=headers)
        except httpx.HTTPError:
            logger.exception("Network error during roster fetch for relationship JWT")
            return None

        if roster_response.status_code != 200:
            logger.error(
                "Roster fetch failed with status %d: %s",
                roster_response.status_code,
                roster_response.text[:300],
            )
            return None

        try:
            roster = roster_response.json()
        except ValueError:
            logger.error("Roster response was not valid JSON")
            return None
        members = roster.get("members", []) if isinstance(roster, dict) else []
        for member in members:
            if not isinstance(member, dict):
                continue
            if str(member.get("puid", "")) != child_id:
                continue
            token = str(member.get("jsonWebToken", "")).strip()
            if token:
                return token
        return None

    @staticmethod
    def _block_until_utc_string(delay_minutes: int) -> str:
        block_until = datetime.now(timezone.utc) + timedelta(minutes=max(1, delay_minutes))
        return block_until.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _handle_api_response(
        self,
        response: httpx.Response,
        action: str,
        duration_or_child_id: str | None = None,
        child_id: str | None = None,
    ) -> bool:
        """Handle API response with consistent error logging."""
        self._last_status_code = response.status_code

        if response.status_code in (200, 201, 204):
            if action == "grant" and duration_or_child_id and child_id:
                logger.info("Successfully granted %s to child %s", duration_or_child_id, child_id)
            elif action == "block" and child_id:
                logger.info("Successfully blocked child %s", child_id)
            return True

        errors = {
            401: "Authentication expired or invalid.",
            403: "Access denied (403). Check child id and account permissions.",
            404: f"{action.capitalize()} API endpoint not found. Microsoft may have changed the API.",
            400: f"{action.capitalize()} API returned 400.",
        }

        if response.status_code in errors:
            logger.error(errors[response.status_code])
            return False

        logger.warning(
            "%s API returned unexpected status %d: %s",
            action.capitalize(),
            response.status_code,
            response.text[:200] if action == "grant" else response.text[:500],
        )
        return False

    @staticmethod
    def _raise_on_login_error(response: httpx.Response) -> None:
        url = str(response.url).lower()
        if MicrosoftFamilyApi._is_authenticated_family_response(response):
            return

        server_data = MicrosoftFamilyApi._extract_server_data(response.text)
        if isinstance(server_data, dict):
            error_text = str(server_data.get("sErrTxt") or "").lower()
            if not server_data.get("fHasError") and not error_text:
                text = ""
            else:
                text = error_text or response.text.lower()
        else:
            text = response.text.lower()

        for keywords, message in (
            (["account doesn't exist", "couldn't find your account"], "Login failed: Account doesn't exist"),
        ):
            if any(kw in text for kw in keywords):
                raise ValueError(message)
        if "help us protect your account" in text:
            raise ValueError(
                "Microsoft requires additional verification. Sign in interactively at "
                "https://account.microsoft.com and complete the requested account check."
            )
        if "proofup" in url or "mfaenter" in url:
            raise ValueError("Microsoft requires an additional interactive account check")

    @staticmethod
    def _is_authenticated_family_response(response: httpx.Response) -> bool:
        if response.status_code != 200:
            return False

        url = str(response.url).lower()
        if "account.microsoft.com/family" not in url:
            return False
        if "/family/about" in url:
            return False

        text = response.text.lower()
        if (
            '"isauthenticated":true' in text
            or '"authenticatedstate":"signedin"' in text
            or 'data-role-name="meeportal"' in text
        ):
            return True
        if "microsoft-365/family-safety" in text or "ocid=family_signin" in text:
            return False
        if "sign in" in text and "sign out" not in text:
            return False
        return True

    @staticmethod
    def _extract_server_data(page_content: str) -> dict | None:
        match = re.search(r"var\s+ServerData\s*=\s*(\{.*?\});", page_content, re.DOTALL)
        if not match:
            return None
        try:
            return json.loads(match.group(1))
        except ValueError:
            return None

    @staticmethod
    def _extract_ppft(page_content: str, server_data: dict | None) -> str | None:
        if server_data and isinstance(ppft := server_data.get("sFT"), str) and ppft:
            extracted = unescape(ppft)
            if match := re.search(r'value=["\']([^"\']+)["\']', extracted):
                return match.group(1)
            return extracted
        if server_data and isinstance(ppft := server_data.get("sFTTag"), str):
            if match := re.search(r'name=["\']PPFT["\'][^>]*value=["\']([^"\']+)["\']', unescape(ppft)):
                return match.group(1)
        if match := re.search(r'name=["\']PPFT["\'][^>]*value=["\']([^"\']+)["\']', page_content):
            return unescape(match.group(1))
        if match := re.search(r'value=["\']([^"\']+)["\'][^>]*name=["\']PPFT["\']', page_content):
            return unescape(match.group(1))
        return None

    @staticmethod
    def _parse_auto_submit_form(html: str) -> tuple[str, str, dict[str, str]] | None:
        if not re.search(r"\.submit\s*\(", html, re.IGNORECASE):
            return None

        for form_match in re.finditer(r"<form(?P<attrs>[^>]*)>(?P<body>.*?)</form>", html, re.IGNORECASE | re.DOTALL):
            attrs = MicrosoftFamilyApi._parse_html_attributes(form_match.group("attrs"))
            if not (action := attrs.get("action")):
                continue
            method = attrs.get("method", "GET")
            body = form_match.group("body")
            fields = {}
            hidden_count = 0
            for input_match in re.finditer(r"<input(?P<attrs>[^>]*)>", body, re.IGNORECASE):
                input_attrs = MicrosoftFamilyApi._parse_html_attributes(input_match.group("attrs"))
                if field_name := input_attrs.get("name"):
                    fields[field_name] = input_attrs.get("value", "")
                    if input_attrs.get("type", "").lower() == "hidden":
                        hidden_count += 1
            if hidden_count > 0:
                return (action, method, fields)
        return None

    @staticmethod
    def _extract_client_side_redirect_url(html: str) -> str | None:
        for meta in re.finditer(r"<meta(?P<attrs>[^>]*)>", html, re.IGNORECASE):
            attrs = MicrosoftFamilyApi._parse_html_attributes(meta.group("attrs"))
            if attrs.get("http-equiv", "").lower() != "refresh":
                continue
            _delay, separator, target = attrs.get("content", "").partition(";")
            if not separator:
                continue
            target = target.strip()
            return target[4:].strip() if target.lower().startswith("url=") else target

        js_replace = re.search(
            r'window\.location\.(?:replace|assign)\(\s*["\']([^"\']+)["\']\s*\)',
            html,
            re.IGNORECASE,
        )
        if js_replace:
            return unescape(js_replace.group(1))
        return None

    @staticmethod
    def _extract_request_verification_token(html: str) -> str | None:
        match = re.search(
            r'name=["\']__RequestVerificationToken["\'][^>]*value=["\']([^"\']+)["\']',
            html,
            re.IGNORECASE,
        )
        return unescape(match.group(1)) if match else None

    @staticmethod
    def _parse_html_attributes(raw_attrs: str) -> dict[str, str]:
        attrs: dict[str, str] = {}
        for match in re.finditer(
            r'([^\s=/>]+)(?:\s*=\s*(".*?"|\'.*?\'|[^\s>]+))?',
            raw_attrs,
            re.DOTALL,
        ):
            key = match.group(1).strip().lower()
            value = match.group(2)
            if value is None:
                attrs[key] = ""
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            attrs[key] = unescape(value)
        return attrs
