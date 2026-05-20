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
- **No webhook signature verification** — Eko API doesn't document one. Added comment in code.
- **No slow-LLM postback** — Eko has no template button API equivalent (quick reply exists but deferred)
- **401 handling** — dedicated `_EkoAuthError` exception class, not string matching
- **`_bot_user_id`** — reserved but never set (Eko doesn't provide a bot identity API)

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
2. **Group chat support** — Eko API supports it
3. **Webhook signature verification** — check with Eko support
4. Run `pytest tests/gateway/test_eko_plugin.py` to validate tests
5. Tune reply token TTL

## Eko API Quirks

- OAuth token endpoint: **form-urlencoded**, not JSON
- Webhook events have no event ID (we hash the full JSON for dedup)
- Source can use `userId` or `uid` depending on event type
- Reply endpoint uses multipart/form-data
- Push endpoint uses JSON
