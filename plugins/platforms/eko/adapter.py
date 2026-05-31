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

**Webhook signature verification.** Inbound webhook requests are verified
via the ``X-Eko-Signature`` header (HMAC-SHA256 of the raw body, Base64-
encoded, keyed by the OAuth client secret). If the header is present but
the signature doesn't match, the request is rejected with 403.

**Configurable base URL.** Eko uses customer-specific hostnames
(e.g. ``customer-h1.ekoapp.com``) so the base URL is a required env var.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.config import Platform

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_WEBHOOK_PORT = 8647
DEFAULT_WEBHOOK_PATH = "/eko/webhook"
DEFAULT_REPLY_TOKEN_TTL = 50  # conservative below Eko's estimated ~60 s
WEBHOOK_BODY_MAX_BYTES = 1_048_576  # 1 MiB
DEFAULT_MESSAGE_MAX_CHARS = 5000  # conservative until Eko limit confirmed
DEFAULT_MAX_UPLOAD_BYTES = 26_214_400  # 25 MiB
DEFAULT_MAX_INBOUND_MEDIA_BYTES = 26_214_400  # 25 MiB


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


def _parse_explicit_routing(chat_id: str) -> Optional[Dict[str, str]]:
    """Parse explicit group/topic routing from a chat_id string.

    Accepts the format ``group:<gid>:topic:<tid>`` and returns
    ``{"groupId": gid, "topicId": tid}`` when valid, or ``None``
    when the chat_id does not use the explicit routing format.

    Returns a sentinel dict with an ``"error"`` key when the format
    is recognised but malformed (missing gid or tid values).
    """
    if not chat_id.startswith("group:"):
        return None
    parts = chat_id.split(":")
    # Expected: ["group", <gid>, "topic", <tid>]
    if len(parts) != 4 or parts[2] != "topic":
        return {"error": f"Invalid explicit routing format: {chat_id!r}. "
                        f"Expected group:<gid>:topic:<tid>"}
    gid, tid = parts[1], parts[3]
    if not gid or not tid:
        return {"error": f"Invalid explicit routing format: {chat_id!r}. "
                        f"group ID and topic ID must be non-empty"}
    return {"groupId": gid, "topicId": tid}


# Re-export client class for backward compat within this package.
try:
    from .client import _EkoClient  # noqa: F401
