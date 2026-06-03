"""Tests for EkoConfig — the Eko configuration module.

Each test exercises one behaviour through the public EkoConfig interface.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clear_eko_env():
    """Remove all EKO_ env vars (for isolated tests)."""
    for key in list(os.environ):
        if key.startswith("EKO_"):
            os.environ.pop(key, None)


# ---------------------------------------------------------------------------
# Test #1 — required creds from env
# ---------------------------------------------------------------------------


def test_required_credentials_from_env():
    """base_url, oauth_client_id, oauth_client_secret read from env vars."""
    from plugins.platforms.eko.config import EkoConfig

    env = {
        "EKO_BASE_URL": "https://example.ekoapp.com",
        "EKO_OAUTH_CLIENT_ID": "my-client-id",
        "EKO_OAUTH_CLIENT_SECRET": "my-secret",
    }
    with patch.dict(os.environ, env, clear=False):
        cfg = EkoConfig.from_env()

    assert cfg.base_url == "https://example.ekoapp.com"
    assert cfg.oauth_client_id == "my-client-id"
    assert cfg.oauth_client_secret == "my-secret"
    assert cfg.has_credentials() is True


# ---------------------------------------------------------------------------
# Test #2 — missing creds → has_credentials() False
# ---------------------------------------------------------------------------


def test_missing_credentials_has_credentials_false():
    """has_credentials() returns False when any required field is empty."""
    from plugins.platforms.eko.config import EkoConfig

    with patch.dict(os.environ, {}):
        _clear_eko_env()
        cfg = EkoConfig.from_env()

    assert cfg.has_credentials() is False

    # Partially set: base_url only
    with patch.dict(os.environ, {"EKO_BASE_URL": "https://example.com"}):
        _clear_eko_env()
        os.environ["EKO_BASE_URL"] = "https://example.com"
        cfg = EkoConfig.from_env()

    assert cfg.has_credentials() is False


# ---------------------------------------------------------------------------
# Test #3 — webhook defaults when env unset
# ---------------------------------------------------------------------------


def test_webhook_defaults():
    """Port, host, path, require_signature use defaults when env unset."""
    from plugins.platforms.eko.config import (
        DEFAULT_WEBHOOK_PORT,
        DEFAULT_WEBHOOK_PATH,
        EkoConfig,
    )

    with patch.dict(os.environ, {}):
        _clear_eko_env()
        cfg = EkoConfig.from_env()

    assert cfg.webhook_host == "0.0.0.0"
    assert cfg.webhook_port == DEFAULT_WEBHOOK_PORT
    assert cfg.webhook_path == DEFAULT_WEBHOOK_PATH
    assert cfg.require_signature is True


# ---------------------------------------------------------------------------
# Test #4 — numeric parsing with bad values falls back to defaults
# ---------------------------------------------------------------------------


def test_malformed_numeric_values_fallback():
    """Malformed int env vars fall back to hardcoded defaults."""
    from plugins.platforms.eko.config import (
        DEFAULT_WEBHOOK_PORT,
        DEFAULT_MESSAGE_MAX_CHARS,
        EkoConfig,
    )

    with patch.dict(
        os.environ,
        {
            "EKO_PORT": "not-a-number",
            "EKO_MESSAGE_MAX_CHARS": "abc",
            "EKO_MAX_UPLOAD_BYTES": "",
        },
    ):
        cfg = EkoConfig.from_env()

    assert cfg.webhook_port == DEFAULT_WEBHOOK_PORT
    assert cfg.message_max_chars == DEFAULT_MESSAGE_MAX_CHARS


# ---------------------------------------------------------------------------
# Test #5 — user allowlist
# ---------------------------------------------------------------------------


def test_user_allowlist():
    """is_user_allowed respects the set and allow_all_users flag."""
    from plugins.platforms.eko.config import EkoConfig

    # allow_all_users=False, explicit list
    with patch.dict(
        os.environ,
        {
            "EKO_ALLOW_ALL_USERS": "false",
            "EKO_ALLOWED_USERS": "alice,bob",
        },
    ):
        cfg = EkoConfig.from_env()

    assert cfg.is_user_allowed("alice") is True
    assert cfg.is_user_allowed("bob") is True
    assert cfg.is_user_allowed("eve") is False

    # allow_all_users=True → everyone allowed
    with patch.dict(os.environ, {"EKO_ALLOW_ALL_USERS": "true"}):
        cfg = EkoConfig.from_env()

    assert cfg.is_user_allowed("anyone") is True


# ---------------------------------------------------------------------------
# Test #6 — group allowlist
# ---------------------------------------------------------------------------


def test_group_allowlist():
    """is_group_allowed respects the set and allow_all_groups flag."""
    from plugins.platforms.eko.config import EkoConfig

    # Default: allow_all_groups=True
    with patch.dict(os.environ, {}):
        _clear_eko_env()
        cfg = EkoConfig.from_env()

    assert cfg.is_group_allowed("any-group") is True

    # Explicit allowlist
    with patch.dict(
        os.environ,
        {
            "EKO_ALLOW_ALL_GROUPS": "false",
            "EKO_ALLOWED_GROUPS": "grp1,grp2",
        },
    ):
        cfg = EkoConfig.from_env()

    assert cfg.is_group_allowed("grp1") is True
    assert cfg.is_group_allowed("grp3") is False


# ---------------------------------------------------------------------------
# Test #7 — topic allowlist (gid:tid format)
# ---------------------------------------------------------------------------


def test_topic_allowlist():
    """is_topic_allowed uses gid:tid format in the allowlist."""
    from plugins.platforms.eko.config import EkoConfig

    with patch.dict(
        os.environ,
        {
            "EKO_ALLOW_ALL_GROUPS": "false",
            "EKO_ALLOWED_TOPICS": "grp1:topic1,grp1:topic2",
        },
    ):
        cfg = EkoConfig.from_env()

    assert cfg.is_topic_allowed("grp1", "topic1") is True
    assert cfg.is_topic_allowed("grp1", "topic2") is True
    assert cfg.is_topic_allowed("grp1", "topic3") is False
    assert cfg.is_topic_allowed("grp2", "topic1") is False

    # allow_all_groups=True → topics always allowed
    with patch.dict(os.environ, {"EKO_ALLOW_ALL_GROUPS": "true"}):
        cfg = EkoConfig.from_env()

    assert cfg.is_topic_allowed("any", "any") is True


def test_topic_in_allowed_group_is_allowed():
    """Regression: topic messages accepted when the group is in allowed_groups.

    EKO_ALLOWED_GROUPS=g1 should allow all topics under g1, even if no
    gid:tid entries appear in EKO_ALLOWED_TOPICS.
    """
    from plugins.platforms.eko.config import EkoConfig

    with patch.dict(
        os.environ,
        {
            "EKO_ALLOW_ALL_GROUPS": "false",
            "EKO_ALLOWED_GROUPS": "g1",
            "EKO_ALLOWED_TOPICS": "",
        },
    ):
        cfg = EkoConfig.from_env()

    assert cfg.is_topic_allowed("g1", "t1") is True
    assert cfg.is_topic_allowed("g1", "any-topic") is True
    assert cfg.is_topic_allowed("g2", "t1") is False


# ---------------------------------------------------------------------------
# Test #8 — mention triggers default to ["Hermes Agent"]
# ---------------------------------------------------------------------------


def test_mention_triggers_default():
    """Empty env → mention_triggers defaults to ["Hermes Agent"]."""
    from plugins.platforms.eko.config import EkoConfig

    with patch.dict(os.environ, {}):
        _clear_eko_env()
        cfg = EkoConfig.from_env()

    assert cfg.mention_triggers == ["Hermes Agent"]


def test_metadata_mentions_runtime_trigger_default():
    """Plugin metadata and README document the runtime trigger default."""
    plugin_yaml = Path("plugins/platforms/eko/plugin.yaml").read_text()
    readme = Path("plugins/platforms/eko/README.md").read_text()

    assert "EKO_MENTION_TRIGGERS" in plugin_yaml
    assert "default: Hermes Agent" in plugin_yaml
    assert "EKO_MENTION_TRIGGERS" in readme
    assert "| `EKO_MENTION_TRIGGERS` | No | `Hermes Agent` |" in readme
    assert "default: `Hermes Agent`" in readme


# ---------------------------------------------------------------------------
# Test #9 — custom mention triggers from env
# ---------------------------------------------------------------------------


def test_mention_triggers_custom():
    """Custom CSV mention triggers parsed correctly."""
    from plugins.platforms.eko.config import EkoConfig

    with patch.dict(
        os.environ, {"EKO_MENTION_TRIGGERS": "Bot, Alice, Bob"}
    ):
        cfg = EkoConfig.from_env()

    assert cfg.mention_triggers == ["Bot", "Alice", "Bob"]


# ---------------------------------------------------------------------------
# Test #10 — extra dict fallback
# ---------------------------------------------------------------------------


def test_env_values_override_extra_values():
    """Environment variables take precedence over config extra values."""
    from plugins.platforms.eko.config import EkoConfig

    with patch.dict(
        os.environ,
        {
            "EKO_BASE_URL": "https://env.ekoapp.com",
            "EKO_OAUTH_CLIENT_ID": "env-id",
            "EKO_PORT": "1234",
        },
    ):
        cfg = EkoConfig.from_env(
            extra={
                "base_url": "https://extra.ekoapp.com",
                "oauth_client_id": "extra-id",
                "port": 9999,
            }
        )

    assert cfg.base_url == "https://env.ekoapp.com"
    assert cfg.oauth_client_id == "env-id"
    assert cfg.webhook_port == 1234


def test_extra_dict_fallback():
    """extra dict fills in when env vars are unset."""
    from plugins.platforms.eko.config import EkoConfig

    with patch.dict(os.environ, {}):
        _clear_eko_env()
        cfg = EkoConfig.from_env(
            extra={
                "base_url": "https://extra.ekoapp.com",
                "oauth_client_id": "extra-id",
                "oauth_client_secret": "extra-secret",
                "webhook_path": "/custom/webhook",
                "port": 9999,
            }
        )

    assert cfg.base_url == "https://extra.ekoapp.com"
    assert cfg.oauth_client_id == "extra-id"
    assert cfg.oauth_client_secret == "extra-secret"
    assert cfg.webhook_path == "/custom/webhook"
    assert cfg.webhook_port == 9999


# ---------------------------------------------------------------------------
# Test #11 — env + extra union for set fields
# ---------------------------------------------------------------------------


def test_env_extra_union_for_sets():
    """CSV env and extra list are unioned for allowlists."""
    from plugins.platforms.eko.config import EkoConfig

    with patch.dict(os.environ, {"EKO_ALLOWED_USERS": "alice"}):
        cfg = EkoConfig.from_env(
            extra={"allowed_users": ["bob"]}
        )

    assert cfg.allowed_users == frozenset({"alice", "bob"})


# ---------------------------------------------------------------------------
# Test #12 — webhook secret falls back to oauth secret
# ---------------------------------------------------------------------------


def test_webhook_secret_fallback():
    """webhook_secret falls back to oauth_client_secret when not set."""
    from plugins.platforms.eko.config import EkoConfig

    # No EKO_WEBHOOK_SECRET set → falls back to oauth secret
    with patch.dict(
        os.environ,
        {
            "EKO_OAUTH_CLIENT_SECRET": "oauth-secret",
        },
    ):
        cfg = EkoConfig.from_env()

    assert cfg.webhook_secret == "oauth-secret"

    # Explicit webhook secret overrides
    with patch.dict(
        os.environ,
        {
            "EKO_OAUTH_CLIENT_SECRET": "oauth-secret",
            "EKO_WEBHOOK_SECRET": "webhook-secret",
        },
    ):
        cfg = EkoConfig.from_env()

    assert cfg.webhook_secret == "webhook-secret"


# ---------------------------------------------------------------------------
# Test #13 — size limits custom values
# ---------------------------------------------------------------------------


def test_size_limits():
    """Custom size limit values are respected."""
    from plugins.platforms.eko.config import EkoConfig

    with patch.dict(
        os.environ,
        {
            "EKO_MESSAGE_MAX_CHARS": "1000",
            "EKO_MAX_UPLOAD_BYTES": "1000000",
            "EKO_MAX_INBOUND_MEDIA_BYTES": "500000",
            "EKO_REPLY_TOKEN_TTL": "30",
        },
    ):
        cfg = EkoConfig.from_env()

    assert cfg.message_max_chars == 1000
    assert cfg.max_upload_bytes == 1_000_000
    assert cfg.max_inbound_media_bytes == 500_000
    assert cfg.reply_token_ttl == 30
