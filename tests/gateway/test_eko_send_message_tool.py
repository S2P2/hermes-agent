"""Tests for Eko targets in tools/send_message_tool.py."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gateway.config import Platform
from tools.send_message_tool import SEND_MESSAGE_SCHEMA, _parse_target_ref, send_message_tool


def _run_async_immediately(coro):
    return asyncio.run(coro)


def test_eko_group_topic_target_is_explicit():
    """Eko explicit topic routes are preserved for outbound validation."""
    chat_id, thread_id, is_explicit = _parse_target_ref("eko", "group:g1:topic:t1")

    assert chat_id == "group:g1:topic:t1"
    assert thread_id is None
    assert is_explicit is True


def test_eko_malformed_group_target_stays_explicit_for_outbound_validation():
    """Malformed Eko group routes do not fall through to name resolution."""
    chat_id, thread_id, is_explicit = _parse_target_ref("eko", "group:only-one-part")

    assert chat_id == "group:only-one-part"
    assert thread_id is None
    assert is_explicit is True


def test_send_message_eko_explicit_target_skips_channel_name_resolution():
    """send_message preserves explicit Eko topic routes through dispatch."""
    platform = Platform("eko")
    pconfig = SimpleNamespace(enabled=True, token="", extra={})
    config = SimpleNamespace(
        platforms={platform: pconfig},
        get_home_channel=lambda _platform: None,
    )

    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch("tools.interrupt.is_interrupted", return_value=False), \
         patch("gateway.channel_directory.resolve_channel_name") as resolve_mock, \
         patch("model_tools._run_async", side_effect=_run_async_immediately), \
         patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True})) as send_mock, \
         patch("gateway.mirror.mirror_to_session", return_value=True):
        result = json.loads(
            send_message_tool(
                {
                    "action": "send",
                    "target": "eko:group:g1:topic:t1",
                    "message": "hello",
                }
            )
        )

    assert result["success"] is True
    resolve_mock.assert_not_called()
    send_mock.assert_awaited_once_with(
        platform,
        pconfig,
        "group:g1:topic:t1",
        "hello",
        thread_id=None,
        media_files=[],
        force_document=False,
    )


def test_send_message_schema_documents_eko_explicit_target():
    """The send_message target schema includes the Eko route example."""
    description = SEND_MESSAGE_SCHEMA["parameters"]["properties"]["target"]["description"]

    assert "eko:group:<gid>:topic:<tid>" in description
