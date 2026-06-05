# Eko Image/File Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add inbound image receiving (with vision tool integration) and outbound image/file sending to the Eko adapter.

**Architecture:** Extend `_EkoClient` with media download/upload methods. Extend `_handle_message_event` to download and cache inbound pictures. Override `send_image`, `send_image_file`, and `send_document` on `EkoAdapter` for native outbound delivery. Follow existing LINE adapter patterns.

**Tech Stack:** Python 3.13, aiohttp (HTTP client), Hermes base adapter cache utilities (`cache_image_from_bytes`, `cache_document_from_bytes`)

---

## Task 1: Add `fetch_picture` to `_EkoClient` — download inbound images

**Files:**
- Modify: `plugins/platforms/eko/adapter.py` (inside `_EkoClient` class, after `push_text`)

- [ ] **Step 1: Write the failing test**

Add to `tests/gateway/test_eko_plugin.py`:

```python
class TestFetchPicture:

    @pytest.mark.asyncio
    async def test_fetch_picture_returns_bytes(self):
        client = _EkoClient.__new__(_EkoClient)
        client._base_url = "https://test.ekoapp.com"
        client._client_id = "id"
        client._client_secret = "secret"
        client._timeout = 15.0
        client._access_token = "test_token"
        client._token_expires_at = time.time() + 3600

        fake_image = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100

        with patch("aiohttp.ClientSession") as mock_session_cls:
            mock_resp = AsyncMock()
            mock_resp.status = 200
            mock_resp.read = AsyncMock(return_value=fake_image)
            mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
            mock_resp.__aexit__ = AsyncMock(return_value=False)

            mock_session = AsyncMock()
            mock_session.get.return_value = mock_resp
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = mock_session

            result = await client.fetch_picture("pic123")
            assert result == fake_image

    @pytest.mark.asyncio
    async def test_fetch_picture_401_raises_auth_error(self):
        client = _EkoClient.__new__(_EkoClient)
        client._base_url = "https://test.ekoapp.com"
        client._client_id = "id"
        client._client_secret = "secret"
        client._timeout = 15.0
        client._access_token = "expired_token"
        client._token_expires_at = time.time() + 3600

        with patch("aiohttp.ClientSession") as mock_session_cls:
            mock_resp = AsyncMock()
            mock_resp.status = 401
            mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
            mock_resp.__aexit__ = AsyncMock(return_value=False)

            mock_session = AsyncMock()
            mock_session.get.return_value = mock_resp
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = mock_session

            with pytest.raises(_EkoAuthError):
                await client.fetch_picture("pic123")
            assert client._access_token is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/jo/hermes-sivwork/hermes-agent && source /tmp/eko-test-venv/bin/activate && python -m pytest tests/gateway/test_eko_plugin.py::TestFetchPicture -v`
Expected: FAIL — `AttributeError: '_EkoClient' object has no attribute 'fetch_picture'`

- [ ] **Step 3: Write the implementation**

Add after the `push_text` method in `_EkoClient` class (after line ~223 in `plugins/platforms/eko/adapter.py`):

```python

    async def fetch_picture(self, picture_id: str) -> bytes:
        """Download an inbound picture by its pictureId.

        Uses ``GET /file/view/{pictureId}?size=large`` with Bearer auth.
        Returns the raw image bytes.
        """
        import aiohttp

        token = await self.ensure_token()
        url = f"{self._base_url}/file/view/{picture_id}?size=large"
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(
                url, headers=self._auth_headers(token)
            ) as resp:
                if resp.status == 401:
                    self.clear_token()
                    raise _EkoAuthError(
                        "Eko picture download returned 401 Unauthorized"
                    )
                if resp.status >= 400:
                    body = await resp.text()
                    raise RuntimeError(
                        f"Eko picture download failed ({resp.status}): {body[:200]}"
                    )
                return await resp.read()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/jo/hermes-sivwork/hermes-agent && source /tmp/eko-test-venv/bin/activate && python -m pytest tests/gateway/test_eko_plugin.py::TestFetchPicture -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add plugins/platforms/eko/adapter.py tests/gateway/test_eko_plugin.py
git commit -m "feat(eko): add _EkoClient.fetch_picture for inbound image download"
```

