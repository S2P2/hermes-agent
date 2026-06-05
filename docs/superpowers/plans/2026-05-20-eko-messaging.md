# Eko Messaging Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a Hermes gateway plugin that connects to the Eko Messaging API via webhooks, enabling bidirectional text chat between Eko users and the Hermes agent.

**Architecture:** Plugin-form adapter at `plugins/platforms/eko/`, modeled on the LINE adapter. Runs an aiohttp webhook server, receives Eko events, relays to the Hermes agent, and sends responses using reply tokens with push fallback. OAuth2 client-credentials auth with proactive refresh.

**Tech Stack:** Python 3.13, aiohttp (webhook server + HTTP client), Hermes plugin SDK (`PluginContext.register_platform`)

---

## Task 1: Create plugin.yaml manifest

**Files:**
- Create: `plugins/platforms/eko/plugin.yaml`

- [ ] **Step 1: Write plugin.yaml**

```yaml
name: eko-platform
label: Eko
kind: platform
version: 1.0.0
description: >
  Eko Messaging API gateway adapter for Hermes Agent.
  Runs an aiohttp webhook server that receives Eko webhook events
  and relays messages between Eko users and the Hermes agent.
  Outbound replies prefer the reply token endpoint and fall back
  to the push API when the token has expired or is absent.
author: Hermes Agent contributors
requires_env:
  - name: EKO_BASE_URL
    description: "Eko server base URL (e.g. https://customer-h1.ekoapp.com)"
    prompt: "Eko base URL"
    password: false
  - name: EKO_OAUTH_CLIENT_ID
    description: "Bot OAuth client ID from Eko admin panel"
    prompt: "OAuth client ID"
    password: false
  - name: EKO_OAUTH_CLIENT_SECRET
    description: "Bot OAuth client secret from Eko admin panel"
    prompt: "OAuth client secret"
    password: true
optional_env:
  - name: EKO_PORT
    description: "Webhook listen port (default: 8647)"
    prompt: "Webhook port"
    password: false
  - name: EKO_HOST
    description: "Webhook bind host (default: 0.0.0.0)"
    prompt: "Webhook host"
    password: false
  - name: EKO_WEBHOOK_PATH
    description: "Webhook endpoint path (default: /eko/webhook)"
    prompt: "Webhook path"
    password: false
  - name: EKO_ALLOWED_USERS
    description: "Comma-separated Eko user IDs allowed to DM the bot"
    prompt: "Allowed user IDs (comma-separated)"
    password: false
  - name: EKO_ALLOW_ALL_USERS
    description: "Allow any Eko user to talk to the bot (dev only)"
    prompt: "Allow all users? (true/false)"
    password: false
  - name: EKO_HOME_CHANNEL
    description: "Default user ID for cron / notification delivery"
    prompt: "Home channel ID (or empty)"
    password: false
  - name: EKO_REPLY_TOKEN_TTL
    description: "Reply-token TTL in seconds (default: 50)"
    prompt: "Reply token TTL (seconds)"
    password: false
```

- [ ] **Step 2: Commit**

```bash
git add plugins/platforms/eko/plugin.yaml
git commit -m "feat(eko): add plugin.yaml manifest"
```

---

## Task 2: Create __init__.py entry point

**Files:**
- Create: `plugins/platforms/eko/__init__.py`

- [ ] **Step 1: Write __init__.py**

```python
from .adapter import register

__all__ = ["register"]
```

- [ ] **Step 2: Commit**

```bash
git add plugins/platforms/eko/__init__.py
git commit -m "feat(eko): add plugin entry point"
```

---

## Task 3: Create adapter.py — imports, constants, OAuth client

**Files:**
- Create: `plugins/platforms/eko/adapter.py`

This is the largest file. We build it incrementally and commit at each logical boundary.

- [ ] **Step 1: Write the top section — imports, constants, and `_EkoClient`**

```python
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
```

- [ ] **Step 2: Commit**

```bash
git add plugins/platforms/eko/adapter.py
git commit -m "feat(eko): add imports, constants, dedup, and OAuth client"
```

---

## Task 4: Add EkoAdapter class — init, connect, disconnect

**Files:**
- Modify: `plugins/platforms/eko/adapter.py` (append after `_EkoClient`)

- [ ] **Step 1: Add the EkoAdapter class with `__init__`, `connect`, `disconnect`**

