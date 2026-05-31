"""Eko configuration module.

Owns env/config precedence, defaults, validation, parsed allowlists,
mention settings, size limits, and webhook settings.  The Eko adapter
and plugin hooks consume this module instead of each re-encoding pieces
of the configuration interface.

Design notes
------------

* ``from_env()`` classmethod resolves each setting from environment
  variables first, then falls back to the ``extra`` dict, then to
  hardcoded defaults — matching the existing ``EkoAdapter.__init__``
  precedence.
* The dataclass is frozen so config is immutable after construction.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Set


# ---------------------------------------------------------------------------
# Constants (re-exported from adapter for single source of truth)
# ---------------------------------------------------------------------------

DEFAULT_WEBHOOK_PORT: int = 8647
DEFAULT_WEBHOOK_PATH: str = "/eko/webhook"
DEFAULT_REPLY_TOKEN_TTL: int = 50
DEFAULT_MESSAGE_MAX_CHARS: int = 5000
DEFAULT_MAX_UPLOAD_BYTES: int = 26_214_400  # 25 MiB
DEFAULT_MAX_INBOUND_MEDIA_BYTES: int = 26_214_400  # 25 MiB


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _csv_set(value: str) -> Set[str]:
    """Split a CSV string into a stripped set, dropping empties."""
    if not value:
        return set()
    return {x.strip() for x in value.split(",") if x.strip()}


def _truthy_env(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# EkoConfig
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EkoConfig:
    """Immutable, fully-resolved Eko platform configuration."""

    # Required credentials
    base_url: str = ""
    oauth_client_id: str = ""
    oauth_client_secret: str = ""

    # Webhook server
    webhook_host: str = "0.0.0.0"
    webhook_port: int = DEFAULT_WEBHOOK_PORT
    webhook_path: str = DEFAULT_WEBHOOK_PATH
    webhook_secret: str = ""
    require_signature: bool = True

    # Size limits
    message_max_chars: int = DEFAULT_MESSAGE_MAX_CHARS
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    max_inbound_media_bytes: int = DEFAULT_MAX_INBOUND_MEDIA_BYTES
    reply_token_ttl: int = DEFAULT_REPLY_TOKEN_TTL

    # Allowlists
    allowed_users: FrozenSet[str] = field(default_factory=frozenset)
    allowed_groups: FrozenSet[str] = field(default_factory=frozenset)
    allowed_topics: FrozenSet[str] = field(default_factory=frozenset)
    allow_all_users: bool = False
    allow_all_groups: bool = True

    # Mention
    require_mention: bool = True
    mention_triggers: List[str] = field(default_factory=lambda: ["Hermes Agent"])

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls, extra: Optional[Dict] = None) -> "EkoConfig":
        """Build from environment variables + plugin ``extra`` dict."""
        extra = extra or {}

        # Required credentials
        base_url = (
            os.getenv("EKO_BASE_URL") or extra.get("base_url", "")
        ).rstrip("/")
        oauth_client_id = (
            os.getenv("EKO_OAUTH_CLIENT_ID")
            or extra.get("oauth_client_id", "")
        )
        oauth_client_secret = (
            os.getenv("EKO_OAUTH_CLIENT_SECRET")
            or extra.get("oauth_client_secret", "")
        )
        webhook_secret = (
            os.getenv("EKO_WEBHOOK_SECRET")
            or extra.get("webhook_secret", "")
        ) or oauth_client_secret

        require_signature = _truthy_env(
            "EKO_REQUIRE_SIGNATURE",
            bool(extra.get("require_signature", True)),
        )

        # Webhook server
        webhook_host = os.getenv("EKO_HOST") or extra.get("host", "0.0.0.0")
        webhook_port = _parse_int(
            os.getenv("EKO_PORT") or extra.get("port", DEFAULT_WEBHOOK_PORT),
            DEFAULT_WEBHOOK_PORT,
        )
        webhook_path = (
            os.getenv("EKO_WEBHOOK_PATH")
            or extra.get("webhook_path", DEFAULT_WEBHOOK_PATH)
        )

        # Allowlists
        allow_all_users = _truthy_env(
            "EKO_ALLOW_ALL_USERS",
            bool(extra.get("allow_all_users", False)),
        )
        allowed_users = frozenset(
            _csv_set(os.getenv("EKO_ALLOWED_USERS", ""))
            | set(extra.get("allowed_users", []))
        )

        allow_all_groups = _truthy_env(
            "EKO_ALLOW_ALL_GROUPS",
            bool(extra.get("allow_all_groups", True)),
        )
        allowed_groups = frozenset(
            _csv_set(os.getenv("EKO_ALLOWED_GROUPS", ""))
            | set(extra.get("allowed_groups", []))
        )
        allowed_topics = frozenset(
            _csv_set(os.getenv("EKO_ALLOWED_TOPICS", ""))
            | set(extra.get("allowed_topics", []))
        )

        # Mention
        require_mention = _truthy_env(
            "EKO_REQUIRE_MENTION",
            bool(extra.get("require_mention", True)),
        )
        _triggers = (
            os.getenv("EKO_MENTION_TRIGGERS", "")
            or ",".join(extra.get("mention_triggers", []))
        )
        mention_triggers = [w.strip() for w in _triggers.split(",") if w.strip()]
        if not mention_triggers:
            mention_triggers = ["Hermes Agent"]

        # Size limits
        reply_token_ttl = _parse_int(
            os.getenv("EKO_REPLY_TOKEN_TTL")
            or extra.get("reply_token_ttl", DEFAULT_REPLY_TOKEN_TTL),
            DEFAULT_REPLY_TOKEN_TTL,
        )
        message_max_chars = _parse_int(
            os.getenv("EKO_MESSAGE_MAX_CHARS")
            or extra.get("message_max_chars", DEFAULT_MESSAGE_MAX_CHARS),
            DEFAULT_MESSAGE_MAX_CHARS,
        )
        max_upload_bytes = _parse_int(
            os.getenv("EKO_MAX_UPLOAD_BYTES")
            or extra.get("max_upload_bytes", DEFAULT_MAX_UPLOAD_BYTES),
            DEFAULT_MAX_UPLOAD_BYTES,
        )
        max_inbound_media_bytes = _parse_int(
            os.getenv("EKO_MAX_INBOUND_MEDIA_BYTES")
            or extra.get(
                "max_inbound_media_bytes", DEFAULT_MAX_INBOUND_MEDIA_BYTES
            ),
            DEFAULT_MAX_INBOUND_MEDIA_BYTES,
        )

        return cls(
            base_url=base_url,
            oauth_client_id=oauth_client_id,
            oauth_client_secret=oauth_client_secret,
            webhook_host=webhook_host,
            webhook_port=webhook_port,
            webhook_path=webhook_path,
            webhook_secret=webhook_secret,
            require_signature=require_signature,
            message_max_chars=message_max_chars,
            max_upload_bytes=max_upload_bytes,
            max_inbound_media_bytes=max_inbound_media_bytes,
            reply_token_ttl=reply_token_ttl,
            allowed_users=allowed_users,
            allowed_groups=allowed_groups,
            allowed_topics=allowed_topics,
            allow_all_users=allow_all_users,
            allow_all_groups=allow_all_groups,
            require_mention=require_mention,
            mention_triggers=mention_triggers,
        )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def has_credentials(self) -> bool:
        """All three required credential fields are non-empty."""
        return bool(self.base_url and self.oauth_client_id and self.oauth_client_secret)

    def is_user_allowed(self, uid: str) -> bool:
        """Check if *uid* passes the user allowlist."""
        if self.allow_all_users:
            return True
        return uid in self.allowed_users

    def is_group_allowed(self, group_id: str) -> bool:
        """Check if *group_id* passes the group allowlist."""
        if self.allow_all_groups:
            return True
        return group_id in self.allowed_groups

    def is_topic_allowed(self, group_id: str, topic_id: str) -> bool:
        """Check if ``group_id:topic_id`` passes the topic allowlist."""
        if self.allow_all_groups:
            return True
        return f"{group_id}:{topic_id}" in self.allowed_topics


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_int(value, fallback: int) -> int:
    """Parse *value* as int; return *fallback* on failure."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback
