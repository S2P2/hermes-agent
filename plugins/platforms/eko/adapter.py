"""
Eko Messaging API platform adapter for Hermes Agent.

A bundled platform plugin that runs an aiohttp webhook server, accepts Eko
webhook events, and relays messages to/from the agent via the standard
``BasePlatformAdapter`` interface.

Design highlights
-----------------

**Reply token preferred, Push fallback.** Eko's reply token is single-use
and has an estimated ~60 s TTL. We try Reply first and fall back to the
Push API when the token is absent, expired, or rejected.

**OAuth2 client-credentials.** Access token is fetched at startup and
proactively refreshed before expiry. On 401, the token is cleared and
the request is retried once with a fresh token.

**Configurable base URL.** Eko uses customer-specific hostnames
(e.g. ``customer-h1.ekoapp.com``) so the base URL is a required env var.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.config import Platform
from gateway.session import SessionSource

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_WEBHOOK_PORT = 8647
DEFAULT_WEBHOOK_PATH = "/eko/webhook"
DEFAULT_REPLY_TOKEN_TTL = 50  # conservative below Eko's estimated ~60 s
WEBHOOK_BODY_MAX_BYTES = 1_048_576  # 1 MiB


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _csv_set(value: str) -> Set[str]:
    if not value:
        return set()
    return {x.strip() for x in value.split(",") if x.strip()}


def _truthy_env(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Event dedup
# ---------------------------------------------------------------------------

class _MessageDeduplicator:
    """Bounded LRU of event hashes to ignore at-least-once retries."""

    def __init__(self, max_size: int = 500) -> None:
        self._seen: Dict[str, float] = {}
        self._max = max_size

    def is_duplicate(self, event: Dict[str, Any]) -> bool:
        # Hash key fields for dedup — Eko doesn't provide a webhook event ID.
        raw = json.dumps(event, sort_keys=True, default=str)
        digest = hashlib.sha256(raw.encode()).hexdigest()[:24]
        if digest in self._seen:
            return True
        if len(self._seen) >= self._max:
            cutoff = sorted(self._seen.values())[len(self._seen) // 10 or 1]
            self._seen = {k: v for k, v in self._seen.items() if v > cutoff}
        self._seen[digest] = time.time()
        return False


# ---------------------------------------------------------------------------
# OAuth2 HTTP client
# ---------------------------------------------------------------------------

class _EkoClient:
    """Thin async wrapper around the Eko Messaging API with OAuth2 management.

    Holds a cached access token + expiry. ``ensure_token()`` proactively
    refreshes before expiry; on 401 the token is cleared and the caller
    retries once.
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
        # Eko OAuth expects client_credentials grant.
        payload = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "scope": "bot",
        }
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    raise RuntimeError(
                        f"Eko OAuth token request failed ({resp.status}): {body[:200]}"
                    )
                data = await resp.json()
                self._access_token = data.get("access_token", "")
                # Proactive refresh: use expires_in if provided, else 3600 s.
                expires_in = float(data.get("expires_in", 3600))
                # Refresh 60 s before actual expiry.
                self._token_expires_at = time.time() + max(expires_in - 60, 30)

    def _auth_headers(self, token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
        }

    async def reply_text(self, reply_token: str, message: str) -> None:
        """Send a text reply using a reply token."""
        import aiohttp

        token = await self.ensure_token()
        url = f"{self._base_url}/bot/v1/message/text"
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            # Eko reply endpoint uses multipart/form-data.
            data = aiohttp.FormData()
            data.add_field("message", message)
            data.add_field("replyToken", reply_token)
            async with session.post(
                url, headers=self._auth_headers(token), data=data
            ) as resp:
                if resp.status == 401:
                    self.clear_token()
                    raise RuntimeError("Eko API returned 401 Unauthorized")
                if resp.status >= 400:
                    body = await resp.text()
                    raise RuntimeError(
                        f"Eko reply failed ({resp.status}): {body[:200]}"
                    )

    async def push_text(self, uid: str, message: str) -> None:
        """Push a text message to a user by uid."""
        import aiohttp

        token = await self.ensure_token()
        url = f"{self._base_url}/bot/v1/direct/message"
        payload = {
            "uid": uid,
            "message": {"type": "text", "data": message},
        }
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.post(
                url,
                headers={**self._auth_headers(token), "Content-Type": "application/json"},
                json=payload,
            ) as resp:
                if resp.status == 401:
                    self.clear_token()
                    raise RuntimeError("Eko API returned 401 Unauthorized")
                if resp.status >= 400:
                    body = await resp.text()
                    raise RuntimeError(
                        f"Eko push failed ({resp.status}): {body[:200]}"
                    )