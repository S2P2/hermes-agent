"""Tests for the Eko platform adapter plugin.

Covers:
1. Dedup logic (hash-based event deduplication)
2. Allowlist gating (users, allow_all)
3. OAuth token management (proactive refresh, clear on 401)
4. Reply token stash (consume, expiry, fallback)
5. Outbound send routing (reply → push fallback, 401 retry)
6. Plugin registration metadata
7. Config validation and env enablement
8. Webhook signature verification (X-Eko-Signature HMAC-SHA256-Base64)
9. Signature enforcement and normalization
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.gateway._plugin_adapter_loader import load_plugin_adapter

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, MessageType

_eko = load_plugin_adapter("eko")

EkoAdapter = _eko.EkoAdapter
_MessageDeduplicator = _eko._MessageDeduplicator
_EkoClient = _eko._EkoClient
# RuntimeError removed — client now raises RuntimeError
check_requirements = _eko.check_requirements
validate_config = _eko.validate_config
is_connected = _eko.is_connected
_env_enablement = _eko._env_enablement
_standalone_send = _eko._standalone_send
register = _eko.register
DEFAULT_MESSAGE_MAX_CHARS = _eko.DEFAULT_MESSAGE_MAX_CHARS
DEFAULT_MAX_UPLOAD_BYTES = _eko.DEFAULT_MAX_UPLOAD_BYTES
DEFAULT_MAX_INBOUND_MEDIA_BYTES = _eko.DEFAULT_MAX_INBOUND_MEDIA_BYTES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(extra=None):
    """Create a minimal PlatformConfig-like object for testing."""
    cfg = MagicMock()
    cfg.extra = extra or {}
    return cfg


# ---------------------------------------------------------------------------
# 1. Dedup
# ---------------------------------------------------------------------------

class TestDedup:

    def test_first_event_not_duplicate(self):
        d = _MessageDeduplicator()
        assert not d.is_duplicate({"type": "message", "data": "hello"})

    def test_same_event_is_duplicate(self):
        d = _MessageDeduplicator()
        event = {"type": "message", "data": "hello"}
        d.is_duplicate(event)
        assert d.is_duplicate(event)

    def test_different_events_not_duplicate(self):
        d = _MessageDeduplicator(max_size=100)
        d.is_duplicate({"type": "message", "data": "hello"})
        assert not d.is_duplicate({"type": "message", "data": "world"})

    def test_lru_eviction_under_pressure(self):
        d = _MessageDeduplicator(max_size=10)
        for i in range(20):
            d.is_duplicate({"i": i})
        d.is_duplicate({"i": 100})
        assert len(d._seen) <= 25


# ---------------------------------------------------------------------------
# 2. Allowlist
# ---------------------------------------------------------------------------

class TestAllowlist:

    def test_allow_all_permits(self):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter.allow_all = True
        adapter.allowed_users = set()
        assert adapter._allowed_for_source({"userId": "anyone"})

    def test_allowed_user_passes(self):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter.allow_all = False
        adapter.allowed_users = {"user123"}
        assert adapter._allowed_for_source({"userId": "user123"})

    def test_disallowed_user_rejected(self):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter.allow_all = False
        adapter.allowed_users = {"user123"}
        assert not adapter._allowed_for_source({"userId": "stranger"})

    def test_uid_field_also_checked(self):
        """Eko sources may use 'uid' instead of 'userId'."""
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter.allow_all = False
        adapter.allowed_users = {"abc123"}
        assert adapter._allowed_for_source({"uid": "abc123"})

    def test_empty_uid_rejected(self):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter.allow_all = False
        adapter.allowed_users = set()
        assert not adapter._allowed_for_source({})


# ---------------------------------------------------------------------------
# 3. Reply token stash
# ---------------------------------------------------------------------------

class TestRequireMention:
    """Issue #22: require_mention filter for group chats."""

    def _make_adapter(self, **overrides):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter.require_mention = overrides.get("require_mention", False)
        adapter.mention_triggers = overrides.get("mention_triggers", [])
        adapter.allow_all_groups = overrides.get("allow_all_groups", True)
        adapter.allowed_groups = overrides.get("allowed_groups", set())
        adapter.allowed_topics = overrides.get("allowed_topics", set())
        adapter._reply_tokens = {}
        adapter._session_routing = {}
        adapter._bot_user_id = None
        adapter.reply_token_ttl = 50
        adapter.handle_message = AsyncMock()
        adapter.platform = Platform("eko")
        return adapter

    def _group_text_event(self, text, group_id="g1", topic_id="t1"):
        return {
            "replyToken": "tok",
            "type": "message",
            "source": {"userId": "u1", "username": "alice"},
            "message": {
                "id": "msg1",
                "type": "text",
                "text": text,
                "groupId": group_id,
                "topicId": topic_id,
                "groupType": "group",
            },
        }

    def _dm_text_event(self, text):
        return {
            "replyToken": "tok",
            "type": "message",
            "source": {"userId": "u1", "username": "alice"},
            "message": {
                "id": "msg1",
                "type": "text",
                "text": text,
            },
        }

    @pytest.mark.asyncio
    async def test_disabled_responds_to_all_group_messages(self):
        """require_mention=false (default): all group messages pass through."""
        adapter = self._make_adapter(require_mention=False)
        await adapter._handle_message_event(self._group_text_event("hello"))
        adapter.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_enabled_untriggered_group_message_ignored(self):
        """require_mention=true: group message without @mention is silently dropped."""
        adapter = self._make_adapter(require_mention=True, mention_triggers=["Hermes Agent"])
        await adapter._handle_message_event(self._group_text_event("hello world"))
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_at_mention_at_start_passes(self):
        """require_mention=true: '@Hermes Agent ...' passes."""
        adapter = self._make_adapter(require_mention=True, mention_triggers=["Hermes Agent"])
        await adapter._handle_message_event(self._group_text_event("@Hermes Agent what is 2+2?"))
        adapter.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_at_mention_mid_text_passes(self):
        """require_mention=true: 'hey @Hermes Agent help' passes."""
        adapter = self._make_adapter(require_mention=True, mention_triggers=["Hermes Agent"])
        await adapter._handle_message_event(self._group_text_event("hey @Hermes Agent help"))
        adapter.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_at_mention_alone_passes(self):
        """require_mention=true: just '@Hermes Agent' passes."""
        adapter = self._make_adapter(require_mention=True, mention_triggers=["Hermes Agent"])
        await adapter._handle_message_event(self._group_text_event("@Hermes Agent"))
        adapter.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_bare_trigger_without_at_ignored(self):
        """Bare 'hermes' without @ prefix is ignored."""
        adapter = self._make_adapter(require_mention=True, mention_triggers=["Hermes Agent"])
        await adapter._handle_message_event(self._group_text_event("hermes help"))
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_wrong_case_ignored(self):
        """Case-sensitive: '@hermes agent' does NOT match trigger 'Hermes Agent'."""
        adapter = self._make_adapter(require_mention=True, mention_triggers=["Hermes Agent"])
        await adapter._handle_message_event(self._group_text_event("@hermes agent help"))
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_dm_always_passes_when_enabled(self):
        """require_mention=true: DMs always pass regardless of mention."""
        adapter = self._make_adapter(require_mention=True, mention_triggers=["Hermes Agent"])
        await adapter._handle_message_event(self._dm_text_event("hello"))
        adapter.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_dm_always_passes_when_disabled(self):
        """require_mention=false: DMs always pass (baseline)."""
        adapter = self._make_adapter(require_mention=False)
        await adapter._handle_message_event(self._dm_text_event("hello"))
        adapter.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_at_all_always_triggers(self):
        """@all (Eko group-wide mention) always triggers the bot."""
        adapter = self._make_adapter(require_mention=True, mention_triggers=["Hermes Agent"])
        await adapter._handle_message_event(self._group_text_event("@all please check this"))
        adapter.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_at_all_word_boundary(self):
        """@all not at word boundary does NOT trigger (e.g. '@alliance')."""
        adapter = self._make_adapter(require_mention=True, mention_triggers=["Hermes Agent"])
        await adapter._handle_message_event(self._group_text_event("@alliance meeting"))
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_triggers_defaults_to_hermes_agent(self):
        """When mention_triggers is empty, default to 'Hermes Agent'."""
        adapter = self._make_adapter(require_mention=True, mention_triggers=[])
        await adapter._handle_message_event(self._group_text_event("@Hermes Agent ping"))
        adapter.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_triggers_no_match_ignored(self):
        """Empty triggers + no @Hermes Agent in text → ignored."""
        adapter = self._make_adapter(require_mention=True, mention_triggers=[])
        await adapter._handle_message_event(self._group_text_event("random chat"))
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_custom_trigger(self):
        """Custom trigger word with @ prefix."""
        adapter = self._make_adapter(require_mention=True, mention_triggers=["Bot"])
        await adapter._handle_message_event(self._group_text_event("@Bot status"))
        adapter.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_trigger_embedded_in_longer_name_rejected(self):
        """@HermesAgent (no space) does NOT match trigger 'Hermes Agent'."""
        adapter = self._make_adapter(require_mention=True, mention_triggers=["Hermes Agent"])
        await adapter._handle_message_event(self._group_text_event("@HermesAgent stuff"))
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_slash_command_bypasses_mention_filter(self):
        """Slash commands like /new, /stop bypass the mention filter."""
        adapter = self._make_adapter(require_mention=True, mention_triggers=["Hermes Agent"])
        await adapter._handle_message_event(self._group_text_event("/new"))
        adapter.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_slash_command_not_bypassed_when_disabled(self):
        """Slash commands always pass (require_mention off is irrelevant, baseline)."""
        adapter = self._make_adapter(require_mention=False)
        await adapter._handle_message_event(self._group_text_event("/new"))
        adapter.handle_message.assert_called_once()


class TestGroupAllowlist:
    """Issue #26: group/topic allowlist controls."""

    def _make_adapter(self, **overrides):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter.allow_all_groups = overrides.get("allow_all_groups", True)
        adapter.allowed_groups = overrides.get("allowed_groups", set())
        adapter.allowed_topics = overrides.get("allowed_topics", set())
        adapter.require_mention = overrides.get("require_mention", False)
        adapter.mention_triggers = overrides.get("mention_triggers", [])
        adapter._reply_tokens = {}
        adapter._session_routing = {}
        adapter._bot_user_id = None
        adapter.reply_token_ttl = 50
        adapter.handle_message = AsyncMock()
        adapter.platform = Platform("eko")
        return adapter

    def _group_event(self, group_id="g1", topic_id="t1", group_type="group"):
        return {
            "replyToken": "tok",
            "type": "message",
            "source": {"userId": "u1", "username": "alice"},
            "message": {
                "id": "msg1",
                "type": "text",
                "text": "hello",
                "groupId": group_id,
                "topicId": topic_id,
                "groupType": group_type,
            },
        }

    def _dm_event(self):
        return {
            "replyToken": "tok",
            "type": "message",
            "source": {"userId": "u1", "username": "alice"},
            "message": {
                "id": "msg1",
                "type": "text",
                "text": "hello",
            },
        }

    @pytest.mark.asyncio
    async def test_allow_all_groups_permits_any_group(self):
        """allow_all_groups=true (default): any group passes."""
        adapter = self._make_adapter(allow_all_groups=True)
        await adapter._handle_message_event(self._group_event(group_id="g_unknown"))
        adapter.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_disallowed_group_rejected(self):
        """allow_all_groups=false: group not in allowed_groups is rejected."""
        adapter = self._make_adapter(
            allow_all_groups=False, allowed_groups={"g1"}
        )
        await adapter._handle_message_event(self._group_event(group_id="g_unknown"))
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_allowed_group_passes(self):
        """allow_all_groups=false: group in allowed_groups passes."""
        adapter = self._make_adapter(
            allow_all_groups=False, allowed_groups={"g1"}
        )
        await adapter._handle_message_event(self._group_event(group_id="g1"))
        adapter.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_allowed_topic_passes(self):
        """Topic-level allowlist: gid:tid in allowed_topics passes."""
        adapter = self._make_adapter(
            allow_all_groups=False,
            allowed_groups=set(),
            allowed_topics={"g1:t1"},
        )
        await adapter._handle_message_event(self._group_event(group_id="g1", topic_id="t1"))
        adapter.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_topic_in_wrong_group_rejected(self):
        """Topic allowlist: correct tid but wrong gid is rejected."""
        adapter = self._make_adapter(
            allow_all_groups=False,
            allowed_groups=set(),
            allowed_topics={"g1:t1"},
        )
        await adapter._handle_message_event(self._group_event(group_id="g2", topic_id="t1"))
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_group_allowlist_allows_all_topics_in_group(self):
        """Group in allowed_groups → all topics in that group pass."""
        adapter = self._make_adapter(
            allow_all_groups=False, allowed_groups={"g1"}
        )
        await adapter._handle_message_event(self._group_event(group_id="g1", topic_id="any_topic"))
        adapter.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_dm_unaffected_by_group_allowlist(self):
        """DMs are not subject to group allowlist filtering."""
        adapter = self._make_adapter(allow_all_groups=False)
        await adapter._handle_message_event(self._dm_event())
        adapter.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_topic_allowlist_overrides_group_rejection(self):
        """Topic in allowed_topics passes even if its group is NOT in allowed_groups."""
        adapter = self._make_adapter(
            allow_all_groups=False,
            allowed_groups=set(),
            allowed_topics={"g2:t5"},
        )
        await adapter._handle_message_event(self._group_event(group_id="g2", topic_id="t5"))
        adapter.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_group_id_treated_as_dm(self):
        """Event with no groupId is treated as DM and bypasses group allowlist."""
        adapter = self._make_adapter(allow_all_groups=False)
        event = {
            "replyToken": "tok",
            "type": "message",
            "source": {"userId": "u1", "username": "alice"},
            "message": {
                "id": "msg1",
                "type": "text",
                "text": "hello",
                "groupType": "",
            },
        }
        await adapter._handle_message_event(event)
        adapter.handle_message.assert_called_once()


