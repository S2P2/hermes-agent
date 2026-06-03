"""Tests for Eko inbound webhook normalization."""

from unittest.mock import AsyncMock

import pytest

from gateway.platforms.base import MessageType
from plugins.platforms.eko.inbound import normalize_message_event


def _text_event(text="hello", *, group_id="", topic_id="", group_type=""):
    message = {"id": "msg1", "type": "text", "text": text}
    if group_id:
        message.update({"groupId": group_id, "topicId": topic_id, "groupType": group_type})
    return {
        "replyToken": "tok",
        "type": "message",
        "source": {"userId": "u1", "username": "alice"},
        "message": message,
    }


@pytest.mark.asyncio
async def test_normalizes_group_topic_text_route():
    result = await normalize_message_event(
        _text_event(group_id="g1", topic_id="t1", group_type="group"),
        require_mention=False,
        allow_source=lambda _source: False,
        allow_group=lambda gid, tid: gid == "g1" and tid == "t1",
        has_mention_trigger=lambda _text: False,
        download_picture=AsyncMock(),
        mime_from_filename=lambda _filename: "image/jpeg",
    )

    assert result.accepted is True
    assert result.chat_id == "g1_t1"
    assert result.routing_metadata == {
        "uid": "u1",
        "groupId": "g1",
        "topicId": "t1",
        "groupType": "group",
    }
    assert result.source_kwargs["chat_type"] == "group"
    assert result.source_kwargs["chat_name"] == "g1"
    assert result.message_kwargs["text"] == "hello"


@pytest.mark.asyncio
async def test_rejects_group_without_required_mention():
    result = await normalize_message_event(
        _text_event("hello", group_id="g1", topic_id="t1", group_type="group"),
        require_mention=True,
        allow_source=lambda _source: True,
        allow_group=lambda _gid, _tid: True,
        has_mention_trigger=lambda _text: False,
        download_picture=AsyncMock(),
        mime_from_filename=lambda _filename: "image/jpeg",
    )

    assert result.accepted is False
    assert result.reason == "missing_mention"


@pytest.mark.asyncio
async def test_picture_normalization_uses_downloader_and_mime_mapper():
    event = {
        "replyToken": "tok",
        "type": "message",
        "source": {"userId": "u1", "username": "alice"},
        "message": {
            "id": "msg1",
            "type": "picture",
            "pictureId": "pic1",
            "fileName": "cat.png",
        },
    }
    download_picture = AsyncMock(return_value="/cache/cat.png")

    result = await normalize_message_event(
        event,
        require_mention=False,
        allow_source=lambda _source: True,
        allow_group=lambda _gid, _tid: True,
        has_mention_trigger=lambda _text: False,
        download_picture=download_picture,
        mime_from_filename=lambda filename: "image/png" if filename == "cat.png" else "image/jpeg",
    )

    assert result.accepted is True
    download_picture.assert_awaited_once_with(event["message"])
    assert result.message_kwargs["text"] == "[image]"
    assert result.message_kwargs["message_type"] == MessageType.PHOTO
    assert result.message_kwargs["media_urls"] == ["/cache/cat.png"]
    assert result.message_kwargs["media_types"] == ["image/png"]
