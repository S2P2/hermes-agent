"""Inbound Eko webhook event normalization.

This module owns the Eko-specific message mapping from raw webhook events to
normalized gateway-message data plus routing/reply-token side effects. The
adapter remains responsible for transport, deduplication, self-message
filtering, and invoking the standard gateway handler.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from gateway.platforms.base import MessageType


@dataclass(frozen=True)
class InboundRouteResult:
    """Normalized inbound message data for the adapter to apply."""

    accepted: bool
    reason: str = ""
    chat_id: str = ""
    chat_type: str = "dm"
    uid: str = ""
    username: str = ""
    group_id: str = ""
    topic_id: str = ""
    group_type: str = ""
    reply_token: str = ""
    routing_metadata: Optional[Dict[str, str]] = None
    source_kwargs: Optional[Dict[str, Any]] = None
    message_kwargs: Optional[Dict[str, Any]] = None


async def normalize_message_event(
    event: Dict[str, Any],
    *,
    require_mention: bool,
    allow_source: Callable[[Dict[str, Any]], bool],
    allow_group: Callable[[str, str], bool],
    has_mention_trigger: Callable[[str], bool],
    download_picture: Callable[[Dict[str, Any]], Awaitable[Optional[str]]],
    mime_from_filename: Callable[[str], str],
) -> InboundRouteResult:
    """Normalize one raw Eko message event.

    Returns an accepted result with source/message kwargs for ``MessageEvent``
    construction, or a rejected result with a stable reason. The caller owns
    logging and mutating adapter state from the returned routing metadata and
    reply token.
    """
    msg = event.get("message") or {}
    msg_type = msg.get("type", "")
    message_id = msg.get("id", "")
    reply_token = event.get("replyToken", "")
    source = event.get("source") or {}

    uid = source.get("userId") or source.get("uid", "")
    username = source.get("username", "") or uid

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

    if group_type == "direct_chat":
        chat_type = "dm"
    elif group_type:
        chat_type = "group"
    else:
        chat_type = "dm"

    if chat_type == "dm" and not allow_source(source):
        return InboundRouteResult(accepted=False, reason="unauthorized_dm_source")

    if chat_type == "group" and group_id:
        raw_text = (msg.get("text", "") or "") if msg_type == "text" else ""
        if not allow_group(group_id, topic_id):
            return InboundRouteResult(accepted=False, reason="group_not_allowed")

        is_slash_command = raw_text.startswith("/")
        if require_mention and not is_slash_command and not has_mention_trigger(raw_text):
            return InboundRouteResult(accepted=False, reason="missing_mention")

    media_urls: List[str] = []
    media_types: List[str] = []
    text = ""
    message_type = MessageType.TEXT

    if msg_type == "text":
        text = msg.get("text", "") or ""
    elif msg_type == "picture":
        local_path = await download_picture(msg)
        if local_path:
            media_urls.append(local_path)
            media_types.append(mime_from_filename(msg.get("fileName", "")))
            message_type = MessageType.PHOTO
        text = "[image]"
    elif msg_type == "sticker":
        text = "[sticker]"
    elif msg_type == "file":
        text = "[file]"
    else:
        text = f"[unsupported message type: {msg_type}]"

    effective_chat_name = group_id if chat_type == "group" and group_id else username

    return InboundRouteResult(
        accepted=True,
        chat_id=chat_id,
        chat_type=chat_type,
        uid=uid,
        username=username,
        group_id=group_id,
        topic_id=topic_id,
        group_type=group_type,
        reply_token=reply_token,
        routing_metadata={
            "uid": uid,
            "groupId": group_id,
            "topicId": topic_id,
            "groupType": group_type,
        },
        source_kwargs={
            "chat_id": chat_id,
            "chat_type": chat_type,
            "user_id": uid,
            "user_name": username,
            "chat_name": effective_chat_name,
            "thread_id": topic_id or None,
        },
        message_kwargs={
            "text": text,
            "message_type": message_type,
            "raw_message": event,
            "message_id": message_id,
            "media_urls": media_urls,
            "media_types": media_types,
        },
    )