Append this after the `_EkoClient` class:

```python

# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class EkoAdapter(BasePlatformAdapter):
    """Eko Messaging API gateway adapter."""

    def __init__(self, config, **kwargs):
        platform = Platform("eko")
        super().__init__(config=config, platform=platform)

        extra = getattr(config, "extra", {}) or {}

        # Required credentials
        self.base_url = (
            os.getenv("EKO_BASE_URL") or extra.get("base_url", "")
        ).rstrip("/")
        self.oauth_client_id = (
            os.getenv("EKO_OAUTH_CLIENT_ID")
            or extra.get("oauth_client_id", "")
        )
        self.oauth_client_secret = (
            os.getenv("EKO_OAUTH_CLIENT_SECRET")
            or extra.get("oauth_client_secret", "")
        )

        # Webhook server
        self.webhook_host = os.getenv("EKO_HOST") or extra.get("host", "0.0.0.0")
        try:
            self.webhook_port = int(
                os.getenv("EKO_PORT") or extra.get("port", DEFAULT_WEBHOOK_PORT)
            )
        except (TypeError, ValueError):
            self.webhook_port = DEFAULT_WEBHOOK_PORT
        self.webhook_path = (
            os.getenv("EKO_WEBHOOK_PATH")
            or extra.get("webhook_path", DEFAULT_WEBHOOK_PATH)
        )

        # Allowlist
        self.allow_all = _truthy_env(
            "EKO_ALLOW_ALL_USERS", bool(extra.get("allow_all_users", False))
        )
        self.allowed_users = _csv_set(
            os.getenv("EKO_ALLOWED_USERS", "")
        ) | set(extra.get("allowed_users", []))

        # Reply token TTL
        try:
            self.reply_token_ttl = float(
                os.getenv("EKO_REPLY_TOKEN_TTL")
                or extra.get("reply_token_ttl", DEFAULT_REPLY_TOKEN_TTL)
            )
        except (TypeError, ValueError):
            self.reply_token_ttl = DEFAULT_REPLY_TOKEN_TTL

        # Runtime state
        self._client: Optional[_EkoClient] = None
        self._app = None  # aiohttp.web.Application
        self._runner = None  # aiohttp.web.AppRunner
        self._site = None  # aiohttp.web.TCPSite
        self._reply_tokens: Dict[str, Tuple[str, float]] = {}  # chat_id → (token, expiry)
        self._dedup = _MessageDeduplicator()
        self._bot_user_id: Optional[str] = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        if not self.base_url:
            self._set_fatal_error(
                "config_missing",
                "EKO_BASE_URL must be set",
                retryable=False,
            )
            return False
        if not self.oauth_client_id or not self.oauth_client_secret:
            self._set_fatal_error(
                "config_missing",
                "EKO_OAUTH_CLIENT_ID and EKO_OAUTH_CLIENT_SECRET must be set",
                retryable=False,
            )
            return False

        self._client = _EkoClient(
            base_url=self.base_url,
            client_id=self.oauth_client_id,
            client_secret=self.oauth_client_secret,
        )

        # Verify OAuth credentials work before starting the webhook server.
        try:
            await self._client.ensure_token()
        except Exception as exc:
            self._set_fatal_error(
                "auth_failed",
                f"Eko OAuth authentication failed: {exc}",
                retryable=True,
            )
            return False

        # Start the aiohttp webhook server.
        try:
            from aiohttp import web
        except ImportError:
            self._set_fatal_error(
                "missing_dep",
                "aiohttp is required for the Eko adapter — install with `pip install aiohttp`",
                retryable=False,
            )
            return False

        self._app = web.Application(client_max_size=WEBHOOK_BODY_MAX_BYTES)
        self._app.router.add_post(self.webhook_path, self._handle_webhook)
        self._app.router.add_get(
            f"{self.webhook_path}/health", self._handle_health
        )

        self._runner = web.AppRunner(self._app)
        try:
            await self._runner.setup()
            self._site = web.TCPSite(
                self._runner, self.webhook_host, self.webhook_port
            )
            await self._site.start()
        except OSError as exc:
            self._set_fatal_error(
                "bind_failed",
                f"Could not bind Eko webhook on {self.webhook_host}:{self.webhook_port}: {exc}",
                retryable=True,
            )
            return False

        self._mark_connected()
        logger.info(
            "Eko: webhook listening on %s:%s%s",
            self.webhook_host,
            self.webhook_port,
            self.webhook_path,
        )
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()

        if self._site is not None:
            try:
                await self._site.stop()
            except Exception:
                pass
            self._site = None
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                pass
            self._runner = None
        self._app = None
```