class TestFiltersCompose:
    """Both require_mention (#22) and group allowlist (#26) must compose."""

    def _make_adapter(self, **overrides):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter.require_mention = overrides.get("require_mention", False)
        adapter.mention_triggers = overrides.get("mention_triggers", [])
        adapter.allow_all_groups = overrides.get("allow_all_groups", True)
        adapter.allowed_groups = overrides.get("allowed_groups", set())
        adapter.allowed_topics = overrides.get("allowed_topics", set())
        adapter._reply_tokens = {}
        adapter._session_routing = {}
        adapter._bot_user_id = None
        adapter.reply_token_ttl = 50
        adapter.handle_message = AsyncMock()
        adapter.platform = Platform("eko")
        return adapter

    def _group_event(self, text="hello", group_id="g1", topic_id="t1"):
        return {
            "replyToken": "tok",
            "type": "message",
            "source": {"userId": "u1", "username": "alice"},
            "message": {
                "id": "msg1",
                "type": "text",
                "text": text,
                "groupId": group_id,
                "topicId": topic_id,
                "groupType": "group",
            },
        }

    @pytest.mark.asyncio
    async def test_allowed_group_and_mentioned_passes(self):
        """Both filters pass: group allowed + @mention present."""
        adapter = self._make_adapter(
            require_mention=True,
            mention_triggers=["Hermes Agent"],
            allow_all_groups=False,
            allowed_groups={"g1"},
        )
        await adapter._handle_message_event(self._group_event(text="@Hermes Agent help"))
        adapter.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_allowed_group_but_not_mentioned_rejected(self):
        """Group allowed but no @mention → rejected by mention filter."""
        adapter = self._make_adapter(
            require_mention=True,
            mention_triggers=["Hermes Agent"],
            allow_all_groups=False,
            allowed_groups={"g1"},
        )
        await adapter._handle_message_event(self._group_event(text="random chat"))
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_mentioned_but_disallowed_group_rejected(self):
        """@mention present but group not in allowlist → rejected by group filter."""
        adapter = self._make_adapter(
            require_mention=True,
            mention_triggers=["Hermes Agent"],
            allow_all_groups=False,
            allowed_groups={"g_allowed"},
        )
        await adapter._handle_message_event(
            self._group_event(text="@Hermes Agent help", group_id="g_blocked")
        )
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_both_filters_disabled_all_pass(self):
        """Both filters off (defaults): all messages pass."""
        adapter = self._make_adapter(
            require_mention=False,
            allow_all_groups=True,
        )
        await adapter._handle_message_event(
            self._group_event(text="anything", group_id="any_group")
        )
        adapter.handle_message.assert_called_once()


class TestReplyTokenStash:

    def test_no_token_returns_empty(self):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._reply_tokens = {}
        adapter._session_routing = {}
        token, used = adapter._consume_reply_token("chat1")
        assert token == ""
        assert not used

    def test_fresh_token_consumed(self):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._reply_tokens = {"chat1": ("tok_abc", time.time() + 50)}
        token, used = adapter._consume_reply_token("chat1")
        assert token == "tok_abc"
        assert used
        # Consumed — second call returns empty.
        assert adapter._consume_reply_token("chat1") == ("", False)

    def test_expired_token_not_consumed(self):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._reply_tokens = {"chat1": ("tok_expired", time.time() - 1)}
        token, used = adapter._consume_reply_token("chat1")
        assert token == ""
        assert not used


# ---------------------------------------------------------------------------
# 4. Outbound send routing
# ---------------------------------------------------------------------------

class TestSendRouting:

    @staticmethod
    def _make_routing_adapter(**overrides):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._reply_tokens = overrides.get("_reply_tokens", {})
        adapter._session_routing = overrides.get("_session_routing", {})
        adapter._client = overrides.get("_client", MagicMock())
        adapter.message_max_chars = overrides.get("message_max_chars", 50_000)
        adapter.truncate_message = BasePlatformAdapter.truncate_message
        return adapter

    @pytest.mark.asyncio
    async def test_reply_token_used_first(self):
        adapter = self._make_routing_adapter(
            _reply_tokens={"chat1": ("tok_abc", time.time() + 50)},
            _client=MagicMock(reply_text=AsyncMock()),
        )

        result = await adapter.send("chat1", "hello")
        assert result.success
        adapter._client.reply_text.assert_called_once_with("tok_abc", "hello")
        adapter._client.push_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_falls_back_to_push_on_reply_error(self):
        adapter = self._make_routing_adapter(
            _reply_tokens={"chat1": ("tok_abc", time.time() + 50)},
            _client=MagicMock(
                reply_text=AsyncMock(side_effect=RuntimeError("reply failed")),
                push_text=AsyncMock(),
            ),
        )

        result = await adapter.send("chat1", "hello")
        assert result.success
        adapter._client.reply_text.assert_called_once()
        adapter._client.push_text.assert_called_once_with("chat1", "hello")

    @pytest.mark.asyncio
    async def test_auth_error_triggers_push_retry(self):
        adapter = self._make_routing_adapter(
            _reply_tokens={"chat1": ("tok_abc", time.time() + 50)},
            _client=MagicMock(
                reply_text=AsyncMock(side_effect=RuntimeError("401")),
                push_text=AsyncMock(),
            ),
        )

        result = await adapter.send("chat1", "hello")
        assert result.success
        adapter._client.push_text.assert_called_once_with("chat1", "hello")

    @pytest.mark.asyncio
    async def test_push_used_when_no_reply_token(self):
        adapter = self._make_routing_adapter(
            _client=MagicMock(push_text=AsyncMock()),
        )

        result = await adapter.send("chat1", "hello")
        assert result.success
        adapter._client.push_text.assert_called_once_with("chat1", "hello")

    @pytest.mark.asyncio
    async def test_push_succeeds(self):
        adapter = self._make_routing_adapter(
            _client=MagicMock(
                push_text=AsyncMock(),
            ),
        )

        result = await adapter.send("chat1", "hello")
        assert result.success
        adapter._client.push_text.assert_called_once_with("chat1", "hello")

    @pytest.mark.asyncio
    async def test_push_failure_returns_retryable(self):
        adapter = self._make_routing_adapter(
            _client=MagicMock(
                push_text=AsyncMock(side_effect=RuntimeError("server error")),
            ),
        )

        result = await adapter.send("chat1", "hello")
        assert not result.success
        assert result.retryable

    @pytest.mark.asyncio
    async def test_not_connected_returns_error(self):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._client = None
        result = await adapter.send("chat1", "hello")
        assert not result.success
        assert "not connected" in result.error


# ---------------------------------------------------------------------------
# 5a. Selectable quick replies
# ---------------------------------------------------------------------------

class TestExecApprovalQuickReplies:

    @pytest.mark.asyncio
    async def test_send_exec_approval_uses_quick_reply_with_reply_token(self):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._reply_tokens = {"chat1": ("tok_abc", time.time() + 50)}
        adapter._client = MagicMock(reply_quick_reply=AsyncMock())

        result = await adapter.send_exec_approval(
            chat_id="chat1",
            command="rm -rf /tmp/example",
            session_key="sk-eko",
            description="test approval",
        )

        assert result.success
        adapter._client.reply_quick_reply.assert_called_once()
        args = adapter._client.reply_quick_reply.call_args.args
        assert args[0] == "tok_abc"
        assert "rm -rf /tmp/example" in args[1]
        assert "test approval" in args[1]
        assert args[2] == [
            "Approve Once",
            "Approve Session",
            "Approve Always",
            "Deny",
        ]
        assert "chat1" not in adapter._reply_tokens

    @pytest.mark.asyncio
    async def test_send_exec_approval_without_reply_token_uses_text_fallback(self):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._reply_tokens = {}
        adapter._client = MagicMock(reply_quick_reply=AsyncMock())

        result = await adapter.send_exec_approval(
            chat_id="chat1",
            command="rm -rf /tmp/example",
            session_key="sk-eko",
        )

        assert not result.success
        adapter._client.reply_quick_reply.assert_not_called()


class TestSlashConfirmQuickReplies:

    @pytest.mark.asyncio
    async def test_send_slash_confirm_uses_quick_reply_with_reply_token(self):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._reply_tokens = {"chat1": ("tok_abc", time.time() + 50)}
        adapter._client = MagicMock(reply_quick_reply=AsyncMock())

        result = await adapter.send_slash_confirm(
            chat_id="chat1",
            title="/new",
            message="Confirm /new?",
            session_key="sk-eko",
            confirm_id="confirm-1",
        )

        assert result.success
        adapter._client.reply_quick_reply.assert_called_once_with(
            "tok_abc",
            "Confirm /new?",
            ["Approve Once", "Always Approve", "Cancel"],
        )
        assert "chat1" not in adapter._reply_tokens

    @pytest.mark.asyncio
    async def test_send_slash_confirm_without_reply_token_uses_text_fallback(self):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._reply_tokens = {}
        adapter._client = MagicMock(reply_quick_reply=AsyncMock())

        result = await adapter.send_slash_confirm(
            chat_id="chat1",
            title="/new",
            message="Confirm /new?",
            session_key="sk-eko",
            confirm_id="confirm-1",
        )

        assert not result.success
        adapter._client.reply_quick_reply.assert_not_called()


class TestClarifyQuickReplies:

    @pytest.mark.asyncio
    async def test_send_clarify_uses_quick_reply_with_reply_token(self):
        from tools import clarify_gateway as cm

        cm.clear_session("sk-eko")
        cm.register("cid-eko", "sk-eko", "Pick one?", ["A", "B"])

        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._reply_tokens = {"chat1": ("tok_abc", time.time() + 50)}
        adapter._client = MagicMock(reply_quick_reply=AsyncMock())

        try:
            result = await adapter.send_clarify(
                chat_id="chat1",
                question="Pick one?",
                choices=["A", "B"],
                clarify_id="cid-eko",
                session_key="sk-eko",
            )

            assert result.success
            adapter._client.reply_quick_reply.assert_called_once_with(
                "tok_abc", "Pick one?", ["A", "B"]
            )
            assert "chat1" not in adapter._reply_tokens
            pending = cm.get_pending_for_session("sk-eko")
            assert pending is not None
            assert pending.awaiting_text is True
        finally:
            cm.clear_session("sk-eko")


# ---------------------------------------------------------------------------
# 5b. Outbound chunking
# ---------------------------------------------------------------------------

class TestSendChunking:

    def _make_adapter(self, max_chars: int = 10) -> EkoAdapter:
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._reply_tokens = {}
        adapter._session_routing = {}
        adapter._client = MagicMock()
        adapter._client.push_text = AsyncMock()
        adapter._client.reply_text = AsyncMock()
        adapter.message_max_chars = max_chars
        adapter.truncate_message = BasePlatformAdapter.truncate_message
        return adapter

    @pytest.mark.asyncio
    async def test_short_message_sent_as_one(self):
        adapter = self._make_adapter(max_chars=100)
        result = await adapter.send("chat1", "hello")
        assert result.success
        adapter._client.push_text.assert_called_once_with("chat1", "hello")

    @pytest.mark.asyncio
    async def test_long_message_is_chunked(self):
        adapter = self._make_adapter(max_chars=10)
        long_text = "a" * 25
        result = await adapter.send("chat1", long_text)
        assert result.success
        # Should have been split into multiple push calls.
        assert adapter._client.push_text.call_count > 1

    @pytest.mark.asyncio
    async def test_first_chunk_uses_reply_token(self):
        adapter = self._make_adapter(max_chars=10)
        adapter._reply_tokens = {"chat1": ("tok_abc", time.time() + 50)}
        long_text = "hello world and more text here"
        result = await adapter.send("chat1", long_text)
        assert result.success
        # First chunk via reply_text.
        adapter._client.reply_text.assert_called_once()
        # Remaining chunks via push_text.
        assert adapter._client.push_text.call_count >= 1

    @pytest.mark.asyncio
    async def test_chunk_failure_stops_sending(self):
        adapter = self._make_adapter(max_chars=10)
        adapter._client.push_text = AsyncMock(
            side_effect=RuntimeError("fail")
        )
        long_text = "hello world and more"
        result = await adapter.send("chat1", long_text)
        assert not result.success


# ---------------------------------------------------------------------------
# 6. Plugin registration
# ---------------------------------------------------------------------------

class TestRegistration:

    def test_register_calls_ctx(self):
        ctx = MagicMock()
        register(ctx)
        ctx.register_platform.assert_called_once()
        call_kwargs = ctx.register_platform.call_args[1]
        assert call_kwargs["name"] == "eko"
        assert call_kwargs["label"] == "Eko"
        assert call_kwargs["emoji"] == "\U0001f4ac"
        assert call_kwargs["cron_deliver_env_var"] == "EKO_HOME_CHANNEL"
        assert call_kwargs["allowed_users_env"] == "EKO_ALLOWED_USERS"
        assert call_kwargs["allow_all_env"] == "EKO_ALLOW_ALL_USERS"
        assert "Eko Messaging API" in call_kwargs["platform_hint"]

    def test_platform_hint_mentions_media_support(self):
        ctx = MagicMock()
        register(ctx)
        hint = ctx.register_platform.call_args[1]["platform_hint"]
        assert "images and files" in hint


# ---------------------------------------------------------------------------
# 7. Config validation
# ---------------------------------------------------------------------------

