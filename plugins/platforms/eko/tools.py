"""Eko management tools — group/topic creation and user lookup.

Registered as async Hermes tools via the Eko plugin. Only available when
the Eko adapter is connected in the gateway.

Each tool resolves the live ``_EkoClient`` from the running gateway's
adapter instance, then delegates to the corresponding ``_EkoClient`` method.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

from tools.registry import registry, tool_error, tool_result

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
# Schemas
# ---------------------------------------------------------------------------

_EKO_CREATE_GROUP_SCHEMA = {
    "name": "eko_create_group",
    "description": (
        "Create an Eko group chat with the specified members. "
        "Returns the group ID and group details. "
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
                    "Eko user IDs to add as group members. "
                    "Use this if you already have the user IDs; "
                    "otherwise use member_usernames."
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
                "description": "The Eko group ID to create the topic in.",
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
    check_fn=_check_eko_active,
    is_async=True,
    emoji="👥",
)

registry.register(
    name="eko_create_topic",
    toolset="eko",
    schema=_EKO_CREATE_TOPIC_SCHEMA,
    handler=_handle_create_topic,
    check_fn=_check_eko_active,
    is_async=True,
    emoji="📋",
)

registry.register(
    name="eko_query_users",
    toolset="eko",
    schema=_EKO_QUERY_USERS_SCHEMA,
    handler=_handle_query_users,
    check_fn=_check_eko_active,
    is_async=True,
    emoji="🔍",
)
