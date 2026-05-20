# Eko Adapter — Session Notes

## Current State

**Working:** v1.0.0 live at `S2P2/hermes-agent` — bidirectional text chat with Eko works.

**Tested and confirmed working:**
- Webhook receives messages from Eko
- OAuth2 auth (form-urlencoded, not JSON)
- Reply token → push fallback
- User allowlist (`EKO_ALLOWED_USERS`)
- Home channel (`EKO_HOME_CHANNEL`)
- Gateway restart notifications

**Bug fixed during testing:**
- Eko OAuth endpoint requires `application/x-www-form-urlencoded`, not JSON.
  The original adapter sent `json=payload`, changed to `data=aiohttp.FormData()`.

## Key Decisions

- **Plugin form** (not built-in) — lives at `plugins/platforms/eko/`, zero core code changes
- **Configurable base URL** — Eko uses customer-specific hostnames (e.g. `customer-h1.ekoapp.com`)
- **Reply token TTL** — defaulted to 50s (conservative), needs tuning after testing with real Eko
- **Webhook signature verification** — **Done (branch: `feat/eko-webhook-signature`)**. Header is `X-Eko-Signature` (not `x-amity-signature` — that's Amity Social Cloud, a different product). Algorithm: HMAC-SHA256 of raw body, Base64-encoded, keyed by the OAuth client secret. Live-confirmed 2026-05-21.
- **No slow-LLM postback** — Eko has no template button API equivalent (quick reply exists but deferred)
- **401 handling** — dedicated `_EkoAuthError` exception class, not string matching
- **`_bot_user_id`** — can now be populated from `meta.botId` in webhook events (live-confirmed 2026-05-21)

## Repo Setup

```
origin   → https://github.com/S2P2/hermes-agent.git (your fork)
upstream → https://github.com/NousResearch/hermes-agent.git
Default PR repo → S2P2/hermes-agent
```

## Files Created

| File | Purpose |
|------|---------|
| `plugins/platforms/eko/plugin.yaml` | Plugin manifest with env var declarations |
| `plugins/platforms/eko/__init__.py` | Entry point |
| `plugins/platforms/eko/adapter.py` | Full adapter (732 lines) |
| `plugins/platforms/eko/README.md` | Setup guide + roadmap |
| `tests/gateway/test_eko_plugin.py` | Tests (339 lines) |
| `docs/superpowers/specs/2026-05-20-eko-messaging-design.md` | Design spec |
| `docs/superpowers/plans/2026-05-20-eko-messaging.md` | Implementation plan |

## Next Steps (see README roadmap)

1. **Image/file receiving + sending** — biggest v2 feature
2. **Group chat support** — Eko API supports it; real payload has `groupType`, `groupId`, `topicId`
3. ~~**Webhook signature verification**~~ **Done (branch: `feat/eko-webhook-signature`)**
4. Run `pytest tests/gateway/test_eko_plugin.py` to validate tests
5. Tune reply token TTL
6. **Populate `_bot_user_id`** from `meta.botId` — enables self-message filtering
7. **Use `sessionId`** for Hermes session grouping (format: `{groupId}_{topicId}`)

## Eko API Quirks

- OAuth token endpoint: **form-urlencoded**, not JSON
- Webhook events have no event ID (we hash the full JSON for dedup)
- Source can use `userId` or `uid` depending on event type
- Reply endpoint uses multipart/form-data
- Push endpoint uses JSON
- **Webhook signature**: `X-Eko-Signature` header, HMAC-SHA256-Base64, key = OAuth client secret (live-confirmed)
- **Webhook user-agent**: `axios/0.19.2` — useful for identifying Eko traffic in proxy logs

## Real Webhook Payload — Undocumented Fields (live-captured 2026-05-21)

| Field | Path | Notes |
|-------|------|-------|
| `source.email` | event.source | Always present, may be empty |
| `source.profile` | event.source | User profile fields (FullName, Division, Department, TH names) |
| `meta` | event | Contains `botId`, `networkId`, `clientId`, `userId` (bot operator) |
| `meta.deep_research` | event.meta | Boolean flag — purpose unknown, always `false` so far |
| `message.groupId` | event.message | Chat/conversation ID — useful for group chat routing |
| `message.groupType` | event.message | `"direct_chat"` for DMs, likely `"group_chat"` for groups |
| `message.topicId` | event.message | Topic (thread) within the group |
| `sessionId` | event | Composite `{groupId}_{topicId}` — natural Hermes session key |
