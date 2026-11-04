"""agent-wechat 原生 HTTP API 的最小异步客户端（aiohttp）。

只实现适配器用到的接口。token 只从文件/参数读取，永不写入日志。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp


class WeChatApiError(Exception):
    def __init__(self, status: int, code: str = "", message: str = "", retryable: bool = False):
        super().__init__(f"HTTP {status} {code} {message}".strip())
        self.status = status
        self.code = code
        self.retryable = retryable


TERMINAL_JOB_STATES = {
    "accepted", "completed", "succeeded", "sent", "server_accepted", "available", "failed",
    "partial", "cancelled", "expired", "needs_review",
}
SUCCESS_JOB_STATES = {"accepted", "completed", "succeeded", "sent", "server_accepted"}


class WeChatClient:
    def __init__(
        self,
        base_url: str,
        token: str = "",
        token_file: str = "",
        account: str = "",
        request_timeout: float = 30.0,
        media_timeout: float = 300.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.token_file = token_file
        self.account_hint = account
        self.account_id: str | None = None
        self.request_timeout = request_timeout
        self.media_timeout = media_timeout
        self._session: aiohttp.ClientSession | None = None

    # ---------- plumbing ----------
    def _load_token(self) -> str:
        if self.token_file:
            try:
                value = Path(self.token_file).read_text(encoding="utf-8").strip()
                if value:
                    self._token = value
            except OSError:
                pass
        return self._token

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {}
        token = self._load_token()
        if token:
            h["Authorization"] = f"Bearer {token}"
        if extra:
            h.update(extra)
        return h

    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # 守护进程在本机，显式不走环境代理
            self._session = aiohttp.ClientSession(trust_env=False)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _raise_for(self, resp: aiohttp.ClientResponse) -> None:
        if resp.status < 400:
            return
        code, msg, retryable = "", "", resp.status >= 500
        try:
            data = await resp.json(content_type=None)
            err = data.get("error", data) if isinstance(data, dict) else {}
            if isinstance(err, dict):
                code = str(err.get("code", ""))
                msg = str(err.get("message", ""))[:200]
                retryable = bool(err.get("retryable", retryable))
        except Exception:
            pass
        raise WeChatApiError(resp.status, code, msg, retryable)

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        data: Any = None,
        timeout: float | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        async with self.session().request(
            method,
            url,
            json=json_body,
            data=data,
            params=params,
            headers=self._headers(headers),
            timeout=aiohttp.ClientTimeout(total=timeout or self.request_timeout),
        ) as resp:
            await self._raise_for(resp)
            if resp.status == 204:
                return None
            return await resp.json(content_type=None)

    def _acc(self, suffix: str) -> str:
        if not self.account_id:
            raise RuntimeError("account not resolved")
        return f"v1/accounts/{self.account_id}/{suffix.lstrip('/')}"

    # ---------- account ----------
    async def health(self) -> bool:
        try:
            await self.request("GET", "health", timeout=5)
            return True
        except Exception:
            return False

    async def resolve_account(self) -> dict:
        data = await self.request("GET", "v1/accounts")
        items = data.get("items", []) if isinstance(data, dict) else []
        if not items:
            raise WeChatApiError(404, "no_account", "daemon has no account")
        pick = None
        if self.account_hint:
            for it in items:
                if self.account_hint in (it.get("id"), it.get("alias")):
                    pick = it
                    break
        if pick is None:
            if self.account_hint and len(items) > 1:
                raise WeChatApiError(404, "account_not_found", self.account_hint)
            pick = items[0]
        self.account_id = pick["id"]
        return pick

    async def account(self) -> dict:
        return await self.request("GET", f"v1/accounts/{self.account_id}")

    async def capabilities(self) -> dict:
        return await self.request("GET", self._acc("capabilities"))

    # ---------- messages / events ----------
    async def message(self, message_id: str) -> dict:
        return await self.request("GET", self._acc(f"messages/{message_id}"))

    async def events_page(self, after: str = "0", limit: int = 200) -> dict:
        return await self.request(
            "GET", self._acc("events"), params={"after": after, "limit": limit}
        )

    async def latest_cursor(self) -> str:
        cursor = "0"
        for _ in range(10000):
            page = await self.events_page(cursor, 200)
            nxt = str(page.get("nextCursor", cursor))
            if nxt == cursor or not page.get("items"):
                return nxt
            cursor = nxt
        return cursor

    async def ack(self, consumer: str, cursor: str) -> None:
        await self.request(
            "POST", self._acc(f"consumers/{consumer}/ack"), json_body={"cursor": str(cursor)}
        )

    async def stream_events(self, after: str, idle_timeout: float = 75.0) -> AsyncIterator[dict]:
        """SSE。守护进程每 ~15s 发心跳，idle_timeout 内无任何数据视为断线。"""
        url = f"{self.base_url}/{self._acc('events/stream')}"
        async with self.session().get(
            url,
            params={"after": after},
            headers=self._headers({"Accept": "text/event-stream"}),
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=idle_timeout),
        ) as resp:
            await self._raise_for(resp)
            if not (resp.headers.get("Content-Type", "").startswith("text/event-stream")):
                raise WeChatApiError(resp.status, "not_sse", "expected text/event-stream")
            data_lines: list[str] = []
            ev_type = ""
            ev_id = ""
            async for raw in resp.content:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line:
                    if data_lines:
                        payload = json.loads("\n".join(data_lines))
                        if ev_type == "error":
                            err = payload.get("error", {}) if isinstance(payload, dict) else {}
                            code = str(err.get("code", "stream_failed"))
                            raise WeChatApiError(410 if code == "event_cursor_expired" else 500, code)
                        if isinstance(payload, dict) and "kind" in payload:
                            if "cursor" not in payload or payload["cursor"] in (None, ""):
                                payload["cursor"] = ev_id
                            payload["cursor"] = str(payload["cursor"])
                            yield payload
                    data_lines, ev_type, ev_id = [], "", ""
                elif line.startswith(":"):
                    continue
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip(" "))
                elif line.startswith("event:"):
                    ev_type = line[6:].strip()
                elif line.startswith("id:"):
                    ev_id = line[3:].strip()

    # ---------- media ----------
    async def media_assets(self, message_id: str) -> list[dict]:
        data = await self.request(
            "POST", self._acc("media/lookup"), json_body={"messageIds": [message_id]}
        )
        for item in data.get("items", []):
            if item.get("messageId") == message_id:
                return list(item.get("assets", []))
        return []

    async def transfer_media(self, message_id: str, quality: str = "best") -> dict:
        return await self.request(
            "POST",
            self._acc(f"messages/{message_id}/media-transfers"),
            json_body={"messageId": message_id, "assetId": None, "quality": quality},
        )

    async def media_content(self, asset_id: str, max_bytes: int = 64 * 1024 * 1024) -> tuple[bytes, str]:
        return await self._get_bytes(self._acc(f"media/{asset_id}/content"), max_bytes)

    async def avatar(self, user_id: str, max_bytes: int = 2 * 1024 * 1024) -> bytes | None:
        """联系人头像（守护进程给 132px 小图）。本地没缓存就请它刷新一次再取；取不到返回 None。"""
        path = self._acc(f"avatars/{user_id}")
        for delay in (0, 0.5, 1.0, 1.5, 2.0):
            await asyncio.sleep(delay)
            try:
                return (await self._get_bytes(path, max_bytes))[0]
            except WeChatApiError as e:
                if e.code != "media_not_cached":
                    return None
            if delay == 0:
                # 刷新是异步的（202 queued），之后轮询几次
                await self.request("POST", self._acc(f"avatars/{user_id}/refresh"))
        return None

    async def _get_bytes(self, path: str, max_bytes: int) -> tuple[bytes, str]:
        url = f"{self.base_url}/{path}"
        async with self.session().get(
            url,
            headers=self._headers(),
            timeout=aiohttp.ClientTimeout(total=self.media_timeout),
        ) as resp:
            await self._raise_for(resp)
            buf = bytearray()
            async for chunk in resp.content.iter_chunked(65536):
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    raise WeChatApiError(413, "too_large", "media too large")
            return bytes(buf), resp.headers.get("Content-Type", "application/octet-stream")

    async def upload(self, data: bytes, name: str, content_type: str) -> dict:
        form = aiohttp.FormData()
        form.add_field("file", data, filename=name, content_type=content_type)
        return await self.request(
            "POST", self._acc("uploads"), data=form, timeout=self.media_timeout
        )

    async def job(self, job_id: str) -> dict:
        return await self.request("GET", self._acc(f"jobs/{job_id}"))

    async def wait_job(self, job: dict, timeout: float = 65.0) -> dict:
        deadline = asyncio.get_event_loop().time() + timeout
        delay = 0.25
        while job.get("state") not in TERMINAL_JOB_STATES:
            if asyncio.get_event_loop().time() >= deadline:
                break
            await asyncio.sleep(delay)
            delay = min(delay * 1.6, 2.0)
            job = await self.job(job["id"])
        return job

    async def wait_send(self, intent: dict, timeout: float = 30.0) -> dict:
        """文字发送意图在 queued/submitting 时轮询 sends/{id}（文字没有 job）。"""
        deadline = asyncio.get_event_loop().time() + timeout
        delay = 0.25
        while intent.get("state") in ("queued", "submitting"):
            if asyncio.get_event_loop().time() >= deadline:
                break
            await asyncio.sleep(delay)
            delay = min(delay * 1.6, 2.0)
            intent = await self.request("GET", self._acc(f"sends/{intent['id']}"))
        return intent

    # ---------- sends ----------
    async def _post_send(self, body: dict, key: str) -> dict:
        # 同一个 Idempotency-Key 下，网络错误/5xx 重试是安全的
        last: Exception | None = None
        for attempt in range(3):
            try:
                return await self.request(
                    "POST", self._acc("sends"), json_body=body,
                    headers={"Idempotency-Key": key},
                )
            except WeChatApiError as e:
                if not e.retryable:
                    raise
                last = e
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last = e
            await asyncio.sleep(0.5 * (attempt + 1))
        assert last is not None
        raise last

    async def send_text(
        self,
        thread_id: str,
        text: str,
        *,
        quote: dict | None = None,
        mention_ids: list[str] | None = None,
        mention_all: bool = False,
        key: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {"threadId": thread_id, "text": text}
        if quote or mention_ids or mention_all:
            body["kind"] = "text"
            if quote:
                body["quote"] = {"messageId": quote["messageId"], "serverId": quote["serverId"]}
            if mention_ids or mention_all:
                m: dict[str, Any] = {"userIds": list(mention_ids or [])}
                if mention_all:
                    m["all"] = True
                body["mentions"] = m
        return await self._post_send(body, key or uuid.uuid4().hex)

    async def send_media(
        self,
        thread_id: str,
        kind: str,
        source_id: str,
        *,
        duration_ms: int | None = None,
        key: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "threadId": thread_id,
            "kind": kind,
            "sourceId": source_id,
            "standardSourceId": None,
            "thumbnailSourceId": None,
            "playSeconds": None,
            "durationMs": duration_ms,
        }
        if kind == "video":
            body["videoQuality"] = "normal"
        return await self._post_send(body, key or uuid.uuid4().hex)

    async def group_members(self, group_id: str) -> list[dict]:
        data = await self.request("GET", self._acc(f"groups/{group_id}/members"))
        if isinstance(data, dict):
            return list(data.get("items", []))
        return list(data or [])

    async def group_lookup(self, group_id: str) -> list[dict]:
        # members 在 wx 1.3.3 上常为空；lookup 直接读 contact.db，带 nickname/roomDisplayName
        data = await self.request("GET", self._acc(f"groups/{group_id}/lookup"))
        return list((data or {}).get("items", []))

    async def thread_messages(self, thread_id: str, limit: int = 200) -> list[dict]:
        data = await self.request("GET", self._acc(f"threads/{thread_id}/messages"), params={"limit": limit})
        return list((data or {}).get("items", []))

    async def thread(self, thread_id: str) -> dict:
        return await self.request("GET", self._acc(f"threads/{thread_id}"))
