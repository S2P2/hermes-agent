# Eko Messaging Adapter

Connects [Hermes Agent](https://hermes-agent.nousresearch.com/) to the
[Eko Messaging API](https://eko.gitbook.io/api/messaging-api/) via webhooks,
enabling bidirectional text chat between Eko users and the Hermes agent.

## Features

- 1:1 direct message and topic support (each topic gets its own session)
- OAuth2 client-credentials authentication with proactive token refresh
- Reply token (free) with automatic push fallback
- Event deduplication
- User allowlist gating
- Cron/notification delivery
- Interactive setup wizard (`hermes gateway setup`)
- Webhook signature verification (`X-Eko-Signature` HMAC-SHA256)
- Image receiving (download, cache, vision tool integration)
- Image sending (reply token + push fallback, DM and group/topic)
- File sending (push to user or group/topic)
- Cron media attachments (images and files via standalone sender)
- Group/topic-aware outbound routing (auto-detects DM vs group)
- Management tools: create groups, create topics, query users from agent chat

## Prerequisites

- Hermes Agent with gateway support
- An Eko admin panel account with bot integration enabled
- A publicly reachable HTTPS URL for the webhook (e.g. via ngrok, caddy, or reverse proxy)
- `aiohttp` Python package

## Setup

### 1. Create a bot in Eko Admin Panel

1. Navigate to **BOT INTEGRATION** → **Add Integration** → **Webhook API**
2. Fill in bot name and set the **Webhook URL** to your public URL:
   `https://your-public-url/eko/webhook`
3. Save — you'll receive a **Client ID** and **Client Secret**

### 2. Configure Hermes

Add to `~/.hermes/.env`:

```bash
# Required
EKO_BASE_URL=https://customer-h1.ekoapp.com
EKO_OAUTH_CLIENT_ID=your_client_id
EKO_OAUTH_CLIENT_SECRET=your_client_secret

# Optional
EKO_WEBHOOK_SECRET=your_webhook_signing_secret

# Recommended
EKO_ALLOWED_USERS=your_eko_user_id
EKO_HOME_CHANNEL=your_eko_user_id

# Optional (defaults shown)
# EKO_PORT=8647
# EKO_HOST=0.0.0.0
# EKO_WEBHOOK_PATH=/eko/webhook
# EKO_REPLY_TOKEN_TTL=50
# EKO_ALLOW_ALL_USERS=false
```

Or configure via `~/.hermes/config.yaml`:

```yaml
gateway:
  platforms:
    eko:
      extra:
        base_url: "https://customer-h1.ekoapp.com"
        oauth_client_id: "your_client_id"
        oauth_client_secret: "your_client_secret"
        allowed_users:
          - "your_eko_user_id"
```

Config precedence: env vars > config.yaml > defaults.

### 3. Start the gateway

```bash
hermes gateway
```

Verify in logs:

```
Eko: webhook listening on 0.0.0.0:8647/eko/webhook
✓ eko connected
```

Test the health endpoint:

```bash
curl http://localhost:8647/eko/webhook/health
# {"status": "ok", "platform": "eko"}
```

### 4. Expose the webhook

The adapter listens on `0.0.0.0:8647` by default. Eko needs HTTPS:

```bash
# Quick dev setup
ngrok http 8647
```

Update the webhook URL in the Eko admin panel to the ngrok URL.

### 5. Chat

Open Eko, create a 1:1 chat with the bot, and send a message.

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `EKO_BASE_URL` | Yes | — | Eko server base URL |
| `EKO_OAUTH_CLIENT_ID` | Yes | — | Bot OAuth client ID |
| `EKO_OAUTH_CLIENT_SECRET` | Yes | — | Bot OAuth client secret |
| `EKO_WEBHOOK_SECRET` | No | (OAuth secret) | Separate webhook HMAC signing key |
| `EKO_PORT` | No | `8647` | Webhook listen port |
| `EKO_HOST` | No | `0.0.0.0` | Webhook bind host |
| `EKO_WEBHOOK_PATH` | No | `/eko/webhook` | Webhook endpoint path |
| `EKO_ALLOWED_USERS` | No | (empty) | Comma-separated Eko user IDs |
| `EKO_ALLOW_ALL_USERS` | No | `false` | Allow any user (dev only) |
| `EKO_HOME_CHANNEL` | No | (empty) | Default user ID for cron delivery |
| `EKO_REPLY_TOKEN_TTL` | No | `50` | Reply-token TTL in seconds |
| `EKO_MESSAGE_MAX_CHARS` | No | `5000` | Max chars per outbound message (chunks longer text) |

## Agent Tools

Three management tools are registered when the Eko adapter is connected.
Enable the `eko` toolset for your platform to make them available:

```
hermes tools
```

Or in `config.yaml`:

```yaml
tools:
  eko:
    enabled:
      - eko
```

| Tool | Description |
|------|-------------|
| `eko_create_group` | Create a group chat. Accepts `member_usernames` (auto-resolved to IDs) or `member_uids`. Optional `name`. |
| `eko_create_topic` | Create a topic in an existing group by `group_id` and `name`. |
| `eko_query_users` | Look up users by `username`. Returns `_id`, `username`, `email`. |

These tools are **async** and gated on the Eko adapter being connected in the gateway.
They disappear from the tool list when the gateway isn't running.

### Example usage in chat

```
User: look up alice on eko
Agent: [calls eko_query_users(username="alice")]

User: create a group with alice and bob called Project Alpha
Agent: [calls eko_create_group(member_usernames=["alice", "bob"], name="Project Alpha")]

User: create a topic called Weekly Sync in that group
Agent: [calls eko_create_topic(group_id="grp_123", name="Weekly Sync")]
```

## Management API

The `_EkoClient` exposes low-level management methods. The agent tools above wrap these.

| Method | Endpoint | Description |
|--------|----------|-------------|
| `create_group(uids, name)` | `POST /bot/v1/groups` | Create a group chat with member uids (multipart) |
| `create_topic(gid, name)` | `POST /bot/v1/groups/{gid}/topics` | Create a topic in a group (JSON) |
| `query_users(username)` | `GET /bot/v1/users?username=...` | Look up users by username |

All methods follow the standard pattern: Bearer auth via `ensure_token()`, 401 → clear token → raise `_EkoAuthError`, other errors → `RuntimeError`.

## Architecture

```
Eko Server ──webhook──► aiohttp (/eko/webhook) ──► EkoAdapter
                                                       │
                                          handle_message(event)
                                                       │
                                                       ▼
                                                  Hermes Agent
                                                       │
                                                  response text
                                                       │
                                                  EkoAdapter.send()
                                                       │
                                          1. reply token (if fresh)
                                          2. push to DM (/bot/v1/direct/*)
                                             or group (/bot/v1/group/*)
```

Outbound messages prefer the reply token endpoint (single-use, ~60s TTL).
When the token is absent or expired, the adapter falls back to the push API.

### Session Routing

Each Eko conversation (DM or topic) gets its own Hermes session. The adapter
uses the webhook `sessionId` (`{groupId}_{topicId}`) as the `chat_id`. Routing
metadata (uid, groupId, topicId) is stored so outbound push calls can resolve
the correct endpoint.

**Outbound routing** is determined by the presence of `groupId` + `topicId` —
not by `groupType`. Eko sets `groupType: "direct_chat"` even for topics inside
DM-type groups, so any conversation with both `groupId` and `topicId` uses the
`/bot/v1/group/*` endpoints. Bare uid routing (no groupId/topicId) falls back
to `/bot/v1/direct/*`.

| Eko source | chat_id | chat_type |
|------------|---------|----------|
| DM (main topic) | `{groupId}_{topicId}` | `dm` |
| DM (new topic) | `{groupId}_{topicId}` | `dm` |
| Group chat | `{groupId}_{topicId}` | `group` |

## Version History

### v1.5.0

- **Agent tools for group/topic management.** `eko_create_group`, `eko_create_topic`, `eko_query_users` registered as async Hermes tools (Issue #17). Gated on Eko adapter being connected. `eko_create_group` accepts usernames (auto-resolves via `eko_query_users`) or raw user IDs.
- 16 new tool tests (131 total, up from 115).

### v1.4.0

- **Management API methods on `_EkoClient`.** Added `create_group(uids, name)`, `create_topic(gid, name)`, and `query_users(username)` for programmatic group/topic creation and user lookup (Issue #16).
- 10 new tests (115 total, up from 105).

### v1.3.1

- **Fixed document/file routing to topics.** Eko sets `groupType: "direct_chat"` even for
  topics inside DM-type groups. Routing now checks for `groupId`+`topicId` presence instead of
  `groupType`, so all topic-bound messages (text, images, files) correctly use `/bot/v1/group/*`
  endpoints regardless of the group's type classification.
- `_standalone_send` (cron path) now supports group/topic routing via live adapter lookup.
- Debug logging for `send_document` routing decisions.

### v1.3.0

- Outbound image/file sending: native multipart upload with correct MIME types
- Cron media attachments: `_standalone_send()` now sends images and files
- `send_message` MEDIA support: Eko added to platform allowlist for `MEDIA:<path>` delivery
- Group/topic outbound routing: auto-detects DM vs group, routes to `/bot/v1/group/*` or `/bot/v1/direct/*`
- Session context remap: media sends from topics resolve the correct chat_id
- Platform hint updated with `MEDIA:<path>` syntax

### v1.2.0

- Topic/session routing: each Eko topic gets its own Hermes session
- Group message `chat_type` derived from `groupType` field
- Push fallback resolves user uid from session routing metadata
- Reply tokens stashed per session (not per user)

### v1.1.0

- Webhook signature verification via `X-Eko-Signature` (HMAC-SHA256-Base64)
- Image receiving: download inbound pictures, cache locally, vision tool integration
- Image sending: native multipart upload with reply token + push fallback
- File sending: push files to users via multipart upload
- Sticker webhook events: surface `[sticker]` placeholder

### v1.0.0

- 1:1 direct message text support
- OAuth2 client-credentials authentication
- Reply token with push fallback
- Event deduplication
- User allowlist
- Cron/notification delivery via `EKO_HOME_CHANNEL`
- Interactive setup wizard

## Roadmap

### High priority

| Feature | Description | Eko API |
|---------|-------------|--------|
| Group allowlist | Allow/deny specific groups (currently only user-level allowlist) | Needs testing |

### Medium priority

| Feature | Description | Notes |
|---------|-------------|-------|
| `require_mention` config | Bot only responds when @mentioned in group chats (DMs always respond) | Issue #22 |
| Quick reply buttons | Tap-to-respond options for users | Eko supports it via `/bot/v1/message/quickreply` |

### Low priority

| Feature | Description | Notes |
|---------|-------------|-------|
| Management actions config gate | `eko.management_actions` allowlist to control which tools are available | Issue #23 |
| Connection pooling | Reuse `aiohttp.ClientSession` across requests | Current pattern creates one per request (matches LINE adapter) |
| Typing indicator | Show agent-is-working feedback | Eko may not have a typing API — needs investigation |
| Markdown formatting | ~~Test if Eko renders any formatting~~ **Done — plain text only.** `format_message()` strips Markdown. | Bare URLs auto-linked by client |

## Design Decisions

See `docs/adr/` for architectural decision records:

- **ADR-0001** — Eko does not auto-create topics on new sessions. Users create topics manually; each gets its own Hermes session via `sessionId`.
- **ADR-0002** — Management tools are 3 separate tools (`eko_create_group`, `eko_create_topic`, `eko_query_users`), not a single action-dispatch tool. Separate tools give focused schemas and self-documenting names. Discord's single-tool pattern exists because it has 20+ actions.

## Maintenance

- [ ] Run test suite: `scripts/run_tests.sh tests/gateway/test_eko_plugin.py`
- [ ] Tune reply token TTL (current default: 50s — verify against actual Eko TTL)
- [ ] Test OAuth token TTL handling (refresh before expiry)
