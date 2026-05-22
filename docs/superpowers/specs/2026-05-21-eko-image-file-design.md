# Eko Image/File Support Design

**Date:** 2026-05-21
**Status:** Approved

## Overview

Add image and file support to the Eko adapter. Covers inbound image receiving
(with vision tool integration) and outbound image/file sending. Based on
live-captured webhook payloads and API testing.

## What Eko Actually Supports (live-confirmed)

| User sends | Webhook? | `message.type` | Key fields |
|---|---|---|---|
| Text | ✅ | `"text"` | `text` |
| Image | ✅ | `"picture"` | `pictureId`, `fileName` |
| Sticker | ✅ | `"sticker"` | `packageId`, `stickerId` (no download URL) |
| File | ❌ | — | No webhook event — Eko silently drops it |

## Inbound: Image Receiving

### Download

Eko doesn't include a URL in the webhook. Images are downloaded via:

```
GET {base_url}/file/view/{pictureId}?size=large
Authorization: Bearer {oauth_token}
```

- `?size=large` returns the original file; without it, returns a thumbnail.
- Requires Bearer auth (same OAuth token as other API calls).
- Response is raw image bytes.

### Flow

1. `_handle_message_event` sees `message.type == "picture"`.
2. Extract `pictureId` and `fileName`.
3. Call `_download_picture(picture_id)` → `GET /file/view/{id}?size=large` with auth.
4. Cache via `cache_image_from_bytes(data, ext)` from the base adapter.
5. Derive extension from `fileName` (fallback `.jpg`).
6. Set `message_type = MessageType.PHOTO`, `media_urls = [cached_path]`,
   `media_types = [mime_type]`.
7. Gateway runner handles the rest (vision tool routing).

### Stickers

Stickers have `packageId`/`stickerId` but no `pictureId` and no documented
download API. Surface as `[sticker]` placeholder text — same as current behavior
for unsupported types.

### Files

Not sent by Eko's webhook API. No inbound handling possible.

## Outbound: Image and File Sending

Override `send_image_file` and `send_document` on the adapter. Follow existing
Hermes pattern: reply token if fresh, push fallback.

### Endpoints

| Action | Endpoint | Content-Type | Params |
|---|---|---|---|
| Reply picture | `POST /bot/v1/message/picture` | multipart/form-data | `file`, `replyToken` |
| Push picture to user | `POST /bot/v1/direct/picture` | multipart/form-data | `uid`, `caption`, `file` |
| Push file to user | `POST /bot/v1/direct/file` | multipart/form-data | `uid`, `file` |

### send_image_file(chat_id, image_path, caption, reply_to)

1. Consume reply token if available.
2. If reply token: POST to `/bot/v1/message/picture` with `file` + `replyToken`.
3. Else: POST to `/bot/v1/direct/picture` with `uid` + `caption` + `file`.
4. On 401: clear token, retry once (same as text send).

### send_image(chat_id, image_url, caption, reply_to)

1. Download image from URL to temp file.
2. Delegate to `send_image_file`.

### send_document(chat_id, file_path, caption, file_name)

1. POST to `/bot/v1/direct/file` with `uid` + `file`.
2. No reply-token equivalent documented for files — always push.

## Changes to _EkoClient

Add three new methods:

- `fetch_picture(picture_id) -> bytes` — download inbound image.
- `push_picture(uid, file_bytes, filename, caption)` — send image to user.
- `reply_picture(reply_token, file_bytes, filename)` — reply with image.
- `push_file(uid, file_bytes, filename)` — send file to user.

All use `aiohttp.FormData` for multipart uploads, same pattern as existing
`reply_text`.

## Changes to EkoAdapter._handle_message_event

Extend the message type switch:

```python
if msg_type == "text":
    text = msg.get("text", "")
elif msg_type == "picture":
    # Download, cache, set media_urls
elif msg_type == "sticker":
    text = "[sticker]"
elif msg_type == "file":
    text = "[file]"
else:
    text = f"[unsupported: {msg_type}]"
```

## Config

No new environment variables needed. Uses existing `EKO_BASE_URL` and OAuth
credentials.

## Error Handling

- **Download failure** (network, 404): log warning, fall back to `[image]`
  text placeholder — don't drop the message entirely.
- **Upload failure**: return `SendResult(success=False, retryable=True)`,
  same pattern as text send.
- **Large files**: rely on `WEBHOOK_BODY_MAX_BYTES` for inbound; outbound
  limited by aiohttp defaults.

## Out of Scope

- Sticker downloading/sending (no API).
- File receiving (Eko doesn't send file webhooks).
- Group chat image/file (group support is a separate feature).
- Video/audio (no Eko webhook events for these).
