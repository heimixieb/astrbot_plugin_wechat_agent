"""AstrBot 平台适配器：个人微信（经 agent-wechat 守护进程）。"""

from __future__ import annotations

import asyncio
import os
import random
import re
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import (
    At,
    AtAll,
    BaseMessageComponent,
    File,
    Image,
    Node,
    Nodes,
    Plain,
    Record,
    Reply,
    Video,
)
from astrbot.api.platform import (
    AstrBotMessage,
    Group,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)
from astrbot.core.platform.astr_message_event import MessageSesion
from astrbot.core.utils.astrbot_path import get_astrbot_data_path, get_astrbot_temp_path
from astrbot.core.platform.platform import PlatformStatus

from .wechat_call import CallBridge
from .wechat_client import SUCCESS_JOB_STATES, WeChatApiError, WeChatClient
from .wechat_event import WeChatAgentMessageEvent
from .wechat_media import audio_duration_ms, prepare_image_bytes, to_wav

ADAPTER_NAME = "wechat_agent"
SUPPORTED_TYPES = {1, 3, 34, 47, 49}
_MENTION_PREFIX = re.compile(r"^(?:@[^\s\u2005\u00a0]+[\u2005\u00a0 ]+)+")

DEFAULT_CONFIG = {
    "id": "wechat",
    "type": ADAPTER_NAME,
    "enable": False,
    "base_url": "http://127.0.0.1:6174",
    "token_file": "/run/secrets/wechat-api-token",
    "token": "",
    "account": "",  # 守护进程里的账号 id 或别名；留空 = 用第一个账号
    "consumer": "astrbot",
    "download_media": True,
    "media_wait_seconds": 20,
    "max_media_mb": 32,
    "call_enable": False,  # 微信语音来电 → 豆包实时语音（需守护进程 ≥1.4.0、环境变量 DOUBAO_REALTIME_API_KEY）
    "call_allow": [],  # 允许接听的来电 wxid
    "call_voice": "zh_female_xiaohe_jupiter_bigtts",
    "call_greeting": "喂？你好呀～",
    "call_prompt_file": "",  # 电话里用的人设文件；留空 = 插件自带的 call_prompt.md
    "max_message_age_seconds": 300,  # 停机恢复后补到的旧消息超过这个秒数就不处理（0 = 不限）
}