---

## Task 2: Add `push_picture`, `reply_picture`, and `push_file` to `_EkoClient`

**Files:**
- Modify: `plugins/platforms/eko/adapter.py` (inside `_EkoClient` class, after `fetch_picture`)
- Modify: `tests/gateway/test_eko_plugin.py` (add test class)

- [ ] **Step 1: Write the failing tests**

Add to `tests/gateway/test_eko_plugin.py`:

```python
class TestEkoClientOutboundMedia:

    @pytest.mark.asyncio
    async def test_push_picture_sends_multipart(self):
        client = _EkoClient.__new__(_EkoClient)
        client._base_url = "https://test.ekoapp.com"
        client._client_id = "id"
        client._client_secret = "secret"
        client._timeout = 15.0
        client._access_token = "test_token"
        client._token_expires_at = time.time() + 3600

        with patch("aiohttp.ClientSession") as mock_session_cls:
            mock_resp = AsyncMock()
            mock_resp.status = 200
            mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
            mock_resp.__aexit__ = AsyncMock(return_value=False)

            mock_session = AsyncMock()
            mock_session.post.return_value = mock_resp
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = mock_session

            await client.push_picture(
                uid="user123",
                file_bytes=b"\x89PNG\r\n\x1a\n" + b"\x00" * 10,
                filename="test.png",
                caption="hello",
            )
            mock_session.post.assert_called_once()
            call_url = mock_session.post.call_args[0][0]
            assert call_url.endswith("/bot/v1/direct/picture")

    @pytest.mark.asyncio
    async def test_reply_picture_sends_multipart(self):
        client = _EkoClient.__new__(_EkoClient)
        client._base_url = "https://test.ekoapp.com"
        client._client_id = "id"
        client._client_secret = "secret"
        client._timeout = 15.0
        client._access_token = "test_token"
        client._token_expires_at = time.time() + 3600

        with patch("aiohttp.ClientSession") as mock_session_cls:
            mock_resp = AsyncMock()
            mock_resp.status = 200
            mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
            mock_resp.__aexit__ = AsyncMock(return_value=False)

            mock_session = AsyncMock()
            mock_session.post.return_value = mock_resp
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = mock_session

            await client.reply_picture(
                reply_token="tok_abc",
                file_bytes=b"\x89PNG\r\n\x1a\n" + b"\x00" * 10,
                filename="test.png",
            )
            mock_session.post.assert_called_once()
            call_url = mock_session.post.call_args[0][0]
            assert call_url.endswith("/bot/v1/message/picture")

    @pytest.mark.asyncio
    async def test_push_file_sends_multipart(self):
        client = _EkoClient.__new__(_EkoClient)
        client._base_url = "https://test.ekoapp.com"
        client._client_id = "id"
        client._client_secret = "secret"
        client._timeout = 15.0
        client._access_token = "test_token"
        client._token_expires_at = time.time() + 3600

        with patch("aiohttp.ClientSession") as mock_session_cls:
            mock_resp = AsyncMock()
            mock_resp.status = 200
            mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
            mock_resp.__aexit__ = AsyncMock(return_value=False)

            mock_session = AsyncMock()
            mock_session.post.return_value = mock_resp
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = mock_session

            await client.push_file(
                uid="user123",
                file_bytes=b"%PDF-1.4" + b"\x00" * 10,
                filename="report.pdf",
            )
            mock_session.post.assert_called_once()
            call_url = mock_session.post.call_args[0][0]
            assert call_url.endswith("/bot/v1/direct/file")

    @pytest.mark.asyncio
    async def test_push_picture_401_raises_auth_error(self):
        client = _EkoClient.__new__(_EkoClient)
        client._base_url = "https://test.ekoapp.com"
        client._client_id = "id"
        client._client_secret = "secret"
        client._timeout = 15.0
        client._access_token = "expired"
        client._token_expires_at = time.time() + 3600

        with patch("aiohttp.ClientSession") as mock_session_cls:
            mock_resp = AsyncMock()
            mock_resp.status = 401
            mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
            mock_resp.__aexit__ = AsyncMock(return_value=False)

            mock_session = AsyncMock()
            mock_session.post.return_value = mock_resp
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = mock_session

            with pytest.raises(_EkoAuthError):
                await client.push_picture(
                    uid="user123",
                    file_bytes=b"data",
                    filename="test.png",
                )
            assert client._access_token is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/jo/hermes-sivwork/hermes-agent && source /tmp/eko-test-venv/bin/activate && python -m pytest tests/gateway/test_eko_plugin.py::TestEkoClientOutboundMedia -v`
