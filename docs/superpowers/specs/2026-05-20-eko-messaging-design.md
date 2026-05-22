# Eko Messaging Platform Adapter for Hermes Agent

**Date:** 2026-05-20
**Status:** Approved

## Overview

A Hermes gateway platform plugin that connects to the Eko Messaging API via
webhooks, enabling bidirectional text chat between Eko users and the Hermes
agent. Modeled on the existing LINE adapter plugin.

## Architecture

```
┌─────────────┐  webhook POST   ┌──────────────────┐
│  Eko Server  │ ──────────────► │  aiohttp server   │
│              │ ◄────────────── │  (/eko/webhook)   │
└─────────────┘  200 OK          └────────┬─────────┘
                                           │ parse event
                                           ▼
                                   ┌───────────────┐
                                   │  EkoAdapter    │
                                   │                │
                                   │ - dedup events │
                                   │ - build source │
                                   │ - allowlist    │
                                   └───────┬───────┘
                                           │ handle_message()
                                           ▼
                                   ┌───────────────┐
                                   │  Hermes Agent  │
                                   └───────┬───────┘
                                           │ response text
                                           ▼
                                   ┌───────────────┐
                                   │  EkoAdapter    │
                                   │  .send()       │
                                   │                │
                                   │ 1. reply token │
                                   │    (if fresh)  │
                                   │ 2. push fallback│
                                   └───────────────┘
```

### Inbound flow

