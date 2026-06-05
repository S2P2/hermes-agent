# Eko Adapter — Session Notes

Operational reference accumulated across development sessions. Architecture, setup, and API reference live in `plugins/platforms/eko/README.md`.

## Current State

**Working:** v1.10.0 at `S2P2/hermes-agent` — full bidirectional messaging with group/topic routing, media, management tools, and webhook verification.

**Features:**
- Webhook receives messages from Eko
- OAuth2 auth (form-urlencoded, not JSON)
- Reply token → push fallback
- User allowlist (`EKO_ALLOWED_USERS`)
- Home channel (`EKO_HOME_CHANNEL`)
- Webhook signature verification (`X-Eko-Signature` HMAC-SHA256-Base64)
- Inbound image receiving — download, cache, vision model interpretation
- Outbound image/file sending — multipart upload (DM + group/topic)
- Cron media attachments
- Group/topic outbound routing (by `groupId`+`topicId` presence, NOT `groupType`)
- Management tools: `eko_create_group`, `eko_create_topic`, `eko_query_users` (config-gated via `eko.management_actions`)
- Require mention in groups (`EKO_REQUIRE_MENTION`, `EKO_MENTION_TRIGGERS`)
- Group/topic allowlists (`EKO_ALLOWED_GROUPS`, `EKO_ALLOWED_TOPICS`, `EKO_ALLOW_ALL_GROUPS`)

**Known limitations:**
- Inbound file: no webhook event from Eko (platform limitation)
- Inbound sticker: `[sticker packageId=... stickerId=...]` placeholder only (no download API)

## Key Decisions

- **Plugin form** (not built-in) — lives at `plugins/platforms/eko/`, zero core code changes
- **Configurable base URL** — Eko uses customer-specific hostnames (e.g. `customer-h1.ekoapp.com`)
- **Reply token TTL** — defaulted to 50s (conservative)
- **Webhook signature** — `X-Eko-Signature` HMAC-SHA256-Base64, key = OAuth client secret
- **No auto-topic creation** — see ADR-0001. Agent creates topics via `eko_create_topic` when needed.
- **3 separate management tools** — see ADR-0002. Each tool has focused schema. Not a single action-dispatch tool.
- **`groupType` is unreliable for routing** — always route by `groupId` + `topicId` presence
- **`_EkoClient` creates fresh `aiohttp.ClientSession` per request** — no connection pooling

## Repo Setup

```
origin   → https://github.com/S2P2/hermes-agent.git (fork)
upstream → https://github.com/NousResearch/hermes-agent.git
```

## Files

| File | Purpose |
|------|---------|
| `plugins/platforms/eko/adapter.py` | Adapter + OAuth client (~1000 lines) |
| `plugins/platforms/eko/client.py` | Eko API client (separate from adapter) |
| `plugins/platforms/eko/config.py` | Config dataclass, env/config precedence |
| `plugins/platforms/eko/inbound.py` | Inbound message normalization |
| `plugins/platforms/eko/outbound.py` | Outbound sender, route resolution |
| `plugins/platforms/eko/management.py` | Management tools runtime + config gate |
| `plugins/platforms/eko/tools.py` | Tool registration (3 management tools) |
| `plugins/platforms/eko/routing.py` | Session routing logic |
| `tests/gateway/test_eko_plugin.py` | Tests (212 tests) |

## Eko API Endpoints

| Endpoint | Purpose | Status |
|----------|---------|--------|
| `POST /bot/v1/direct/message` | Send text to user (JSON) | ✅ |
| `POST /bot/v1/direct/picture` | Send image to user (multipart) | ✅ |
| `POST /bot/v1/direct/file` | Send file to user (multipart) | ✅ |
| `POST /bot/v1/group/message` | Send text to group/topic (JSON) | ✅ |
| `POST /bot/v1/group/picture` | Send image to group/topic (multipart) | ✅ |
| `POST /bot/v1/group/file` | Send file to group/topic (multipart) | ✅ |
| `POST /bot/v1/groups` | Create group chat | ✅ |
| `POST /bot/v1/groups/{gid}/topics` | Create topic in a group | ✅ |
| `GET /bot/v1/users?username=...` | Query users by username | ✅ |
| `POST /bot/v1/message/text` | Reply via reply token (multipart) | ✅ |
| `POST /bot/v1/message/picture` | Reply image via reply token (multipart) | ✅ |
| `GET /file/view/{id}?size=large` | Download inbound image | ✅ |

## Eko API Quirks

- OAuth token endpoint: **form-urlencoded**, not JSON
- `groupType` is unreliable — `"direct_chat"` even for topics in DMs. Route by `groupId`+`topicId` presence.
- Webhook events have no event ID (hash full JSON for dedup)
- Source can use `userId` or `uid` depending on event type
- Reply endpoint uses multipart/form-data; push uses JSON
- Webhook signature: `X-Eko-Signature`, HMAC-SHA256-Base64
- Webhook user-agent: `axios/0.19.2`

## Pitfalls

- `runner.adapters` is `Dict[Platform, BasePlatformAdapter]` — use `Platform("eko")` not `"eko"` string
- `send_message` MEDIA delivery has a hardcoded platform allowlist — new platforms must be added
- Empty text chunks cause Eko 400 errors — filter before sending
- `logger.info("format %s", args)` with mismatched arg count silently fails
- `_last_resolved_tool_names` is a process-global in `model_tools.py`
- Eko adapter inherits `edit_message` from `BasePlatformAdapter` (no-op) — gateway uses compact progress messages instead of editable bubbles