- [ ] **Step 2: Commit**

```bash
git add plugins/platforms/eko/adapter.py
git commit -m "feat(eko): add EkoAdapter init, connect, disconnect"
```

---

## Task 5: Add webhook handlers and inbound event processing

**Files:**
- Modify: `plugins/platforms/eko/adapter.py` (append after `disconnect`)

- [ ] **Step 1: Add webhook handlers, event dispatch, and message processing**

Append after `disconnect()` method:

```python

    # ------------------------------------------------------------------
    # Webhook handlers
    # ------------------------------------------------------------------

    async def _handle_health(self, request) -> Any:
        from aiohttp import web
        return web.json_response({"status": "ok", "platform": "eko"})

    async def _handle_webhook(self, request) -> Any:
        from aiohttp import web

        try:
            body = await request.read()
        except Exception as exc:
            logger.debug("Eko: read failed: %s", exc)
            return web.Response(status=400, text="bad request")
        if len(body) > WEBHOOK_BODY_MAX_BYTES:
            return web.Response(status=413, text="payload too large")

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return web.Response(status=400, text="bad json")

        events = payload.get("events", []) or []
        for event in events:
            try:
                await self._dispatch_event(event)
            except Exception:
                logger.exception("Eko: dispatch_event failed")

        return web.Response(status=200, text="ok")

    async def _dispatch_event(self, event: Dict[str, Any]) -> None:
        event_type = event.get("type")

        # Dedup retries.
        if self._dedup.is_duplicate(event):
            logger.debug("Eko: ignoring duplicate event")
            return

        # Filter self-messages if we know our own user ID.
        source = event.get("source") or {}
        sender_uid = source.get("userId") or source.get("uid", "")
        if self._bot_user_id and sender_uid == self._bot_user_id:
            return

        # Allowlist gate.
        if not self._allowed_for_source(source):
            logger.info("Eko: rejecting unauthorized source %s", source)
            return

        if event_type == "message":
            await self._handle_message_event(event)
        elif event_type == "join":
            logger.info("Eko: user created chat — %s", source)
        else:
            logger.debug("Eko: ignoring event type %r", event_type)

    async def _handle_message_event(self, event: Dict[str, Any]) -> None:
        msg = event.get("message") or {}
        msg_type = msg.get("type", "")
        message_id = msg.get("id", "")
        reply_token = event.get("replyToken", "")
        source = event.get("source") or {}

        # Eko source: {"type": "user", "userId": "...", "username": "..."}
        # or {"type": "direct_chat", "uid": "..."}
        uid = source.get("userId") or source.get("uid", "")
        username = source.get("username", "") or uid

        # Stash the reply token for outbound use.
        if uid and reply_token:
            self._reply_tokens[uid] = (
                reply_token,
                time.time() + self.reply_token_ttl,
            )

        # Extract text.
        if msg_type == "text":
            text = msg.get("text", "") or ""
        elif msg_type == "image":
            # Media support deferred — surface a placeholder.
            text = "[image]"
        elif msg_type == "file":
            text = "[file]"
        else:
            text = f"[unsupported message type: {msg_type}]"

        source_obj = self.build_source(
            chat_id=uid,
            chat_type="dm",
            user_id=uid,
            user_name=username,
            chat_name=username,
        )

        event_obj = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source_obj,
            raw_message=event,
            message_id=message_id,
        )

        await self.handle_message(event_obj)

    def _allowed_for_source(self, source: Dict[str, Any]) -> bool:
        """Check if the source user is in the allowlist."""
        if self.allow_all:
            return True
        uid = source.get("userId") or source.get("uid", "")
        if not uid:
            return False
        return uid in self.allowed_users
```

- [ ] **Step 2: Commit**

```bash
git add plugins/platforms/eko/adapter.py
git commit -m "feat(eko): add webhook handlers and inbound event processing"
```

---

## Task 6: Add outbound send and adapter interface methods

**Files:**
- Modify: `plugins/platforms/eko/adapter.py` (append after `_allowed_for_source`)