@register_platform_adapter(
    ADAPTER_NAME,
    "个人微信（agent-wechat 守护进程）",
    default_config_tmpl=DEFAULT_CONFIG,
    adapter_display_name="微信 (agent-wechat)",
    support_streaming_message=False,
)
class WeChatAgentAdapter(Platform):
    def __init__(self, platform_config: dict, platform_settings: dict, event_queue: asyncio.Queue) -> None:
        super().__init__(platform_config, event_queue)
        self.settings = platform_settings
        cfg = platform_config
        self.client = WeChatClient(
            base_url=str(cfg.get("base_url") or DEFAULT_CONFIG["base_url"]),
            token=str(cfg.get("token") or ""),
            token_file=str(cfg.get("token_file") or ""),
            account=str(cfg.get("account") or ""),
        )
        self.consumer = str(cfg.get("consumer") or "astrbot")
        self.download_media = bool(cfg.get("download_media", True))
        self.media_wait = float(cfg.get("media_wait_seconds", 20))
        self.max_media_bytes = int(float(cfg.get("max_media_mb", 32)) * 1024 * 1024)
        self.max_age = float(cfg.get("max_message_age_seconds", DEFAULT_CONFIG["max_message_age_seconds"]))
        # 风控：本账号所有发送串行，条间隔随机 send_min~max_interval 秒，每分钟最多 send_max_per_minute 条
        self.send_gap = (float(cfg.get("send_min_interval", 2.0)), float(cfg.get("send_max_interval", 4.0)))
        self.send_per_min = int(cfg.get("send_max_per_minute", 12))
        self._send_lock = asyncio.Lock()
        self._last_send = 0.0
        self._sent_times: deque[float] = deque()
        self.metadata = PlatformMetadata(
            name=ADAPTER_NAME,
            description="个人微信（agent-wechat 守护进程）",
            id=str(cfg.get("id", "wechat")),
            support_streaming_message=False,
        )
        self.own_id: str = ""
        self._stop = asyncio.Event()
        self._cursor = ""
        self._data_dir = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_wechat_agent" / self.metadata.id
        self._cursor_file = self._data_dir / "cursor.txt"
        self._tmp_dir = Path(get_astrbot_temp_path()) / "wechat_agent"
        self._caps: dict = {}
        self._group_names: dict[str, tuple[str, float]] = {}
        self._member_names: dict[str, tuple[dict[str, str], float]] = {}
        self._last_state_log = ""
        self.stats = {"received": 0, "ignored": 0, "sent": 0, "errors": 0}
        # 群聊引用概率用：每个群最近的发言 (消息id, 是否自己, 时间)，以及已决定过是否引用的目标消息
        self._thread_log: dict[str, deque[tuple[str, bool, float]]] = {}
        self._quote_decided: deque[str] = deque(maxlen=200)
        self.calls = CallBridge(self.client, cfg) if cfg.get("call_enable") else None
        self._call_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ meta
    def meta(self) -> PlatformMetadata:
        return self.metadata

    def get_client(self) -> WeChatClient:
        return self.client

    def create_event(self, message: AstrBotMessage) -> WeChatAgentMessageEvent:
        return WeChatAgentMessageEvent(
            message_str=message.message_str,
            message_obj=message,
            platform_meta=self.meta(),
            session_id=message.session_id,
            platform=self,
        )

    async def terminate(self) -> None:
        self._stop.set()
        if self.calls:
            await self.calls.stop()
        await self.client.close()

    # ------------------------------------------------------------- main loop
    def run(self):
        return self._run()

    def _log_state(self, state: str, msg: str) -> None:
        if state != self._last_state_log:
            self._last_state_log = state
            logger.warning(f"[wechat_agent] {msg}")

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    async def _run(self) -> None:
        backoff = 2.0
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        self.status = PlatformStatus.RUNNING
        if self.calls:
            self._call_task = asyncio.create_task(self.calls.run())  # 等账号解析好后自己连通话控制通道
        while not self._stop.is_set():
            try:
                acc = await self.client.resolve_account()
                if not acc.get("authenticated"):
                    self._log_state("unauth", "微信账号尚未登录（需扫码），30 秒后重试；请在守护进程那边运行 wx auth login 扫码登录")
                    await self._sleep(30)
                    continue
                self.own_id = str(acc.get("ownUsername") or "")
                if not self.own_id:
                    acc = await self.client.account()
                    self.own_id = str(acc.get("ownUsername") or "")
                if not self.own_id:
                    self._log_state("noid", "已登录但还拿不到自己的微信号，稍后重试")
                    await self._sleep(10)
                    continue
                try:
                    self._caps = await self.client.capabilities()
                except Exception:
                    self._caps = {}
                self.client_self_id = self.own_id
                await self._consume()
                backoff = 2.0
            except asyncio.CancelledError:
                raise
            except WeChatApiError as e:
                self.stats["errors"] += 1
                if e.status == 401 or e.status == 403:
                    self._log_state("auth", f"守护进程拒绝 token ({e.status})，检查 token_file")
                else:
                    self._log_state(f"api{e.status}{e.code}", f"守护进程返回错误: {e}")
                await self._sleep(backoff)
                backoff = min(backoff * 2, 60.0)
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                self.stats["errors"] += 1
                self._log_state("conn", f"连接守护进程失败/中断: {type(e).__name__}，{backoff:.0f}s 后重连")
                await self._sleep(backoff)
                backoff = min(backoff * 2, 60.0)
            except Exception as e:  # noqa: BLE001
                self.stats["errors"] += 1
                logger.exception(f"[wechat_agent] 未预期错误: {e}")
                await self._sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    def _load_cursor(self) -> str:
        try:
            return self._cursor_file.read_text().strip()
        except OSError:
            return ""

    def _save_cursor(self, cursor: str) -> None:
        self._cursor = cursor
        try:
            self._cursor_file.write_text(cursor)
        except OSError:
            pass

    async def _consume(self) -> None:
        cursor = self._cursor or self._load_cursor()
        if not cursor:
            # 首次启动：跳过历史，只处理之后的新消息
            cursor = await self.client.latest_cursor()
            self._save_cursor(cursor)
        logger.info(f"[wechat_agent] 已连接守护进程，账号 {self.own_id}，事件游标 {cursor}")
        self._last_state_log = ""
        try:
            async for event in self.client.stream_events(cursor):
                if self._stop.is_set():
                    return
                try:
                    await self.handle_event(event)
                except Exception as e:  # noqa: BLE001
                    self.stats["errors"] += 1
                    logger.exception(f"[wechat_agent] 处理事件失败(已跳过): {e}")
                cur = str(event.get("cursor") or "")
                if cur:
                    self._save_cursor(cur)
                    try:
                        await self.client.ack(self.consumer, cur)
                    except Exception:
                        pass
        except WeChatApiError as e:
            if e.code == "event_cursor_expired":
                logger.warning("[wechat_agent] 事件游标过期，重置到最新")
                self._cursor = ""
                try:
                    self._cursor_file.unlink()
                except OSError:
                    pass
                return
            raise

    # ------------------------------------------------------------ inbound
    async def handle_event(self, event: dict) -> AstrBotMessage | None:
        if event.get("kind") != "message.committed":
            return None
        data = event.get("data") or {}
        origin = data.get("origin")
        if origin is not None and origin != "live":
            return None  # 历史/回填消息
        mid = data.get("messageId")
        if not isinstance(mid, str) or not mid:
            return None
        msg = await self.client.message(mid)
        if self._too_old(msg):
            self.stats["ignored"] += 1
            logger.debug(f"[wechat_agent] 跳过旧消息 {msg.get('createdAt')}")
            return None
        abm = await self.convert_message(msg)
        if abm is None:
            self.stats["ignored"] += 1
            return None
        self.stats["received"] += 1
        if abm.type == MessageType.GROUP_MESSAGE:
            self._note(abm.native_thread_id, abm.message_id, False)  # type: ignore[attr-defined]
        self.commit_event(self.create_event(abm))
        return abm

    def _too_old(self, msg: dict) -> bool:
        """机器人停了很久再恢复时，补到的旧消息不触发回复（免得突然回几个小时前的话）。"""
        if self.max_age <= 0:
            return False
        try:
            ts = datetime.fromisoformat(str(msg.get("createdAt")).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            return False
        return time.time() - ts > self.max_age

    def _note(self, thread_id: str, message_id: str, from_self: bool) -> None:
        self._thread_log.setdefault(thread_id, deque(maxlen=40)).append((message_id, from_self, time.monotonic()))

    def _want_quote(self, thread_id: str, target_id: str, text_length: int) -> bool:
        """麦麦式群聊引用：同一条目标消息只决定一次，按概率引用（后面插进来的消息越多越倾向引用）。"""
        if target_id in self._quote_decided:
            return False
        self._quote_decided.append(target_id)
        log = list(self._thread_log.get(thread_id) or [])
        ids = [m[0] for m in log]
        distance = len(ids) - 1 - ids.index(target_id) if target_id in ids else 6
        prev_self, prev_seconds = (log[-1][1], time.monotonic() - log[-1][2]) if log else (False, 3600.0)
        return random.random() < quote_probability(text_length, prev_self, prev_seconds, distance)

    async def convert_message(self, msg: dict) -> AstrBotMessage | None:
        if msg.get("direction") != "incoming":
            return None
        mtype = msg.get("type")
        if mtype not in SUPPORTED_TYPES:
            return None
        app_type = msg.get("appMessageType")
        if mtype == 49 and app_type not in (57, 1, None):
            # 仅处理引用回复(57)和文字类应用消息；上传提示(74)等忽略
            if app_type != 57:
                return None
        thread_id = str(msg.get("threadId") or "")
        sender_id = str(msg.get("senderId") or "")
        if not thread_id or not sender_id:
            return None
        is_group = thread_id.endswith("@chatroom")
        mentions = msg.get("mentions") or {}
        at_self = bool(mentions.get("state") == "known" and mentions.get("self"))
        at_all = bool(mentions.get("state") == "known" and mentions.get("all"))
        body = msg.get("body") or ""

        abm = AstrBotMessage()
        abm.self_id = self.own_id
        abm.type = MessageType.GROUP_MESSAGE if is_group else MessageType.FRIEND_MESSAGE
        abm.message_id = str(msg["id"])
        abm.native_thread_id = thread_id  # type: ignore[attr-defined]
        abm.session_id = thread_id if is_group else sender_id
        abm.sender = MessageMember(user_id=sender_id, nickname=await self._nickname(thread_id, sender_id, is_group))
        if is_group:
            abm.group = Group(group_id=thread_id, group_name=await self._group_name(thread_id))
        abm.raw_message = msg
        try:
            abm.timestamp = int(datetime.fromisoformat(str(msg.get("createdAt")).replace("Z", "+00:00")).timestamp())
        except Exception:
            abm.timestamp = int(time.time())

        chain: list[BaseMessageComponent] = []
        text = ""

        if is_group and at_self:
            chain.append(At(qq=self.own_id, name=""))
            body = _MENTION_PREFIX.sub("", body, count=1)
        if is_group and at_all:
            chain.append(AtAll())

        quote = msg.get("quote")
        if quote:
            qtext = quote.get("embeddedText") or ""
            qsender = self.own_id if quote.get("direction") == "outgoing" else str(quote.get("senderId") or "")
            qchain: list[BaseMessageComponent] = [Plain(qtext)] if qtext else []
            if quote.get("type") == 3 and quote.get("embeddedImage"):
                qimg = await self._quoted_image(thread_id, str(quote.get("serverId") or ""))
                if qimg:
                    qchain.append(Image.fromFileSystem(qimg))
            chain.append(
                Reply(
                    id=str(quote.get("serverId") or ""),
                    chain=qchain,
                    sender_id=qsender,
                    sender_nickname="",
                    time=0,
                    message_str=qtext,
                    text=qtext,
                )
            )

        if mtype == 1 or (mtype == 49):
            text = body.strip()
            if text:
                chain.append(Plain(text))
        elif mtype in (3, 47):
            path = await self._fetch_media(msg, kind="image")
            if path:
                chain.append(Image.fromFileSystem(path))
            else:
                text = "[图片(下载失败)]"
                chain.append(Plain(text))
        elif mtype == 34:
            path = await self._fetch_media(msg, kind="voice")
            if path:
                chain.append(Record.fromFileSystem(path))
            else:
                text = "[语音(下载失败)]"
                chain.append(Plain(text))

        if is_group and mentions.get("state") == "known":
            for uid in mentions.get("userIds") or []:
                if uid and uid != self.own_id:
                    chain.append(At(qq=str(uid), name=await self._nickname(thread_id, str(uid), True)))

        abm.message = chain
        abm.message_str = text
        return abm

    async def _quoted_image(self, thread_id: str, server_id: str) -> str | None:
        """引用的图片：按 serverId 在本会话里找到原消息再下载，画图插件会把它当参考图。"""
        if not server_id:
            return None
        try:
            for m in await self.client.thread_messages(thread_id):
                if str(m.get("serverId")) == server_id and m.get("type") in (3, 47):
                    return await self._fetch_media(m, "image")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[wechat_agent] 引用图片获取失败: {type(e).__name__}: {e}")
        return None

    async def _fetch_media(self, msg: dict, kind: str) -> str | None:
        if not self.download_media:
            return None
        mid = msg["id"]
        try:
            asset = _select_asset(await self.client.media_assets(mid), kind)
            if asset is None:
                job = await self.client.transfer_media(mid, "normal" if kind == "voice" else "best")
                job = await self.client.wait_job(job, self.media_wait)
                asset = _select_asset(await self.client.media_assets(mid), kind)
            if asset is None:
                logger.warning(f"[wechat_agent] 媒体不可用 message={mid[:24]} kind={kind}")
                return None
            data, ctype = await self.client.media_content(asset["id"], self.max_media_bytes)
            ext = _ext_for(ctype, asset.get("filename"), kind, data)
            self._tmp_dir.mkdir(parents=True, exist_ok=True)
            path = self._tmp_dir / f"{kind}_{uuid.uuid4().hex}{ext}"
            path.write_bytes(data)
            if kind == "voice" and ext not in (".wav", ".mp3", ".m4a", ".ogg"):
                wav = await to_wav(str(path))
                if wav:
                    return wav
            return str(path)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[wechat_agent] 下载媒体失败 kind={kind}: {type(e).__name__}: {e}")
            return None

    async def _group_name(self, group_id: str) -> str | None:
        hit = self._group_names.get(group_id)
        if hit and time.time() - hit[1] < 3600:
            return hit[0]
        try:
            t = await self.client.thread(group_id)
            name = t.get("displayName") or group_id
        except Exception:
            name = group_id
        self._group_names[group_id] = (name, time.time())
        return name

    async def _nickname(self, thread_id: str, sender_id: str, is_group: bool) -> str:
        if not is_group:
            hit = self._group_names.get(thread_id)
            if hit and time.time() - hit[1] < 3600:
                return hit[0]
            try:
                t = await self.client.thread(thread_id)
                name = t.get("displayName") or sender_id
            except Exception:
                name = sender_id
            self._group_names[thread_id] = (name, time.time())
            return name
        cache = self._member_names.get(thread_id)
        if not cache or time.time() - cache[1] > 3600:
            names: dict[str, str] = {}
            for fetch in (self.client.group_members, self.client.group_lookup):
                try:
                    for m in await fetch(thread_id):
                        mid = m.get("id") or m.get("userId") or m.get("username")
                        nm = m.get("roomDisplayName") or m.get("displayName") or m.get("mentionName") or m.get("nickname")
                        if mid and nm:
                            names[str(mid)] = str(nm)
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"[wechat_agent] 群成员名获取失败({fetch.__name__}): {e}")
                if names:
                    break
            cache = (names, time.time())
            self._member_names[thread_id] = cache
        return cache[0].get(sender_id, sender_id)

    # ----------------------------------------------------------- outbound
    async def send_by_session(self, session: MessageSesion, message_chain: MessageChain) -> None:
        await super().send_by_session(session, message_chain)
        await self.send_chain(session.session_id, message_chain)

    async def _paced(self, send):
        """风控节流：串行 + 最小间隔 + 每分钟上限。send 返回前应已确认投递，才不会在守护进程排队后一起发出。"""
        async with self._send_lock:
            while True:
                now = time.monotonic()
                while self._sent_times and now - self._sent_times[0] >= 60:
                    self._sent_times.popleft()
                if self.send_per_min <= 0 or len(self._sent_times) < self.send_per_min:
                    break
                wait = 60 - (now - self._sent_times[0])
                logger.warning(f"[wechat_agent] 风控：1 分钟内已发 {len(self._sent_times)} 条，暂停 {wait:.0f}s")
                await asyncio.sleep(wait)
            wait = self._last_send + random.uniform(*self.send_gap) - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                return await send()
            finally:
                self._last_send = time.monotonic()
                self._sent_times.append(self._last_send)

    async def _resolve_quote(self, reply: Reply) -> dict | None:
        if not self._caps.get("quotedTextSend", True):
            return None
        try:
            mid = str(reply.id)
            m = await self.client.message(mid)
            if m.get("serverId"):
                return {"messageId": m["id"], "serverId": str(m["serverId"])}
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[wechat_agent] 引用目标解析失败，改为普通发送: {e}")
        return None

    async def send_chain(self, thread_id: str, chain: MessageChain, reply_message_id: str | None = None) -> None:
        """按消息链顺序发送：相邻文字合并为一条；引用/@ 只作用于第一条文字。"""
        is_group = thread_id.endswith("@chatroom")
        comps = list(_flatten(chain.chain))
        quote_comp: Reply | None = None
        mention_ids: list[str] = []
        mention_all = False
        for comp in comps:
            if isinstance(comp, Reply):
                quote_comp = comp
            elif isinstance(comp, AtAll) or (isinstance(comp, At) and str(comp.qq) == "all"):
                mention_all = mention_all or is_group
            elif isinstance(comp, At):
                if is_group and str(comp.qq) not in mention_ids:
                    mention_ids.append(str(comp.qq))
        text_length = sum(len(c.text) for c in comps if isinstance(c, Plain))
        if is_group and reply_message_id and quote_comp is None and text_length:
            if self._want_quote(thread_id, reply_message_id, text_length):
                quote_comp = Reply(id=reply_message_id)

        buf: list[str] = []
        first_text = True

        async def flush() -> None:
            nonlocal first_text
            text = "".join(buf).strip()
            buf.clear()
            if not text:
                return
            fix = None
            if reply_message_id:  # 只给回复别人的话加错字，工具/主动推送的内容不动
                text, fix = make_typo(text)
            if first_text:
                first_text = False
                await self._paced(lambda: self._send_text(thread_id, text, quote_comp, mention_ids, mention_all))
            else:
                await self._paced(lambda: self._send_text(thread_id, text, None, [], False))
            if fix:
                await self._paced(lambda: self._send_text(thread_id, f"*{fix}", None, [], False))

        for comp in comps:
            if isinstance(comp, (Reply, At, AtAll)):
                continue
            if isinstance(comp, Plain):
                buf.append(comp.text)
                continue
            await flush()
            await self._paced(lambda: self._send_component(thread_id, comp))
        await flush()

    async def _send_text(self, thread_id: str, text: str, quote_comp: Reply | None, mention_ids: list[str], mention_all: bool) -> None:
        quote = await self._resolve_quote(quote_comp) if quote_comp else None
        key = uuid.uuid4().hex
        try:
            intent = await self.client.send_text(
                thread_id, text, quote=quote, mention_ids=mention_ids, mention_all=mention_all, key=key
            )
        except WeChatApiError as e:
            if (quote or mention_ids or mention_all) and e.status in (400, 409, 422):
                logger.warning(f"[wechat_agent] 引用/@ 发送被拒绝({e.code})，降级为纯文本")
                intent = await self.client.send_text(thread_id, text, key=uuid.uuid4().hex)
            else:
                raise
        if intent.get("id"):
            try:  # 等守护进程真正发出再放行下一条
                intent = await self.client.wait_send(intent, 30)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[wechat_agent] 查询文本发送状态失败（只靠间隔节流）: {e}")
        self.stats["sent"] += 1
        if thread_id.endswith("@chatroom"):
            self._note(thread_id, "", True)
        if intent.get("state") not in SUCCESS_JOB_STATES:
            logger.warning(f"[wechat_agent] 文本发送状态 {intent.get('state')}（{intent.get('errorCode')}）；不会自动重发")

    async def _component_bytes(self, comp: BaseMessageComponent) -> tuple[bytes, str]:
        path = await comp.convert_to_file_path()  # type: ignore[attr-defined]
        return Path(path).read_bytes(), path

    async def _send_component(self, thread_id: str, comp: BaseMessageComponent) -> None:
        if isinstance(comp, Image):
            raw, path = await self._component_bytes(comp)
            data, name, ctype = prepare_image_bytes(raw, os.path.basename(path))
            src = await self.client.upload(data, name, ctype)
            job = await self.client.send_media(thread_id, "image", src["id"])
            await self._finish_job(job, "image")
        elif isinstance(comp, Record):
            path = await comp.convert_to_file_path()
            # TTS 流式 wav 的头部长度是占位值（会被读成几万秒），守护进程转语音会 500；先用 ffmpeg 重写成规整 wav
            path = await to_wav(path) or path
            raw = Path(path).read_bytes()
            dur = await audio_duration_ms(path)
            name = os.path.basename(path)
            ctype = _audio_ctype(name)
            src = await self.client.upload(raw, name, ctype)
            job = await self.client.send_media(thread_id, "voice", src["id"], duration_ms=dur)
            await self._finish_job(job, "voice", timeout=300)
        elif isinstance(comp, Video):
            path = await comp.convert_to_file_path()
            src = await self.client.upload(Path(path).read_bytes(), os.path.basename(path), "video/mp4")
            job = await self.client.send_media(thread_id, "video", src["id"])
            await self._finish_job(job, "video", timeout=600)
        elif isinstance(comp, File):
            path = await comp.get_file()  # type: ignore[attr-defined]
            src = await self.client.upload(Path(path).read_bytes(), comp.name or os.path.basename(path), "application/octet-stream")
            job = await self.client.send_media(thread_id, "file", src["id"])
            await self._finish_job(job, "file", timeout=600)
        else:
            logger.debug(f"[wechat_agent] 忽略不支持的消息段 {type(comp).__name__}")

    async def _finish_job(self, job: dict, kind: str, timeout: float = 65.0) -> None:
        job = await self.client.wait_job(job, timeout)
        state = job.get("state")
        if state in SUCCESS_JOB_STATES:
            self.stats["sent"] += 1
            return
        if state == "needs_review":
            logger.warning(f"[wechat_agent] {kind} 发送状态未知(needs_review)，不会自动重发")
            return
        raise WeChatApiError(500, str(job.get("reason") or state), f"{kind} send {state}")