1. Eko sends webhook POST with `{events: [{type, replyToken, source, message, timestamp}]}`.
2. aiohttp handler parses JSON, dispatches events individually.
3. Dedup by event content hash (Eko docs don't show a webhook event ID).
4. Allowlist check on `source.uid`.
5. Build `SessionSource` + `MessageEvent`, call `self.handle_message(event)`.

### Outbound flow

1. `send(chat_id, content)` called by gateway.
2. Check for cached reply token — if present and <50 s old, use reply
   endpoint (`/bot/v1/message/text` with `replyToken`).
3. Otherwise push via `/bot/v1/direct/message` with `uid`.
4. Text sent as-is.

### OAuth2 client

- Inner class `_EkoClient` holds access token + expiry.
- `ensure_token()` checks expiry, fetches new via client-credentials if
  expired or missing.
- All API calls go through `_EkoClient` which auto-attaches `Bearer` header.
- On 401 response, clears token and retries once.

## File structure

```
plugins/platforms/eko/
├── __init__.py          # exports register()
├── adapter.py           # EkoAdapter class + all hooks
└── plugin.yaml          # plugin metadata, env vars
```

## Configuration

### Required environment variables

| Variable | Description |
|----------|-------------|
| `EKO_BASE_URL` | Eko server base URL (e.g. `https://customer-h1.ekoapp.com`) |
| `EKO_OAUTH_CLIENT_ID` | Bot OAuth client ID from Eko admin panel |
| `EKO_OAUTH_CLIENT_SECRET` | Bot OAuth client secret |

### Optional environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `EKO_PORT` | `8647` | Webhook listen port |
| `EKO_HOST` | `0.0.0.0` | Webhook bind host |
| `EKO_WEBHOOK_PATH` | `/eko/webhook` | Webhook endpoint path |
| `EKO_ALLOWED_USERS` | (empty) | Comma-separated Eko user IDs |
| `EKO_ALLOW_ALL_USERS` | `false` | Dev-only bypass |
| `EKO_HOME_CHANNEL` | (empty) | Default user ID for cron delivery |
| `EKO_REPLY_TOKEN_TTL` | `50` | Reply-token TTL in seconds |

Config precedence: env vars > `config.yaml` `platforms.eko.extra` > defaults.

## EkoAdapter class

### Public methods

| Method | Behavior |
|--------|----------|
| `connect()` | Validate config, OAuth handshake, start aiohttp webhook server |
| `disconnect()` | Stop webhook server, cancel tasks, cleanup |
| `send(chat_id, content)` | Reply token if fresh, else push |
| `send_typing(chat_id)` | No-op (no Eko typing API documented) |
| `get_chat_info(chat_id)` | Return `{name: chat_id, type: "dm"}` |
| `format_message(content)` | Identity — pass through as-is |

### Internal helpers

| Helper | Purpose |
|--------|---------|
| `_EkoClient` | OAuth token management + HTTP with auto-retry on 401 |
| `_handle_webhook(request)` | Parse body, dispatch events |
| `_dispatch_event(event)` | Route join/message events, dedup, allowlist |
| `_handle_message_event(event)` | Extract text, build `MessageEvent` |
| `_consume_reply_token(chat_id)` | Return stashed token if < TTL |
| `check_requirements()` | Gate: 3 required env vars + aiohttp |

### Reply token stash

`_reply_tokens: Dict[str, Tuple[str, float]]` keyed by `chat_id`. Consumed on
first outbound send. Expired after configurable TTL (default 50 s).

## Plugin registration

```python
ctx.register_platform(
    name="eko",
    label="Eko",
    adapter_factory=lambda cfg: EkoAdapter(cfg),
    check_fn=check_requirements,
    required_env=["EKO_BASE_URL", "EKO_OAUTH_CLIENT_ID", "EKO_OAUTH_CLIENT_SECRET"],
    cron_deliver_env_var="EKO_HOME_CHANNEL",
    standalone_sender_fn=_standalone_send,
    allowed_users_env="EKO_ALLOWED_USERS",
    allow_all_env="EKO_ALLOW_ALL_USERS",
    platform_hint="...",
    emoji="💬",
)
```

## Error handling

- **Webhook:** body cap 1 MiB, invalid JSON → 400, per-event catch+log,
  always return 200 OK to Eko quickly.
- **OAuth:** on 401, clear cached token, re-authenticate, retry once.
- **5xx / network:** return `SendResult(success=False, retryable=True)` —
  base adapter retries automatically.
- **Circuit breaker:** inherited from `BasePlatformAdapter`.
- **Bind failure:** set fatal error (retryable).

## Platform hint

```
You are chatting via Eko Messaging API. Messages are plain text.
Keep responses concise and well-structured.
```

## Out of scope for v1

| Feature | Reason |
|---------|--------|
| Group chat support | MVP is 1:1 DMs only |
| Image/file sending | Designed for, implemented later |
| Image/file receiving | Depends on Eko content download API |
| Quick reply buttons | Eko supports it, not needed yet |
| Slow-LLM postback | No Eko template-button API |
| Webhook signature verification | No Eko signature header documented |
| Voice/audio/video | No Eko API documented |

## Eko API reference

### Endpoints used

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `{base}/oauth/token` | POST | Client-credentials OAuth2 |
| `{base}/bot/v1/message/text` | POST | Reply using replyToken |
| `{base}/bot/v1/direct/message` | POST | Push message to user by uid |
| Webhook (inbound) | POST | Eko sends events to our server |

### Webhook event: join

```json
{
  "events": [{
    "replyToken": "...",
    "type": "join",
    "source": {"type": "direct_chat", "uid": "5d8af2ff..."},
    "timestamp": 1569563054859
  }]
}
```

### Webhook event: message

```json
{
  "events": [{
    "replyToken": "8350939a...",
    "type": "message",
    "source": {
      "type": "user",
      "userId": "5ac20cd3...",
      "username": "alice"
    },
    "message": {
      "id": "5bcaa505...",
      "type": "text",
      "text": "hello"
    },
    "timestamp": "2018-10-19T03:46:07.866Z"
  }]
}
```

### Reply with text

```
POST {base}/bot/v1/message/text
Content-Type: multipart/form-data
Authorization: Bearer {access_token}

message=Hello There
replyToken=8350939a...
```

### Push text to user

```
POST {base}/bot/v1/direct/message
Content-Type: application/json
Authorization: Bearer {access_token}

{"uid": "5d8af2ff...", "message": {"type": "text", "data": "hi"}}
```