- [ ] **Step 1: Add send, send_typing, get_chat_info, format_message, and reply token helpers**

Append after `_allowed_for_source()` method:

```python

    # ------------------------------------------------------------------
    # Outbound send (text)
    # ------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if not self._client:
            return SendResult(success=False, error="Eko adapter not connected")

        # Try reply token first, fall back to push.
        token, used_reply = self._consume_reply_token(chat_id)
        if used_reply:
            try:
                await self._client.reply_text(token, content)
                return SendResult(success=True, message_id=token)
            except RuntimeError as exc:
                if "401" in str(exc):
                    # Token expired or invalid — retry with fresh auth + push.
                    try:
                        await self._client.push_text(chat_id, content)
                        return SendResult(success=True, message_id=None)
                    except Exception as exc2:
                        logger.error("Eko: push after 401 failed: %s", exc2)
                        return SendResult(success=False, error=str(exc2))
                logger.info(
                    "Eko: reply token rejected (%s); falling back to push", exc
                )
                # Fall through to push.

        try:
            await self._client.push_text(chat_id, content)
            return SendResult(success=True, message_id=None)
        except RuntimeError as exc:
            if "401" in str(exc):
                # Retry once with fresh token.
                try:
                    await self._client.push_text(chat_id, content)
                    return SendResult(success=True, message_id=None)
                except Exception as exc2:
                    return SendResult(success=False, error=str(exc2))
            logger.error("Eko: push send failed: %s", exc)
            return SendResult(success=False, error=str(exc), retryable=True)
        except Exception as exc:
            logger.error("Eko: send failed: %s", exc)
            return SendResult(success=False, error=str(exc), retryable=True)

    def _consume_reply_token(self, chat_id: str) -> Tuple[str, bool]:
        """Consume a stashed reply token if present and unexpired.

        Returns ``(token, used_reply)``.
        """
        entry = self._reply_tokens.pop(chat_id, None)
        if not entry:
            return "", False
        token, expires_at = entry
        if not token or time.time() >= expires_at:
            return "", False
        return token, True

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """No-op — Eko has no documented typing indicator API."""

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id or "", "type": "dm"}

    def format_message(self, content: str) -> str:
        return content
```

- [ ] **Step 2: Commit**

```bash
git add plugins/platforms/eko/adapter.py
git commit -m "feat(eko): add outbound send and adapter interface methods"
```

---

## Task 7: Add plugin hooks and register function

**Files:**
- Modify: `plugins/platforms/eko/adapter.py` (append after `format_message`)

- [ ] **Step 1: Add check_requirements, validate_config, is_connected, _env_enablement, _standalone_send, interactive_setup, and register**

Append after `format_message()` method:

