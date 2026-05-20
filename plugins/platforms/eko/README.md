# Eko Messaging Adapter

Connects [Hermes Agent](https://hermes-agent.nousresearch.com/) to the
[Eko Messaging API](https://eko.gitbook.io/api/messaging-api/) via webhooks,
enabling bidirectional text chat between Eko users and the Hermes agent.

## Features

- 1:1 direct message support
- OAuth2 client-credentials authentication with proactive token refresh
- Reply token (free) with automatic push fallback
- Event deduplication
- User allowlist gating
- Cron/notification delivery
- Interactive setup wizard (`hermes gateway setup`)

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
| `EKO_PORT` | No | `8647` | Webhook listen port |
| `EKO_HOST` | No | `0.0.0.0` | Webhook bind host |
| `EKO_WEBHOOK_PATH` | No | `/eko/webhook` | Webhook endpoint path |
| `EKO_ALLOWED_USERS` | No | (empty) | Comma-separated Eko user IDs |
| `EKO_ALLOW_ALL_USERS` | No | `false` | Allow any user (dev only) |
| `EKO_HOME_CHANNEL` | No | (empty) | Default user ID for cron delivery |
| `EKO_REPLY_TOKEN_TTL` | No | `50` | Reply-token TTL in seconds |

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
                                          2. push fallback
```

Outbound messages prefer the reply token endpoint (single-use, ~60s TTL).
When the token is absent or expired, the adapter falls back to the push API.

## Version History

### v1.0.0

- 1:1 direct message text support
- OAuth2 client-credentials authentication
- Reply token with push fallback
- Event deduplication
- User allowlist
- Cron/notification delivery via `EKO_HOME_CHANNEL`
- Interactive setup wizard

## Not Yet Supported

- Group chat messaging
- Image/file sending and receiving
- Quick reply buttons
- Webhook signature verification (Eko API does not document one)
- Typing indicators
