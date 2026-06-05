"""Runtime for Eko management tools.

Keeps the three public Hermes tools separate while centralizing their shared
runtime behavior: connected-client lookup, action allowlist checks, username
resolution, and result/error formatting.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Callable, Iterable, List, Optional

from tools.registry import tool_error, tool_result

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from plugins.platforms.eko.adapter import _EkoClient


VALID_MANAGEMENT_ACTIONS = frozenset({"create_group", "create_topic", "query_users"})


# Maps registered tool name → canonical action name.
TOOL_TO_ACTION = {
    "eko_create_group": "create_group",
    "eko_create_topic": "create_topic",
    "eko_query_users": "query_users",
}


def get_connected_client() -> "_EkoClient | None":
    """Fallback client resolver — reaches through the gateway runner.

 Only used when no client has been injected via ``set_client()``.
 In normal gateway operation the adapter injects the client directly
 at connection time, so this path is rarely hit.
 """
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


def load_management_actions_config() -> Optional[List[str]]:
    """Read ``eko.management_actions`` from user config.

    Returns a list of allowed action names, or ``None`` if the user hasn't
    restricted the set (default: all actions allowed).

    Result is cached for 60 s to avoid hitting the filesystem on every
    tool invocation.
    """
    now = time.time()
    if load_management_actions_config._cache_ts and (now - load_management_actions_config._cache_ts < 60):
        return load_management_actions_config._cache_val
    result = _load_management_actions_raw()
    load_management_actions_config._cache_val = result
    load_management_actions_config._cache_ts = now
    return result


load_management_actions_config._cache_val: Optional[List[str]] = None
load_management_actions_config._cache_ts: float = 0.0


def _load_management_actions_raw() -> Optional[List[str]]:
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

    valid = [n for n in names if n in VALID_MANAGEMENT_ACTIONS]
    invalid = [n for n in names if n not in VALID_MANAGEMENT_ACTIONS]
    if invalid:
        logger.warning(
            "eko.management_actions: unknown action(s) ignored: %s. Known: %s",
            ", ".join(invalid),
            ", ".join(sorted(VALID_MANAGEMENT_ACTIONS)),
        )
    return valid


class EkoManagementRuntime:
    """Shared runtime behind the three Eko management tool handlers."""

    def __init__(
        self,
        *,
        client_getter: Callable[[], "_EkoClient | None"] = get_connected_client,
        action_loader: Callable[[], Optional[List[str]]] = load_management_actions_config,
    ) -> None:
        self._client: "Optional[_EkoClient]" = None
        self._client_getter = client_getter
        self._action_loader = action_loader

    def set_client(self, client: "_EkoClient") -> None:
        """Inject a connected client (called by the adapter at connect time)."""
        self._client = client

    def clear_client(self) -> None:
        """Clear the injected client (called by the adapter at disconnect time)."""
        self._client = None

    def set_action_loader(
        self, loader: Callable[[], Optional[List[str]]]
    ) -> Optional[Callable[[], Optional[List[str]]]]:
        """Override the action-allowlist loader.

        Returns the previous loader so callers can restore it.
        """
        prev = self._action_loader
        self._action_loader = loader
        return prev

    def get_client(self) -> "_EkoClient | None":
        if self._client is not None:
            return self._client
        return self._client_getter()

    def config_gate_error(self, action: str) -> Optional[str]:
        """Return a config-gate error message, or ``None`` if allowed."""
        allowed = self._action_loader()
        if allowed is not None and action not in allowed:
            return (
                f"Action '{action}' is disabled by config (eko.management_actions). "
                f"Allowed: {', '.join(allowed) if allowed else '<none>'}"
            )
        return None

    def is_action_allowed(self, action: str) -> bool:
        return self.config_gate_error(action) is None

    def check_action_available(self, action: str) -> bool:
        if self.get_client() is None:
            return False
        return self.is_action_allowed(action)

    async def resolve_member_usernames(
        self,
        client: "_EkoClient",
        usernames: Iterable[str],
    ) -> tuple[list[str], Optional[str]]:
        """Resolve exact Eko usernames to user IDs.

        Returns ``(uids, None)`` on success or ``([], error_message)`` on
        validation/API failure. Exact-match behavior is intentionally strict
        so fuzzy API results cannot silently add the wrong member.
        """
        uids: list[str] = []
        for username in usernames:
            try:
                users = await client.query_users(username)
            except Exception as exc:
                return [], f"Failed to query user '{username}': {exc}"
            if not isinstance(users, list) or not users:
                return [], f"User '{username}' not found"

            exact = [u for u in users if u.get("username") == username]
            if len(exact) == 1:
                uid = str(exact[0].get("_id", "")).strip()
                if not uid:
                    return [], f"User '{username}' has no valid user ID"
                uids.append(uid)
            elif len(exact) > 1:
                candidates = self._format_candidates(exact)
                return [], (
                    f"Username '{username}' is ambiguous — "
                    f"multiple exact matches: {candidates}"
                )
            else:
                candidates = self._format_candidates(users)
                return [], f"User '{username}' not found. Similar users: {candidates}"
        return uids, None

    async def create_group(self, args: dict) -> str:
        client = self.get_client()
        if not client:
            return tool_error("Eko adapter not connected")

        gate_err = self.config_gate_error("create_group")
        if gate_err:
            return tool_error(gate_err)

        uids: list[str] = list(args.get("member_uids") or [])
        usernames: list[str] = list(args.get("member_usernames") or [])
        if not uids and not usernames:
            return tool_error("Provide member_usernames or member_uids")

        resolved, error = await self.resolve_member_usernames(client, usernames)
        if error:
            return tool_error(error)
        uids.extend(resolved)

        name = str(args.get("name") or "").strip()
        try:
            result = await client.create_group(uids, name=name)
            return tool_result(result)
        except Exception as exc:
            return tool_error(f"Failed to create group: {exc}")

    async def create_topic(self, args: dict) -> str:
        client = self.get_client()
        if not client:
            return tool_error("Eko adapter not connected")

        gate_err = self.config_gate_error("create_topic")
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

    async def query_users(self, args: dict) -> str:
        client = self.get_client()
        if not client:
            return tool_error("Eko adapter not connected")

        gate_err = self.config_gate_error("query_users")
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

    @staticmethod
    def _format_candidates(users) -> str:
        return ", ".join(
            f"{u.get('username')} ({u.get('_id', '?')})" for u in users
        )


# ---------------------------------------------------------------------------
# Module-level default runtime
# ---------------------------------------------------------------------------

# Process-global singleton: safe because each Hermes profile runs in its
# own process. The adapter injects the client at connect time.
_default_runtime = EkoManagementRuntime()


def get_default_runtime() -> EkoManagementRuntime:
    """Return the shared management runtime used by all Eko management tools."""
    return _default_runtime