# ---------------------------------------------------------------- helpers
def quote_probability(text_length: int, previous_self: bool, previous_seconds: float, distance: int) -> float:
    """群聊引用概率，照搬 MaiBot src/maisaka/quote/rules.py 的群聊公式（"偶尔引用"档）。

    distance：目标消息之后又来了几条发言（0 = 目标就是最新一条，大家都知道在回谁）。
    """
    if previous_self:
        p = 0.0118 if previous_seconds <= 3 else 0.0776
    else:
        p = 0.2093 if text_length <= 50 else 0.0861
    mult = 0.5 if distance == 0 else 1.0 if distance <= 2 else 1.1 if distance <= 5 else 1.5
    return min(1.0, p * mult)


# 打错字：照搬 MaiBot chinese_typo 的做法——每个汉字 1% 概率出错，出错后一半会补发一条更正。
# shortcut: 用常见同音字表代替 MaiBot 的拼音+字频库（不加新依赖），只有表里的字会被打错。
TYPO_CHAR_RATE = 0.01
_HOMOPHONES = {
    "的": "得地", "得": "的地", "地": "的得", "在": "再", "再": "在", "做": "作", "作": "做",
    "已": "以", "以": "已", "那": "哪", "哪": "那", "像": "象", "象": "像", "会": "回", "回": "会",
    "就": "旧", "到": "道", "道": "到", "吗": "嘛", "嘛": "吗", "他": "她", "她": "他", "是": "事",
    "事": "是", "带": "戴", "戴": "带", "坐": "座", "座": "坐", "期": "其", "其": "期", "题": "提",
    "提": "题", "意": "义", "嗯": "恩", "恩": "嗯", "辛": "心", "心": "新", "新": "心", "又": "有", "有": "又",
}