Expected: FAIL — `AttributeError` on missing methods

- [ ] **Step 3: Write the implementation**

Add after `fetch_picture` in `_EkoClient` class in `plugins/platforms/eko/adapter.py`:

```python

    async def push_picture(
        self,
        uid: str,
        file_bytes: bytes,
        filename: str,
        caption: str = "",
    ) -> None:
        """Push an image to a user by uid via multipart upload."""
        import aiohttp

        token = await self.ensure_token()
        url = f"{self._base_url}/bot/v1/direct/picture"
        data = aiohttp.FormData()
        data.add_field("uid", uid)
        if caption:
            data.add_field("caption", caption)
        data.add_field(
            "file", file_bytes, filename=filename, content_type="application/octet-stream"
        )
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.post(
                url, headers=self._auth_headers(token), data=data
            ) as resp:
                if resp.status == 401:
                    self.clear_token()
                    raise _EkoAuthError("Eko push picture returned 401 Unauthorized")
                if resp.status >= 400:
                    body = await resp.text()
                    raise RuntimeError(
                        f"Eko push picture failed ({resp.status}): {body[:200]}"
                    )

    async def reply_picture(
        self,
        reply_token: str,
        file_bytes: bytes,
        filename: str,
    ) -> None:
        """Reply with an image using a reply token via multipart upload."""
        import aiohttp

        token = await self.ensure_token()
        url = f"{self._base_url}/bot/v1/message/picture"
        data = aiohttp.FormData()
        data.add_field("replyToken", reply_token)
        data.add_field(
            "file", file_bytes, filename=filename, content_type="application/octet-stream"
        )
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.post(
                url, headers=self._auth_headers(token), data=data
            ) as resp:
                if resp.status == 401:
                    self.clear_token()
                    raise _EkoAuthError("Eko reply picture returned 401 Unauthorized")
                if resp.status >= 400:
                    body = await resp.text()
                    raise RuntimeError(
                        f"Eko reply picture failed ({resp.status}): {body[:200]}"
                    )

    async def push_file(
        self,
        uid: str,
        file_bytes: bytes,
        filename: str,
    ) -> None:
        """Push a file to a user by uid via multipart upload."""
        import aiohttp

        token = await self.ensure_token()
        url = f"{self._base_url}/bot/v1/direct/file"
        data = aiohttp.FormData()
        data.add_field("uid", uid)
        data.add_field(
            "file", file_bytes, filename=filename, content_type="application/octet-stream"
        )
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.post(
                url, headers=self._auth_headers(token), data=data
            ) as resp:
                if resp.status == 401:
                    self.clear_token()
                    raise _EkoAuthError("Eko push file returned 401 Unauthorized")
                if resp.status >= 400:
                    body = await resp.text()
                    raise RuntimeError(
                        f"Eko push file failed ({resp.status}): {body[:200]}"
                    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/jo/hermes-sivwork/hermes-agent && source /tmp/eko-test-venv/bin/activate && python -m pytest tests/gateway/test_eko_plugin.py::TestEkoClientOutboundMedia -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add plugins/platforms/eko/adapter.py tests/gateway/test_eko_plugin.py
git commit -m "feat(eko): add push_picture, reply_picture, push_file to _EkoClient"
```

---

## Task 3: Add inbound image download and caching to `_handle_message_event`

**Files:**
- Modify: `plugins/platforms/eko/adapter.py` (`_handle_message_event` + new `_download_picture` helper)
- Modify: `tests/gateway/test_eko_plugin.py` (add test class)

- [ ] **Step 1: Write the failing tests**

Add to `tests/gateway/test_eko_plugin.py`:

