"""Eko management tools — group/topic creation and user lookup.

Registered as async Hermes tools via the Eko plugin. Only available when
the Eko adapter is connected in the gateway **and** the tool is allowed by
the ``eko.management_actions`` config allowlist.

Each tool resolves the live ``_EkoClient`` from the running gateway's
adapter instance, then delegates to the corresponding ``_EkoClient`` method.
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, List, Optional

from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from plugins.platforms.eko.adapter import _EkoClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_eko_client() -> "_EkoClient | None":
    """Return the ``_EkoClient`` from the running Eko adapter, or None."""
    try:
        from gateway.run import _gateway_runner_ref
        from gateway.config import Platform as _Platform

        runner = _gateway_runner_ref()
        if not runner:
            return None
        adapter = runner.adapters.get(_Platform("eko"))
        if adapter and hasattr(adapter, "_client"):
            return adapter._client
    except Exception:
        pass
    return None


def _check_eko_active() -> bool:
    """Gate: tools only appear when the Eko adapter is connected."""
    return _get_eko_client() is not None


# ---------------------------------------------------------------------------
# Config-based management action allowlist
# ---------------------------------------------------------------------------

# Canonical action names (match the _EkoClient method names, not the
# registered tool names with the ``eko_`` prefix).
_VALID_MANAGEMENT_ACTIONS = frozenset({"create_group", "create_topic", "query_users"})

# Maps registered tool name → canonical action name.
_TOOL_TO_ACTION = {
    "eko_create_group": "create_group",
    "eko_create_topic": "create_topic",
    "eko_query_users": "query_users",
}


def _load_management_actions_config() -> Optional[List[str]]:
    """Read ``eko.management_actions`` from user config.

    Returns a list of allowed action names, or ``None`` if the user
    hasn't restricted the set (default: all actions allowed).

    Accepts either a comma-separated string or a YAML list.
    Unknown action names are dropped with a log warning.
    """
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
    except Exception as exc:
        logger.debug("eko: could not load config (%s); allowing all management actions.", exc)
        return None

    raw = (cfg.get("eko") or {}).get("management_actions")
    if raw is None or raw == "":
        return None

    if isinstance(raw, str):
        names = [n.strip() for n in raw.split(",") if n.strip()]
    elif isinstance(raw, (list, tuple)):
        names = [str(n).strip() for n in raw if str(n).strip()]
    else:
        logger.warning(
            "eko.management_actions: unexpected type %s; ignoring.",
            type(raw).__name__,
        )
        return None

    valid = [n for n in names if n in _VALID_MANAGEMENT_ACTIONS]
    invalid = [n for n in names if n not in _VALID_MANAGEMENT_ACTIONS]
    if invalid:
        logger.warning(
            "eko.management_actions: unknown action(s) ignored: %s. "
            "Known: %s",
            ", ".join(invalid), ", ".join(sorted(_VALID_MANAGEMENT_ACTIONS)),
        )
    return valid


def _is_action_allowed(action: str) -> bool:
    """Check if a management action is allowed by config.

    Returns ``True`` when config is unset (backward compatible) or when
    the action is in the explicit allowlist.
    """
    allowed = _load_management_actions_config()
    if allowed is None:
        return True
    return action in allowed


def _make_check_fn(action: str):
    """Create a ``check_fn`` that gates on adapter connection + config allowlist."""
    def _check() -> bool:
        if not _check_eko_active():
            return False
        return _is_action_allowed(action)
    return _check


def _config_gate_error(action: str) -> Optional[str]:
    """Return a config-gate error message, or ``None`` if the action is allowed.

    Used as a defense-in-depth check inside handlers.
    """
    allowed = _load_management_actions_config()
    if allowed is not None and action not in allowed:
        return (
            f"Action '{action}' is disabled by config (eko.management_actions). "
            f"Allowed: {', '.join(allowed) if allowed else '<none>'}"
        )
    return None


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

_EKO_CREATE_GROUP_SCHEMA = {
    "name": "eko_create_group",
    "description": (
        "Create an Eko group chat with the specified members. "
        "Returns the group ID (a 24-char hex string like '6a1b1373bfd10bc5370d921f') "
        "and group details. "
        "Use eko_query_users to look up user IDs by username first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "member_usernames": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Eko usernames to add as group members.",
            },
            "member_uids": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Eko user IDs (24-char hex strings) to add as group members. "
                    "Use eko_query_users to resolve usernames to IDs first; "
                    "otherwise use member_usernames directly."
                ),
            },
            "name": {
                "type": "string",
                "description": "Optional name for the group chat.",
            },
        },
    },
}

_EKO_CREATE_TOPIC_SCHEMA = {
    "name": "eko_create_topic",
    "description": (
        "Create a topic (thread) inside an existing Eko group chat. "
        "Returns the topic ID."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "group_id": {
                "type": "string",
                "description": (
                    "The Eko group ID (a 24-char hex string like '6a1b1373bfd10bc5370d921f') "
                    "to create the topic in. Get this from eko_create_group response, "
                    "NOT from a user ID or chat ID."
                ),
            },
            "name": {
                "type": "string",
                "description": "Name for the new topic.",
            },
        },
        "required": ["group_id", "name"],
    },
}

_EKO_QUERY_USERS_SCHEMA = {
    "name": "eko_query_users",
    "description": (
        "Look up Eko users by username. Returns user IDs, usernames, and emails. "
        "Use this to resolve usernames to user IDs before calling eko_create_group."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "username": {
                "type": "string",
                "description": "Username to search for.",
            },
        },
        "required": ["username"],
    },
}


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _handle_create_group(args: dict, **kw) -> str:
    client = _get_eko_client()
    if not client:
        return tool_error("Eko adapter not connected")

    # Defense in depth: config gate.
    gate_err = _config_gate_error("create_group")
    if gate_err:
        return tool_error(gate_err)

    # Resolve usernames → uids if needed.
    uids: list[str] = list(args.get("member_uids") or [])
    usernames: list[str] = list(args.get("member_usernames") or [])

    if not uids and not usernames:
        return tool_error("Provide member_usernames or member_uids")

    for username in usernames:
        try:
            users = await client.query_users(username)
        except Exception as exc:
            return tool_error(f"Failed to query user '{username}': {exc}")
        if not users:
            return tool_error(f"User '{username}' not found")
        # Take the first match.
        uids.append(str(users[0].get("_id", "")))

    name = str(args.get("name") or "").strip()

    try:
        result = await client.create_group(uids, name=name)
        return tool_result(result)
    except Exception as exc:
        return tool_error(f"Failed to create group: {exc}")


async def _handle_create_topic(args: dict, **kw) -> str:
    client = _get_eko_client()
    if not client:
        return tool_error("Eko adapter not connected")

    # Defense in depth: config gate.
    gate_err = _config_gate_error("create_topic")
    if gate_err:
        return tool_error(gate_err)

    group_id = str(args.get("group_id") or "").strip()
    name = str(args.get("name") or "").strip()

    if not group_id:
        return tool_error("group_id is required")
    if not name:
        return tool_error("name is required")

    try:
        result = await client.create_topic(group_id, name)
        return tool_result(result)
    except Exception as exc:
        return tool_error(f"Failed to create topic: {exc}")


async def _handle_query_users(args: dict, **kw) -> str:
    client = _get_eko_client()
    if not client:
        return tool_error("Eko adapter not connected")

    # Defense in depth: config gate.
    gate_err = _config_gate_error("query_users")
    if gate_err:
        return tool_error(gate_err)

    username = str(args.get("username") or "").strip()
    if not username:
        return tool_error("username is required")

    try:
        result = await client.query_users(username)
        return tool_result(result)
    except Exception as exc:
        return tool_error(f"Failed to query users: {exc}")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

registry.register(
    name="eko_create_group",
    toolset="eko",
    schema=_EKO_CREATE_GROUP_SCHEMA,
    handler=_handle_create_group,
    check_fn=_make_check_fn("create_group"),
    is_async=True,
    emoji="👥",
)

registry.register(
    name="eko_create_topic",
    toolset="eko",
    schema=_EKO_CREATE_TOPIC_SCHEMA,
    handler=_handle_create_topic,
    check_fn=_make_check_fn("create_topic"),
    is_async=True,
    emoji="📋",
)

registry.register(
    name="eko_query_users",
    toolset="eko",
    schema=_EKO_QUERY_USERS_SCHEMA,
    handler=_handle_query_users,
    check_fn=_make_check_fn("query_users"),
    is_async=True,
    emoji="🔍",
)
