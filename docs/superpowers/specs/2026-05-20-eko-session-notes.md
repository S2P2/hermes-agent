# Eko Adapter — Session Notes

## Current State

**Working:** v1.1.0 live at `S2P2/hermes-agent` — bidirectional text chat + image support with Eko.

**Tested and confirmed working:**
- Webhook receives messages from Eko
- OAuth2 auth (form-urlencoded, not JSON)
- Reply token → push fallback
- User allowlist (`EKO_ALLOWED_USERS`)
- Home channel (`EKO_HOME_CHANNEL`)
- Gateway restart notifications
- Webhook signature verification (`X-Eko-Signature` HMAC-SHA256-Base64)
- **Inbound image receiving** — download, cache, vision model interpretation ✅

**Known issues:**
- **Outbound image/file sending not working** — `send_image_file`/`send_image`/`send_document` are implemented and tested in unit tests, but fail in live testing. Needs investigation:
  - Check gateway logs for error messages from `push_picture`/`push_file`
  - Verify the multipart upload format matches what Eko expects
  - Check if file size limits or content-type headers are causing rejections
  - May need to test the exact curl equivalent against the real Eko API
  - Platform hint updated but LLM may not be triggering the `MEDIA:` path correctly

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

1. ~~**Image/file receiving + sending**~~ **Inbound done; outbound needs debugging** — inbound pictures work (vision model interprets them). Outbound `send_image_file`/`send_document` fail in live testing — needs investigation (see Known Issues above).
2. **Group chat support** — Eko API supports it; real payload has `groupType`, `groupId`, `topicId`
3. ~~**Webhook signature verification**~~ **Done (merged to main)**
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

## Eko Media — Live Testing Results (2026-05-21)

**Inbound image:** ✅ Working
- `message.type == "picture"` in webhook event
- Download: `GET {base}/file/view/{pictureId}?size=large` with Bearer auth
- Cache via `cache_image_from_bytes`, vision model interprets correctly

**Inbound file:** ❌ No webhook event sent by Eko
**Inbound sticker:** Webhook received with `packageId`/`stickerId`, no download API — `[sticker]` placeholder

**Outbound image:** ❌ Not working in live test
- Endpoints: `/bot/v1/direct/picture` (push), `/bot/v1/message/picture` (reply)
- Multipart upload with `file`, `uid`, `caption` fields
- Unit tests pass but live Eko rejects or silently drops
- Needs: check gateway logs, test raw curl, verify multipart format

**Outbound file:** ❌ Not tested yet (same issue expected)
- Endpoint: `/bot/v1/direct/file`

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