```python
class TestInboundPicture:

    @pytest.mark.asyncio
    async def test_picture_downloads_and_caches(self):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._client = MagicMock()
        fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        adapter._client.fetch_picture = AsyncMock(return_value=fake_png)
        adapter._reply_tokens = {}
        adapter._bot_user_id = None
        adapter.reply_token_ttl = 50
        adapter.handle_message = AsyncMock()

        event = {
            "replyToken": "tok123",
            "type": "message",
            "source": {"type": "user", "userId": "user123", "username": "alice"},
            "message": {
                "id": "msg123",
                "type": "picture",
                "pictureId": "pic456",
                "fileName": "photo.png",
                "groupId": "g1",
                "groupType": "direct_chat",
                "topicId": "t1",
            },
            "timestamp": "2026-05-21T00:00:00.000Z",
        }

        with patch("gateway.platforms.base.cache_image_from_bytes", return_value="/cache/img_abc.png") as mock_cache:
            await adapter._handle_message_event(event)

        adapter._client.fetch_picture.assert_called_once_with("pic456")
        mock_cache.assert_called_once_with(fake_png, ext=".png")

        call_args = adapter.handle_message.call_args[0][0]
        assert call_args.message_type == MessageType.PHOTO
        assert call_args.media_urls == ["/cache/img_abc.png"]
        assert "image/png" in call_args.media_types

    @pytest.mark.asyncio
    async def test_picture_download_failure_falls_back_to_placeholder(self):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._client = MagicMock()
        adapter._client.fetch_picture = AsyncMock(side_effect=RuntimeError("download failed"))
        adapter._reply_tokens = {}
        adapter._bot_user_id = None
        adapter.reply_token_ttl = 50
        adapter.handle_message = AsyncMock()

        event = {
            "replyToken": "tok123",
            "type": "message",
            "source": {"type": "user", "userId": "user123", "username": "alice"},
            "message": {
                "id": "msg123",
                "type": "picture",
                "pictureId": "pic456",
                "fileName": "photo.png",
            },
            "timestamp": "2026-05-21T00:00:00.000Z",
        }

        await adapter._handle_message_event(event)
        call_args = adapter.handle_message.call_args[0][0]
        assert call_args.text == "[image]"
        assert call_args.media_urls == []
        assert call_args.message_type == MessageType.TEXT

    @pytest.mark.asyncio
    async def test_sticker_surfaces_placeholder(self):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._reply_tokens = {}
        adapter._bot_user_id = None
        adapter.reply_token_ttl = 50
        adapter.handle_message = AsyncMock()

        event = {
            "replyToken": "tok123",
            "type": "message",
            "source": {"type": "user", "userId": "user123", "username": "alice"},
            "message": {
                "id": "msg_sticker",
                "type": "sticker",
                "packageId": "pkg1",
                "stickerId": "stk1",
            },
            "timestamp": "2026-05-21T00:00:00.000Z",
        }

        await adapter._handle_message_event(event)
        call_args = adapter.handle_message.call_args[0][0]
        assert call_args.text == "[sticker]"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/jo/hermes-sivwork/hermes-agent && source /tmp/eko-test-venv/bin/activate && python -m pytest tests/gateway/test_eko_plugin.py::TestInboundPicture -v`
Expected: FAIL — picture events produce `"[image]"` placeholder, not PHOTO type

- [ ] **Step 3: Write the implementation**

Replace the `_handle_message_event` method in `plugins/platforms/eko/adapter.py` and add a `_download_picture` helper after it:

```python
    async def _handle_message_event(self, event: Dict[str, Any]) -> None:
        msg = event.get("message") or {}
        msg_type = msg.get("type", "")
        message_id = msg.get("id", "")
        reply_token = event.get("replyToken", "")
        source = event.get("source") or {}

        uid = source.get("userId") or source.get("uid", "")
        username = source.get("username", "") or uid

        # Stash the reply token for outbound use.
        if uid and reply_token:
            self._reply_tokens[uid] = (
                reply_token,
                time.time() + self.reply_token_ttl,
            )

        # Media attachments (downloaded and cached locally).
        media_urls: List[str] = []
        media_types: List[str] = []
        text = ""
        message_type = MessageType.TEXT

        if msg_type == "text":
            text = msg.get("text", "") or ""
        elif msg_type == "picture":
            local_path = await self._download_picture(msg)
            if local_path:
                media_urls.append(local_path)
                media_types.append(self._mime_from_filename(msg.get("fileName", "")))
                message_type = MessageType.PHOTO
            text = "[image]"
        elif msg_type == "sticker":
            text = "[sticker]"
        elif msg_type == "file":
            text = "[file]"
        else:
            text = f"[unsupported message type: {msg_type}]"

        source_obj = self.build_source(
            chat_id=uid,
            chat_type="dm",
            user_id=uid,
            user_name=username,
            chat_name=username,
        )

        event_obj = MessageEvent(
            text=text,
            message_type=message_type,
            source=source_obj,
            raw_message=event,
            message_id=message_id,
            media_urls=media_urls,
            media_types=media_types,
        )

        await self.handle_message(event_obj)

    async def _download_picture(self, msg: Dict[str, Any]) -> Optional[str]:
        """Download an inbound picture and cache it locally.

        Returns the cached file path, or None on failure.
        """
        picture_id = msg.get("pictureId", "")
        if not picture_id or not self._client:
            return None
        try:
            data = await self._client.fetch_picture(picture_id)
        except Exception as exc:
            logger.warning("Eko: failed to download picture %s: %s", picture_id, exc)
            return None
        ext = self._ext_from_filename(msg.get("fileName", ""), default=".jpg")
        try:
            from gateway.platforms.base import cache_image_from_bytes
            return cache_image_from_bytes(data, ext=ext)
        except Exception as exc:
            logger.warning("Eko: failed to cache picture %s: %s", picture_id, exc)
            return None

    @staticmethod
    def _ext_from_filename(filename: str, default: str = ".bin") -> str:
        """Extract extension from a filename, with a fallback."""
        if filename and "." in filename:
            ext = "." + filename.rsplit(".", 1)[-1].lower()
            return ext if len(ext) <= 10 else default
        return default

    @staticmethod
    def _mime_from_filename(filename: str) -> str:
        """Guess MIME type from filename extension."""
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        return {
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "gif": "image/gif",
            "webp": "image/webp",
        }.get(ext, "image/jpeg")
```