def make_typo(text: str) -> tuple[str, str | None]:
    """返回 (可能带一个错字的文本, 需要补发更正的正确字或 None)。"""
    if len(text) < 5 or any(m in text for m in ("http", "www.", "`", "[", "]", "{", "}", "@")):
        return text, None
    han = [(i, c) for i, c in enumerate(text) if "一" <= c <= "鿿"]
    if len(han) < 3 or random.random() >= 1 - (1 - TYPO_CHAR_RATE) ** len(han):
        return text, None
    cands = [(i, c) for i, c in han if c in _HOMOPHONES]
    if not cands:
        return text, None
    i, c = random.choice(cands)
    typo = random.choice(_HOMOPHONES[c])
    return text[:i] + typo + text[i + 1:], (c if random.random() < 0.5 else None)


def _flatten(comps):
    """微信没有合并转发：把 Node/Nodes 拆成普通消息段按顺序发，节点之间换行。"""
    for c in comps:
        if isinstance(c, Nodes):
            for n in c.nodes:
                yield from _flatten([n])
        elif isinstance(c, Node):
            yield from _flatten(c.content or [])
            yield Plain("\n")
        else:
            yield c


def _select_asset(assets: list[dict], kind: str) -> dict | None:
    ok = [a for a in assets if a.get("state") == "available"]
    if not ok:
        return None
    if kind == "voice":
        pref = ["normal"]
    else:
        pref = ["original", "standard", "thumbnail", "encoded"]

    def rank(a: dict) -> int:
        v = a.get("actualVariant") or a.get("variant")
        if kind == "voice":
            # 优先 wav（ASR 友好）
            ct = (a.get("contentType") or "")
            return (0 if "wav" in ct else 1) + (0 if v in pref else 5)
        return pref.index(v) if v in pref else len(pref)

    return sorted(ok, key=rank)[0]


def _ext_for(ctype: str, filename: str | None, kind: str, data: bytes) -> str:
    ct = (ctype or "").split(";")[0].strip().lower()
    table = {
        "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
        "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
        "audio/ogg": ".ogg", "audio/silk": ".silk", "audio/x-silk": ".silk",
    }
    if ct in table:
        return table[ct]
    if filename and os.path.splitext(filename)[1]:
        return os.path.splitext(filename)[1].lower()
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"RIFF":
        return ".wav" if kind == "voice" else ".webp"
    if data[:9] == b"#!SILK_V3" or data[1:10] == b"#!SILK_V3":
        return ".silk"
    return ".bin"


def _audio_ctype(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    return {
        ".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".m4a": "audio/mp4",
        ".silk": "audio/silk", ".amr": "audio/amr", ".opus": "audio/ogg", ".flac": "audio/flac",
    }.get(ext, "application/octet-stream")