```python


# ---------------------------------------------------------------------------
# Plugin entry-point hooks
# ---------------------------------------------------------------------------

def check_requirements() -> bool:
    """Plugin gate: require credentials AND aiohttp at runtime."""
    if not os.getenv("EKO_BASE_URL"):
        return False
    if not os.getenv("EKO_OAUTH_CLIENT_ID"):
        return False
    if not os.getenv("EKO_OAUTH_CLIENT_SECRET"):
        return False
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        return False
    return True


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    has_url = bool(os.getenv("EKO_BASE_URL") or extra.get("base_url"))
    has_id = bool(
        os.getenv("EKO_OAUTH_CLIENT_ID") or extra.get("oauth_client_id")
    )
    has_secret = bool(
        os.getenv("EKO_OAUTH_CLIENT_SECRET") or extra.get("oauth_client_secret")
    )
    return has_url and has_id and has_secret


def is_connected(config) -> bool:
    """Surface in ``hermes status`` even before the adapter is instantiated."""
    return validate_config(config)


def _env_enablement() -> Optional[Dict[str, Any]]:
    """Auto-seed PlatformConfig.extra from env-only setups."""
    if not (
        os.getenv("EKO_BASE_URL")
        and os.getenv("EKO_OAUTH_CLIENT_ID")
        and os.getenv("EKO_OAUTH_CLIENT_SECRET")
    ):
        return None
    seeded: Dict[str, Any] = {}
    if os.getenv("EKO_PORT"):
        try:
            seeded["port"] = int(os.environ["EKO_PORT"])
        except ValueError:
            pass
    if os.getenv("EKO_HOST"):
        seeded["host"] = os.environ["EKO_HOST"]
    if os.getenv("EKO_WEBHOOK_PATH"):
        seeded["webhook_path"] = os.environ["EKO_WEBHOOK_PATH"]
    if os.getenv("EKO_HOME_CHANNEL"):
        seeded["home_channel"] = os.environ["EKO_HOME_CHANNEL"]
    return seeded or {}


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Out-of-process push delivery for cron jobs running detached from the gateway."""
    extra = getattr(pconfig, "extra", {}) or {}
    base_url = os.getenv("EKO_BASE_URL") or extra.get("base_url", "")
    client_id = os.getenv("EKO_OAUTH_CLIENT_ID") or extra.get("oauth_client_id", "")
    client_secret = os.getenv("EKO_OAUTH_CLIENT_SECRET") or extra.get("oauth_client_secret", "")
    if not base_url or not client_id or not client_secret or not chat_id:
        return {"error": "Eko standalone send: missing config or chat_id"}

    client = _EkoClient(base_url, client_id, client_secret)
    try:
        await client.push_text(chat_id, message)
        return {"success": True, "message_id": None}
    except Exception as exc:
        return {"error": str(exc)}


def interactive_setup() -> None:
    """Minimal stdin wizard for ``hermes setup eko``."""
    print()
    print("Eko Messaging API setup")
    print("-----------------------")
    print("Create a Webhook API bot at your Eko admin panel,")
    print("then copy the values below.")
    print()

    try:
        from hermes_cli.config import get_env_var, set_env_var
    except ImportError:
        print(
            "hermes_cli.config not available; "
            "set EKO_* vars manually in ~/.hermes/.env"
        )
        return

    def _prompt(var: str, prompt: str, *, secret: bool = False) -> None:
        existing = get_env_var(var) if callable(get_env_var) else None
        suffix = " [keep current]" if existing else ""
        try:
            if secret:
                import getpass
                value = getpass.getpass(f"{prompt}{suffix}: ")
            else:
                value = input(f"{prompt}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if value:
            set_env_var(var, value)

    _prompt("EKO_BASE_URL", "Eko base URL (e.g. https://customer-h1.ekoapp.com)")
    _prompt("EKO_OAUTH_CLIENT_ID", "OAuth client ID")
    _prompt("EKO_OAUTH_CLIENT_SECRET", "OAuth client secret", secret=True)
    _prompt("EKO_ALLOWED_USERS", "Allowed user IDs (comma-separated; blank=skip)")
    print(
        "Done. Set the webhook URL in the Eko admin panel to "
        "<your-public-url>/eko/webhook"
    )


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system at startup."""
    ctx.register_platform(
        name="eko",
        label="Eko",
        adapter_factory=lambda cfg: EkoAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=[
            "EKO_BASE_URL",
            "EKO_OAUTH_CLIENT_ID",
            "EKO_OAUTH_CLIENT_SECRET",
        ],
        install_hint="pip install aiohttp",
        setup_fn=interactive_setup,
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="EKO_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="EKO_ALLOWED_USERS",
        allow_all_env="EKO_ALLOW_ALL_USERS",
        emoji="💬",
        pii_safe=False,
        allow_update_command=True,
        platform_hint=(
            "You are chatting via Eko Messaging API. Messages are plain text. "
            "Keep responses concise and well-structured. "
            "Media support (images, files) is not yet available."
        ),
    )
```

- [ ] **Step 2: Commit**

```bash
git add plugins/platforms/eko/adapter.py
git commit -m "feat(eko): add plugin hooks and register function"
```

---

## Task 8: Verify the adapter loads correctly

**Files:**
- No new files

- [ ] **Step 1: Check that Python can import the module without errors**

```bash
cd /home/jo/hermes-sivwork/hermes-agent
python -c "import ast; ast.parse(open('plugins/platforms/eko/adapter.py').read()); print('Syntax OK')"
```

Expected: `Syntax OK`

- [ ] **Step 2: Verify the plugin directory structure is complete**

```bash
ls -la plugins/platforms/eko/
```

Expected: `__init__.py`, `adapter.py`, `plugin.yaml` all present.

- [ ] **Step 3: Commit (if any minor fixes were needed)**

```bash
git add -A plugins/platforms/eko/
git commit -m "fix(eko): address import/syntax issues from verification"
```

(Only if fixes were needed — skip if Step 1 passed cleanly.)