Also add `List` to the import at the top of the file if not already present. Check that the existing imports include `from typing import Any, Dict, List, Optional, Set, Tuple` — they should already be there.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/jo/hermes-sivwork/hermes-agent && source /tmp/eko-test-venv/bin/activate && python -m pytest tests/gateway/test_eko_plugin.py::TestInboundPicture -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add plugins/platforms/eko/adapter.py tests/gateway/test_eko_plugin.py
git commit -m "feat(eko): handle inbound picture messages with download and cache"
```

---

## Task 4: Add outbound `send_image_file`, `send_image`, and `send_document`

**Files:**
- Modify: `plugins/platforms/eko/adapter.py` (add methods to `EkoAdapter` class, after `send` method)
- Modify: `tests/gateway/test_eko_plugin.py` (add test class)

- [ ] **Step 1: Write the failing tests**

Add to `tests/gateway/test_eko_plugin.py`:

```python
class TestOutboundMedia:

    @pytest.mark.asyncio
    async def test_send_image_file_uses_reply_token(self):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._reply_tokens = {"chat1": ("tok_abc", time.time() + 50)}
        adapter._client = MagicMock()
        adapter._client.reply_picture = AsyncMock()

        with patch("builtins.open", MagicMock(return_value=MagicMock())):
            with patch("pathlib.Path.read_bytes", return_value=b"\x89PNG data"):
                result = await adapter.send_image_file("chat1", "/fake/img.png", caption="hi")
        assert result.success
        adapter._client.reply_picture.assert_called_once()
        adapter._client.push_picture.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_image_file_falls_back_to_push(self):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._reply_tokens = {}
        adapter._client = MagicMock()
        adapter._client.push_picture = AsyncMock()

        with patch("pathlib.Path.read_bytes", return_value=b"\x89PNG data"):
            result = await adapter.send_image_file("chat1", "/fake/img.png", caption="hi")
        assert result.success
        adapter._client.push_picture.assert_called_once_with(
            uid="chat1",
            file_bytes=b"\x89PNG data",
            filename="img.png",
            caption="hi",
        )

    @pytest.mark.asyncio
    async def test_send_image_downloads_and_delegates(self):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._reply_tokens = {}
        adapter._client = MagicMock()
        adapter._client.push_picture = AsyncMock()

        with patch("gateway.platforms.base.cache_image_from_url", AsyncMock(return_value="/cache/img_abc.jpg")):
            with patch("pathlib.Path.read_bytes", return_value=b"\xff\xd8\xff image data"):
                result = await adapter.send_image("chat1", "https://example.com/img.jpg", caption="look")
        assert result.success
        adapter._client.push_picture.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_document_pushes_file(self):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._reply_tokens = {}
        adapter._client = MagicMock()
        adapter._client.push_file = AsyncMock()

        with patch("pathlib.Path.read_bytes", return_value=b"%PDF-1.4 data"):
            result = await adapter.send_document(
                "chat1", "/fake/report.pdf", file_name="report.pdf"
            )
        assert result.success
        adapter._client.push_file.assert_called_once_with(
            uid="chat1",
            file_bytes=b"%PDF-1.4 data",
            filename="report.pdf",
        )

    @pytest.mark.asyncio
    async def test_send_image_file_auth_error_retries_push(self):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._reply_tokens = {"chat1": ("tok_abc", time.time() + 50)}
        adapter._client = MagicMock()
        adapter._client.reply_picture = AsyncMock(side_effect=_EkoAuthError("401"))
        adapter._client.push_picture = AsyncMock()

        with patch("pathlib.Path.read_bytes", return_value=b"\x89PNG data"):
            result = await adapter.send_image_file("chat1", "/fake/img.png")
        assert result.success
        adapter._client.push_picture.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_document_not_connected_returns_error(self):
        adapter = EkoAdapter.__new__(EkoAdapter)
        adapter._client = None
        result = await adapter.send_document("chat1", "/fake/file.pdf")
        assert not result.success
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/jo/hermes-sivwork/hermes-agent && source /tmp/eko-test-venv/bin/activate && python -m pytest tests/gateway/test_eko_plugin.py::TestOutboundMedia -v`
Expected: FAIL — missing methods on `EkoAdapter`

- [ ] **Step 3: Write the implementation**

Add after the `send` method in the `EkoAdapter` class, before `_consume_reply_token`:

```python

    # ------------------------------------------------------------------
    # Outbound send (images and files)
    # ------------------------------------------------------------------

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send a local image file to an Eko user."""
        if not self._client:
            return SendResult(success=False, error="Eko adapter not connected")

        from pathlib import Path

        try:
            file_bytes = Path(image_path).read_bytes()
        except OSError as exc:
            return SendResult(success=False, error=f"Cannot read image: {exc}")

        filename = Path(image_path).name or "image.jpg"

        # Try reply token first, fall back to push.
        token, used_reply = self._consume_reply_token(chat_id)
        if used_reply:
            try:
                await self._client.reply_picture(token, file_bytes, filename)
                return SendResult(success=True, message_id=token)
            except _EkoAuthError:
                try:
                    await self._client.push_picture(
                        chat_id, file_bytes, filename, caption=caption or ""
                    )
                    return SendResult(success=True, message_id=None)
                except Exception as exc2:
                    return SendResult(success=False, error=str(exc2))
            except RuntimeError as exc:
                logger.info("Eko: reply picture rejected (%s); falling back to push", exc)

        try:
            await self._client.push_picture(
                chat_id, file_bytes, filename, caption=caption or ""
            )
            return SendResult(success=True, message_id=None)
        except _EkoAuthError:
            try:
                await self._client.push_picture(
                    chat_id, file_bytes, filename, caption=caption or ""
                )
                return SendResult(success=True, message_id=None)
            except Exception as exc2:
                return SendResult(success=False, error=str(exc2))
        except RuntimeError as exc:
            return SendResult(success=False, error=str(exc), retryable=True)
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=True)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an image from a URL to an Eko user.

        Downloads the image to a local cache first, then delegates to
        send_image_file for native delivery.
        """
        try:
            from gateway.platforms.base import cache_image_from_url

            local_path = await cache_image_from_url(image_url)
        except Exception as exc:
            logger.warning("Eko: failed to download image URL: %s", exc)
            return SendResult(success=False, error=f"Cannot download image: {exc}")

        return await self.send_image_file(
            chat_id, local_path, caption=caption, reply_to=reply_to, metadata=metadata
        )

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send a file/document to an Eko user."""
        if not self._client:
            return SendResult(success=False, error="Eko adapter not connected")

        from pathlib import Path

        try:
            file_bytes = Path(file_path).read_bytes()
        except OSError as exc:
            return SendResult(success=False, error=f"Cannot read file: {exc}")

        filename = file_name or Path(file_path).name or "document"

        # No reply-token endpoint documented for files — always push.
        try:
            await self._client.push_file(chat_id, file_bytes, filename)
            return SendResult(success=True, message_id=None)
        except _EkoAuthError:
            try:
                await self._client.push_file(chat_id, file_bytes, filename)
                return SendResult(success=True, message_id=None)
            except Exception as exc2:
                return SendResult(success=False, error=str(exc2))
        except RuntimeError as exc:
            return SendResult(success=False, error=str(exc), retryable=True)
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/jo/hermes-sivwork/hermes-agent && source /tmp/eko-test-venv/bin/activate && python -m pytest tests/gateway/test_eko_plugin.py::TestOutboundMedia -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add plugins/platforms/eko/adapter.py tests/gateway/test_eko_plugin.py
git commit -m "feat(eko): add outbound send_image_file, send_image, send_document"
```

---

## Task 5: Verify syntax and run full test suite

**Files:**
- No new files

- [ ] **Step 1: Verify adapter syntax**

```bash
cd /home/jo/hermes-sivwork/hermes-agent
python -c "import ast; ast.parse(open('plugins/platforms/eko/adapter.py').read()); print('Syntax OK')"
```

Expected: `Syntax OK`

- [ ] **Step 2: Verify test file syntax**

```bash
python -c "import ast; ast.parse(open('tests/gateway/test_eko_plugin.py').read()); print('Syntax OK')"
```

Expected: `Syntax OK`

- [ ] **Step 3: Run all Eko tests**

```bash
source /tmp/eko-test-venv/bin/activate
python -m pytest tests/gateway/test_eko_plugin.py -v
```

Expected: ALL PASS

- [ ] **Step 4: Commit (if any fixes were needed)**

```bash
git add -A plugins/platforms/eko/ tests/gateway/test_eko_plugin.py
git commit -m "fix(eko): address issues from verification"
```

(Only if fixes were needed — skip if Steps 1-3 passed cleanly.)

---

## Task 6: Update docs (README, session notes)

**Files:**
- Modify: `plugins/platforms/eko/README.md`
- Modify: `docs/superpowers/specs/2026-05-20-eko-session-notes.md`

- [ ] **Step 1: Update README features and roadmap**

In `plugins/platforms/eko/README.md`, update the Features section to add:

```markdown
- Image receiving (download, cache, vision tool integration)
- Image sending (reply token + push fallback)
- File sending (push to user)
```

Move "Image/file receiving + sending" from the roadmap to the version history, and add "Image receiving (picture messages only)" to v1.1.0.

Update the Roadmap "High priority" table — remove the image/file rows since they're done.

- [ ] **Step 2: Update session notes**

In `docs/superpowers/specs/2026-05-20-eko-session-notes.md`, update the "Next Steps" section to mark image/file as done, and add findings:

- `message.type == "picture"` (not `"image"`)
- Download URL: `{base}/file/view/{pictureId}?size=large` (Bearer auth required)
- File webhook events not sent by Eko
- Sticker webhook events have `packageId`/`stickerId` but no download API

- [ ] **Step 3: Commit**

```bash
git add plugins/platforms/eko/README.md docs/superpowers/specs/2026-05-20-eko-session-notes.md
git commit -m "docs(eko): update README and session notes for image/file support"
```
