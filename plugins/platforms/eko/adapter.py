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

from .config import EkoConfig

from .outbound import OutboundSender

from .inbound import normalize_message_event

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

#
# Constants are retained in config.py as the single source of truth.
# The aliases below are still referenced by adapter internals.
#

WEBHOOK_BODY_MAX_BYTES = 1_048_576  # 1 MiB
DEFAULT_MAX_UPLOAD_BYTES = 26_214_400  # 25 MiB
DEFAULT_MAX_INBOUND_MEDIA_BYTES = 26_214_400  # 25 MiB
DEFAULT_MESSAGE_MAX_CHARS = 5000

# Legacy defaults still referenced via getattr fallbacks in send methods.
DEFAULT_WEBHOOK_PORT = 8647
DEFAULT_WEBHOOK_PATH = "/eko/webhook"
DEFAULT_REPLY_TOKEN_TTL = 50



from .client import _EkoClient
from .management import get_default_runtime as _get_management_runtime


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
        self._eko_config = EkoConfig.from_env(extra)

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

        # Outbound sender — owns route resolution, reply tokens, endpoint selection
        self._sender = OutboundSender(
            config=self._eko_config,
            client=None,  # set in connect() after client is created
            session_routing=self._session_routing,
            reply_tokens=self._reply_tokens,
        )

    def _get_sender(self) -> OutboundSender:
        """Return the outbound sender. Must be initialized by ``__init__`` or the test factory."""
        sender = self.__dict__.get("_sender")
        if sender is None:
            raise RuntimeError("OutboundSender not initialized")
        return sender



    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        cfg = self._eko_config
        if not cfg.base_url:
            self._set_fatal_error(
                "config_missing",
                "EKO_BASE_URL must be set",
                retryable=False,
            )
            return False
        if not cfg.oauth_client_id or not cfg.oauth_client_secret:
            self._set_fatal_error(
                "config_missing",
                "EKO_OAUTH_CLIENT_ID and EKO_OAUTH_CLIENT_SECRET must be set",
                retryable=False,
            )
            return False

        self._client = _EkoClient(
            base_url=cfg.base_url,
            client_id=cfg.oauth_client_id,
            client_secret=cfg.oauth_client_secret,
        )
        self._get_sender()._client = self._client

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
        self._app.router.add_post(cfg.webhook_path, self._handle_webhook)
        self._app.router.add_get(
            f"{cfg.webhook_path}/health", self._handle_health
        )

        self._runner = web.AppRunner(self._app)
        try:
            await self._runner.setup()
            self._site = web.TCPSite(
                self._runner, cfg.webhook_host, cfg.webhook_port
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
                f"Could not start Eko webhook on {cfg.webhook_host}:{cfg.webhook_port}: {exc}",
                retryable=True,
            )
            return False

        self._mark_connected()
        _get_management_runtime().set_client(self._client)
        logger.info(
            "Eko: webhook listening on %s:%s%s",
            cfg.webhook_host,
            cfg.webhook_port,
            cfg.webhook_path,
        )
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()
        _get_management_runtime().clear_client()

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
            if self._eko_config.require_signature:
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

        # User allowlist gates DMs in _handle_message_event() after the
        # Eko conversation context is known. Non-message events only carry a
        # source, so keep the source-user gate here for those events.
        if event_type != "message" and not self._allowed_for_source(source):
            logger.info("Eko: rejecting unauthorized source %s", source)
            return

        if event_type == "message":
            await self._handle_message_event(event)
        elif event_type == "join":
            logger.info("Eko: user created chat - %s", source)
        else:
            logger.debug("Eko: ignoring event type %r", event_type)

    async def _handle_message_event(self, event: Dict[str, Any]) -> None:
        normalized = await normalize_message_event(
            event,
            require_mention=self._eko_config.require_mention,
            allow_source=self._allowed_for_source,
            allow_group=self._allowed_group,
            has_mention_trigger=self._has_mention_trigger,
            download_picture=self._download_picture,
            mime_from_filename=self._mime_from_filename,
        )
        if not normalized.accepted:
            if normalized.reason == "unauthorized_dm_source":
                logger.info("Eko: rejecting unauthorized DM source %s", event.get("source") or {})
            elif normalized.reason == "group_not_allowed":
                msg = event.get("message") or {}
                logger.info(
                    "Eko: rejecting group %s topic %s (not in allowlist)",
                    msg.get("groupId", ""),
                    msg.get("topicId", ""),
                )
            elif normalized.reason == "missing_mention":
                logger.debug("Eko: ignoring group message (require_mention, no trigger)")
            return

        if normalized.routing_metadata:
            self._session_routing[normalized.chat_id] = normalized.routing_metadata

        if normalized.chat_id and normalized.reply_token:
            self._reply_tokens[normalized.chat_id] = (
                normalized.reply_token,
                time.time() + self._eko_config.reply_token_ttl,
            )

        source_obj = self.build_source(**(normalized.source_kwargs or {}))
        event_obj = MessageEvent(source=source_obj, **(normalized.message_kwargs or {}))

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
        max_inbound = self._eko_config.max_inbound_media_bytes
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
        uid = source.get("userId") or source.get("uid", "")
        if not uid:
            return False
        return self._eko_config.is_user_allowed(uid)

    def _allowed_group(self, group_id: str, topic_id: str = "") -> bool:
        """Check if a group/topic is in the allowlist (#26)."""
        cfg = self._eko_config
        return cfg.is_topic_allowed(group_id, topic_id) if topic_id else cfg.is_group_allowed(group_id)

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

        triggers = self._eko_config.mention_triggers or ["Hermes Agent"]
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

    async def send_exec_approval(
        self,
        chat_id: str,
        command: str,
        session_key: str,
        description: str = "dangerous command",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Render dangerous-command approval as Eko quick replies.

        Eko quick replies are reply-token only.  If no fresh token is
        available, return unsupported so the gateway sends its text fallback.
        Button ``value`` fields are set to slash commands (e.g. ``/approve``)
        so the tap arrives as a real command that bypasses the agent-active
        queue in base.py.
        """
        if not self._client:
            return SendResult(success=False, error="Eko adapter not connected")

        token, used_reply = self._consume_reply_token(chat_id)
        if not used_reply:
            return SendResult(success=False, error="No Eko reply token available")

        cmd_preview = command[:3800] + "..." if len(command) > 3800 else command
        prompt = (
            "⚠️ Command Approval Required\n\n"
            f"```{cmd_preview}```\n\n"
            f"Reason: {description}"
        )

        try:
            await self._client.reply_quick_reply(
                token,
                prompt,
                ["Approve Once", "Approve Session", "Approve Always", "Deny"],
                values=["/approve", "/approve session", "/approve always", "/deny"],
            )
        except Exception as exc:
            logger.debug(
                "Eko: exec-approval quick reply failed, falling back to text prompt: %s",
                exc,
            )
            return SendResult(success=False, error=str(exc), retryable=True)

        return SendResult(success=True, message_id=token)

    async def send_slash_confirm(
        self,
        chat_id: str,
        title: str,
        message: str,
        session_key: str,
        confirm_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Render slash confirmations as Eko quick replies when possible.

        Eko quick replies are reply-token only.  If no fresh token is
        available, return unsupported so the gateway sends its text fallback.
        Button ``value`` fields are set to slash commands (e.g. ``/approve``)
        so the tap arrives as a real command that bypasses the agent-active
        queue in base.py.
        """
        if not self._client:
            return SendResult(success=False, error="Eko adapter not connected")

        token, used_reply = self._consume_reply_token(chat_id)
        if not used_reply:
            return SendResult(success=False, error="No Eko reply token available")

        try:
            await self._client.reply_quick_reply(
                token,
                message,
                ["Approve Once", "Always Approve", "Cancel"],
                values=["/approve", "/always", "/cancel"],
            )
        except Exception as exc:
            logger.debug(
                "Eko: slash-confirm quick reply failed, falling back to text prompt: %s",
                exc,
            )
            return SendResult(success=False, error=str(exc), retryable=True)

        return SendResult(success=True, message_id=token)

    async def send_clarify(
        self,
        chat_id: str,
        question: str,
        choices: Optional[list],
        clarify_id: str,
        session_key: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Render clarify choices as Eko quick replies when possible.

        Eko quick replies are reply-token only.  If no fresh token is
        available, fall back to the base numbered-text prompt.
        """
        if not choices or not self._client:
            return await super().send_clarify(
                chat_id=chat_id,
                question=question,
                choices=choices,
                clarify_id=clarify_id,
                session_key=session_key,
                metadata=metadata,
            )

        token, used_reply = self._consume_reply_token(chat_id)
        if not used_reply:
            return await super().send_clarify(
                chat_id=chat_id,
                question=question,
                choices=choices,
                clarify_id=clarify_id,
                session_key=session_key,
                metadata=metadata,
            )

        try:
            await self._client.reply_quick_reply(
                token,
                question,
                [str(c) for c in choices],
            )
        except Exception as exc:
            logger.debug(
                "Eko: quick reply failed, falling back to text prompt: %s",
                exc,
            )
            return await super().send_clarify(
                chat_id=chat_id,
                question=question,
                choices=choices,
                clarify_id=clarify_id,
                session_key=session_key,
                metadata=metadata,
            )

        # Eko quick-reply taps arrive back as ordinary text messages with a
        # fresh reply token.  Mark this clarify as text-capturing so the
        # gateway resolves it instead of starting a new agent turn.
        from tools.clarify_gateway import mark_awaiting_text
        mark_awaiting_text(clarify_id)
        return SendResult(success=True, message_id=token)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if not self._client:
            return SendResult(success=False, error="Eko adapter not connected")

        chunks = self.truncate_message(content, self._eko_config.message_max_chars)
        if not chunks:
            return SendResult(success=True)

        # Filter out empty chunks (can happen when message is only MEDIA: tags).
        chunks = [c for c in chunks if c.strip()]
        if not chunks:
            return SendResult(success=True)

        last_result = SendResult(success=True)
        for chunk in chunks:
            try:
                last_result = await self._get_sender().send_text(chat_id, chunk)
            except RuntimeError as exc:
                logger.error("Eko: send failed: %s", exc)
                return SendResult(success=False, error=str(exc), retryable=True)
            except Exception as exc:
                logger.error("Eko: send failed: %s", exc)
                return SendResult(success=False, error=str(exc), retryable=True)
            if not last_result.success:
                return last_result
        return last_result

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
                if not self._get_sender().check_size(file_size):
                    return SendResult(
                        success=False,
                        error=f"Image file too large ({file_size} bytes, limit {self._eko_config.max_upload_bytes})",
                    )
            except OSError:
                pass  # stat failed; let read_bytes try
            file_bytes = fpath.read_bytes()
        except OSError as exc:
            return SendResult(success=False, error=f"Cannot read image: {exc}")

        filename = Path(image_path).name or "image.jpg"
        _caption = caption or ""

        try:
            return await self._get_sender().send_image(
                chat_id, file_bytes, filename, caption=_caption
            )
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
                if not self._get_sender().check_size(file_size):
                    return SendResult(
                        success=False,
                        error=f"File too large ({file_size} bytes, limit {self._eko_config.max_upload_bytes})",
                    )
            except OSError:
                pass  # stat failed; let read_bytes try
            file_bytes = fpath.read_bytes()
        except OSError as exc:
            return SendResult(success=False, error=f"Cannot read file: {exc}")

        filename = file_name or Path(file_path).name or "document"

        try:
            return await self._get_sender().send_file(chat_id, file_bytes, filename)
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
        secret = self._eko_config.webhook_secret
        if not secret:
            return False
        sig = signature.strip()
        if sig.lower().startswith("sha256="):
            sig = sig[7:]
        expected = base64.b64encode(
            hmac.new(
                secret.encode("utf-8"),
                body,
                hashlib.sha256,
            ).digest()
        ).decode("utf-8")
        return hmac.compare_digest(expected, sig)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """No-op - Eko has no documented typing indicator API."""

    def get_session_routing(self, chat_id: str) -> Optional[Dict[str, str]]:
        """Return routing metadata for *chat_id*, or ``None`` if unknown.

        Used by the standalone sender to resolve session-based routes
        without accessing private adapter internals.
        """
        return self._session_routing.get(chat_id)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        routing = self._session_routing.get(chat_id)
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
    """Plugin gate: require runtime dependencies only."""
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        return False
    return True


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    return EkoConfig.from_env(extra).has_credentials()


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

    Route resolution is delegated to OutboundSender.resolve_route().
    """
    extra = getattr(pconfig, "extra", {}) or {}
    base_url = os.getenv("EKO_BASE_URL") or extra.get("base_url", "")
    client_id = os.getenv("EKO_OAUTH_CLIENT_ID") or extra.get("oauth_client_id", "")
    client_secret = os.getenv("EKO_OAUTH_CLIENT_SECRET") or extra.get("oauth_client_secret", "")
    if not base_url or not client_id or not client_secret or not chat_id:
        return {"error": "Eko standalone send: missing config or chat_id"}

    client = _EkoClient(base_url, client_id, client_secret)

    # Build session_routing from live adapter if available, for resolve_route().
    session_routing: Dict[str, Dict[str, str]] = {}
    # Skip live adapter lookup if explicit routing will handle it.
    needs_live = not chat_id.startswith("group:")
    if needs_live:
        try:
            from gateway.run import _gateway_runner_ref
            from gateway.config import Platform as _Platform
            _runner = _gateway_runner_ref()
            if _runner:
                _adapter = _runner.adapters.get(_Platform("eko"))
                if _adapter:
                    routing = _adapter.get_session_routing(chat_id)
                    if routing:
                        session_routing[chat_id] = routing
        except Exception:
            pass

    cfg = EkoConfig.from_env(extra)
    sender = OutboundSender(
        config=cfg,
        client=client,
        session_routing=session_routing,
        reply_tokens={},  # standalone: no reply tokens
    )
    route = sender.resolve_route(chat_id)
    if route.error:
        return {"error": route.error}

    # Send text body.
    if message:
        try:
            await sender.send_text(chat_id, message)
        except Exception as exc:
            return {"error": str(exc)}

    # Upload media attachments.
    _IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    media_warnings: List[str] = []

    for media_path in media_files or []:
        try:
            from pathlib import Path
            p = Path(media_path)
            file_size = p.stat().st_size
            if not sender.check_size(file_size):
                media_warnings.append(
                    f"File too large: {p.name} ({file_size} bytes, limit {cfg.max_upload_bytes})"
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
                await sender.send_image(chat_id, file_bytes, filename)
            else:
                await sender.send_file(chat_id, file_bytes, filename)
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