except ImportError:
    # Test loader imports adapter.py as a standalone module
    # (no package context), so relative import fails.
    import importlib.util as _ilu
    import sys as _sys
    from pathlib import Path as _Path

    _client_path = _Path(__file__).with_name("client.py")
    _client_spec = _ilu.spec_from_file_location(
        "plugins.platforms.eko.client", _client_path
    )
    if _client_spec and _client_spec.loader:
        _client_mod = _ilu.module_from_spec(_client_spec)
        _sys.modules["plugins.platforms.eko.client"] = _client_mod
        _client_spec.loader.exec_module(_client_mod)
        _EkoClient = _client_mod._EkoClient
    else:
        raise ImportError(f"Cannot load _EkoClient from {_client_path}")


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
        self.webhook_secret = (
            os.getenv("EKO_WEBHOOK_SECRET")
            or extra.get("webhook_secret", "")
        ) or self.oauth_client_secret
        self.require_signature = _truthy_env(
            "EKO_REQUIRE_SIGNATURE", bool(extra.get("require_signature", True))
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

        # Group/topic allowlist (#26)
        self.allow_all_groups = _truthy_env(
            "EKO_ALLOW_ALL_GROUPS", bool(extra.get("allow_all_groups", True))
        )
        self.allowed_groups = _csv_set(
            os.getenv("EKO_ALLOWED_GROUPS", "")
        ) | set(extra.get("allowed_groups", []))
        # Topics use gid:tid format
        self.allowed_topics = _csv_set(
            os.getenv("EKO_ALLOWED_TOPICS", "")
        ) | set(extra.get("allowed_topics", []))

        # Require mention in groups (#22)
        self.require_mention = _truthy_env(
            "EKO_REQUIRE_MENTION", bool(extra.get("require_mention", True))
        )
        _triggers = (
            os.getenv("EKO_MENTION_TRIGGERS", "")
            or ",".join(extra.get("mention_triggers", []))
        )
        self.mention_triggers = [w.strip() for w in _triggers.split(",") if w.strip()]

        # Reply token TTL
        try:
            self.reply_token_ttl = float(
                os.getenv("EKO_REPLY_TOKEN_TTL")
                or extra.get("reply_token_ttl", DEFAULT_REPLY_TOKEN_TTL)
            )
        except (TypeError, ValueError):
            self.reply_token_ttl = DEFAULT_REPLY_TOKEN_TTL

        # Outbound message chunking
        try:
            self.message_max_chars = int(
                os.getenv("EKO_MESSAGE_MAX_CHARS")
                or extra.get("message_max_chars", DEFAULT_MESSAGE_MAX_CHARS)
            )
        except (TypeError, ValueError):
            self.message_max_chars = DEFAULT_MESSAGE_MAX_CHARS

        # Upload size limit (outbound)
        try:
            self.max_upload_bytes = int(
                os.getenv("EKO_MAX_UPLOAD_BYTES")
                or extra.get("max_upload_bytes", DEFAULT_MAX_UPLOAD_BYTES)
            )
        except (TypeError, ValueError):
            self.max_upload_bytes = DEFAULT_MAX_UPLOAD_BYTES

        # Inbound media size limit
        try:
            self.max_inbound_media_bytes = int(
                os.getenv("EKO_MAX_INBOUND_MEDIA_BYTES")
                or extra.get("max_inbound_media_bytes", DEFAULT_MAX_INBOUND_MEDIA_BYTES)
            )
        except (TypeError, ValueError):
            self.max_inbound_media_bytes = DEFAULT_MAX_INBOUND_MEDIA_BYTES

        # Runtime state
        self._client: Optional[_EkoClient] = None
        self._app = None  # aiohttp.web.Application
        self._runner = None  # aiohttp.web.AppRunner
        self._site = None  # aiohttp.web.TCPSite
        self._reply_tokens: Dict[str, Tuple[str, float]] = {}  # chat_id -> (token, expiry)
        self._dedup = _MessageDeduplicator()
        # Session routing: maps composite chat_id to Eko routing metadata
        # so outbound push can resolve uid / groupId / topicId.
        self._session_routing: Dict[str, Dict[str, str]] = {}
        # Reserved for future self-message filtering if Eko provides
        # a bot identity API.
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
                "aiohttp is required for the Eko adapter - install with `pip install aiohttp`",
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
        except Exception as exc:
            # Clean up partially initialized runner on failure.
            if self._runner is not None:
                try:
                    await self._runner.cleanup()
                except Exception:
                    pass
                self._runner = None
            self._site = None
            self._set_fatal_error(
                "bind_failed",
                f"Could not start Eko webhook on {self.webhook_host}:{self.webhook_port}: {exc}",
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

        # Verify X-Eko-Signature (HMAC-SHA256 of raw body, Base64-encoded,
        # keyed by the OAuth client secret). Signature verification is
        # required by default; local dev can disable it with
        # EKO_REQUIRE_SIGNATURE=false.
        sig_header = request.headers.get("x-eko-signature", "")
        if not sig_header:
            if self.require_signature:
                logger.warning("Eko: missing X-Eko-Signature — rejecting webhook")
                return web.Response(status=401, text="missing signature")
            logger.debug("Eko: no X-Eko-Signature header — skipping verification")
        elif not self._verify_signature(body, sig_header):
            logger.warning("Eko: invalid X-Eko-Signature — rejecting webhook")
            return web.Response(status=403, text="invalid signature")

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
            logger.info("Eko: user created chat - %s", source)
        else:
            logger.debug("Eko: ignoring event type %r", event_type)

    async def _handle_message_event(self, event: Dict[str, Any]) -> None:
        msg = event.get("message") or {}
        msg_type = msg.get("type", "")
        message_id = msg.get("id", "")
        reply_token = event.get("replyToken", "")
        source = event.get("source") or {}

        uid = source.get("userId") or source.get("uid", "")
        username = source.get("username", "") or uid

        # Build a stable chat_id from groupId + topicId so each Eko
        # conversation (topic) gets its own Hermes session.
        group_id = msg.get("groupId", "")
        topic_id = msg.get("topicId", "")
        group_type = msg.get("groupType", "")
        session_id = event.get("sessionId", "")

        if session_id:
            chat_id = session_id
        elif group_id and topic_id:
            chat_id = f"{group_id}_{topic_id}"
        else:
            chat_id = uid

        # Determine chat_type from groupType.
        if group_type == "direct_chat":
            chat_type = "dm"
        elif group_type:
            chat_type = "group"
        else:
            chat_type = "dm"

        # -- Group-level filters (only for non-DM conversations) ---------
        if chat_type == "group" and group_id:
            # Extract raw text for mention matching (before media processing).
            raw_text = msg.get("text", "") or "" if msg_type == "text" else ""

            # #26: Group/topic allowlist gate.
            if not self._allowed_group(group_id, topic_id):
                logger.info(
                    "Eko: rejecting group %s topic %s (not in allowlist)",
                    group_id, topic_id,
                )
                return

            # #22: Require mention filter.
            # Slash commands (e.g. /new, /stop) bypass the mention filter —
            # they are explicit bot directives, not casual group chat.
            is_slash_command = raw_text.startswith("/")
            if (
                self.require_mention
                and not is_slash_command
                and not self._has_mention_trigger(raw_text)
            ):
                logger.debug(
                    "Eko: ignoring group message (require_mention, no trigger)"
                )
                return

        # Store routing metadata so outbound send can resolve
        # the user uid for push fallback.
        self._session_routing[chat_id] = {
            "uid": uid,
            "groupId": group_id,
            "topicId": topic_id,
            "groupType": group_type,
        }

        # Stash the reply token keyed by chat_id.
        if chat_id and reply_token:
            self._reply_tokens[chat_id] = (
                reply_token,
                time.time() + self.reply_token_ttl,
            )

        # Media attachments (downloaded and cached locally).
        media_urls: List[str] = []
        media_types: List[str] = []
        text = ""
        message_type = MessageType.TEXT

        if msg_type == "text":
            text = msg.get("text", "") or ""
        elif msg_type == "picture":
            local_path = await self._download_picture(msg)
            if local_path:
                media_urls.append(local_path)
                media_types.append(self._mime_from_filename(msg.get("fileName", "")))
                message_type = MessageType.PHOTO
            text = "[image]"
        elif msg_type == "sticker":
            text = "[sticker]"
        elif msg_type == "file":
            text = "[file]"
        else:
            text = f"[unsupported message type: {msg_type}]"

        # For group chats, use group_id as chat_name so the agent
        # doesn't confuse the sender's username with the group name.
        # For DMs, the sender's username is the correct chat name.
        effective_chat_name = group_id if chat_type == "group" and group_id else username

        source_obj = self.build_source(
            chat_id=chat_id,
            chat_type=chat_type,
            user_id=uid,
            user_name=username,
            chat_name=effective_chat_name,
            thread_id=topic_id or None,
        )

        event_obj = MessageEvent(
            text=text,
            message_type=message_type,
            source=source_obj,
            raw_message=event,
            message_id=message_id,
            media_urls=media_urls,
            media_types=media_types,
        )

        await self.handle_message(event_obj)

    async def _download_picture(self, msg: Dict[str, Any]) -> Optional[str]:
        """Download an inbound picture and cache it locally.

        Returns the cached file path, or None on failure.
        """
        picture_id = msg.get("pictureId", "")
        if not picture_id or not self._client:
            return None
        try:
            data = await self._client.fetch_picture(picture_id)
        except Exception as exc:
            logger.warning("Eko: failed to download picture %s: %s", picture_id, exc)
            return None
        max_inbound = getattr(self, 'max_inbound_media_bytes', DEFAULT_MAX_INBOUND_MEDIA_BYTES)
        if max_inbound and len(data) > max_inbound:
            logger.warning(
                "Eko: inbound picture %s too large (%d bytes, limit %d)",
                picture_id, len(data), max_inbound,
            )
            return None
        ext = self._ext_from_filename(msg.get("fileName", ""), default=".jpg")
        try:
            from gateway.platforms.base import cache_image_from_bytes
            return cache_image_from_bytes(data, ext=ext)
        except Exception as exc:
            logger.warning("Eko: failed to cache picture %s: %s", picture_id, exc)
            return None

    @staticmethod
    def _ext_from_filename(filename: str, default: str = ".bin") -> str:
        """Extract extension from a filename, with a fallback."""
        if filename and "." in filename:
            ext = "." + filename.rsplit(".", 1)[-1].lower()
            return ext if len(ext) <= 10 else default
        return default

    @staticmethod
    def _mime_from_filename(filename: str) -> str:
        """Guess MIME type from filename extension."""
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        return {
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "gif": "image/gif",
            "webp": "image/webp",
        }.get(ext, "image/jpeg")

    def _allowed_for_source(self, source: Dict[str, Any]) -> bool:
        """Check if the source user is in the allowlist."""
        if self.allow_all:
            return True
        uid = source.get("userId") or source.get("uid", "")
        if not uid:
            return False
        return uid in self.allowed_users

    def _allowed_group(self, group_id: str, topic_id: str = "") -> bool:
        """Check if a group/topic is in the allowlist (#26).

        allow_all_groups=true  → always allow.
        Otherwise: group_id must be in allowed_groups, OR
                   group_id:topic_id must be in allowed_topics.
        """
        if self.allow_all_groups:
            return True
        if group_id in self.allowed_groups:
            return True
        if topic_id and f"{group_id}:{topic_id}" in self.allowed_topics:
            return True
        return False

    def _has_mention_trigger(self, text: str) -> bool:
        """Check if text contains an @mention trigger (#22).

        Eko sends @mentions as plain text (e.g. "@Hermes Agent").
        Matching is case-sensitive (Eko autocompletes exact bot name).
        Also matches @all (group-wide mention).
        """
        # @all always triggers (Eko group-wide mention).
        if "@all" in text:
            # Word boundary check: @all must not be part of a longer word
            idx = text.find("@all")
            while idx != -1:
                end = idx + 4  # len("@all")
                if end >= len(text) or not text[end].isalnum():
                    return True
                idx = text.find("@all", idx + 1)

        triggers = self.mention_triggers or ["Hermes Agent"]
        for trigger in triggers:
            # Case-sensitive: look for @trigger in text.
            needle = "@" + trigger
            idx = text.find(needle)
            while idx != -1:
                end = idx + len(needle)
                # Word boundary after: end of string or non-alnum char.
                if end >= len(text) or not text[end].isalnum():
                    return True
                idx = text.find(needle, idx + 1)
        return False

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

        chunks = self.truncate_message(content, self.message_max_chars)
        if not chunks:
            return SendResult(success=True)

        # Filter out empty chunks (can happen when message is only MEDIA: tags).
        chunks = [c for c in chunks if c.strip()]
        if not chunks:
            return SendResult(success=True)

        last_result = SendResult(success=True)
        for i, chunk in enumerate(chunks):
            if i == 0:
                # First chunk: try reply token, fall back to push.
                last_result = await self._send_reply_or_push(chat_id, chunk)
            else:
                # Subsequent chunks: push only (reply token is single-use).
                last_result = await self._send_push_only(chat_id, chunk)
            if not last_result.success:
                return last_result
        return last_result

    def _resolve_uid(self, chat_id: str) -> str:
        """Resolve the Eko user uid for push delivery.

        chat_id may be a sessionId (groupId_topicId) or a plain uid.
        """
        routing = self._session_routing.get(chat_id) if hasattr(self, '_session_routing') else None
        if routing:
            return routing.get("uid", chat_id)
        return chat_id

    def _get_routing(self, chat_id: str) -> Optional[Dict[str, str]]:
        """Return routing metadata for a chat_id, or None."""
        return self._session_routing.get(chat_id) if hasattr(self, '_session_routing') else None

    def _is_group_chat(self, chat_id: str) -> bool:
        """Check if chat_id maps to a group/topic (not a bare DM uid).

        Returns True when the routing metadata has both groupId and topicId,
        meaning the conversation is within a group+topic context that requires
        the ``/bot/v1/group/*`` endpoints — regardless of ``groupType``.
        Eko sets ``groupType: "direct_chat"`` even for topics inside DM-type
        groups, so we cannot rely on it to distinguish DM from topic routing.
        """
        routing = self._get_routing(chat_id)
        return bool(
            routing
            and routing.get("groupId")
            and routing.get("topicId")
        )

    async def _send_reply_or_push(
        self, chat_id: str, content: str
    ) -> SendResult:
        """Send content using reply token first, push as fallback."""
        uid = self._resolve_uid(chat_id)
        routing = self._get_routing(chat_id)
        is_group = self._is_group_chat(chat_id)
        token, used_reply = self._consume_reply_token(chat_id)
        if used_reply:
            try:
                await self._client.reply_text(token, content)
                return SendResult(success=True, message_id=token)
            except Exception as exc:
                logger.info(
                    "Eko: reply token rejected (%s); falling back to push", exc
                )
                # Fall through to push.

        try:
            if is_group and routing:
                await self._client.push_group_text(routing["groupId"], routing["topicId"], content)
            else:
                await self._client.push_text(uid, content)
            return SendResult(success=True, message_id=None)
        except RuntimeError as exc:
            logger.error("Eko: push send failed: %s", exc)
            return SendResult(success=False, error=str(exc), retryable=True)
        except Exception as exc:
            logger.error("Eko: send failed: %s", exc)
            return SendResult(success=False, error=str(exc), retryable=True)

    async def _send_push_only(
        self, chat_id: str, content: str
    ) -> SendResult:
        """Send content via push API only (for chunk N+1)."""
        uid = self._resolve_uid(chat_id)
        routing = self._get_routing(chat_id)
        is_group = self._is_group_chat(chat_id)

        async def _do_push():
            if is_group and routing:
                await self._client.push_group_text(routing["groupId"], routing["topicId"], content)
            else:
                await self._client.push_text(uid, content)

        try:
            await _do_push()
            return SendResult(success=True, message_id=None)
        except Exception as exc:
            logger.error("Eko: push chunk failed: %s", exc)
            return SendResult(success=False, error=str(exc), retryable=True)

    # ------------------------------------------------------------------
    # Outbound send (images and files)
    # ------------------------------------------------------------------

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send a local image file to an Eko user or group/topic."""
        if not self._client:
            return SendResult(success=False, error="Eko adapter not connected")

        from pathlib import Path

        try:
            fpath = Path(image_path)
            # Check file size before reading into memory.
            try:
                file_size = fpath.stat().st_size
                max_upload = getattr(self, 'max_upload_bytes', DEFAULT_MAX_UPLOAD_BYTES)
                if max_upload and file_size > max_upload:
                    return SendResult(
                        success=False,
                        error=f"Image file too large ({file_size} bytes, limit {max_upload})",
                    )
            except OSError:
                pass  # stat failed; let read_bytes try
            file_bytes = fpath.read_bytes()
        except OSError as exc:
            return SendResult(success=False, error=f"Cannot read image: {exc}")

        filename = Path(image_path).name or "image.jpg"
        uid = self._resolve_uid(chat_id)
        routing = self._get_routing(chat_id)
        is_group = self._is_group_chat(chat_id)
        _caption = caption or ""

        async def _do_push_picture():
            if is_group and routing:
                await self._client.push_group_picture(
                    routing["groupId"], routing["topicId"],
                    file_bytes, filename, caption=_caption,
                )
            else:
                await self._client.push_picture(uid, file_bytes, filename, caption=_caption)

        # Try reply token first, fall back to push.
        token, used_reply = self._consume_reply_token(chat_id)
        if used_reply:
            try:
                await self._client.reply_picture(token, file_bytes, filename)
                return SendResult(success=True, message_id=token)
            except Exception as exc:
                logger.info("Eko: reply picture rejected (%s); falling back to push", exc)

        try:
            await _do_push_picture()
            return SendResult(success=True, message_id=None)
        except RuntimeError as exc:
            return SendResult(success=False, error=str(exc), retryable=True)
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=True)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an image from a URL to an Eko user.

        Downloads the image to a local cache first, then delegates to
        send_image_file for native delivery.
        """
        try:
            from gateway.platforms.base import cache_image_from_url

            local_path = await cache_image_from_url(image_url)
        except Exception as exc:
            logger.warning("Eko: failed to download image URL: %s", exc)
            return SendResult(success=False, error=f"Cannot download image: {exc}")

        return await self.send_image_file(
            chat_id, local_path, caption=caption, reply_to=reply_to, metadata=metadata
        )

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send a file/document to an Eko user or group/topic."""
        if not self._client:
            return SendResult(success=False, error="Eko adapter not connected")

        from pathlib import Path

        try:
            fpath = Path(file_path)
            # Check file size before reading into memory.
            try:
                file_size = fpath.stat().st_size
                max_upload = getattr(self, 'max_upload_bytes', DEFAULT_MAX_UPLOAD_BYTES)
                if max_upload and file_size > max_upload:
                    return SendResult(
                        success=False,
                        error=f"File too large ({file_size} bytes, limit {max_upload})",
                    )
            except OSError:
                pass  # stat failed; let read_bytes try
            file_bytes = fpath.read_bytes()
        except OSError as exc:
            return SendResult(success=False, error=f"Cannot read file: {exc}")

        filename = file_name or Path(file_path).name or "document"
        uid = self._resolve_uid(chat_id)
        routing = self._get_routing(chat_id)
        is_group = self._is_group_chat(chat_id)

        async def _do_push_file():
            if is_group and routing:
                await self._client.push_group_file(
                    routing["groupId"], routing["topicId"],
                    file_bytes, filename,
                )
            else:
                await self._client.push_file(uid, file_bytes, filename)

        # No reply-token endpoint documented for files — always push.
        try:
            await _do_push_file()
            return SendResult(success=True, message_id=None)
        except RuntimeError as exc:
            return SendResult(success=False, error=str(exc), retryable=True)
        except Exception as exc:
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

    def _verify_signature(self, body: bytes, signature: str) -> bool:
        """Verify X-Eko-Signature HMAC-SHA256-Base64 digest.

        Eko signs webhook payloads with HMAC-SHA256. The signing key
        defaults to the OAuth client secret but can be overridden via
        ``EKO_WEBHOOK_SECRET`` for tenants that provide a separate key.

        The header is normalized before comparison:
        - Leading/trailing whitespace is stripped.
        - An optional ``sha256=`` prefix is stripped so proxy-added
          prefixes still verify against the raw Base64 digest.
        """
        if not self.webhook_secret:
            return False
        sig = signature.strip()
        if sig.lower().startswith("sha256="):
            sig = sig[7:]
        expected = base64.b64encode(
            hmac.new(
                self.webhook_secret.encode("utf-8"),
                body,
                hashlib.sha256,
            ).digest()
        ).decode("utf-8")
        return hmac.compare_digest(expected, sig)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """No-op - Eko has no documented typing indicator API."""

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        routing = self._get_routing(chat_id)
        if routing and routing.get("groupId"):
            group_id = routing.get("groupId", "")
            topic_id = routing.get("topicId", "")
            chat_type = "topic" if topic_id else "group"
            return {
                "name": chat_id,
                "type": chat_type,
                "group_id": group_id,
                "topic_id": topic_id,
                "user_id": routing.get("uid", ""),
                "group_type": routing.get("groupType", ""),
            }
        return {"name": chat_id or "", "type": "dm"}

    def format_message(self, content: str) -> str:
        return content


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
    if os.getenv("EKO_WEBHOOK_SECRET"):
        seeded["webhook_secret"] = os.environ["EKO_WEBHOOK_SECRET"]
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
    """Out-of-process push delivery for cron jobs running detached from the gateway.

    Sends the text message first, then uploads any ``media_files`` as images
    or documents (detected by extension).  ``force_document`` sends all files
    as documents regardless of extension.

    Supports group/topic routing: when the live gateway adapter is available,
    resolves ``chat_id`` through its ``_session_routing`` dict to determine
    DM vs group endpoints.  Falls back to DM endpoints when no routing
    metadata is available.
    """
    extra = getattr(pconfig, "extra", {}) or {}
    base_url = os.getenv("EKO_BASE_URL") or extra.get("base_url", "")
    client_id = os.getenv("EKO_OAUTH_CLIENT_ID") or extra.get("oauth_client_id", "")
    client_secret = os.getenv("EKO_OAUTH_CLIENT_SECRET") or extra.get("oauth_client_secret", "")
    if not base_url or not client_id or not client_secret or not chat_id:
        return {"error": "Eko standalone send: missing config or chat_id"}

    client = _EkoClient(base_url, client_id, client_secret)

    # Resolve group/topic routing.
    # 1. Explicit routing format (group:<gid>:topic:<tid>) — works without gateway.
    # 2. Live adapter routing — requires running gateway.
    routing: Optional[Dict[str, str]] = None
    is_group = False
    explicit = _parse_explicit_routing(chat_id)
    if explicit is not None:
        if "error" in explicit:
            return {"error": explicit["error"]}
        routing = explicit
        is_group = True
    else:
        try:
            from gateway.run import _gateway_runner_ref
            from gateway.config import Platform as _Platform
            _runner = _gateway_runner_ref()
            if _runner:
                _adapter = _runner.adapters.get(_Platform("eko"))
                if _adapter and hasattr(_adapter, "_get_routing"):
                    routing = _adapter._get_routing(chat_id)
                    if routing and routing.get("groupId") and routing.get("topicId"):
                        is_group = True
        except Exception:
            pass

    # Send text body.
    if message:
        try:
            if is_group and routing:
                await client.push_group_text(routing["groupId"], routing["topicId"], message)
            else:
                await client.push_text(chat_id, message)
        except Exception as exc:
            return {"error": str(exc)}

    # Upload media attachments.
    _IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    media_warnings: List[str] = []

    # Resolve upload size limit from env/config.
    try:
        max_upload = int(
            os.getenv("EKO_MAX_UPLOAD_BYTES")
            or extra.get("max_upload_bytes", DEFAULT_MAX_UPLOAD_BYTES)
        )
    except (TypeError, ValueError):
        max_upload = DEFAULT_MAX_UPLOAD_BYTES

    for media_path in media_files or []:
        try:
            from pathlib import Path
            p = Path(media_path)
            file_size = p.stat().st_size
            if max_upload and file_size > max_upload:
                media_warnings.append(
                    f"File too large: {p.name} ({file_size} bytes, limit {max_upload})"
                )
                continue
            file_bytes = p.read_bytes()
            filename = p.name or "file"
            ext = p.suffix.lower()
        except OSError as exc:
            media_warnings.append(f"Cannot read {media_path}: {exc}")
            continue

        try:
            if not force_document and ext in _IMAGE_EXTS:
                if is_group and routing:
                    await client.push_group_picture(routing["groupId"], routing["topicId"], file_bytes, filename)
                else:
                    await client.push_picture(chat_id, file_bytes, filename)
            else:
                if is_group and routing:
                    await client.push_group_file(routing["groupId"], routing["topicId"], file_bytes, filename)
                else:
                    await client.push_file(chat_id, file_bytes, filename)
        except Exception as exc:
            media_warnings.append(f"Failed to send {filename}: {exc}")

    result: Dict[str, Any] = {"success": True, "message_id": None}
    if media_warnings:
        result["warnings"] = media_warnings
    return result


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
    _prompt("EKO_WEBHOOK_SECRET", "Webhook signing secret (blank = use OAuth secret)", secret=True)
    _prompt("EKO_ALLOWED_USERS", "Allowed user IDs (comma-separated; blank=skip)")
    print(
        "Done. Set the webhook URL in the Eko admin panel to "
        "<your-public-url>/eko/webhook"
    )


def register(ctx) -> None:
    """Plugin entry point - called by the Hermes plugin system at startup."""
    # Import tools module to trigger registry.register() calls.
    import plugins.platforms.eko.tools  # noqa: F401
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
            "You are chatting via Eko Messaging API. "
            "Eko renders plain text only — Markdown syntax appears as literal characters. "
            "Bare URLs are auto-linked; use https://example.com instead of "
            "[label](url). "
            "You can send images and files to the user using the send_message tool "
            "with MEDIA:<local_path> in the message. "
            "Keep responses concise."
        ),
    )