from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from html import unescape
from urllib.parse import urljoin

import httpx

from family_safety_bot.formatting import format_duration

logger = logging.getLogger(__name__)

_FAMILY_URL = "https://account.microsoft.com/family"
_FAMILY_HOME_URL = "https://account.microsoft.com/family/home"
_LOGIN_URL = "https://login.live.com/login.srf"
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

    def __init__(self, email: str, password: str) -> None:
        self._email = email
        self._password = password
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

    async def ensure_authenticated(self, force: bool = False) -> None:
        if force:
            await self.aclose()

        if self._client and self._authenticated:
            check = await self._client.get(_FAMILY_HOME_URL)
            if self._is_authenticated_family_response(check):
                return
            logger.info("Existing Family Safety session appears expired; re-authenticating.")
            await self.aclose()

        await self._authenticate_web()

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("MicrosoftFamilyApi client is not initialized")
        return self._client

    async def _authenticate_web(self) -> None:
        logger.info("Authenticating with Microsoft Family Safety web portal...")
        self._client = httpx.AsyncClient(
            headers=_WEB_HEADERS,
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
        )
        client = self._require_client()

        login_page_response = await client.get(_LOGIN_URL)
        if "login.live.com" not in str(login_page_response.url).lower():
            raise ValueError(
                "Authentication flow did not reach Microsoft login page. "
                f"Final URL was: {login_page_response.url}"
            )

        server_data = self._extract_server_data(login_page_response.text)
        ppft = self._extract_ppft(login_page_response.text, server_data)
        if not ppft:
            raise ValueError("Could not find PPFT token on login page")

        url_post = self._extract_login_post_url(
            login_page_response.text,
            server_data,
            str(login_page_response.url),
        )
        form_fields = {
            "PPFT": ppft,
            "login": self._email,
            "passwd": self._password,
        }

        submit_response = await client.post(
            url_post,
            data=form_fields,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://login.live.com",
                "Referer": str(login_page_response.url),
            },
        )
        self._raise_on_login_error(submit_response)
        submit_response = await self._complete_sign_in_handoffs(submit_response)

        family_response = await self._finalize_family_session()
        if not self._is_authenticated_family_response(family_response):
            raise ValueError("Authentication failed: Family Safety session was not established")

        logger.info("Successfully authenticated with Microsoft Family Safety web portal")
        self._authenticated = True

    async def _follow_html_form_redirects(self, response: httpx.Response) -> httpx.Response:
        current = response
        client = self._require_client()
        for _ in range(3):
            parsed = self._parse_auto_submit_form(current.text)
            if not parsed:
                break
            action, method, fields = parsed
            target = urljoin(str(current.url), action)
            if method.lower() == "post":
                current = await client.post(target, data=fields)
            else:
                current = await client.get(target, params=fields)
        return current

    async def _follow_client_side_redirects(self, response: httpx.Response) -> httpx.Response:
        current = response
        client = self._require_client()
        for _ in range(4):
            next_url = self._extract_client_side_redirect_url(current.text)
            if not next_url:
                break
            current = await client.get(urljoin(str(current.url), next_url))
        return current

    async def _finalize_family_session(self) -> httpx.Response:
        client = self._require_client()
        last_response: httpx.Response | None = None
        for _ in range(4):
            response = await client.get(_FAMILY_URL)
            response = await self._follow_html_form_redirects(response)
            response = await self._follow_client_side_redirects(response)
            last_response = response
            if self._is_authenticated_family_response(response):
                break
        if last_response is None:
            raise RuntimeError("Failed to resolve Family Safety session")
        return last_response

    async def _complete_sign_in_handoffs(self, response: httpx.Response) -> httpx.Response:
        current = response
        client = self._require_client()

        server_data = self._extract_server_data(current.text)
        if isinstance(server_data, dict):
            url_post = server_data.get("urlPost")
            ppft = server_data.get("sFT")
            if isinstance(url_post, str) and url_post and isinstance(ppft, str) and ppft:
                # After the credential post, Microsoft emits a JS-driven intermediate
                # sign-in step. The minimal reliable payload we observed for that
                # follow-up post is PPFT + type=28 + login + loginfmt.
                current = await client.post(
                    url_post,
                    data={
                        "PPFT": ppft,
                        "type": "28",
                        "login": self._email,
                        "loginfmt": self._email,
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
                        headers={
                            "Content-Type": "application/x-www-form-urlencoded",
                            "Referer": str(current.url),
                        },
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
        text = response.text.lower()
        url = str(response.url).lower()
        for keywords, message in (
            (["password is incorrect", "password you entered is incorrect"], "Login failed: Incorrect password"),
            (["account doesn't exist", "couldn't find your account"], "Login failed: Account doesn't exist"),
        ):
            if any(kw in text for kw in keywords):
                raise ValueError(message)
        if "help us protect your account" in text:
            raise ValueError("Microsoft requires additional verification")
        if "proofup" in url or "mfaenter" in url:
            raise ValueError("Two-factor authentication detected but not supported")

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
        patterns = [
            r'name=["\']PPFT["\'][^>]*value=["\']([^"\']+)["\']',
            r'value=["\']([^"\']+)["\'][^>]*name=["\']PPFT["\']',
        ]
        for pattern in patterns:
            match = re.search(pattern, page_content)
            if match:
                return match.group(1)

        if server_data and isinstance(server_data.get("sFTTag"), str):
            sft_tag_unescaped = unescape(server_data["sFTTag"])
            match = re.search(r'name=["\']PPFT["\'][^>]*value=["\']([^"\']+)["\']', sft_tag_unescaped)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _extract_login_post_url(page_content: str, server_data: dict | None, fallback: str) -> str:
        url_post_match = re.search(r'urlPost:\s*[\'"]([^\'\"]+)[\'"]', page_content)
        if url_post_match:
            return url_post_match.group(1).replace("&amp;", "&")
        if server_data:
            json_url_post = server_data.get("urlPost") or server_data.get("urlPostMsa")
            if isinstance(json_url_post, str) and json_url_post:
                return unescape(json_url_post).replace("&amp;", "&")
        action_match = re.search(r'<form[^>]*action=["\']([^"\']+)["\']', page_content)
        if action_match:
            return action_match.group(1).replace("&amp;", "&")
        return fallback

    @staticmethod
    def _parse_auto_submit_form(html: str) -> tuple[str, str, dict[str, str]] | None:
        for form_match in re.finditer(
            r"<form(?P<attrs>[^>]*)>(?P<body>.*?)</form>",
            html,
            re.IGNORECASE | re.DOTALL,
        ):
            form_attrs = MicrosoftFamilyApi._parse_html_attributes(form_match.group("attrs"))
            action = form_attrs.get("action")
            if not action:
                continue
            method = form_attrs.get("method", "GET")
            body = form_match.group("body")
            fields: dict[str, str] = {}
            hidden_fields = 0
            for input_match in re.finditer(r"<input(?P<attrs>[^>]*)>", body, re.IGNORECASE):
                attrs = MicrosoftFamilyApi._parse_html_attributes(input_match.group("attrs"))
                field_name = attrs.get("name")
                if field_name is None:
                    continue
                if attrs.get("type", "").lower() == "hidden":
                    hidden_fields += 1
                fields[field_name] = attrs.get("value", "")
            if hidden_fields == 0:
                continue
            return (action, method, fields)
        return None

    @staticmethod
    def _extract_client_side_redirect_url(html: str) -> str | None:
        meta = re.search(
            r'<meta[^>]*http-equiv=["\']refresh["\'][^>]*content=["\'][^"\']*;\s*([^"\']+)["\']',
            html,
            re.IGNORECASE,
        )
        if meta:
            content = unescape(meta.group(1)).strip()
            return content[4:] if content.lower().startswith("url=") else content

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
