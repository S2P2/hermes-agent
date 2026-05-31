"""Eko outbound delivery module.

Owns route resolution (DM vs group/topic vs explicit standalone),
reply-token lifecycle, and media size checks.  The adapter's send
methods and the standalone cron sender delegate to this module.

Design notes
------------

* ``resolve_route()`` is the single path every send type calls first.
* Reply tokens are consumed on the first send per conversation;
  subsequent sends fall through to push.
* Group/topic routing is based on ``groupId`` + ``topicId`` presence,
  **not** ``groupType`` (per ADR-0001).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from gateway.platforms.base import SendResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Route resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedRoute:
    """One resolved target for an outbound send."""

    mode: str  # "dm" | "group" | "explicit"
    uid: Optional[str] = None
    group_id: Optional[str] = None
    topic_id: Optional[str] = None


# ---------------------------------------------------------------------------
# OutboundSender
# ---------------------------------------------------------------------------


class OutboundSender:
    """Owns route resolution, reply-token lifecycle, and media checks."""

    def __init__(
        self,
        config,
        client,
        session_routing: Dict[str, Dict[str, str]],
        reply_tokens: Dict[str, Tuple[str, float]],
    ) -> None:
        self._config = config
        self._client = client
        self._session_routing = session_routing
        self._reply_tokens = reply_tokens

    # ------------------------------------------------------------------
    # Route resolution
    # ------------------------------------------------------------------

    def resolve_route(self, chat_id: str) -> ResolvedRoute:
        """Single route-resolution path for all send types."""
        # 1. Try explicit standalone routing
        explicit = _parse_explicit_routing(chat_id)
        if explicit is not None:
            if "error" in explicit:
                return ResolvedRoute(mode="dm", uid=chat_id)
            return ResolvedRoute(
                mode="explicit",
                group_id=explicit["groupId"],
                topic_id=explicit["topicId"],
            )

        # 2. Session routing (populated by inbound webhook)
        routing = self._session_routing.get(chat_id)
        if routing:
            gid = routing.get("groupId", "")
            tid = routing.get("topicId", "")
            if gid and tid:
                return ResolvedRoute(
                    mode="group",
                    uid=routing.get("uid"),
                    group_id=gid,
                    topic_id=tid,
                )
            # DM with routing metadata
            uid = routing.get("uid", chat_id)
            return ResolvedRoute(mode="dm", uid=uid)

        # 3. Fallback: treat chat_id as bare uid (DM)
        return ResolvedRoute(mode="dm", uid=chat_id)

    # ------------------------------------------------------------------
    # Reply token lifecycle
    # ------------------------------------------------------------------

    def _consume_reply_token(self, chat_id: str) -> Tuple[str, bool]:
        """Pop and return a reply token if present and unexpired."""
        entry = self._reply_tokens.pop(chat_id, None)
        if not entry:
            return "", False
        token, expires_at = entry
        if not token or time.time() >= expires_at:
            return "", False
        return token, True

    # ------------------------------------------------------------------
    # Size checks
    # ------------------------------------------------------------------

    def check_size(self, file_size: int) -> bool:
        """Return True if *file_size* is within the outbound limit."""
        limit = self._config.max_upload_bytes
        if limit and file_size > limit:
            return False
        return True

    # ------------------------------------------------------------------
    # Is this a group/topic route?
    # ------------------------------------------------------------------

    def _is_group(self, route: ResolvedRoute) -> bool:
        return route.group_id is not None and route.topic_id is not None

    # ------------------------------------------------------------------
    # Send methods
    # ------------------------------------------------------------------

    async def send_text(self, chat_id: str, content: str) -> SendResult:
        """Send a text message, preferring reply token then push."""
        route = self.resolve_route(chat_id)

        # Try reply token first (only for DM / non-group)
        if not self._is_group(route):
            token, used_reply = self._consume_reply_token(chat_id)
            if used_reply:
                try:
                    await self._client.reply_text(token, content)
                    return SendResult(success=True, message_id=token)
                except Exception as exc:
                    logger.debug("Eko: reply_text failed, falling back to push: %s", exc)

        # Push
        if self._is_group(route):
            await self._client.push_group_text(
                route.group_id, route.topic_id, content
            )
        else:
            await self._client.push_text(route.uid, content)
        return SendResult(success=True)

    async def send_image(
        self,
        chat_id: str,
        file_bytes: bytes,
        filename: str,
        caption: str = "",
    ) -> SendResult:
        """Send an image, preferring reply_picture then push."""
        route = self.resolve_route(chat_id)

        # Try reply_picture for DM
        if not self._is_group(route):
            token, used_reply = self._consume_reply_token(chat_id)
            if used_reply:
                try:
                    await self._client.reply_picture(token, file_bytes, filename)
                    return SendResult(success=True, message_id=token)
                except Exception as exc:
                    logger.debug("Eko: reply_picture failed, falling back to push: %s", exc)

        # Push
        if self._is_group(route):
            await self._client.push_group_picture(
                route.group_id, route.topic_id, file_bytes, filename, caption
            )
        else:
            await self._client.push_picture(
                route.uid, file_bytes, filename, caption=caption
            )
        return SendResult(success=True)

    async def send_file(
        self,
        chat_id: str,
        file_bytes: bytes,
        filename: str,
    ) -> SendResult:
        """Send a file — always push (no reply endpoint for files)."""
        route = self.resolve_route(chat_id)

        if self._is_group(route):
            await self._client.push_group_file(
                route.group_id, route.topic_id, file_bytes, filename
            )
        else:
            await self._client.push_file(route.uid, file_bytes, filename)
        return SendResult(success=True)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_explicit_routing(chat_id: str) -> Optional[Dict[str, str]]:
    """Parse ``group:<gid>:topic:<tid>`` format."""
    if not chat_id.startswith("group:"):
        return None
    parts = chat_id.split(":")
    if len(parts) != 4 or parts[2] != "topic":
        return {"error": f"Invalid explicit routing format: {chat_id!r}"}
    gid, tid = parts[1], parts[3]
    if not gid or not tid:
        return {"error": f"Invalid explicit routing format: {chat_id!r}"}
    return {"groupId": gid, "topicId": tid}
