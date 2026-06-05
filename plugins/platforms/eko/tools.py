"""Eko management tools — group/topic creation and user lookup.

Registered as async Hermes tools via the Eko plugin. Only available when
the Eko adapter is connected in the gateway **and** the tool is allowed by
the ``eko.management_actions`` config allowlist.

Each tool resolves the live ``_EkoClient`` from the shared management
runtime (injected by the adapter at connect time), then delegates to the
corresponding ``_EkoClient`` method.
"""

from __future__ import annotations

from tools.registry import registry

from plugins.platforms.eko.management import get_default_runtime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_check_fn(action: str):
    """Create a ``check_fn`` that gates on adapter connection + config allowlist."""
    def _check() -> bool:
        return get_default_runtime().check_action_available(action)
    return _check


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
    return await get_default_runtime().create_group(args)


async def _handle_create_topic(args: dict, **kw) -> str:
    return await get_default_runtime().create_topic(args)


async def _handle_query_users(args: dict, **kw) -> str:
    return await get_default_runtime().query_users(args)


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
