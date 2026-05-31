"""
Eko Messaging API HTTP client with OAuth2 client-credentials management.

Extracted from ``adapter.py`` so the adapter stays focused on
gateway/webhook/routing logic while this module owns all HTTP plumbing.

Design highlights
-----------------

**Shared request helpers.** All endpoint methods delegate to
``_request_json_post``, ``_request_json_get``, or ``_request_form`` which handle
token acquisition, session lifecycle, 401 auto-retry, and error reporting.

**OAuth2 client-credentials.** Access token is fetched at startup and
proactively refreshed before expiry. On 401 the token is cleared and
the request is retried once with a fresh token.

**Configurable base URL.** Eko uses customer-specific hostnames
(e.g. ``customer-h1.ekoapp.com``) so the base URL is a required env var.
"""

from __future__ import annotations

import logging
import mimetypes
import time
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _guess_content_type(filename: str) -> str:
    """Guess MIME type from filename for multipart uploads."""
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class _EkoClient:
    """Thin async wrapper around the Eko Messaging API with OAuth2 management.

    Holds a cached access token + expiry.  ``ensure_token()`` proactively
    refreshes before expiry; on 401 the token is cleared and the request
    is retried once.
    """

    def __init__(
        self,
        base_url: str,
        client_id: str,
        client_secret: str,
        *,
        timeout: float = 15.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout = timeout
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    async def ensure_token(self) -> str:
        """Return a valid access token, refreshing if needed."""
        if self._access_token and time.time() < self._token_expires_at:
            return self._access_token
        await self._refresh_token()
        if not self._access_token:
            raise RuntimeError("Failed to obtain Eko access token")
        return self._access_token

    def clear_token(self) -> None:
        """Clear cached token — called after a 401 response."""
        self._access_token = None
        self._token_expires_at = 0.0

    async def _refresh_token(self) -> None:
        """Fetch a new access token via OAuth2 client-credentials."""
        import aiohttp

        url = f"{self._base_url}/oauth/token"
        payload = aiohttp.FormData()
        payload.add_field("grant_type", "client_credentials")
        payload.add_field("client_id", self._client_id)
        payload.add_field("client_secret", self._client_secret)
        payload.add_field("scope", "bot")
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.post(url, data=payload) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    raise RuntimeError(
                        f"Eko OAuth token request failed ({resp.status}): {body[:200]}"
                    )
                data = await resp.json()
                self._access_token = data.get("access_token", "")
                expires_in = float(data.get("expires_in", 3600))
                self._token_expires_at = time.time() + max(expires_in - 60, 30)

    # ------------------------------------------------------------------
    # Shared request helpers
    # ------------------------------------------------------------------

    def _auth_headers(self, token: str) -> Dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    async def _request_json_post(
        self,
        path: str,
        *,
        json: Optional[Dict[str, Any]] = None,
        expect_json: bool = True,
    ) -> Optional[dict]:
        """Send a JSON POST request with auth and auto-retry on 401.

        If ``expect_json`` is True (default), parses and returns the JSON
        response body.  Otherwise returns ``None``.
        """
        import aiohttp

        token = await self.ensure_token()
        url = f"{self._base_url}{path}"
        headers = {**self._auth_headers(token), "Content-Type": "application/json"}
        timeout = aiohttp.ClientTimeout(total=self._timeout)

        for _attempt in (True, False):
            async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
                async with session.post(url, headers=headers, json=json) as resp:
                    if resp.status == 401:
                        self.clear_token()
                        if _attempt:
                            token = await self.ensure_token()
                            headers = {
                                **self._auth_headers(token),
                                "Content-Type": "application/json",
                            }
                            continue
                        body = await resp.text()
                        raise RuntimeError(
                            f"Eko API 401 after retry ({path}): {body[:200]}"
                        )
                    if resp.status >= 400:
                        body = await resp.text()
                        raise RuntimeError(
                            f"Eko API {resp.status} (POST {path}): {body[:200]}"
                        )
                    if expect_json:
                        return await resp.json()
                    return None

    async def _request_json_get(
        self,
        path: str,
        *,
        params: Optional[Dict[str, str]] = None,
        expect_json: bool = True,
    ) -> Optional[dict]:
        """Send a GET request with auth and auto-retry on 401.

        If ``expect_json`` is True (default), parses and returns the JSON
        response body.  Otherwise returns ``None``.
        """
        import aiohttp

        token = await self.ensure_token()
        url = f"{self._base_url}{path}"
        headers = self._auth_headers(token)
        timeout = aiohttp.ClientTimeout(total=self._timeout)

        for _attempt in (True, False):
            async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
                async with session.get(url, headers=headers, params=params) as resp:
                    if resp.status == 401:
                        self.clear_token()
                        if _attempt:
                            token = await self.ensure_token()
                            headers = self._auth_headers(token)
                            continue
                        body = await resp.text()
                        raise RuntimeError(
                            f"Eko API 401 after retry ({path}): {body[:200]}"
                        )
                    if resp.status >= 400:
                        body = await resp.text()
                        raise RuntimeError(
                            f"Eko API {resp.status} (GET {path}): {body[:200]}"
                        )
                    if expect_json:
                        return await resp.json()
                    return None

    async def _request_form(
        self,
        path: str,
        *,
        data: Any,
        expect_json: bool = False,
    ) -> Optional[dict]:
        """Send a multipart/form-data POST with auth and auto-retry on 401.

        If ``expect_json`` is True, parses and returns the JSON response body.
        Otherwise returns ``None``.
        """
        import aiohttp

        token = await self.ensure_token()
        url = f"{self._base_url}{path}"
        headers = self._auth_headers(token)
        timeout = aiohttp.ClientTimeout(total=self._timeout)

        for _attempt in (True, False):
            async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
                async with session.post(url, headers=headers, data=data) as resp:
                    if resp.status == 401:
                        self.clear_token()
                        if _attempt:
                            token = await self.ensure_token()
                            headers = self._auth_headers(token)
                            continue
                        body = await resp.text()
                        raise RuntimeError(
                            f"Eko API 401 after retry ({path}): {body[:200]}"
                        )
                    if resp.status >= 400:
                        body = await resp.text()
                        raise RuntimeError(
                            f"Eko API {resp.status} (POST {path}): {body[:200]}"
                        )
                    if expect_json:
                        return await resp.json()
                    return None

    # ------------------------------------------------------------------
    # Direct message endpoints
    # ------------------------------------------------------------------

    async def reply_text(self, reply_token: str, message: str) -> None:
        """Send a text reply using a reply token."""
        data = aiohttp.FormData()
        data.add_field("message", message)
        data.add_field("replyToken", reply_token)
        await self._request_form("/bot/v1/message/text", data=data)

    async def reply_quick_reply(
        self,
        reply_token: str,
        message: str,
        choices: List[str],
        values: Optional[List[str]] = None,
    ) -> None:
        """Send a quick-reply prompt using a reply token.

        ``choices`` sets the display label (``data.text``).  ``values`` sets
        the reply payload (``value``).  When ``values`` is omitted, each
        choice is used as its own value (backward-compatible).
        """
        items = [
            {
                "data": {"text": choice},
                "type": "label",
                "value": (values[i] if values else choice),
            }
            for i, choice in enumerate(choices)
        ]
        await self._request_json_post(
            "/bot/v1/message/quickreply",
            json={
                "replyToken": reply_token,
                "message": {
                    "data": message,
                    "meta": {
                        "quickreply": {
                            "template": "default",
                            "items": items,
                        }
                    },
                },
            },
            expect_json=False,
        )

    async def push_text(self, uid: str, message: str) -> None:
        """Push a text message to a user by uid."""
        await self._request_json_post(
            "/bot/v1/direct/message",
            json={"uid": uid, "message": {"type": "text", "data": message}},
        )

    async def fetch_picture(self, picture_id: str) -> bytes:
        """Download an inbound picture from Eko by picture ID.

        Uses a direct ``aiohttp`` call since it returns raw bytes, not JSON.
        """
        import aiohttp

        token = await self.ensure_token()
        url = f"{self._base_url}/file/view/{picture_id}?size=large"
        timeout = aiohttp.ClientTimeout(total=self._timeout)

        for _attempt in (True, False):
            async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
                async with session.get(
                    url, headers=self._auth_headers(token)
                ) as resp:
                    if resp.status == 401:
                        self.clear_token()
                        if _attempt:
                            token = await self.ensure_token()
                            continue
                        raise RuntimeError(
                            f"Eko API 401 after retry (fetch_picture): token exhausted"
                        )
                    if resp.status >= 400:
                        body = await resp.text()
                        raise RuntimeError(
                            f"Eko fetch picture failed ({resp.status}): {body[:200]}"
                        )
                    return await resp.read()

    async def push_picture(
        self,
        uid: str,
        file_bytes: bytes,
        filename: str,
        caption: str = "",
    ) -> None:
        """Push an image to a user by uid via multipart upload."""
        data = aiohttp.FormData()
        data.add_field("uid", uid)
        if caption:
            data.add_field("caption", caption)
        data.add_field(
            "file",
            file_bytes,
            filename=filename,
            content_type=_guess_content_type(filename),
        )
        await self._request_form("/bot/v1/direct/picture", data=data)

    async def reply_picture(
        self,
        reply_token: str,
        file_bytes: bytes,
        filename: str,
    ) -> None:
        """Reply with an image using a reply token via multipart upload."""
        data = aiohttp.FormData()
        data.add_field("replyToken", reply_token)
        data.add_field(
            "file",
            file_bytes,
            filename=filename,
            content_type=_guess_content_type(filename),
        )
        await self._request_form("/bot/v1/message/picture", data=data)

    async def push_file(
        self,
        uid: str,
        file_bytes: bytes,
        filename: str,
    ) -> None:
        """Push a file to a user by uid via multipart upload."""
        data = aiohttp.FormData()
        data.add_field("uid", uid)
        data.add_field(
            "file",
            file_bytes,
            filename=filename,
            content_type=_guess_content_type(filename),
        )
        await self._request_form("/bot/v1/direct/file", data=data)

    # ------------------------------------------------------------------
    # Group/topic endpoints
    # ------------------------------------------------------------------

    async def push_group_text(self, gid: str, tid: str, message: str) -> None:
        """Push a text message to a group/topic."""
        await self._request_json_post(
            "/bot/v1/group/message",
            json={
                "gid": gid,
                "tid": tid,
                "message": {"type": "text", "data": message},
            },
        )

    async def push_group_picture(
        self,
        gid: str,
        tid: str,
        file_bytes: bytes,
        filename: str,
        caption: str = "",
    ) -> None:
        """Push an image to a group/topic via multipart upload."""
        data = aiohttp.FormData()
        data.add_field("gid", gid)
        data.add_field("tid", tid)
        if caption:
            data.add_field("caption", caption)
        data.add_field(
            "file",
            file_bytes,
            filename=filename,
            content_type=_guess_content_type(filename),
        )
        await self._request_form("/bot/v1/group/picture", data=data)

    async def push_group_file(
        self,
        gid: str,
        tid: str,
        file_bytes: bytes,
        filename: str,
    ) -> None:
        """Push a file to a group/topic via multipart upload."""
        data = aiohttp.FormData()
        data.add_field("gid", gid)
        data.add_field("tid", tid)
        data.add_field(
            "file",
            file_bytes,
            filename=filename,
            content_type=_guess_content_type(filename),
        )
        await self._request_form("/bot/v1/group/file", data=data)

    # ------------------------------------------------------------------
    # Management endpoints (group/topic creation, user lookup)
    # ------------------------------------------------------------------

    async def create_group(
        self,
        member_uids: list,
        name: str = "",
    ) -> dict:
        """Create a group chat with the given member uids.

        ``POST /bot/v1/groups`` via multipart/form-data.
        Returns the created group object (includes ``_id`` and ``type``).
        """
        data = aiohttp.FormData()
        for uid in member_uids:
            data.add_field("uids", str(uid))
        if name:
            data.add_field("name", name)
        result = await self._request_form("/bot/v1/groups", data=data, expect_json=True)
        return result or {}

    async def create_topic(self, gid: str, name: str) -> dict:
        """Create a topic in an existing group.

        ``POST /bot/v1/groups/{gid}/topics`` with JSON body.
        Returns the created topic object (includes ``_id`` and ``gid``).
        """
        result = await self._request_json_post(
            f"/bot/v1/groups/{gid}/topics",
            json={"name": name},
        )
        return result or {}

    async def query_users(self, username: str) -> list:
        """Look up users by username.

        ``GET /bot/v1/users?username=...``.
        Returns the user list (each entry has ``_id``, ``username``, ``email``).
        """
        result = await self._request_json_get("/bot/v1/users", params={"username": username})
        return result if isinstance(result, list) else []
