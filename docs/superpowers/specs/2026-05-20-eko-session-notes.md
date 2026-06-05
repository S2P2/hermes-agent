# Eko Adapter — Session Notes

## Current State

**Working:** v1.5.0 live at `S2P2/hermes-agent` — bidirectional text chat + image/file + group/topic + management tools.

**Tested and confirmed working:**
- Webhook receives messages from Eko
- OAuth2 auth (form-urlencoded, not JSON)
- Reply token → push fallback
- User allowlist (`EKO_ALLOWED_USERS`)
- Home channel (`EKO_HOME_CHANNEL`)
- Webhook signature verification (`X-Eko-Signature` HMAC-SHA256-Base64)
- Inbound image receiving — download, cache, vision model interpretation ✅
- Outbound image/file sending — multipart upload (DM + group/topic) ✅
- Cron media attachments ✅
- Group/topic outbound routing (by `groupId`+`topicId` presence, NOT `groupType`) ✅
- Document routing to topics ✅ (fixed Issue #20)
- Management tools: `eko_create_group`, `eko_create_topic`, `eko_query_users` ✅ (Issues #16, #17)

**Known issues / limitations:**
- Inbound file: no webhook event from Eko (platform limitation)
- Inbound sticker: `[sticker]` placeholder only (no download API)
- No `require_mention` config for group chats yet (Issue #22)
- No management actions config gate yet (Issue #23)

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
origin   → https://github.com/S2P2/hermes-agent.git (your fork)
upstream → https://github.com/NousResearch/hermes-agent.git
```

## Files

| File | Purpose |
|------|---------|
| `plugins/platforms/eko/adapter.py` | Full adapter (~1600 lines) — _EkoClient + EkoAdapter |
| `plugins/platforms/eko/tools.py` | Management agent tools (3 tools) |
| `plugins/platforms/eko/README.md` | Setup guide + roadmap + version history |
| `tests/gateway/test_eko_plugin.py` | Tests (131 tests) |
| `docs/adr/0001-eko-no-auto-topic.md` | ADR: no auto-topic creation |
| `docs/adr/0002-eko-separate-management-tools.md` | ADR: separate tools vs action-dispatch |

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

## Open Issues

| # | Title | Priority |
|---|-------|----------|
| 22 | `eko.require_mention` config — bot only responds when @mentioned in groups | Medium |
| 23 | `eko.management_actions` config gate — allowlist for management tools | Low |

## Pitfalls

- `runner.adapters` is `Dict[Platform, BasePlatformAdapter]` — use `Platform("eko")` not `"eko"` string
- `send_message` MEDIA delivery has a hardcoded platform allowlist — new platforms must be added
- Empty text chunks cause Eko 400 errors — filter before sending
- `logger.info("format %s", args)` with mismatched arg count silently fails
- `_last_resolved_tool_names` is a process-global in `model_tools.py`
