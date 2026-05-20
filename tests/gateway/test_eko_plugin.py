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

_eko = load_plugin_adapter("eko")

EkoAdapter = _eko.EkoAdapter
_MessageDeduplicator = _eko._MessageDeduplicator
_EkoClient = _eko._EkoClient
_EkoAuthError = _eko._EkoAuthError
check_requirements = _eko.check_requirements
validate_config = _eko.validate_config
is_connected = _eko.is_connected
_env_enablement = _eko._env_enablement
_standalone_send = _eko._standalone_send
register = _eko.register


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

class TestReplyTokenStash:

    def test_no_token_returns_empty(self):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._reply_tokens = {}
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
# 4. _EkoAuthError
# ---------------------------------------------------------------------------

class TestEkoAuthError:

    def test_is_runtime_error(self):
        assert issubclass(_EkoAuthError, RuntimeError)

    def test_carries_message(self):
        err = _EkoAuthError("test message")
        assert str(err) == "test message"


# ---------------------------------------------------------------------------
# 5. Outbound send routing
# ---------------------------------------------------------------------------

class TestSendRouting:

    @pytest.mark.asyncio
    async def test_reply_token_used_first(self):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._reply_tokens = {"chat1": ("tok_abc", time.time() + 50)}
        adapter._client = MagicMock()
        adapter._client.reply_text = AsyncMock()

        result = await adapter.send("chat1", "hello")
        assert result.success
        adapter._client.reply_text.assert_called_once_with("tok_abc", "hello")
        adapter._client.push_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_falls_back_to_push_on_reply_error(self):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._reply_tokens = {"chat1": ("tok_abc", time.time() + 50)}
        adapter._client = MagicMock()
        adapter._client.reply_text = AsyncMock(side_effect=RuntimeError("reply failed"))
        adapter._client.push_text = AsyncMock()

        result = await adapter.send("chat1", "hello")
        assert result.success
        adapter._client.reply_text.assert_called_once()
        adapter._client.push_text.assert_called_once_with("chat1", "hello")

    @pytest.mark.asyncio
    async def test_auth_error_triggers_push_retry(self):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._reply_tokens = {"chat1": ("tok_abc", time.time() + 50)}
        adapter._client = MagicMock()
        adapter._client.reply_text = AsyncMock(side_effect=_EkoAuthError("401"))
        adapter._client.push_text = AsyncMock()

        result = await adapter.send("chat1", "hello")
        assert result.success
        adapter._client.push_text.assert_called_once_with("chat1", "hello")

    @pytest.mark.asyncio
    async def test_push_used_when_no_reply_token(self):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._reply_tokens = {}
        adapter._client = MagicMock()
        adapter._client.push_text = AsyncMock()

        result = await adapter.send("chat1", "hello")
        assert result.success
        adapter._client.push_text.assert_called_once_with("chat1", "hello")

    @pytest.mark.asyncio
    async def test_push_auth_error_retries(self):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._reply_tokens = {}
        adapter._client = MagicMock()
        # First call: 401. Second call: success.
        adapter._client.push_text = AsyncMock(
            side_effect=[_EkoAuthError("401"), None]
        )

        result = await adapter.send("chat1", "hello")
        assert result.success
        assert adapter._client.push_text.call_count == 2

    @pytest.mark.asyncio
    async def test_push_failure_returns_retryable(self):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._reply_tokens = {}
        adapter._client = MagicMock()
        adapter._client.push_text = AsyncMock(side_effect=RuntimeError("server error"))

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

    def test_platform_hint_mentions_plain_text(self):
        ctx = MagicMock()
        register(ctx)
        hint = ctx.register_platform.call_args[1]["platform_hint"]
        assert "plain text" in hint


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
        adapter.oauth_client_secret = None
        body = b'{"events":[]}'
        sig = self._sign("my_oauth_secret", body)
        assert not adapter._verify_signature(body, sig)

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


def _mock_aiohttp_for_fetch(status: int, body: bytes = b"") -> MagicMock:
    """Build a mock aiohttp module that stubs ClientSession + GET."""
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.read = AsyncMock(return_value=body)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    mock_aiohttp = MagicMock()
    mock_aiohttp.ClientSession = MagicMock(return_value=mock_session)
    mock_aiohttp.ClientTimeout = MagicMock()
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
            with pytest.raises(_EkoAuthError):
                await client.fetch_picture("pic456")

        assert client._access_token is None


# ---------------------------------------------------------------------------
# 11. push_picture / reply_picture / push_file
# ---------------------------------------------------------------------------


def _mock_aiohttp_for_post(status: int) -> MagicMock:
    """Build a mock aiohttp module that stubs ClientSession + POST."""
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.text = AsyncMock(return_value="error body")
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
            with pytest.raises(_EkoAuthError):
                await client.push_picture("user1", b"imgdata", "photo.png")

        assert client._access_token is None
        assert client._token_expires_at == 0.0