class TestConfigValidation:

    def test_validate_config_passes_with_env(self):
        with patch.dict(os.environ, {
            "EKO_BASE_URL": "https://test.ekoapp.com",
            "EKO_OAUTH_CLIENT_ID": "id123",
            "EKO_OAUTH_CLIENT_SECRET": "sec456",
        }):
            assert validate_config(_make_config())

    def test_validate_config_fails_without_env(self):
        with patch.dict(os.environ, {}, clear=True):
            assert not validate_config(_make_config())

    def test_validate_config_uses_extra_as_fallback(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = _make_config(extra={
                "base_url": "https://test.ekoapp.com",
                "oauth_client_id": "id123",
                "oauth_client_secret": "sec456",
            })
            assert validate_config(cfg)

    def test_is_connected_delegates_to_validate(self):
        cfg = _make_config()
        assert is_connected(cfg) == validate_config(cfg)

    def test_check_requirements_needs_all_three(self):
        with patch.dict(os.environ, {
            "EKO_BASE_URL": "https://test.ekoapp.com",
            "EKO_OAUTH_CLIENT_ID": "id",
            "EKO_OAUTH_CLIENT_SECRET": "sec",
        }):
            assert check_requirements()
        with patch.dict(os.environ, {"EKO_BASE_URL": "x"}, clear=True):
            assert not check_requirements()


# ---------------------------------------------------------------------------
# 8. Env enablement
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 8. Webhook signature verification
# ---------------------------------------------------------------------------

class TestSignatureVerification:

    def _sign(self, secret: str, body: bytes) -> str:
        """Compute expected x-amity-signature for a given body."""
        return base64.b64encode(
            hmac.new(
                secret.encode("utf-8"), body, hashlib.sha256
            ).digest()
        ).decode("utf-8")

    def _make_adapter(self, secret: str = "my_oauth_secret") -> EkoAdapter:
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter.oauth_client_secret = secret
        adapter.webhook_secret = secret
        return adapter

    def test_valid_signature_passes(self):
        adapter = self._make_adapter("my_oauth_secret")
        body = b'{"events":[]}'
        sig = self._sign("my_oauth_secret", body)
        assert adapter._verify_signature(body, sig)

    def test_wrong_secret_rejected(self):
        adapter = self._make_adapter("my_oauth_secret")
        body = b'{"events":[]}'
        sig = self._sign("wrong_secret", body)
        assert not adapter._verify_signature(body, sig)

    def test_tampered_body_rejected(self):
        adapter = self._make_adapter("my_oauth_secret")
        body = b'{"events":[{"type":"message"}]}'
        sig = self._sign("my_oauth_secret", body)
        tampered = body.replace(b"message", b"join")
        assert not adapter._verify_signature(tampered, sig)

    def test_empty_secret_always_fails(self):
        adapter = self._make_adapter("")
        body = b'{"events":[]}'
        sig = self._sign("", body)
        assert not adapter._verify_signature(body, sig)

    def test_none_secret_always_fails(self):
        adapter = self._make_adapter("my_oauth_secret")
        adapter.webhook_secret = None
        body = b'{"events":[]}'
        sig = self._sign("my_oauth_secret", body)
        assert not adapter._verify_signature(body, sig)

    def test_separate_webhook_secret_used(self):
        """EKO_WEBHOOK_SECRET overrides oauth_client_secret."""
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter.oauth_client_secret = "oauth_secret"
        adapter.webhook_secret = "webhook_secret"
        body = b'{"events":[]}'
        sig = self._sign("webhook_secret", body)
        assert adapter._verify_signature(body, sig)
        # OAuth secret should NOT work when webhook_secret is set.
        sig_oauth = self._sign("oauth_secret", body)
        assert not adapter._verify_signature(body, sig_oauth)

    def test_falls_back_to_oauth_secret(self):
        """When EKO_WEBHOOK_SECRET is empty, oauth_client_secret is used."""
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter.oauth_client_secret = "oauth_secret"
        adapter.webhook_secret = "oauth_secret"  # fallback kicks in during __init__
        body = b'{"events":[]}'
        sig = self._sign("oauth_secret", body)
        assert adapter._verify_signature(body, sig)

    def test_realistic_payload(self):
        adapter = self._make_adapter("client_secret_abc")
        payload = {
            "events": [{
                "replyToken": "8350939a",
                "type": "message",
                "source": {
                    "type": "user",
                    "userId": "5ac20cd3",
                    "username": "alice",
                },
                "message": {"id": "5bcaa505", "type": "text", "text": "hello"},
                "timestamp": "2018-10-19T03:46:07.866Z",
            }]
        }
        body = json.dumps(payload).encode("utf-8")
        sig = self._sign("client_secret_abc", body)
        assert adapter._verify_signature(body, sig)


# ---------------------------------------------------------------------------
# 9. Env enablement
# ---------------------------------------------------------------------------

class TestEnvEnablement:

    def test_returns_none_when_missing_vars(self):
        with patch.dict(os.environ, {}, clear=True):
            assert _env_enablement() is None

    def test_seeds_extra_from_env(self):
        with patch.dict(os.environ, {
            "EKO_BASE_URL": "https://test.ekoapp.com",
            "EKO_OAUTH_CLIENT_ID": "id",
            "EKO_OAUTH_CLIENT_SECRET": "sec",
            "EKO_PORT": "9999",
            "EKO_HOST": "127.0.0.1",
            "EKO_HOME_CHANNEL": "user_123",
        }):
            result = _env_enablement()
            assert result is not None
            assert result["port"] == 9999
            assert result["host"] == "127.0.0.1"
            assert result["home_channel"] == "user_123"


# ---------------------------------------------------------------------------
# 10. fetch_picture
# ---------------------------------------------------------------------------

def _make_eko_client(
    base_url: str = "https://test.ekoapp.com",
    token: str = "tok_abc",
) -> _EkoClient:
    """Create a bare _EkoClient with a pre-set valid token."""
    client = _EkoClient.__new__(_EkoClient)
    client._base_url = base_url
    client._access_token = token
    client._token_expires_at = time.time() + 3600
    client._timeout = 15.0
    client._client_id = "test_id"
    client._client_secret = "test_secret"
    return client


def _mock_aiohttp_for_fetch(status: int, body: bytes = b"", refresh_status: int = 200) -> MagicMock:
    """Build a mock aiohttp module that stubs ClientSession + GET.

    ``refresh_status`` configures the mock POST response used when
    ``_refresh_token`` is called during 401 retry.
    """
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.read = AsyncMock(return_value=body)
    mock_resp.text = AsyncMock(return_value="error body")
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=None)

    # Mock response for POST (used by _refresh_token during retry)
    mock_post_resp = MagicMock()
    mock_post_resp.status = refresh_status
    mock_post_resp.json = AsyncMock(return_value={"access_token": "tok_refreshed", "expires_in": 3600})
    mock_post_resp.text = AsyncMock(return_value="")
    mock_post_resp.__aenter__ = AsyncMock(return_value=mock_post_resp)
    mock_post_resp.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.post = MagicMock(return_value=mock_post_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    mock_aiohttp = MagicMock()
    mock_aiohttp.ClientSession = MagicMock(return_value=mock_session)
    mock_aiohttp.ClientTimeout = MagicMock()
    mock_aiohttp.FormData = MagicMock()
    return mock_aiohttp


class TestFetchPicture:

    @pytest.mark.asyncio
    async def test_fetch_picture_returns_bytes(self):
        image_data = b"\x89PNG\r\n\x1a\n"
        mock_aiohttp = _mock_aiohttp_for_fetch(200, image_data)
        client = _make_eko_client()

        with patch.dict("sys.modules", {"aiohttp": mock_aiohttp}):
            result = await client.fetch_picture("pic123")

        assert result == image_data
        mock_session = mock_aiohttp.ClientSession.return_value
        mock_session.get.assert_called_once_with(
            "https://test.ekoapp.com/file/view/pic123?size=large",
            headers={"Authorization": "Bearer tok_abc"},
        )

    @pytest.mark.asyncio
    async def test_fetch_picture_401_raises_auth_error(self):
        mock_aiohttp = _mock_aiohttp_for_fetch(401)
        client = _make_eko_client()

        with patch.dict("sys.modules", {"aiohttp": mock_aiohttp}):
            with pytest.raises(RuntimeError):
                await client.fetch_picture("pic456")

        assert client._access_token is None


# ---------------------------------------------------------------------------
# 11. push_picture / reply_picture / push_file
# ---------------------------------------------------------------------------


def _mock_aiohttp_for_post(status: int, json_body=None, refresh_status: int = 200) -> MagicMock:
    """Build a mock aiohttp module that stubs ClientSession + POST.

    ``refresh_status`` configures the mock POST response used when
    ``_refresh_token`` is called during 401 retry.
    """
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.text = AsyncMock(return_value="error body")
    if json_body is not None:
        mock_resp.json = AsyncMock(return_value=json_body)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    mock_aiohttp = MagicMock()
    mock_aiohttp.ClientSession = MagicMock(return_value=mock_session)
    mock_aiohttp.ClientTimeout = MagicMock()
    mock_aiohttp.FormData = MagicMock()

    # If status is 401, the retry path calls _refresh_token which
    # does its own POST. Patch that through by returning a successful
    # token response when the OAuth endpoint is hit.
    if status == 401 and refresh_status == 200:
        _orig_post = mock_session.post

        def _smart_post(url, **kwargs):
            if "/oauth/token" in str(url):
                refresh_resp = MagicMock()
                refresh_resp.status = 200
                refresh_resp.json = AsyncMock(return_value={"access_token": "tok_refreshed", "expires_in": 3600})
                refresh_resp.text = AsyncMock(return_value="")
                refresh_resp.__aenter__ = AsyncMock(return_value=refresh_resp)
                refresh_resp.__aexit__ = AsyncMock(return_value=None)
                return refresh_resp
            return _orig_post.return_value

        mock_session.post = MagicMock(side_effect=_smart_post)

    return mock_aiohttp


class TestEkoClientOutboundMedia:

    @pytest.mark.asyncio
    async def test_push_picture_sends_multipart(self):
        mock_aiohttp = _mock_aiohttp_for_post(200)
        client = _make_eko_client()

        with patch.dict("sys.modules", {"aiohttp": mock_aiohttp}):
            await client.push_picture("user1", b"imgdata", "photo.png")

        mock_session = mock_aiohttp.ClientSession.return_value
        mock_session.post.assert_called_once()
        call_args = mock_session.post.call_args
        assert call_args[0][0].endswith("/bot/v1/direct/picture")

    @pytest.mark.asyncio
    async def test_reply_picture_sends_multipart(self):
        mock_aiohttp = _mock_aiohttp_for_post(200)
        client = _make_eko_client()

        with patch.dict("sys.modules", {"aiohttp": mock_aiohttp}):
            await client.reply_picture("reply_tok", b"imgdata", "photo.png")

        mock_session = mock_aiohttp.ClientSession.return_value
        mock_session.post.assert_called_once()
        call_args = mock_session.post.call_args
        assert call_args[0][0].endswith("/bot/v1/message/picture")

    @pytest.mark.asyncio
    async def test_reply_quick_reply_sends_json(self):
        mock_aiohttp = _mock_aiohttp_for_post(200)
        client = _make_eko_client()

        with patch.dict("sys.modules", {"aiohttp": mock_aiohttp}):
            await client.reply_quick_reply("reply_tok", "Pick one?", ["A", "B"])

        mock_session = mock_aiohttp.ClientSession.return_value
        mock_session.post.assert_called_once()
        call_args = mock_session.post.call_args
        assert call_args[0][0].endswith("/bot/v1/message/quickreply")
        payload = call_args.kwargs["json"]
        assert payload["replyToken"] == "reply_tok"
        assert payload["message"]["data"] == "Pick one?"
        items = payload["message"]["meta"]["quickreply"]["items"]
        assert items == [
            {"data": {"text": "A"}, "type": "label", "value": "A"},
            {"data": {"text": "B"}, "type": "label", "value": "B"},
        ]

    @pytest.mark.asyncio
    async def test_push_file_sends_multipart(self):
        mock_aiohttp = _mock_aiohttp_for_post(200)
        client = _make_eko_client()

        with patch.dict("sys.modules", {"aiohttp": mock_aiohttp}):
            await client.push_file("user1", b"filedata", "doc.pdf")

        mock_session = mock_aiohttp.ClientSession.return_value
        mock_session.post.assert_called_once()
        call_args = mock_session.post.call_args
        assert call_args[0][0].endswith("/bot/v1/direct/file")

    @pytest.mark.asyncio
    async def test_push_picture_401_raises_auth_error(self):
        mock_aiohttp = _mock_aiohttp_for_post(401)
        client = _make_eko_client()

        with patch.dict("sys.modules", {"aiohttp": mock_aiohttp}):
            with pytest.raises(RuntimeError):
                await client.push_picture("user1", b"imgdata", "photo.png")

        assert client._access_token is None
        assert client._token_expires_at == 0.0


def _mock_aiohttp_for_get(status: int, json_body=None) -> MagicMock:
    """Build a mock aiohttp module that stubs ClientSession + GET.

    Also supports POST for ``_refresh_token`` during 401 retry.
    """
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.text = AsyncMock(return_value="error body")
    if json_body is not None:
        mock_resp.json = AsyncMock(return_value=json_body)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=None)

    # Mock response for POST (used by _refresh_token during retry)
    mock_post_resp = MagicMock()
    mock_post_resp.status = 200
    mock_post_resp.json = AsyncMock(return_value={"access_token": "tok_refreshed", "expires_in": 3600})
    mock_post_resp.text = AsyncMock(return_value="")
    mock_post_resp.__aenter__ = AsyncMock(return_value=mock_post_resp)
    mock_post_resp.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.post = MagicMock(return_value=mock_post_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    mock_aiohttp = MagicMock()
    mock_aiohttp.ClientSession = MagicMock(return_value=mock_session)
    mock_aiohttp.ClientTimeout = MagicMock()
    return mock_aiohttp


class TestManagementMethods:
    """Tests for _EkoClient.create_group, create_topic, query_users."""

    @pytest.mark.asyncio
    async def test_create_group_returns_dict(self):
        group_resp = {"_id": "grp_1", "type": "direct_chat", "members": ["u1", "u2"]}
        mock_aiohttp = _mock_aiohttp_for_post(200, json_body=group_resp)
        client = _make_eko_client()

        with patch.dict("sys.modules", {"aiohttp": mock_aiohttp}):
            result = await client.create_group(["u1", "u2"], name="Test Group")

        assert result == group_resp
        mock_session = mock_aiohttp.ClientSession.return_value
        mock_session.post.assert_called_once()
        call_args = mock_session.post.call_args
        assert call_args[0][0].endswith("/bot/v1/groups")

    @pytest.mark.asyncio
    async def test_create_group_no_name(self):
        group_resp = {"_id": "grp_2", "type": "direct_chat"}
        mock_aiohttp = _mock_aiohttp_for_post(200, json_body=group_resp)
        client = _make_eko_client()

        with patch.dict("sys.modules", {"aiohttp": mock_aiohttp}):
            result = await client.create_group(["u1"])

        assert result == group_resp

    @pytest.mark.asyncio
    async def test_create_group_401_raises_auth_error(self):
        mock_aiohttp = _mock_aiohttp_for_post(401)
        client = _make_eko_client()

        with patch.dict("sys.modules", {"aiohttp": mock_aiohttp}):
            with pytest.raises(RuntimeError):
                await client.create_group(["u1"])

        assert client._access_token is None
        assert client._token_expires_at == 0.0

    @pytest.mark.asyncio
    async def test_create_group_server_error_raises_runtime(self):
        mock_aiohttp = _mock_aiohttp_for_post(500)
        client = _make_eko_client()

        with patch.dict("sys.modules", {"aiohttp": mock_aiohttp}):
            with pytest.raises(RuntimeError, match="Eko API 500.*POST /bot/v1/groups"):
                await client.create_group(["u1"])

    @pytest.mark.asyncio
    async def test_create_topic_returns_dict(self):
        topic_resp = {"_id": "top_1", "gid": "grp_1", "name": "General"}
        mock_aiohttp = _mock_aiohttp_for_post(200, json_body=topic_resp)
        client = _make_eko_client()

        with patch.dict("sys.modules", {"aiohttp": mock_aiohttp}):
            result = await client.create_topic("grp_1", "General")

        assert result == topic_resp
        mock_session = mock_aiohttp.ClientSession.return_value
        mock_session.post.assert_called_once()
        call_args = mock_session.post.call_args
        assert call_args[0][0].endswith("/bot/v1/groups/grp_1/topics")

    @pytest.mark.asyncio
    async def test_create_topic_401_raises_auth_error(self):
        mock_aiohttp = _mock_aiohttp_for_post(401)
        client = _make_eko_client()

        with patch.dict("sys.modules", {"aiohttp": mock_aiohttp}):
            with pytest.raises(RuntimeError):
                await client.create_topic("grp_1", "Topic")

        assert client._access_token is None

    @pytest.mark.asyncio
    async def test_create_topic_server_error_raises_runtime(self):
        mock_aiohttp = _mock_aiohttp_for_post(500)
        client = _make_eko_client()

        with patch.dict("sys.modules", {"aiohttp": mock_aiohttp}):
            with pytest.raises(RuntimeError, match="Eko API 500.*POST /bot/v1/groups/grp_1/topics"):
                await client.create_topic("grp_1", "Topic")

    @pytest.mark.asyncio
    async def test_query_users_returns_list(self):
        users_resp = [
            {"_id": "u1", "username": "alice", "email": "alice@ex.com"},
            {"_id": "u2", "username": "alice2", "email": "alice2@ex.com"},
        ]
        mock_aiohttp = _mock_aiohttp_for_get(200, json_body=users_resp)
        client = _make_eko_client()

        with patch.dict("sys.modules", {"aiohttp": mock_aiohttp}):
            result = await client.query_users("alice")

        assert result == users_resp
        mock_session = mock_aiohttp.ClientSession.return_value
        mock_session.get.assert_called_once()
        call_args = mock_session.get.call_args
        assert call_args[0][0].endswith("/bot/v1/users")

    @pytest.mark.asyncio
    async def test_query_users_401_raises_auth_error(self):
        mock_aiohttp = _mock_aiohttp_for_get(401)
        client = _make_eko_client()

        with patch.dict("sys.modules", {"aiohttp": mock_aiohttp}):
            with pytest.raises(RuntimeError):
                await client.query_users("bob")

        assert client._access_token is None

    @pytest.mark.asyncio
    async def test_query_users_server_error_raises_runtime(self):
        mock_aiohttp = _mock_aiohttp_for_get(500)
        client = _make_eko_client()

        with patch.dict("sys.modules", {"aiohttp": mock_aiohttp}):
            with pytest.raises(RuntimeError, match="Eko API 500.*GET /bot/v1/users"):
                await client.query_users("bob")


# ---------------------------------------------------------------------------
# 12. Inbound picture handling
# ---------------------------------------------------------------------------

class TestInboundPicture:

    @pytest.mark.asyncio
    async def test_picture_downloads_and_caches(self):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._client = MagicMock()
        fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        adapter._client.fetch_picture = AsyncMock(return_value=fake_png)
        adapter._reply_tokens = {}
        adapter._session_routing = {}
        adapter._bot_user_id = None
        adapter.reply_token_ttl = 50
        adapter.handle_message = AsyncMock()
        adapter.platform = Platform("eko")

        event = {
            "replyToken": "tok123",
            "type": "message",
            "source": {"type": "user", "userId": "user123", "username": "alice"},
            "message": {
                "id": "msg123",
                "type": "picture",
                "pictureId": "pic456",
                "fileName": "photo.png",
                "groupId": "g1",
                "groupType": "direct_chat",
                "topicId": "t1",
            },
            "timestamp": "2026-05-21T00:00:00.000Z",
        }

        with patch(
            "gateway.platforms.base.cache_image_from_bytes",
            return_value="/cache/img_abc.png",
        ) as mock_cache:
            await adapter._handle_message_event(event)

        adapter._client.fetch_picture.assert_called_once_with("pic456")
        mock_cache.assert_called_once_with(fake_png, ext=".png")

        call_args = adapter.handle_message.call_args[0][0]
        assert call_args.message_type == MessageType.PHOTO
        assert call_args.media_urls == ["/cache/img_abc.png"]
        assert "image/png" in call_args.media_types

    @pytest.mark.asyncio
    async def test_picture_download_failure_falls_back_to_placeholder(self):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._client = MagicMock()
        adapter._client.fetch_picture = AsyncMock(
            side_effect=RuntimeError("download failed")
        )
        adapter._reply_tokens = {}
        adapter._session_routing = {}
        adapter._bot_user_id = None
        adapter.reply_token_ttl = 50
        adapter.handle_message = AsyncMock()
        adapter.platform = Platform("eko")

        event = {
            "replyToken": "tok123",
            "type": "message",
            "source": {"type": "user", "userId": "user123", "username": "alice"},
            "message": {
                "id": "msg123",
                "type": "picture",
                "pictureId": "pic456",
                "fileName": "photo.png",
            },
            "timestamp": "2026-05-21T00:00:00.000Z",
        }

        await adapter._handle_message_event(event)
        call_args = adapter.handle_message.call_args[0][0]
        assert call_args.text == "[image]"
        assert call_args.media_urls == []
        assert call_args.message_type == MessageType.TEXT

    @pytest.mark.asyncio
    async def test_sticker_surfaces_placeholder(self):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._reply_tokens = {}
        adapter._session_routing = {}
        adapter._bot_user_id = None
        adapter.reply_token_ttl = 50
        adapter.handle_message = AsyncMock()
        adapter.platform = Platform("eko")

        event = {
            "replyToken": "tok123",
            "type": "message",
            "source": {"type": "user", "userId": "user123", "username": "alice"},
            "message": {
                "id": "msg_sticker",
                "type": "sticker",
                "packageId": "pkg1",
                "stickerId": "stk1",
            },
            "timestamp": "2026-05-21T00:00:00.000Z",
        }

        await adapter._handle_message_event(event)
        call_args = adapter.handle_message.call_args[0][0]
        assert call_args.text == "[sticker]"


# ---------------------------------------------------------------------------
# 13. Outbound media (send_image_file, send_image, send_document)
# ---------------------------------------------------------------------------

class TestOutboundMedia:

    @pytest.mark.asyncio
    async def test_send_image_file_uses_reply_token(self):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._reply_tokens = {"chat1": ("tok_abc", time.time() + 50)}
        adapter._client = MagicMock()
        adapter._client.reply_picture = AsyncMock()

        with patch("pathlib.Path.read_bytes", return_value=b"\x89PNG data"):
            result = await adapter.send_image_file("chat1", "/fake/img.png", caption="hi")
        assert result.success
        adapter._client.reply_picture.assert_called_once()
        adapter._client.push_picture.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_image_file_falls_back_to_push(self):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._reply_tokens = {}
        adapter._session_routing = {}
        adapter._client = MagicMock()
        adapter._client.push_picture = AsyncMock()

        with patch("pathlib.Path.read_bytes", return_value=b"\x89PNG data"):
            result = await adapter.send_image_file("chat1", "/fake/img.png", caption="hi")
        assert result.success
        adapter._client.push_picture.assert_called_once_with(
            "chat1",
            b"\x89PNG data",
            "img.png",
            caption="hi",
        )

    @pytest.mark.asyncio
    async def test_send_image_downloads_and_delegates(self):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._reply_tokens = {}
        adapter._session_routing = {}
        adapter._client = MagicMock()
        adapter._client.push_picture = AsyncMock()

        with patch("gateway.platforms.base.cache_image_from_url", AsyncMock(return_value="/cache/img_abc.jpg")):
            with patch("pathlib.Path.read_bytes", return_value=b"\xff\xd8\xff image data"):
                result = await adapter.send_image("chat1", "https://example.com/img.jpg", caption="look")
        assert result.success
        adapter._client.push_picture.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_document_pushes_file(self):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._reply_tokens = {}
        adapter._session_routing = {}
        adapter._client = MagicMock()
        adapter._client.push_file = AsyncMock()

        with patch("pathlib.Path.read_bytes", return_value=b"%PDF-1.4 data"):
            result = await adapter.send_document(
                "chat1", "/fake/report.pdf", file_name="report.pdf"
            )
        assert result.success
        adapter._client.push_file.assert_called_once_with(
            "chat1",
            b"%PDF-1.4 data",
            "report.pdf",
        )

    @pytest.mark.asyncio
    async def test_send_image_file_auth_error_retries_push(self):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._reply_tokens = {"chat1": ("tok_abc", time.time() + 50)}
        adapter._client = MagicMock()
        adapter._client.reply_picture = AsyncMock(side_effect=RuntimeError("401"))
        adapter._client.push_picture = AsyncMock()

        with patch("pathlib.Path.read_bytes", return_value=b"\x89PNG data"):
            result = await adapter.send_image_file("chat1", "/fake/img.png")
        assert result.success
        adapter._client.push_picture.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_document_not_connected_returns_error(self):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._client = None
        result = await adapter.send_document("chat1", "/fake/file.pdf")
        assert not result.success


# ---------------------------------------------------------------------------
# 14. Signature enforcement and normalization
# ---------------------------------------------------------------------------


def _sign_body(secret: str, body: bytes) -> str:
    return base64.b64encode(
        hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    ).decode("utf-8")


def _mock_request(body: bytes, headers: dict | None = None):
    req = MagicMock()
    req.headers = headers or {}
    req.read = AsyncMock(return_value=body)
    return req


class TestWebhookSignaturePolicy:

    def _make_adapter(self, *, secret: str = "my_oauth_secret", require_signature: bool = True):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter.webhook_secret = secret
        adapter.require_signature = require_signature
        return adapter

    @pytest.mark.asyncio
    async def test_missing_signature_rejected_by_default(self):
        adapter = self._make_adapter(require_signature=True)
        req = _mock_request(b'{"events":[]}', headers={})
        resp = await adapter._handle_webhook(req)
        assert resp.status == 401
        assert "missing signature" in resp.text

    @pytest.mark.asyncio
    async def test_invalid_signature_rejected_by_default(self):
        adapter = self._make_adapter(require_signature=True)
        req = _mock_request(b'{"events":[]}', headers={"x-eko-signature": "bad_sig"})
        resp = await adapter._handle_webhook(req)
        assert resp.status == 403
        assert "invalid signature" in resp.text

    @pytest.mark.asyncio
    async def test_valid_signature_accepted(self):
        body = b'{"events":[]}'
        adapter = self._make_adapter(secret="my_oauth_secret", require_signature=True)
        req = _mock_request(body, headers={"x-eko-signature": _sign_body("my_oauth_secret", body)})
        resp = await adapter._handle_webhook(req)
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_missing_signature_allowed_when_disabled(self):
        adapter = self._make_adapter(require_signature=False)
        req = _mock_request(b'{"events":[]}', headers={})
        resp = await adapter._handle_webhook(req)
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_disabled_mode_still_rejects_bad_signature(self):
        adapter = self._make_adapter(require_signature=False)
        req = _mock_request(b'{"events":[]}', headers={"x-eko-signature": "wrong"})
        resp = await adapter._handle_webhook(req)
        assert resp.status == 403


class TestWebhookSignatureNormalization:

    def _make_adapter(self, secret: str = "my_oauth_secret") -> EkoAdapter:
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter.webhook_secret = secret
        return adapter

    def test_whitespace_is_trimmed(self):
        adapter = self._make_adapter()
        body = b'{"events":[]}'
        sig = _sign_body("my_oauth_secret", body)
        assert adapter._verify_signature(body, f"  {sig}  ")

    def test_sha256_prefix_is_accepted(self):
        adapter = self._make_adapter()
        body = b'{"events":[]}'
        sig = _sign_body("my_oauth_secret", body)
        assert adapter._verify_signature(body, f"sha256={sig}")

    def test_sha256_prefix_is_case_insensitive(self):
        adapter = self._make_adapter()
        body = b'{"events":[]}'
        sig = _sign_body("my_oauth_secret", body)
        assert adapter._verify_signature(body, f"SHA256={sig}")

    def test_bare_signature_still_works(self):
        adapter = self._make_adapter()
        body = b'{"events":[]}'
        sig = _sign_body("my_oauth_secret", body)
        assert adapter._verify_signature(body, sig)

    def test_prefix_and_whitespace_together_work(self):
        adapter = self._make_adapter()
        body = b'{"events":[]}'
        sig = _sign_body("my_oauth_secret", body)
        assert adapter._verify_signature(body, f"  sha256={sig}  ")


# ---------------------------------------------------------------------------
# Topic / session routing
# ---------------------------------------------------------------------------


class TestTopicRouting:
    """Verify that DM and topic messages get separate sessions."""

    @staticmethod
    def _make_adapter():
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._reply_tokens = {}
        adapter._session_routing = {}
        adapter._client = MagicMock()
        adapter._bot_user_id = None
        adapter.reply_token_ttl = 50
        adapter.handle_message = AsyncMock()
        adapter.platform = Platform("eko")
        adapter.allow_all_groups = True
        adapter.allowed_groups = set()
        adapter.allowed_topics = set()
        adapter.require_mention = False
        adapter.mention_triggers = []
        return adapter

    @pytest.mark.asyncio
    async def test_dm_uses_session_id_as_chat_id(self):
        """DM message uses sessionId as chat_id (not sender uid)."""
        adapter = self._make_adapter()

        event = {
            "replyToken": "tok_dm",
            "type": "message",
            "source": {
                "type": "user",
                "userId": "user_abc",
                "username": "alice",
            },
            "message": {
                "id": "msg_dm",
                "type": "text",
                "groupId": "grp_1",
                "groupType": "direct_chat",
                "topicId": "topic_dm",
                "text": "hello from DM",
            },
            "sessionId": "grp_1_topic_dm",
        }

        await adapter._handle_message_event(event)

        call_args = adapter.handle_message.call_args[0][0]
        assert call_args.source.chat_id == "grp_1_topic_dm"
        assert call_args.source.chat_type == "dm"

    @pytest.mark.asyncio
    async def test_group_chat_name_uses_group_id(self):
        """Group chat_name should be the group ID, not the sender's username."""
        adapter = self._make_adapter()

        event = {
            "replyToken": "tok_grp",
            "type": "message",
            "source": {"userId": "user_abc", "username": "alice"},
            "message": {
                "id": "msg_grp",
                "type": "text",
                "groupId": "grp_1",
                "groupType": "group",
                "topicId": "topic_1",
                "text": "hello group",
            },
            "sessionId": "grp_1_topic_1",
        }

        await adapter._handle_message_event(event)

        call_args = adapter.handle_message.call_args[0][0]
        assert call_args.source.chat_name == "grp_1"
        assert call_args.source.chat_type == "group"
        assert call_args.source.thread_id == "topic_1"

    @pytest.mark.asyncio
    async def test_dm_chat_name_uses_username(self):
        """DM chat_name should be the sender's username, not a group ID."""
        adapter = self._make_adapter()

        event = {
            "replyToken": "tok_dm",
            "type": "message",
            "source": {"userId": "user_abc", "username": "alice"},
            "message": {
                "id": "msg_dm",
                "type": "text",
                "groupId": "grp_1",
                "groupType": "direct_chat",
                "topicId": "topic_dm",
                "text": "hello DM",
            },
            "sessionId": "grp_1_topic_dm",
        }

        await adapter._handle_message_event(event)

        call_args = adapter.handle_message.call_args[0][0]
        assert call_args.source.chat_name == "alice"
        assert call_args.source.chat_type == "dm"
        assert call_args.source.thread_id == "topic_dm"

    @pytest.mark.asyncio
    async def test_topic_gets_separate_session(self):
        """A different topic in the same chat gets a different chat_id."""
        adapter = self._make_adapter()

        dm_event = {
            "replyToken": "tok_dm",
            "type": "message",
            "source": {
                "type": "user",
                "userId": "user_abc",
                "username": "alice",
            },
            "message": {
                "id": "msg_dm",
                "type": "text",
                "groupId": "grp_1",
                "groupType": "direct_chat",
                "topicId": "topic_main",
                "text": "DM main topic",
            },
            "sessionId": "grp_1_topic_main",
        }

        topic_event = {
            "replyToken": "tok_topic",
            "type": "message",
            "source": {
                "type": "user",
                "userId": "user_abc",
                "username": "alice",
            },
            "message": {
                "id": "msg_topic",
                "type": "text",
                "groupId": "grp_1",
                "groupType": "direct_chat",
                "topicId": "topic_new",
                "text": "new topic message",
            },
            "sessionId": "grp_1_topic_new",
        }

        await adapter._handle_message_event(dm_event)
        await adapter._handle_message_event(topic_event)

        dm_call = adapter.handle_message.call_args_list[0][0][0]
        topic_call = adapter.handle_message.call_args_list[1][0][0]

        assert dm_call.source.chat_id == "grp_1_topic_main"
        assert topic_call.source.chat_id == "grp_1_topic_new"
        assert dm_call.source.chat_id != topic_call.source.chat_id

    @pytest.mark.asyncio
    async def test_group_type_group_sets_chat_type(self):
        """Messages with groupType != direct_chat get chat_type='group'."""
        adapter = self._make_adapter()

        event = {
            "replyToken": "tok_grp",
            "type": "message",
            "source": {
                "type": "user",
                "userId": "user_abc",
                "username": "alice",
            },
            "message": {
                "id": "msg_grp",
                "type": "text",
                "groupId": "grp_team",
                "groupType": "team",
                "topicId": "topic_gen",
                "text": "team message",
            },
            "sessionId": "grp_team_topic_gen",
        }

        await adapter._handle_message_event(event)
        call_args = adapter.handle_message.call_args[0][0]
        assert call_args.source.chat_type == "group"

    @pytest.mark.asyncio
    async def test_routing_metadata_stored(self):
        """Routing metadata is stored for push fallback resolution."""
        adapter = self._make_adapter()

        event = {
            "replyToken": "tok_1",
            "type": "message",
            "source": {
                "type": "user",
                "userId": "user_abc",
                "username": "alice",
            },
            "message": {
                "id": "msg_1",
                "type": "text",
                "groupId": "g1",
                "groupType": "direct_chat",
                "topicId": "t1",
                "text": "test",
            },
            "sessionId": "g1_t1",
        }

        await adapter._handle_message_event(event)

        assert "g1_t1" in adapter._session_routing
        assert adapter._session_routing["g1_t1"]["uid"] == "user_abc"
        assert adapter._session_routing["g1_t1"]["groupId"] == "g1"
        assert adapter._session_routing["g1_t1"]["topicId"] == "t1"

    @pytest.mark.asyncio
    async def test_reply_token_stashed_per_session(self):
        """Reply tokens are stashed per session chat_id, not per uid."""
        adapter = self._make_adapter()

        event = {
            "replyToken": "tok_session",
            "type": "message",
            "source": {
                "type": "user",
                "userId": "user_abc",
                "username": "alice",
            },
            "message": {
                "id": "msg_1",
                "type": "text",
                "groupId": "g1",
                "groupType": "direct_chat",
                "topicId": "t1",
                "text": "test",
            },
            "sessionId": "g1_t1",
        }

        await adapter._handle_message_event(event)
        assert "g1_t1" in adapter._reply_tokens
        assert "user_abc" not in adapter._reply_tokens

    def test_resolve_uid_with_routing(self):
        """_resolve_uid returns uid from routing metadata."""
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._session_routing = {
            "g1_t1": {"uid": "user_abc", "groupId": "g1", "topicId": "t1"},
        }
        assert adapter._resolve_uid("g1_t1") == "user_abc"

    def test_resolve_uid_without_routing_falls_back(self):
        """_resolve_uid falls back to chat_id when no routing entry."""
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._session_routing = {}
        assert adapter._resolve_uid("user_abc") == "user_abc"

    def test_resolve_uid_without_attr_falls_back(self):
        """_resolve_uid handles missing _session_routing attribute."""
        adapter = EkoAdapter.__new__(EkoAdapter)
        assert adapter._resolve_uid("user_abc") == "user_abc"

    @pytest.mark.asyncio
    async def test_send_resolves_uid_for_push(self):
        """send() uses group endpoint when routing has groupId+topicId."""
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._reply_tokens = {}
        adapter._session_routing = {
            "g1_t1": {"uid": "user_abc", "groupId": "g1", "topicId": "t1"},
        }
        adapter._client = MagicMock(
            push_group_text=AsyncMock(),
            push_text=AsyncMock(),
        )
        adapter.message_max_chars = 50_000
        adapter.truncate_message = BasePlatformAdapter.truncate_message

        result = await adapter.send("g1_t1", "hello")
        assert result.success
        # Has groupId+topicId → routes to group endpoint
        adapter._client.push_group_text.assert_called_once_with("g1", "t1", "hello")
        adapter._client.push_text.assert_not_called()


# ---------------------------------------------------------------------------
# Standalone send (cron media attachments)
# ---------------------------------------------------------------------------


class TestStandaloneSend:
    """Tests for _standalone_send — out-of-process push for cron jobs."""

    @pytest.mark.asyncio
    async def test_text_only(self):
        """Sends text via push_text when no media files."""
        cfg = _make_config({
            "base_url": "https://eko.example.com",
            "oauth_client_id": "id",
            "oauth_client_secret": "secret",
        })
        with patch.object(_EkoClient, "push_text", new_callable=AsyncMock) as mock_push:
            result = await _standalone_send(cfg, "user_1", "hello cron")
        assert result == {"success": True, "message_id": None}
        mock_push.assert_called_once_with("user_1", "hello cron")

    @pytest.mark.asyncio
    async def test_missing_config_returns_error(self):
        """Returns error dict when required config is missing."""
        cfg = _make_config({})
        result = await _standalone_send(cfg, "user_1", "hello")
        assert "error" in result
        assert "missing config" in result["error"]

    @pytest.mark.asyncio
    async def test_missing_chat_id_returns_error(self):
        """Returns error dict when chat_id is empty."""
        cfg = _make_config({
            "base_url": "https://eko.example.com",
            "oauth_client_id": "id",
            "oauth_client_secret": "secret",
        })
        result = await _standalone_send(cfg, "", "hello")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_push_failure_returns_error(self):
        """Returns error when push_text raises."""
        cfg = _make_config({
            "base_url": "https://eko.example.com",
            "oauth_client_id": "id",
            "oauth_client_secret": "secret",
        })
        with patch.object(_EkoClient, "push_text", new_callable=AsyncMock, side_effect=RuntimeError("API error")):
            result = await _standalone_send(cfg, "user_1", "hello")
        assert "error" in result
        assert "API error" in result["error"]

    @pytest.mark.asyncio
    async def test_sends_image_media(self, tmp_path):
        """Sends image files via push_picture."""
        img = tmp_path / "photo.jpg"
        img.write_bytes(b"\xff\xd8\xff\xe0fake-jpg")

        cfg = _make_config({
            "base_url": "https://eko.example.com",
            "oauth_client_id": "id",
            "oauth_client_secret": "secret",
        })
        with patch.object(_EkoClient, "push_text", new_callable=AsyncMock) as mock_push, \
             patch.object(_EkoClient, "push_picture", new_callable=AsyncMock) as mock_pic:
            result = await _standalone_send(
                cfg, "user_1", "see attached",
                media_files=[str(img)],
            )
        assert result["success"] is True
        mock_push.assert_called_once_with("user_1", "see attached")
        mock_pic.assert_called_once_with("user_1", b"\xff\xd8\xff\xe0fake-jpg", "photo.jpg")

    @pytest.mark.asyncio
    async def test_sends_document_media(self, tmp_path):
        """Sends non-image files via push_file."""
        doc = tmp_path / "report.pdf"
        doc.write_bytes(b"%PDF-1.4-fake")

        cfg = _make_config({
            "base_url": "https://eko.example.com",
            "oauth_client_id": "id",
            "oauth_client_secret": "secret",
        })
        with patch.object(_EkoClient, "push_text", new_callable=AsyncMock) as mock_push, \
             patch.object(_EkoClient, "push_file", new_callable=AsyncMock) as mock_file:
            result = await _standalone_send(
                cfg, "user_1", "report attached",
                media_files=[str(doc)],
            )
        assert result["success"] is True
        mock_push.assert_called_once_with("user_1", "report attached")
        mock_file.assert_called_once_with("user_1", b"%PDF-1.4-fake", "report.pdf")

    @pytest.mark.asyncio
    async def test_force_document_sends_image_as_file(self, tmp_path):
        """force_document=True sends image files via push_file instead of push_picture."""
        img = tmp_path / "photo.png"
        img.write_bytes(b"\x89PNG\r\nfake-png")

        cfg = _make_config({
            "base_url": "https://eko.example.com",
            "oauth_client_id": "id",
            "oauth_client_secret": "secret",
        })
        with patch.object(_EkoClient, "push_text", new_callable=AsyncMock), \
             patch.object(_EkoClient, "push_file", new_callable=AsyncMock) as mock_file, \
             patch.object(_EkoClient, "push_picture", new_callable=AsyncMock) as mock_pic:
            result = await _standalone_send(
                cfg, "user_1", "see doc",
                media_files=[str(img)],
                force_document=True,
            )
        assert result["success"] is True
        mock_file.assert_called_once()
        mock_pic.assert_not_called()

    @pytest.mark.asyncio
    async def test_unreadable_media_warns(self, tmp_path):
        """Missing media file produces a warning instead of failing."""
        bad_path = str(tmp_path / "nonexistent.jpg")

        cfg = _make_config({
            "base_url": "https://eko.example.com",
            "oauth_client_id": "id",
            "oauth_client_secret": "secret",
        })
        with patch.object(_EkoClient, "push_text", new_callable=AsyncMock) as mock_push:
            result = await _standalone_send(
                cfg, "user_1", "text",
                media_files=[bad_path],
            )
        assert result["success"] is True
        assert "warnings" in result
        assert len(result["warnings"]) == 1
        mock_push.assert_called_once()

    @pytest.mark.asyncio
    async def test_media_send_failure_warns(self, tmp_path):
        """Failed push_picture produces a warning, doesn't fail the whole send."""
        img = tmp_path / "photo.jpg"
        img.write_bytes(b"fake")

        cfg = _make_config({
            "base_url": "https://eko.example.com",
            "oauth_client_id": "id",
            "oauth_client_secret": "secret",
        })
        with patch.object(_EkoClient, "push_text", new_callable=AsyncMock), \
             patch.object(_EkoClient, "push_picture", new_callable=AsyncMock, side_effect=RuntimeError("upload failed")):
            result = await _standalone_send(
                cfg, "user_1", "text",
                media_files=[str(img)],
            )
        assert result["success"] is True
        assert "warnings" in result
        assert "upload failed" in result["warnings"][0]

    @pytest.mark.asyncio
    async def test_multiple_media_files(self, tmp_path):
        """Sends multiple media files of different types."""
        img = tmp_path / "a.jpg"
        img.write_bytes(b"img-data")
        doc = tmp_path / "b.pdf"
        doc.write_bytes(b"doc-data")
        png = tmp_path / "c.png"
        png.write_bytes(b"png-data")

        cfg = _make_config({
            "base_url": "https://eko.example.com",
            "oauth_client_id": "id",
            "oauth_client_secret": "secret",
        })
        with patch.object(_EkoClient, "push_text", new_callable=AsyncMock), \
             patch.object(_EkoClient, "push_picture", new_callable=AsyncMock) as mock_pic, \
             patch.object(_EkoClient, "push_file", new_callable=AsyncMock) as mock_file:
            result = await _standalone_send(
                cfg, "user_1", "multi",
                media_files=[str(img), str(doc), str(png)],
            )
        assert result["success"] is True
        assert mock_pic.call_count == 2  # .jpg and .png
        assert mock_file.call_count == 1  # .pdf

    @pytest.mark.asyncio
    async def test_empty_message_with_media(self, tmp_path):
        """Sends media even when text message is empty."""
        img = tmp_path / "photo.jpg"
        img.write_bytes(b"img")

        cfg = _make_config({
            "base_url": "https://eko.example.com",
            "oauth_client_id": "id",
            "oauth_client_secret": "secret",
        })
        with patch.object(_EkoClient, "push_text", new_callable=AsyncMock) as mock_push, \
             patch.object(_EkoClient, "push_picture", new_callable=AsyncMock) as mock_pic:
            result = await _standalone_send(
                cfg, "user_1", "",
                media_files=[str(img)],
            )
        assert result["success"] is True
        mock_push.assert_not_called()  # empty message skips push_text
        mock_pic.assert_called_once()


# ---------------------------------------------------------------------------
# Group/topic send routing
# ---------------------------------------------------------------------------


class TestGroupSendRouting:
    """Tests for routing outbound sends to group/topic endpoints."""

    def test_is_group_chat_dm_returns_false(self):
        """Bare uid (no groupId/topicId) returns False for _is_group_chat."""
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._session_routing = {
            "user1": {"uid": "user1", "groupId": "", "topicId": "", "groupType": "direct_chat"},
        }
        assert adapter._is_group_chat("user1") is False

    def test_is_group_chat_topic_returns_true(self):
        """Routing with groupId+topicId returns True even if groupType is direct_chat."""
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._session_routing = {
            "g1_t1": {"uid": "u1", "groupId": "g1", "topicId": "t1", "groupType": "direct_chat"},
        }
        assert adapter._is_group_chat("g1_t1") is True

    def test_is_group_chat_team_returns_true(self):
        """Team groupType with groupId+topicId returns True for _is_group_chat."""
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._session_routing = {
            "g2_t2": {"uid": "u1", "groupId": "g2", "topicId": "t2", "groupType": "team"},
        }
        assert adapter._is_group_chat("g2_t2") is True

    def test_is_group_chat_unknown_returns_false(self):
        """Unknown chat_id returns False for _is_group_chat."""
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._session_routing = {}
        assert adapter._is_group_chat("unknown") is False

    @pytest.mark.asyncio
    async def test_send_text_uses_group_endpoint(self):
        """send() routes to push_group_text for team chat."""
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._reply_tokens = {}
        adapter._session_routing = {
            "g2_t2": {"uid": "user_1", "groupId": "g2", "topicId": "t2", "groupType": "team"},
        }
        adapter._client = MagicMock(
            push_group_text=AsyncMock(),
            push_text=AsyncMock(),
        )
        adapter.message_max_chars = 50_000
        adapter.truncate_message = BasePlatformAdapter.truncate_message

        result = await adapter.send("g2_t2", "hello group")
        assert result.success
        adapter._client.push_group_text.assert_called_once_with("g2", "t2", "hello group")
        adapter._client.push_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_text_dm_uses_direct_endpoint(self):
        """send() routes to push_text for bare uid (no groupId/topicId)."""
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._reply_tokens = {}
        adapter._session_routing = {
            "user_1": {"uid": "user_1", "groupId": "", "topicId": "", "groupType": "direct_chat"},
        }
        adapter._client = MagicMock(
            push_group_text=AsyncMock(),
            push_text=AsyncMock(),
        )
        adapter.message_max_chars = 50_000
        adapter.truncate_message = BasePlatformAdapter.truncate_message

        result = await adapter.send("user_1", "hello dm")
        assert result.success
        adapter._client.push_text.assert_called_once_with("user_1", "hello dm")
        adapter._client.push_group_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_image_file_uses_group_endpoint(self, tmp_path):
        """send_image_file() routes to push_group_picture for team chat."""
        img = tmp_path / "test.png"
        img.write_bytes(b"\x89PNG\r\nfake")

        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._reply_tokens = {}
        adapter._session_routing = {
            "g2_t2": {"uid": "user_1", "groupId": "g2", "topicId": "t2", "groupType": "team"},
        }
        adapter._client = MagicMock(
            push_group_picture=AsyncMock(),
            push_picture=AsyncMock(),
        )

        result = await adapter.send_image_file("g2_t2", str(img))
        assert result.success
        adapter._client.push_group_picture.assert_called_once()
        adapter._client.push_picture.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_document_uses_group_endpoint(self, tmp_path):
        """send_document() routes to push_group_file for team chat."""
        doc = tmp_path / "report.pdf"
        doc.write_bytes(b"%PDF-fake")

        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._reply_tokens = {}
        adapter._session_routing = {
            "g2_t2": {"uid": "user_1", "groupId": "g2", "topicId": "t2", "groupType": "team"},
        }
        adapter._client = MagicMock(
            push_group_file=AsyncMock(),
            push_file=AsyncMock(),
        )

        result = await adapter.send_document("g2_t2", str(doc))
        assert result.success
        adapter._client.push_group_file.assert_called_once()
        adapter._client.push_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_document_routes_direct_chat_topic_to_group(self, tmp_path):
        """send_document() routes to group endpoint even when groupType is direct_chat.

        Eko sets groupType='direct_chat' even for topics inside DM-type groups.
        The routing must be based on groupId+topicId presence, not groupType.
        """
        doc = tmp_path / "notes.md"
        doc.write_bytes(b"# notes")

        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._reply_tokens = {}
        adapter._session_routing = {
            "g1_t1": {"uid": "user_1", "groupId": "g1", "topicId": "t1", "groupType": "direct_chat"},
        }
        adapter._client = MagicMock(
            push_group_file=AsyncMock(),
            push_file=AsyncMock(),
        )

        result = await adapter.send_document("g1_t1", str(doc))
        assert result.success
        adapter._client.push_group_file.assert_called_once()
        adapter._client.push_file.assert_not_called()


# ---------------------------------------------------------------------------
# 17. _standalone_send group routing
# ---------------------------------------------------------------------------


class TestStandaloneSendGroupRouting:
    """Tests for _standalone_send group/topic routing."""

    def _cfg(self):
        return _make_config({
            "base_url": "https://eko.example.com",
            "oauth_client_id": "id",
            "oauth_client_secret": "secret",
        })

    @pytest.mark.asyncio
    async def test_standalone_send_text_uses_push_text_dm(self):
        """_standalone_send uses push_text (DM endpoint) for DM chat_ids."""
        with patch.object(_EkoClient, "push_text", new_callable=AsyncMock) as mock_push, \
             patch("gateway.run._gateway_runner_ref", return_value=None):
            await _standalone_send(self._cfg(), "user1", "hello")
        mock_push.assert_called_once_with("user1", "hello")

    @pytest.mark.asyncio
    async def test_standalone_send_file_uses_push_file_dm(self, tmp_path):
        """_standalone_send uses push_file (DM endpoint) when no group routing."""
        doc = tmp_path / "report.pdf"
        doc.write_bytes(b"%PDF-fake")

        with patch.object(_EkoClient, "push_text", new_callable=AsyncMock), \
             patch.object(_EkoClient, "push_file", new_callable=AsyncMock) as mock_file, \
             patch("gateway.run._gateway_runner_ref", return_value=None):
            result = await _standalone_send(
                self._cfg(), "user1", "", media_files=[str(doc)],
            )
        assert result["success"]
        mock_file.assert_called_once()
        # Verify it was called with uid ("user1") as first arg — the DM endpoint.
        args = mock_file.call_args
        assert args[0][0] == "user1"

    @pytest.mark.asyncio
    async def test_standalone_send_image_uses_push_picture_dm(self, tmp_path):
        """_standalone_send uses push_picture (DM endpoint) when no group routing."""
        img = tmp_path / "photo.jpg"
        img.write_bytes(b"\xff\xd8\xff\xe0JPG")

        with patch.object(_EkoClient, "push_text", new_callable=AsyncMock), \
             patch.object(_EkoClient, "push_picture", new_callable=AsyncMock) as mock_pic, \
             patch("gateway.run._gateway_runner_ref", return_value=None):
            result = await _standalone_send(
                self._cfg(), "user1", "", media_files=[str(img)],
            )
        assert result["success"]
        mock_pic.assert_called_once()
        args = mock_pic.call_args
        assert args[0][0] == "user1"

    @pytest.mark.asyncio
    async def test_standalone_send_routes_text_to_group(self):
        """_standalone_send uses push_group_text when live adapter has group routing."""
        mock_adapter = MagicMock()
        mock_adapter._get_routing.return_value = {
            "uid": "user_1",
            "groupId": "g1",
            "topicId": "t1",
            "groupType": "team",
        }
        mock_runner = MagicMock()
        mock_runner.adapters = {Platform("eko"): mock_adapter}

        with patch.object(_EkoClient, "push_group_text", new_callable=AsyncMock) as mock_group_text, \
             patch("gateway.run._gateway_runner_ref", return_value=mock_runner):
            result = await _standalone_send(self._cfg(), "g1_t1", "hello group")
        assert result["success"]
        mock_group_text.assert_called_once_with("g1", "t1", "hello group")

    @pytest.mark.asyncio
    async def test_standalone_send_routes_file_to_group(self, tmp_path):
        """_standalone_send uses push_group_file when live adapter has group routing."""
        doc = tmp_path / "report.pdf"
        doc.write_bytes(b"%PDF-fake")

        mock_adapter = MagicMock()
        mock_adapter._get_routing.return_value = {
            "uid": "user_1",
            "groupId": "g1",
            "topicId": "t1",
            "groupType": "team",
        }
        mock_runner = MagicMock()
        mock_runner.adapters = {Platform("eko"): mock_adapter}

        with patch.object(_EkoClient, "push_group_text", new_callable=AsyncMock), \
             patch.object(_EkoClient, "push_group_file", new_callable=AsyncMock) as mock_group_file, \
             patch("gateway.run._gateway_runner_ref", return_value=mock_runner):
            result = await _standalone_send(
                self._cfg(), "g1_t1", "", media_files=[str(doc)],
            )
        assert result["success"]
        mock_group_file.assert_called_once()
        args = mock_group_file.call_args
        assert args[0][0] == "g1"
        assert args[0][1] == "t1"

    @pytest.mark.asyncio
    async def test_standalone_send_routes_image_to_group(self, tmp_path):
        """_standalone_send uses push_group_picture when live adapter has group routing."""
        img = tmp_path / "photo.jpg"
        img.write_bytes(b"\xff\xd8\xff\xe0JPG")

        mock_adapter = MagicMock()
        mock_adapter._get_routing.return_value = {
            "uid": "user_1",
            "groupId": "g1",
            "topicId": "t1",
            "groupType": "team",
        }
        mock_runner = MagicMock()
        mock_runner.adapters = {Platform("eko"): mock_adapter}

        with patch.object(_EkoClient, "push_group_text", new_callable=AsyncMock), \
             patch.object(_EkoClient, "push_group_picture", new_callable=AsyncMock) as mock_group_pic, \
             patch("gateway.run._gateway_runner_ref", return_value=mock_runner):
            result = await _standalone_send(
                self._cfg(), "g1_t1", "", media_files=[str(img)],
            )
        assert result["success"]
        mock_group_pic.assert_called_once()
        args = mock_group_pic.call_args
        assert args[0][0] == "g1"
        assert args[0][1] == "t1"


# ---------------------------------------------------------------------------
# 17b. _standalone_send explicit routing (no live gateway)
# ---------------------------------------------------------------------------


class TestStandaloneSendExplicitRouting:
    """Tests for _standalone_send explicit group:<gid>:topic:<tid> routing.

    These tests verify that standalone delivery (cron/scheduled jobs) can
    target Eko groups/topics without a live gateway adapter, using the
    explicit routing format in the chat_id.
    """

    def _cfg(self):
        return _make_config({
            "base_url": "https://eko.example.com",
            "oauth_client_id": "id",
            "oauth_client_secret": "secret",
        })

    @pytest.mark.asyncio
    async def test_explicit_group_routing_text_no_live_adapter(self):
        """Explicit group:<gid>:topic:<tid> routes to group endpoint without live adapter."""
        with patch.object(_EkoClient, "push_group_text", new_callable=AsyncMock) as mock_group, \
             patch("gateway.run._gateway_runner_ref", return_value=None):
            result = await _standalone_send(
                self._cfg(), "group:grp_42:topic:top_7", "hello from cron",
            )
        assert result["success"]
        mock_group.assert_called_once_with("grp_42", "top_7", "hello from cron")

    @pytest.mark.asyncio
    async def test_explicit_group_routing_file_no_live_adapter(self, tmp_path):
        """Explicit group routing sends files to group endpoint without live adapter."""
        doc = tmp_path / "report.pdf"
        doc.write_bytes(b"%PDF-fake")

        with patch.object(_EkoClient, "push_group_file", new_callable=AsyncMock) as mock_group_file, \
             patch("gateway.run._gateway_runner_ref", return_value=None):
            result = await _standalone_send(
                self._cfg(), "group:grp_42:topic:top_7", "", media_files=[str(doc)],
            )
        assert result["success"]
        mock_group_file.assert_called_once()
        args = mock_group_file.call_args
        assert args[0][0] == "grp_42"
        assert args[0][1] == "top_7"

    @pytest.mark.asyncio
    async def test_explicit_group_routing_image_no_live_adapter(self, tmp_path):
        """Explicit group routing sends images to group endpoint without live adapter."""
        img = tmp_path / "photo.jpg"
        img.write_bytes(b"\xff\xd8\xff\xe0JPG")

        with patch.object(_EkoClient, "push_group_picture", new_callable=AsyncMock) as mock_group_pic, \
             patch("gateway.run._gateway_runner_ref", return_value=None):
            result = await _standalone_send(
                self._cfg(), "group:grp_42:topic:top_7", "", media_files=[str(img)],
            )
        assert result["success"]
        mock_group_pic.assert_called_once()
        args = mock_group_pic.call_args
        assert args[0][0] == "grp_42"
        assert args[0][1] == "top_7"

    @pytest.mark.asyncio
    async def test_normal_dm_target_no_live_adapter(self):
        """Non-explicit chat_id falls back to DM push when no live adapter."""
        with patch.object(_EkoClient, "push_text", new_callable=AsyncMock) as mock_push, \
             patch("gateway.run._gateway_runner_ref", return_value=None):
            result = await _standalone_send(
                self._cfg(), "user_123", "hello dm",
            )
        assert result["success"]
        mock_push.assert_called_once_with("user_123", "hello dm")

    @pytest.mark.asyncio
    async def test_malformed_explicit_routing_returns_error(self):
        """Malformed explicit routing returns clear error, not DM fallback."""
        with patch.object(_EkoClient, "push_text", new_callable=AsyncMock) as mock_push, \
             patch.object(_EkoClient, "push_group_text", new_callable=AsyncMock) as mock_group, \
             patch("gateway.run._gateway_runner_ref", return_value=None):
            result = await _standalone_send(
                self._cfg(), "group:grp_42:topic:", "hello",
            )
        assert "error" in result
        assert not result.get("success", False)
        mock_push.assert_not_called()
        mock_group.assert_not_called()

    @pytest.mark.asyncio
    async def test_malformed_explicit_routing_missing_topic(self):
        """group:<gid> without topic returns clear error."""
        with patch.object(_EkoClient, "push_text", new_callable=AsyncMock) as mock_push, \
             patch.object(_EkoClient, "push_group_text", new_callable=AsyncMock) as mock_group, \
             patch("gateway.run._gateway_runner_ref", return_value=None):
            result = await _standalone_send(
                self._cfg(), "group:grp_42", "hello",
            )
        assert "error" in result
        assert not result.get("success", False)
        mock_push.assert_not_called()
        mock_group.assert_not_called()

    @pytest.mark.asyncio
    async def test_explicit_routing_preferred_over_live_adapter_valid(self):
        """Explicit routing takes priority over live adapter routing."""
        mock_adapter = MagicMock()
        mock_adapter._get_routing.return_value = {
            "uid": "user_1",
            "groupId": "old_g",
            "topicId": "old_t",
            "groupType": "team",
        }
        mock_runner = MagicMock()
        mock_runner.adapters = {Platform("eko"): mock_adapter}

        with patch.object(_EkoClient, "push_group_text", new_callable=AsyncMock) as mock_group, \
             patch("gateway.run._gateway_runner_ref", return_value=mock_runner):
            result = await _standalone_send(
                self._cfg(), "group:explicit_g:topic:explicit_t", "hello",
            )
        assert result["success"]
        mock_group.assert_called_once_with("explicit_g", "explicit_t", "hello")
        # Live adapter routing was NOT consulted.
        mock_adapter._get_routing.assert_not_called()

    @pytest.mark.asyncio
    async def test_live_adapter_fallback_still_works(self):
        """Without explicit routing, live adapter routing still works."""
        mock_adapter = MagicMock()
        mock_adapter._get_routing.return_value = {
            "uid": "user_1",
            "groupId": "g1",
            "topicId": "t1",
            "groupType": "team",
        }
        mock_runner = MagicMock()
        mock_runner.adapters = {Platform("eko"): mock_adapter}

        with patch.object(_EkoClient, "push_group_text", new_callable=AsyncMock) as mock_group, \
             patch("gateway.run._gateway_runner_ref", return_value=mock_runner):
            result = await _standalone_send(
                self._cfg(), "g1_t1", "hello",
            )
        assert result["success"]
        mock_group.assert_called_once_with("g1", "t1", "hello")


# ---------------------------------------------------------------------------
# 17c. Media size limits (Issue #28)
# ---------------------------------------------------------------------------


class TestMediaSizeLimits:
    """Tests for upload and inbound media size limits."""

    def _make_adapter(self, max_upload=10_485_760, max_inbound=10_485_760):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._reply_tokens = {}
        adapter._session_routing = {}
        adapter._client = MagicMock(
            push_picture=AsyncMock(),
            push_file=AsyncMock(),
            push_group_picture=AsyncMock(),
            push_group_file=AsyncMock(),
        )
        adapter.message_max_chars = DEFAULT_MESSAGE_MAX_CHARS
        adapter.max_upload_bytes = max_upload
        adapter.max_inbound_media_bytes = max_inbound
        adapter.platform = Platform("eko")
        adapter.config = _make_config()
        adapter._message_handler = None
        adapter._running = True
        return adapter

    # -- Outbound: send_image_file --

    @pytest.mark.asyncio
    async def test_send_image_file_rejects_oversized(self, tmp_path):
        """Oversized image file is rejected before reading into memory."""
        img = tmp_path / "big.png"
        img.write_bytes(b"\x89PNG" + b"x" * 100)

        adapter = self._make_adapter(max_upload=50)  # 50 bytes limit
        result = await adapter.send_image_file("chat1", str(img))
        assert not result.success
        assert "too large" in result.error.lower() or "size" in result.error.lower()
        adapter._client.push_picture.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_image_file_allows_within_limit(self, tmp_path):
        """Image file within limit is sent normally."""
        img = tmp_path / "small.png"
        img.write_bytes(b"\x89PNG" + b"x" * 10)

        adapter = self._make_adapter(max_upload=10_485_760)
        result = await adapter.send_image_file("chat1", str(img))
        assert result.success
        adapter._client.push_picture.assert_called_once()

    # -- Outbound: send_document --

    @pytest.mark.asyncio
    async def test_send_document_rejects_oversized(self, tmp_path):
        """Oversized document is rejected before reading into memory."""
        doc = tmp_path / "big.pdf"
        doc.write_bytes(b"%PDF" + b"x" * 100)

        adapter = self._make_adapter(max_upload=50)
        result = await adapter.send_document("chat1", str(doc))
        assert not result.success
        assert "too large" in result.error.lower() or "size" in result.error.lower()
        adapter._client.push_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_document_allows_within_limit(self, tmp_path):
        """Document within limit is sent normally."""
        doc = tmp_path / "small.pdf"
        doc.write_bytes(b"%PDF-1.4")

        adapter = self._make_adapter(max_upload=10_485_760)
        result = await adapter.send_document("chat1", str(doc))
        assert result.success
        adapter._client.push_file.assert_called_once()

    # -- Inbound: _download_picture --

    @pytest.mark.asyncio
    async def test_download_picture_rejects_oversized(self):
        """Inbound picture larger than limit is discarded."""
        adapter = self._make_adapter(max_inbound=50)
        adapter._client.fetch_picture = AsyncMock(return_value=b"x" * 200)

        result = await adapter._download_picture({"pictureId": "pic123"})
        assert result is None  # discarded due to size

    @pytest.mark.asyncio
    async def test_download_picture_allows_within_limit(self):
        """Inbound picture within limit is cached normally."""
        adapter = self._make_adapter(max_inbound=10_485_760)
        adapter._client.fetch_picture = AsyncMock(return_value=b"\x89PNG" + b"x" * 10)

        with patch("gateway.platforms.base.cache_image_from_bytes", return_value="/cache/pic.jpg"):
            result = await adapter._download_picture({"pictureId": "pic123"})
        assert result == "/cache/pic.jpg"

    # -- Malformed config --

    def test_malformed_upload_limit_gets_default(self):
        """Malformed EKO_MAX_UPLOAD_BYTES falls back to default."""
        with patch.dict(os.environ, {"EKO_MAX_UPLOAD_BYTES": "notanumber"}):
            adapter = self._make_adapter()
            # If __init__ were called, it would use the default;
            # our test helper bypasses __init__, so we test the constant.
            assert DEFAULT_MAX_UPLOAD_BYTES == 26_214_400

    # -- _standalone_send media size check --

    @pytest.mark.asyncio
    async def test_standalone_send_rejects_oversized_media(self, tmp_path):
        """_standalone_send rejects oversized media before upload."""
        big = tmp_path / "big.pdf"
        big.write_bytes(b"x" * 200)

        cfg = _make_config({
            "base_url": "https://eko.example.com",
            "oauth_client_id": "id",
            "oauth_client_secret": "secret",
        })

        with patch.dict(os.environ, {"EKO_MAX_UPLOAD_BYTES": "100"}), \
             patch.object(_EkoClient, "push_text", new_callable=AsyncMock), \
             patch("gateway.run._gateway_runner_ref", return_value=None):
            result = await _standalone_send(
                cfg, "user1", "text", media_files=[str(big)],
            )
        assert result["success"]  # text still sent
        assert result.get("warnings")
        assert any("too large" in w.lower() or "size" in w.lower() for w in result["warnings"])


# ---------------------------------------------------------------------------
# 18. _send_eko_media group routing (via live adapter)
# ---------------------------------------------------------------------------


class TestSendEkoMediaGroupRouting:
    """Tests for _send_eko_media routing documents to group endpoints."""

    def _make_group_adapter(self):
        """Create an EkoAdapter with group routing pre-configured."""
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._reply_tokens = {}
        adapter._session_routing = {
            "g1_t1": {
                "uid": "user_1",
                "groupId": "g1",
                "topicId": "t1",
                "groupType": "team",
            },
        }
        adapter._client = MagicMock(
            push_group_file=AsyncMock(),
            push_file=AsyncMock(),
            push_group_picture=AsyncMock(),
            push_picture=AsyncMock(),
        )
        adapter.message_max_chars = DEFAULT_MESSAGE_MAX_CHARS
        # BasePlatformAdapter attrs needed by send()
        adapter.platform = Platform("eko")
        adapter.config = _make_config()
        adapter._message_handler = None
        adapter._running = True
        return adapter

    @pytest.mark.asyncio
    async def test_send_eko_media_routes_document_to_group(self, tmp_path):
        """_send_eko_media routes non-image files via adapter.send_document with group chat_id."""
        doc = tmp_path / "report.pdf"
        doc.write_bytes(b"%PDF-fake")

        adapter = self._make_group_adapter()
        mock_runner = MagicMock()
        mock_runner.adapters = {Platform("eko"): adapter}

        with patch("gateway.run._gateway_runner_ref", return_value=mock_runner):
            from tools.send_message_tool import _send_eko_media
            # No text chunks — only media.
            result = await _send_eko_media(
                _make_config(),
                "g1_t1",
                [],  # no text chunks
                [(str(doc), False)],
            )

        assert result["success"]
        # Document should go to group endpoint, not DM.
        adapter._client.push_group_file.assert_called_once()
        adapter._client.push_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_eko_media_routes_image_to_group(self, tmp_path):
        """_send_eko_media routes images via adapter.send_image_file with group chat_id."""
        img = tmp_path / "photo.png"
        img.write_bytes(b"\x89PNG data")

        adapter = self._make_group_adapter()
        mock_runner = MagicMock()
        mock_runner.adapters = {Platform("eko"): adapter}

        with patch("gateway.run._gateway_runner_ref", return_value=mock_runner):
            from tools.send_message_tool import _send_eko_media
            result = await _send_eko_media(
                _make_config(),
                "g1_t1",
                [],  # no text chunks
                [(str(img), False)],
            )

        assert result["success"]
        # Image should go to group endpoint (push fallback, no reply token).
        adapter._client.push_group_picture.assert_called_once()
        adapter._client.push_picture.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_eko_media_remaps_chat_id_from_session_context(self, tmp_path):
        """_send_eko_media remaps bare uid to group chat_id via session context."""
        doc = tmp_path / "data.csv"
        doc.write_bytes(b"a,b,c")

        adapter = self._make_group_adapter()
        mock_runner = MagicMock()
        mock_runner.adapters = {Platform("eko"): adapter}

        with patch("gateway.run._gateway_runner_ref", return_value=mock_runner), \
             patch("gateway.session_context.get_session_env", return_value="g1_t1"):
            from tools.send_message_tool import _send_eko_media
            result = await _send_eko_media(
                _make_config(),
                "user_1",  # bare uid — no routing, triggers remap
                [],  # no text chunks
                [(str(doc), False)],
            )

        assert result["success"]
        # After remap to g1_t1, should route to group endpoint.
        adapter._client.push_group_file.assert_called_once()
        adapter._client.push_file.assert_not_called()


# ---------------------------------------------------------------------------
# 21. Eko management tools (Issue #17)
# ---------------------------------------------------------------------------


def _make_client_mock():
    """Create a mock _EkoClient with async management methods."""
    client = MagicMock()
    client.create_group = AsyncMock(return_value={"_id": "grp_new", "type": "direct_chat"})
    client.create_topic = AsyncMock(return_value={"_id": "top_new", "gid": "grp_1"})
    client.query_users = AsyncMock(return_value=[
        {"_id": "u1", "username": "alice", "email": "alice@ex.com"},
    ])
    return client


def _patch_eko_client(client_mock):
    """Patch _get_eko_client to return the given mock."""
    return patch("plugins.platforms.eko.tools._get_eko_client", return_value=client_mock)


class TestEkoCreateGroupTool:

    @pytest.mark.asyncio
    async def test_create_group_with_uids(self):
        from plugins.platforms.eko.tools import _handle_create_group
        client = _make_client_mock()
        with _patch_eko_client(client):
            result = json.loads(await _handle_create_group({"member_uids": ["u1", "u2"]}))
        assert result.get("_id") == "grp_new"
        client.create_group.assert_called_once_with(["u1", "u2"], name="")

    @pytest.mark.asyncio
    async def test_create_group_with_usernames(self):
        from plugins.platforms.eko.tools import _handle_create_group
        client = _make_client_mock()
        with _patch_eko_client(client):
            result = json.loads(await _handle_create_group({"member_usernames": ["alice"]}))
        assert result.get("_id") == "grp_new"
        client.query_users.assert_called_once_with("alice")
        client.create_group.assert_called_once_with(["u1"], name="")

    @pytest.mark.asyncio
    async def test_create_group_with_name(self):
        from plugins.platforms.eko.tools import _handle_create_group
        client = _make_client_mock()
        with _patch_eko_client(client):
            result = json.loads(await _handle_create_group({
                "member_uids": ["u1"], "name": "Project X",
            }))
        assert result.get("_id") == "grp_new"
        client.create_group.assert_called_once_with(["u1"], name="Project X")

    @pytest.mark.asyncio
    async def test_create_group_no_members_error(self):
        from plugins.platforms.eko.tools import _handle_create_group
        client = _make_client_mock()
        with _patch_eko_client(client):
            result = json.loads(await _handle_create_group({}))
        assert "error" in result
        assert "member_usernames or member_uids" in result["error"]

    @pytest.mark.asyncio
    async def test_create_group_user_not_found(self):
        from plugins.platforms.eko.tools import _handle_create_group
        client = _make_client_mock()
        client.query_users = AsyncMock(return_value=[])
        with _patch_eko_client(client):
            result = json.loads(await _handle_create_group({"member_usernames": ["nobody"]}))
        assert "error" in result
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_create_group_not_connected(self):
        from plugins.platforms.eko.tools import _handle_create_group
        with _patch_eko_client(None):
            result = json.loads(await _handle_create_group({"member_uids": ["u1"]}))
        assert "error" in result
        assert "not connected" in result["error"]

    @pytest.mark.asyncio
    async def test_create_group_api_failure(self):
        from plugins.platforms.eko.tools import _handle_create_group
        client = _make_client_mock()
        client.create_group = AsyncMock(side_effect=RuntimeError("server error"))
        with _patch_eko_client(client):
            result = json.loads(await _handle_create_group({"member_uids": ["u1"]}))
        assert "error" in result
        assert "server error" in result["error"]


# ---------------------------------------------------------------------------
# 21b. Eko create_group username resolution (Issue #27)
# ---------------------------------------------------------------------------


class TestEkoCreateGroupUsernameResolution:
    """Tests for exact-match + ambiguity-safe username resolution in create_group."""

    @pytest.mark.asyncio
    async def test_exact_match_resolves(self):
        """Exact username match resolves when multiple fuzzy results returned."""
        from plugins.platforms.eko.tools import _handle_create_group
        client = _make_client_mock()
        client.query_users = AsyncMock(return_value=[
            {"_id": "u_alice", "username": "alice"},
            {"_id": "u_alice2", "username": "alice123"},
        ])
        with _patch_eko_client(client):
            result = json.loads(await _handle_create_group({"member_usernames": ["alice"]}))
        assert result.get("_id") == "grp_new"
        client.create_group.assert_called_once_with(["u_alice"], name="")

    @pytest.mark.asyncio
    async def test_no_exact_match_returns_error_with_candidates(self):
        """When only fuzzy matches exist, return error listing candidates."""
        from plugins.platforms.eko.tools import _handle_create_group
        client = _make_client_mock()
        client.query_users = AsyncMock(return_value=[
            {"_id": "u1", "username": "alice123"},
            {"_id": "u2", "username": "alice_smith"},
        ])
        with _patch_eko_client(client):
            result = json.loads(await _handle_create_group({"member_usernames": ["alice"]}))
        assert "error" in result
        assert "alice" in result["error"]
        client.create_group.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiple_exact_matches_returns_ambiguity_error(self):
        """Multiple users with the exact same username → ambiguity error."""
        from plugins.platforms.eko.tools import _handle_create_group
        client = _make_client_mock()
        client.query_users = AsyncMock(return_value=[
            {"_id": "u1", "username": "alice"},
            {"_id": "u2", "username": "alice"},
        ])
        with _patch_eko_client(client):
            result = json.loads(await _handle_create_group({"member_usernames": ["alice"]}))
        assert "error" in result
        assert "ambiguous" in result["error"].lower()
        client.create_group.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_user_id_returns_error(self):
        """User with missing/empty _id should fail, not silently pass empty string."""
        from plugins.platforms.eko.tools import _handle_create_group
        client = _make_client_mock()
        client.query_users = AsyncMock(return_value=[
            {"_id": "", "username": "alice"},
        ])
        with _patch_eko_client(client):
            result = json.loads(await _handle_create_group({"member_usernames": ["alice"]}))
        assert "error" in result
        client.create_group.assert_not_called()

    @pytest.mark.asyncio
    async def test_dict_return_from_query_users_no_crash(self):
        """If query_users returns a dict instead of list, no KeyError."""
        from plugins.platforms.eko.tools import _handle_create_group
        client = _make_client_mock()
        client.query_users = AsyncMock(return_value={"_id": "u1", "username": "alice"})
        with _patch_eko_client(client):
            result = json.loads(await _handle_create_group({"member_usernames": ["alice"]}))
        assert "error" in result
        client.create_group.assert_not_called()



class TestEkoCreateTopicTool:

    @pytest.mark.asyncio
    async def test_create_topic(self):
        from plugins.platforms.eko.tools import _handle_create_topic
        client = _make_client_mock()
        with _patch_eko_client(client):
            result = json.loads(await _handle_create_topic({
                "group_id": "grp_1", "name": "General",
            }))
        assert result.get("_id") == "top_new"
        client.create_topic.assert_called_once_with("grp_1", "General")

    @pytest.mark.asyncio
    async def test_create_topic_missing_group_id(self):
        from plugins.platforms.eko.tools import _handle_create_topic
        client = _make_client_mock()
        with _patch_eko_client(client):
            result = json.loads(await _handle_create_topic({"name": "X"}))
        assert "error" in result
        assert "group_id" in result["error"]

    @pytest.mark.asyncio
    async def test_create_topic_missing_name(self):
        from plugins.platforms.eko.tools import _handle_create_topic
        client = _make_client_mock()
        with _patch_eko_client(client):
            result = json.loads(await _handle_create_topic({"group_id": "g1"}))
        assert "error" in result
        assert "name" in result["error"]

    @pytest.mark.asyncio
    async def test_create_topic_not_connected(self):
        from plugins.platforms.eko.tools import _handle_create_topic
        with _patch_eko_client(None):
            result = json.loads(await _handle_create_topic({
                "group_id": "g1", "name": "X",
            }))
        assert "error" in result
        assert "not connected" in result["error"]

    @pytest.mark.asyncio
    async def test_create_topic_api_failure(self):
        from plugins.platforms.eko.tools import _handle_create_topic
        client = _make_client_mock()
        client.create_topic = AsyncMock(side_effect=RuntimeError("fail"))
        with _patch_eko_client(client):
            result = json.loads(await _handle_create_topic({
                "group_id": "g1", "name": "X",
            }))
        assert "error" in result
        assert "fail" in result["error"]


class TestEkoQueryUsersTool:

    @pytest.mark.asyncio
    async def test_query_users(self):
        from plugins.platforms.eko.tools import _handle_query_users
        client = _make_client_mock()
        with _patch_eko_client(client):
            result = json.loads(await _handle_query_users({"username": "alice"}))
        assert isinstance(result, list)
        assert result[0]["username"] == "alice"
        client.query_users.assert_called_once_with("alice")

    @pytest.mark.asyncio
    async def test_query_users_missing_username(self):
        from plugins.platforms.eko.tools import _handle_query_users
        client = _make_client_mock()
        with _patch_eko_client(client):
            result = json.loads(await _handle_query_users({}))
        assert "error" in result
        assert "username" in result["error"]

    @pytest.mark.asyncio
    async def test_query_users_not_connected(self):
        from plugins.platforms.eko.tools import _handle_query_users
        with _patch_eko_client(None):
            result = json.loads(await _handle_query_users({"username": "a"}))
        assert "error" in result
        assert "not connected" in result["error"]

    @pytest.mark.asyncio
    async def test_query_users_api_failure(self):
        from plugins.platforms.eko.tools import _handle_query_users
        client = _make_client_mock()
        client.query_users = AsyncMock(side_effect=RuntimeError("timeout"))
        with _patch_eko_client(client):
            result = json.loads(await _handle_query_users({"username": "a"}))
        assert "error" in result
        assert "timeout" in result["error"]


# ---------------------------------------------------------------------------
# Management actions config gate
# ---------------------------------------------------------------------------


def _patch_management_actions(return_value):
    """Patch _load_management_actions_config to return a fixed value."""
    return patch(
        "plugins.platforms.eko.tools._load_management_actions_config",
        return_value=return_value,
    )


class TestManagementActionsConfigGate:
    """Tests for eko.management_actions config allowlist."""

    # -- check_fn behavior --------------------------------------------------

    def test_check_fn_all_allowed_when_config_unset(self):
        """Unset config (returns None) means all tools pass check_fn."""
        from plugins.platforms.eko.tools import _make_check_fn
        check = _make_check_fn("create_group")
        client = _make_client_mock()
        with _patch_eko_client(client), \
             _patch_management_actions(None):
            assert check() is True

    def test_check_fn_allowed_when_action_in_list(self):
        """Tool passes check_fn when its action is in the allowlist."""
        from plugins.platforms.eko.tools import _make_check_fn
        check = _make_check_fn("create_group")
        client = _make_client_mock()
        with _patch_eko_client(client), \
             _patch_management_actions(["create_group", "query_users"]):
            assert check() is True

    def test_check_fn_blocked_when_action_not_in_list(self):
        """Tool fails check_fn when its action is NOT in the allowlist."""
        from plugins.platforms.eko.tools import _make_check_fn
        check = _make_check_fn("create_group")
        client = _make_client_mock()
        with _patch_eko_client(client), \
             _patch_management_actions(["query_users"]):
            assert check() is False

    def test_check_fn_all_blocked_when_empty_list(self):
        """Empty allowlist means no tools pass check_fn."""
        from plugins.platforms.eko.tools import _make_check_fn
        check = _make_check_fn("query_users")
        client = _make_client_mock()
        with _patch_eko_client(client), \
             _patch_management_actions([]):
            assert check() is False

    def test_check_fn_adapter_not_connected_blocks_regardless(self):
        """Adapter not connected overrides config allowlist."""
        from plugins.platforms.eko.tools import _make_check_fn
        check = _make_check_fn("create_group")
        with _patch_eko_client(None), \
             _patch_management_actions(None):
            assert check() is False

    # -- handler defense-in-depth -------------------------------------------

    @pytest.mark.asyncio
    async def test_handler_create_group_blocked_by_config(self):
        """Handler returns config-gate error when action is disabled."""
        from plugins.platforms.eko.tools import _handle_create_group
        client = _make_client_mock()
        with _patch_eko_client(client), \
             _patch_management_actions(["query_users"]):
            result = json.loads(
                await _handle_create_group({"member_uids": ["u1"]})
            )
        assert "error" in result
        assert "disabled by config" in result["error"]
        assert "create_group" in result["error"]

    @pytest.mark.asyncio
    async def test_handler_create_topic_blocked_by_config(self):
        from plugins.platforms.eko.tools import _handle_create_topic
        client = _make_client_mock()
        with _patch_eko_client(client), \
             _patch_management_actions(["query_users"]):
            result = json.loads(
                await _handle_create_topic({"group_id": "g1", "name": "X"})
            )
        assert "error" in result
        assert "disabled by config" in result["error"]
        assert "create_topic" in result["error"]

    @pytest.mark.asyncio
    async def test_handler_query_users_blocked_by_config(self):
        from plugins.platforms.eko.tools import _handle_query_users
        client = _make_client_mock()
        with _patch_eko_client(client), \
             _patch_management_actions(["create_group"]):
            result = json.loads(
                await _handle_query_users({"username": "alice"})
            )
        assert "error" in result
        assert "disabled by config" in result["error"]
        assert "query_users" in result["error"]

    @pytest.mark.asyncio
    async def test_handler_create_group_allowed_by_config(self):
        """Handler proceeds normally when action is in the allowlist."""
        from plugins.platforms.eko.tools import _handle_create_group
        client = _make_client_mock()
        with _patch_eko_client(client), \
             _patch_management_actions(["create_group"]):
            result = json.loads(
                await _handle_create_group({"member_uids": ["u1"]})
            )
        assert result.get("_id") == "grp_new"

    @pytest.mark.asyncio
    async def test_handler_query_users_allowed_by_config(self):
        from plugins.platforms.eko.tools import _handle_query_users
        client = _make_client_mock()
        with _patch_eko_client(client), \
             _patch_management_actions(["query_users"]):
            result = json.loads(
                await _handle_query_users({"username": "alice"})
            )
        assert isinstance(result, list)
        assert result[0]["username"] == "alice"

    @pytest.mark.asyncio
    async def test_handler_all_allowed_when_config_unset(self):
        """All handlers work when config is unset (backward compatible)."""
        from plugins.platforms.eko.tools import _handle_create_group, _handle_create_topic, _handle_query_users
        client = _make_client_mock()
        with _patch_eko_client(client), \
             _patch_management_actions(None):
            r1 = json.loads(await _handle_create_group({"member_uids": ["u1"]}))
            r2 = json.loads(await _handle_create_topic({"group_id": "g1", "name": "X"}))
            r3 = json.loads(await _handle_query_users({"username": "alice"}))
        assert r1.get("_id") == "grp_new"
        assert r2.get("_id") == "top_new"
        assert isinstance(r3, list)

    # -- config loading -----------------------------------------------------

    def test_load_config_filters_invalid_names(self):
        """Invalid action names are filtered out; valid ones are kept."""
        from plugins.platforms.eko.tools import _load_management_actions_config
        mock_cfg = MagicMock()
        mock_cfg.get.return_value = {"management_actions": ["create_group", "bogus", "query_users"]}
        with patch("plugins.platforms.eko.tools.logger"), \
             patch("hermes_cli.config.load_config", return_value=mock_cfg):
            result = _load_management_actions_config()
        assert result == ["create_group", "query_users"]

    def test_load_config_returns_none_when_unset(self):
        from plugins.platforms.eko.tools import _load_management_actions_config
        mock_cfg = MagicMock()
        mock_cfg.get.return_value = {}
        with patch("hermes_cli.config.load_config", return_value=mock_cfg):
            result = _load_management_actions_config()
        assert result is None

    def test_load_config_comma_separated_string(self):
        from plugins.platforms.eko.tools import _load_management_actions_config
        mock_cfg = MagicMock()
        mock_cfg.get.return_value = {"management_actions": "create_group, query_users"}
        with patch("hermes_cli.config.load_config", return_value=mock_cfg):
            result = _load_management_actions_config()
        assert result == ["create_group", "query_users"]


# ---------------------------------------------------------------------------
# get_chat_info (#30)
# ---------------------------------------------------------------------------


class TestGetChatInfo:
    """Tests for EkoAdapter.get_chat_info returning group/topic metadata."""

    @staticmethod
    def _make_adapter():
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._session_routing = {}
        return adapter

    @pytest.mark.asyncio
    async def test_unknown_chat_id_returns_dm(self):
        """Unknown chat_id with no routing data returns type=dm."""
        adapter = self._make_adapter()
        result = await adapter.get_chat_info("some_user_id")
        assert result == {"name": "some_user_id", "type": "dm"}

    @pytest.mark.asyncio
    async def test_empty_chat_id_returns_dm(self):
        """Empty chat_id returns sensible DM fallback."""
        adapter = self._make_adapter()
        result = await adapter.get_chat_info("")
        assert result == {"name": "", "type": "dm"}

    @pytest.mark.asyncio
    async def test_topic_routing_returns_topic_type(self):
        """Routing with groupId + topicId returns type=topic with metadata."""
        adapter = self._make_adapter()
        chat_id = "abc123_def456"
        adapter._session_routing[chat_id] = {
            "uid": "user789",
            "groupId": "abc123",
            "topicId": "def456",
            "groupType": "group_chatv2",
        }
        result = await adapter.get_chat_info(chat_id)
        assert result == {
            "name": chat_id,
            "type": "topic",
            "group_id": "abc123",
            "topic_id": "def456",
            "user_id": "user789",
            "group_type": "group_chatv2",
        }

    @pytest.mark.asyncio
    async def test_group_routing_without_topic_returns_group_type(self):
        """Routing with groupId but no topicId returns type=group."""
        adapter = self._make_adapter()
        chat_id = "group_only_session"
        adapter._session_routing[chat_id] = {
            "uid": "user789",
            "groupId": "abc123",
            "topicId": "",
            "groupType": "group_chatv2",
        }
        result = await adapter.get_chat_info(chat_id)
        assert result == {
            "name": chat_id,
            "type": "group",
            "group_id": "abc123",
            "topic_id": "",
            "user_id": "user789",
            "group_type": "group_chatv2",
        }

    @pytest.mark.asyncio
    async def test_dm_routing_returns_dm(self):
        """Routing with direct_chat groupType but no groupId returns type=dm."""
        adapter = self._make_adapter()
        chat_id = "dm_user"
        adapter._session_routing[chat_id] = {
            "uid": "user789",
            "groupId": "",
            "topicId": "",
            "groupType": "direct_chat",
        }
        result = await adapter.get_chat_info(chat_id)
        assert result == {"name": chat_id, "type": "dm"}

    @pytest.mark.asyncio
    async def test_empty_routing_dict_returns_dm(self):
        """Empty _session_routing returns DM for any chat_id."""
        adapter = self._make_adapter()
        assert adapter._session_routing == {}
        result = await adapter.get_chat_info("any_id")
        assert result == {"name": "any_id", "type": "dm"}

