"""Tests for Eko outbound delivery module.

Each test exercises one behaviour through the OutboundSender interface.
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.platforms.base import SendResult
from plugins.platforms.eko.config import EkoConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sender(session_routing=None, reply_tokens=None, **config_overrides):
    """Build an OutboundSender with mocked client and minimal config."""
    from plugins.platforms.eko.outbound import OutboundSender

    cfg = EkoConfig(**config_overrides) if config_overrides else EkoConfig()
    client = MagicMock()
    sr = session_routing if session_routing is not None else {}
    rt = reply_tokens if reply_tokens is not None else {}
    return OutboundSender(cfg, client, sr, rt)


# ---------------------------------------------------------------------------
# Test #1 — DM route resolution (bare uid, no routing metadata)
# ---------------------------------------------------------------------------


def test_dm_route_resolution():
    """Chat ID with no routing metadata resolves to DM mode."""
    sender = _make_sender()

    route = sender.resolve_route("user123")

    assert route.mode == "dm"
    assert route.uid == "user123"
    assert route.group_id is None
    assert route.topic_id is None


# ---------------------------------------------------------------------------
# Test #2 — Group/topic route resolution
# ---------------------------------------------------------------------------


def test_group_topic_route_resolution():
    """Routing with groupId + topicId → mode="group"."""
    sender = _make_sender(
        session_routing={
            "g1_t1": {
                "uid": "u1",
                "groupId": "g1",
                "topicId": "t1",
                "groupType": "group",
            }
        }
    )

    route = sender.resolve_route("g1_t1")

    assert route.mode == "group"
    assert route.uid == "u1"
    assert route.group_id == "g1"
    assert route.topic_id == "t1"


# ---------------------------------------------------------------------------
# Test #3 — Explicit standalone route (group:<gid>:topic:<tid>)
# ---------------------------------------------------------------------------


def test_explicit_standalone_route():
    """Explicit routing format parses into mode="explicit"."""
    sender = _make_sender()

    route = sender.resolve_route("group:my-gid:topic:my-tid")

    assert route.mode == "explicit"
    assert route.group_id == "my-gid"
    assert route.topic_id == "my-tid"


def test_explicit_standalone_malformed():
    """Malformed explicit routing returns error mode."""
    sender = _make_sender()

    route = sender.resolve_route("group:only-one-part")

    assert route.mode == "error"
    assert route.error is not None


# ---------------------------------------------------------------------------
# Test #4 — Text DM: reply token consumed first, then push fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_dm_reply_token_then_push():
    """First send uses reply token; second send falls back to push."""
    client = MagicMock()
    client.reply_text = AsyncMock()
    client.push_text = AsyncMock()

    now = time.time()
    sender = _make_sender(
        reply_tokens={"u1": ("tok123", now + 60)},
    )
    sender._client = client

    # First send — should consume reply token
    result1 = await sender.send_text("u1", "hello")
    assert result1.success is True
    client.reply_text.assert_called_once_with("tok123", "hello")
    assert "u1" not in sender._reply_tokens  # consumed

    # Second send — no token left, should push
    result2 = await sender.send_text("u1", "world")
    assert result2.success is True
    client.reply_text.assert_called_once()  # no additional call
    client.push_text.assert_called_once_with("u1", "world")


# ---------------------------------------------------------------------------
# Test #5 — Text DM: no reply token → direct push
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_dm_no_reply_token_direct_push():
    """When no reply token available, goes straight to push."""
    client = MagicMock()
    client.push_text = AsyncMock()

    sender = _make_sender()
    sender._client = client

    result = await sender.send_text("u1", "hello")

    assert result.success is True
    client.push_text.assert_called_once_with("u1", "hello")


# ---------------------------------------------------------------------------
# Test #6 — Text group: push_group_text used
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_group_uses_group_endpoint():
    """Group route sends via push_group_text."""
    client = MagicMock()
    client.push_group_text = AsyncMock()

    sender = _make_sender(
        session_routing={
            "g1_t1": {
                "uid": "u1",
                "groupId": "g1",
                "topicId": "t1",
                "groupType": "group",
            }
        }
    )
    sender._client = client

    result = await sender.send_text("g1_t1", "hello group")

    assert result.success is True
    client.push_group_text.assert_called_once_with("g1", "t1", "hello group")


# ---------------------------------------------------------------------------
# Test #7 — Image DM: reply_picture then push fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_image_dm_reply_then_push():
    """First image send uses reply_picture; second uses push_picture."""
    client = MagicMock()
    client.reply_picture = AsyncMock()
    client.push_picture = AsyncMock()

    now = time.time()
    sender = _make_sender(
        reply_tokens={"u1": ("tok123", now + 60)},
    )
    sender._client = client

    # First send — reply_picture
    result1 = await sender.send_image("u1", b"\x89PNG", "img.png")
    assert result1.success is True
    client.reply_picture.assert_called_once_with("tok123", b"\x89PNG", "img.png")

    # Second send — push_picture
    result2 = await sender.send_image("u1", b"\x89PNG2", "img2.png")
    assert result2.success is True
    client.push_picture.assert_called_once_with("u1", b"\x89PNG2", "img2.png")


# ---------------------------------------------------------------------------
# Test #8 — Image group: push_group_picture used
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_image_group_uses_group_endpoint():
    """Group route sends image via push_group_picture."""
    client = MagicMock()
    client.push_group_picture = AsyncMock()

    sender = _make_sender(
        session_routing={
            "g1_t1": {
                "uid": "u1",
                "groupId": "g1",
                "topicId": "t1",
                "groupType": "group",
            }
        }
    )
    sender._client = client

    result = await sender.send_image("g1_t1", b"\x89PNG", "img.png", caption="hi")

    assert result.success is True
    client.push_group_picture.assert_called_once_with(
        "g1", "t1", b"\x89PNG", "img.png", "hi"
    )


# ---------------------------------------------------------------------------
# Test #9 — File DM: always push_file (no reply endpoint)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_file_dm_always_push():
    """Files never use reply tokens — always push_file."""
    client = MagicMock()
    client.push_file = AsyncMock()
    client.reply_picture = AsyncMock()  # should NOT be called

    now = time.time()
    sender = _make_sender(
        reply_tokens={"u1": ("tok123", now + 60)},
    )
    sender._client = client

    result = await sender.send_file("u1", b"data", "doc.pdf")

    assert result.success is True
    client.push_file.assert_called_once_with("u1", b"data", "doc.pdf")
    client.reply_picture.assert_not_called()


# ---------------------------------------------------------------------------
# Test #10 — File group: push_group_file used
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_file_group_uses_group_endpoint():
    """Group route sends file via push_group_file."""
    client = MagicMock()
    client.push_group_file = AsyncMock()

    sender = _make_sender(
        session_routing={
            "g1_t1": {
                "uid": "u1",
                "groupId": "g1",
                "topicId": "t1",
                "groupType": "group",
            }
        }
    )
    sender._client = client

    result = await sender.send_file("g1_t1", b"data", "doc.pdf")

    assert result.success is True
    client.push_group_file.assert_called_once_with("g1", "t1", b"data", "doc.pdf")


# ---------------------------------------------------------------------------
# Test #11 — Oversized media rejected
# ---------------------------------------------------------------------------


def test_oversized_media_rejected():
    """check_size returns False when file exceeds max_upload_bytes."""
    sender = _make_sender(max_upload_bytes=1000)

    assert sender.check_size(500) is True
    assert sender.check_size(1001) is False
    assert sender.check_size(1000) is True


# ---------------------------------------------------------------------------
# Test #12 — Standalone delivery resolves route once
# ---------------------------------------------------------------------------


def test_standalone_explicit_routing():
    """Standalone cron path reuses resolve_route for explicit routing."""
    sender = _make_sender()

    route = sender.resolve_route("group:g1:topic:t1")

    assert route.mode == "explicit"
    assert route.group_id == "g1"
    assert route.topic_id == "t1"


# ---------------------------------------------------------------------------
# Regression: group/topic routes must also use reply tokens
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_group_uses_reply_token_first():
    """Group route with reply token: reply_text first, push_group_text fallback."""
    client = MagicMock()
    client.reply_text = AsyncMock()
    client.push_group_text = AsyncMock()

    now = time.time()
    sender = _make_sender(
        session_routing={
            "g1_t1": {
                "uid": "u1",
                "groupId": "g1",
                "topicId": "t1",
                "groupType": "group",
            }
        },
        reply_tokens={"g1_t1": ("tok-grp", now + 60)},
    )
    sender._client = client

    # First send — should consume reply token even for group route
    result = await sender.send_text("g1_t1", "hello group")
    assert result.success is True
    client.reply_text.assert_called_once_with("tok-grp", "hello group")
    assert "g1_t1" not in sender._reply_tokens  # consumed

    # Second send — no token, push_group_text
    result2 = await sender.send_text("g1_t1", "second msg")
    assert result2.success is True
    client.push_group_text.assert_called_once_with("g1", "t1", "second msg")


@pytest.mark.asyncio
async def test_image_group_uses_reply_token_first():
    """Group route with reply token: reply_picture first, push_group_picture fallback."""
    client = MagicMock()
    client.reply_picture = AsyncMock()
    client.push_group_picture = AsyncMock()

    now = time.time()
    sender = _make_sender(
        session_routing={
            "g1_t1": {
                "uid": "u1",
                "groupId": "g1",
                "topicId": "t1",
                "groupType": "group",
            }
        },
        reply_tokens={"g1_t1": ("tok-grp", now + 60)},
    )
    sender._client = client

    result = await sender.send_image("g1_t1", b"\x89PNG", "img.png", caption="hi")
    assert result.success is True
    client.reply_picture.assert_called_once_with("tok-grp", b"\x89PNG", "img.png")
    assert "g1_t1" not in sender._reply_tokens
