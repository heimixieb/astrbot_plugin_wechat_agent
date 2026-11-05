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
from .wechat_media import VOICE_MAX_SECONDS, audio_duration_ms, prepare_image_bytes, qr_modules, qr_text, split_voice, to_wav

ADAPTER_NAME = "wechat_agent"
SUPPORTED_TYPES = {1, 3, 34, 47, 49}
# 49 类应用消息里按文字处理的：分享文字(1)、聊天记录合并转发(19)、引用回复(57)、群公告(87)；带 link 的卡片另算
APP_TEXT_TYPES = {1, 19, 57, 87, None}
_MENTION_PREFIX = re.compile(r"^(?:@[^\s\u2005\u00a0]+[\u2005\u00a0 ]+)+")
LOGIN_QR_FILE = "login_qr.png"
# 登录操作的结束状态（和 wx auth login 一致）
LOGIN_GRACE_SECONDS = 45  # 发现没登录后先等这么久，让守护进程自己重连
LOGIN_DONE = {"succeeded", "failed", "cancelled", "expired", "rejected", "needs_login", "approval_required", "challenge_required"}

DEFAULT_CONFIG = {
    "id": "wechat",
    "type": ADAPTER_NAME,
    "enable": False,
    "base_url": "http://127.0.0.1:6174",
    "token_file": "/run/secrets/wechat-api-token",
    "token": "",
    "account": "",  # 守护进程里的账号 id 或别名；留空 = 用第一个账号
    "consumer": "astrbot",
    "auto_login": True,  # 没登录时在 AstrBot 日志里打出登录二维码
    "download_media": True,
    "media_wait_seconds": 20,
    "max_media_mb": 32,
    "send_min_interval": 2.0,
    "send_max_interval": 4.0,
    "send_max_per_minute": 12,
    "max_message_age_seconds": 300,  # 停机恢复后补到的旧消息超过这个秒数就不处理（0 = 不限）
    "quote_reply": True,  # 群聊偶尔引用对方（麦麦式概率）
    "skip_recalled": True,  # 对方撤回了就不回
    "attach_recent_image": True,  # 先发图再 @ 机器人时把图带上
    "recent_image_seconds": 180,
    "parse_share_card": True,  # 分享卡片 / 聊天记录转文字
    "split_long_voice": True,  # 超过 55 秒的语音切成几条
    "call_enable": False,  # 微信语音来电 → 豆包实时语音（需守护进程 ≥1.4.0、环境变量 DOUBAO_REALTIME_API_KEY）
    "call_allow": [],  # 允许接听的来电 wxid
    "call_voice": "zh_female_xiaohe_jupiter_bigtts",
    "call_speech_rate": 0,  # 电话语速 -50~100，0 正常，100 = 2 倍速
    "call_greeting": "喂？你好呀～",
    "call_prompt_file": "",  # 电话里用的人设文件；留空 = 插件自带的 call_prompt.md
    "call_max_minutes": 10,
    "call_api_key_env": "DOUBAO_REALTIME_API_KEY",
}

# WebUI「机器人」页的表单说明（不写的话只显示原始键名）
CONFIG_METADATA = {
    "base_url": {"description": "守护进程地址", "type": "string",
                 "hint": "agent-wechat 守护进程的地址，默认 http://127.0.0.1:6174。守护进程只听本机，AstrBot 在 Docker 里时要用 host 网络。"},
    "token_file": {"description": "Token 文件路径", "type": "string",
                   "hint": "守护进程 tokenFile 指向的那个文件。Docker 里要把它只读挂进容器，比如挂到 /run/secrets/wechat-api-token。"},
    "token": {"description": "Token（直接填）", "type": "string", "secret": True,
              "hint": "不想挂文件就直接填 token。填了 token_file 时优先读文件。"},
    "account": {"description": "账号", "type": "string",
                "hint": "守护进程里的账号 id 或别名。留空用第一个；守护进程里一个账号都没有时，开着「自动扫码登录」会自动建一个。"},
    "consumer": {"description": "事件消费者名", "type": "string", "hint": "一般不用改。"},
    "auto_login": {"description": "自动扫码登录", "type": "bool",
                   "hint": "微信没登录时自动发起登录：能免扫码就免扫码，不行就把二维码打在 AstrBot 日志里（同时存成图片），用手机微信扫码并在手机上点确认。"},
    "download_media": {"description": "下载收到的图片和语音", "type": "bool", "hint": "关掉后机器人看不到图、听不到语音。"},
    "media_wait_seconds": {"description": "媒体下载等待秒数", "type": "float"},
    "max_media_mb": {"description": "媒体大小上限（MB）", "type": "float"},
    "send_min_interval": {"description": "发送间隔下限（秒）", "type": "float",
                          "hint": "防风控：每条消息之间随机等 下限~上限 秒。设 0 不等。"},
    "send_max_interval": {"description": "发送间隔上限（秒）", "type": "float"},
    "send_max_per_minute": {"description": "每分钟最多发几条", "type": "int", "hint": "防风控，0 = 不限。"},
    "max_message_age_seconds": {"description": "旧消息不回（秒）", "type": "int",
                                "hint": "机器人停了很久再恢复时，超过这么多秒的旧消息不回复。0 = 不限。"},
    "quote_reply": {"description": "群聊偶尔引用对方", "type": "bool",
                    "hint": "照搬 MaiBot 的概率：她回的那条后面插进来的消息越多，越倾向引用原消息（约 10%~31%）；私聊从不引用。"},
    "skip_recalled": {"description": "撤回了就不回", "type": "bool", "hint": "对方撤回了消息、机器人还没回的话，这条回复就不发了。"},
    "attach_recent_image": {"description": "先发图再 @ 时带上图", "type": "bool",
                            "hint": "微信图文不能同条发。群里有人发图后不久又 @ 机器人，把他刚发的图带进这条，看图、改图插件都能拿到。"},
    "recent_image_seconds": {"description": "带图时限（秒）", "type": "int", "hint": "发图后多少秒内 @ 才算在说这张图。"},
    "parse_share_card": {"description": "分享卡片转文字", "type": "bool",
                         "hint": "文章/视频/音乐/视频号分享转成「标题 + 简介 + 网址」，聊天记录合并转发转成「谁：说了什么」。关掉就忽略这类消息。"},
    "split_long_voice": {"description": "长语音自动切段", "type": "bool",
                         "hint": "微信语音最长 60 秒，超过 55 秒的按换气停顿切成几条发。关掉后超长语音会发送失败。"},
    "call_enable": {"description": "接听语音电话", "type": "bool",
                    "hint": "白名单里的人打来微信语音，自动接到豆包端到端实时语音。需要守护进程 ≥1.4.0 和豆包 API Key（见下面的环境变量名）。"},
    "call_allow": {"description": "允许来电的 wxid", "type": "list", "hint": "其他人打来自动拒接。"},
    "call_voice": {"description": "电话音色", "type": "string", "hint": "豆包音色 id，也可以填自己复刻的 S_ 开头音色。"},
    "call_speech_rate": {"description": "电话语速", "type": "int", "hint": "-50~100，0 正常，100 = 2 倍速。"},
    "call_greeting": {"description": "接通第一句", "type": "string"},
    "call_prompt_file": {"description": "电话人设文件", "type": "string", "hint": "留空用插件自带的 call_prompt.md。"},
    "call_max_minutes": {"description": "单次通话最长（分钟）", "type": "int"},
    "call_api_key_env": {"description": "豆包 API Key 环境变量名", "type": "string",
                         "hint": "AstrBot 进程里放 Key 的环境变量名，默认 DOUBAO_REALTIME_API_KEY。"},
}


@register_platform_adapter(
    ADAPTER_NAME,
    "个人微信（agent-wechat 守护进程）",
    default_config_tmpl=DEFAULT_CONFIG,
    adapter_display_name="微信 (agent-wechat)",
    logo_path="logo.png",
    support_streaming_message=False,
    config_metadata=CONFIG_METADATA,
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
        # 可选功能（默认全开，WebUI 里可单独关）
        self.auto_login = bool(cfg.get("auto_login", True))
        self.quote_reply = bool(cfg.get("quote_reply", True))
        self.skip_recalled = bool(cfg.get("skip_recalled", True))
        self.attach_recent_image = bool(cfg.get("attach_recent_image", True))
        self.recent_image_seconds = float(cfg.get("recent_image_seconds", 180))
        self.parse_share_card = bool(cfg.get("parse_share_card", True))
        self.split_long_voice = bool(cfg.get("split_long_voice", True))
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
        self._unauth_since = 0.0
        self.stats ={"received": 0, "ignored": 0, "sent": 0, "errors": 0}
        # 群聊引用概率用：每个群最近的发言 (消息id, 是否自己, 时间)，以及已决定过是否引用的目标消息
        self._thread_log: dict[str, deque[tuple[str, bool, float]]] = {}
        self._quote_decided: deque[str] = deque(maxlen=200)
        # 最近被撤回的消息（消息 id 和 serverId 都记），回复它的话就不再发
        self._recalled: deque[str] = deque(maxlen=500)
        # 微信图文不能同条发：群里每人最近发的一张图 (路径, 时间)，紧接着 @ 机器人时带上
        self._recent_image: dict[tuple[str, str], tuple[str, float]] = {}
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
                try:
                    acc = await self.client.resolve_account()
                except WeChatApiError as e:
                    if e.code != "no_account" or not self.auto_login:
                        raise
                    new_id = self.client.account_hint or "astrbot"
                    await self.client.create_account(new_id)
                    logger.info(f"[wechat_agent] 守护进程里还没有账号，已自动创建账号「{new_id}」")
                    acc = await self.client.resolve_account()
                if not acc.get("authenticated"):
                    # 守护进程刚重启时会自己用保存的登录态重连，先等一会儿，别抢着发起登录
                    self._unauth_since = self._unauth_since or time.monotonic()
                    if self.auto_login and time.monotonic() - self._unauth_since >= LOGIN_GRACE_SECONDS:
                        await self._login()
                        continue
                    if self.auto_login:
                        self._log_state("unauth_wait", "微信还没登录，先等守护进程自己重连；连不上会自动发起扫码登录")
                        await self._sleep(10)
                        continue
                    self._log_state("unauth", "微信账号尚未登录，30 秒后重试；请在守护进程那边运行 wx auth login 扫码，或在适配器配置里打开「自动扫码登录」")
                    await self._sleep(30)
                    continue
                self._unauth_since = 0.0
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
                self._log_state(f"api{e.status}{e.code}", self._explain(e))
                await self._sleep(backoff)
                backoff = min(backoff * 2, 60.0)
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                self.stats["errors"] += 1
                self._log_state(
                    "conn",
                    f"连不上守护进程 {self.client.base_url}（{type(e).__name__}），{backoff:.0f}s 后重连。"
                    "请确认守护进程在运行（wx daemon status 或 systemctl status）；"
                    "守护进程只听本机 127.0.0.1，AstrBot 在 Docker 里时要给容器开 host 网络（network_mode: host）",
                )
                await self._sleep(backoff)
                backoff = min(backoff * 2, 60.0)
            except Exception as e:  # noqa: BLE001
                self.stats["errors"] += 1
                logger.exception(f"[wechat_agent] 未预期错误: {e}")
                await self._sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    def _explain(self, e: WeChatApiError) -> str:
        """守护进程报错 → 带解决办法的中文提示。"""
        if e.status in (401, 403):
            tf = self.client.token_file
            if tf and not os.path.isfile(tf) and not self.client._token:
                return f"找不到 token 文件 {tf}：AstrBot 在 Docker 里时要把守护进程的 token 文件挂进容器，或者在配置里直接填 token"
            return f"守护进程拒绝了 token（{e.status}）：token_file / token 要和守护进程配置里 tokenFile 的内容一致"
        if e.code == "no_account":
            return "守护进程里还没有微信账号：运行 wx accounts create <名字> 再 wx auth login，或打开「自动扫码登录」让插件自动建"
        if e.code == "account_not_found":
            return f"守护进程里没有叫「{self.client.account_hint}」的账号：用 wx accounts list 查一下，或把 account 留空用第一个"
        return f"守护进程返回错误: {e}"

    async def _login(self) -> None:
        """没登录：先试免扫码，不行把二维码打进日志、存成图片；结束后回主循环重新检查登录状态。"""
        op = await self.client.start_login()
        op_id = str(op.get("id") or "")
        qr_path = self._data_dir / LOGIN_QR_FILE
        self._data_dir.mkdir(parents=True, exist_ok=True)
        shown, approval, lan = 0, False, False
        deadline = time.monotonic() + 340
        while op_id and not self._stop.is_set() and time.monotonic() < deadline:
            state = str(op.get("state") or "")
            result = op.get("result") or {}
            if state in LOGIN_DONE:
                break
            gen = int(result.get("qrGeneration") or 0)
            if result.get("qrAvailable") and gen > shown:
                shown = gen
                png = await self.client.login_qr(op_id, gen)
                qr_path.write_bytes(png)
                mods = qr_modules(png)
                art = "\n" + qr_text(mods) if mods else ""
                logger.warning(f"[wechat_agent] 微信未登录，请用手机微信扫下面的二维码（约 2 分钟换一张），图片也存在 {qr_path}{art}")
            if state == "awaiting_approval" and not approval:
                logger.warning("[wechat_agent] 已扫码，请在手机微信上点「登录」确认")
            approval = state == "awaiting_approval"
            if result.get("lanChallenge") == "waiting_for_phone" and not lan:
                lan = True
                logger.warning("[wechat_agent] 微信要求本地网络验证：让手机连到能访问这台机器的网络")
            await self._sleep(2)
            op = await self.client.operation(op_id)
        state = str(op.get("state") or "")
        try:
            qr_path.unlink()
        except OSError:
            pass
        if state == "succeeded":
            logger.info("[wechat_agent] 微信登录成功")
        else:
            logger.warning(f"[wechat_agent] 这次登录没成功（{state or '超时'}），30 秒后重新发起")
            await self._sleep(30)

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
        if event.get("kind") == "message.recalled":
            data = event.get("data") or {}
            self._recalled.extend(str(k) for k in (data.get("messageId"), data.get("serverId")) if k)
            return None
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

    def is_recalled(self, *ids: str | None) -> bool:
        return self.skip_recalled and any(i and str(i) in self._recalled for i in ids)

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
        if msg.get("direction") != "incoming" or msg.get("recall"):
            return None
        mtype = msg.get("type")
        if mtype not in SUPPORTED_TYPES:
            return None
        app_type = msg.get("appMessageType")
        link = msg.get("link") or {}
        if mtype == 49 and app_type not in APP_TEXT_TYPES and not link.get("url"):
            return None  # 上传提示(74)等忽略
        if mtype == 49 and not self.parse_share_card and (link.get("url") or app_type == 19):
            return None  # 关了「分享卡片转文字」：分享卡片和聊天记录都不处理
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
            qtext = quote.get("embeddedText") or _link_text(quote.get("embeddedLink"))
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
            if link.get("url"):
                text = _link_text(link)
            elif app_type == 19 and text:
                text = f"[聊天记录]\n{text}"  # 守护进程已把合并转发整理成「谁：说了什么」
            if text:
                chain.append(Plain(text))
        elif mtype in (3, 47):
            path = await self._fetch_media(msg, kind="image")
            if path:
                chain.append(Image.fromFileSystem(path))
                if is_group and self.attach_recent_image:
                    self._recent_image[(thread_id, sender_id)] = (path, abm.timestamp)
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

        # 先发图再 @ 她说「把这张扩一下」：把这人刚发的图带上，看图和画图插件都能拿到
        if self.attach_recent_image and is_group and at_self and mtype not in (3, 47):
            recent = self._recent_image.pop((thread_id, sender_id), None)
            quoted_img = any(isinstance(c, Reply) and any(isinstance(x, Image) for x in c.chain) for c in chain)
            if recent and not quoted_img and abm.timestamp - recent[1] <= self.recent_image_seconds and os.path.exists(recent[0]):
                chain.append(Image.fromFileSystem(recent[0]))

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
        if self.quote_reply and is_group and reply_message_id and quote_comp is None and text_length:
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
            if first_text:
                first_text = False
                await self._paced(lambda: self._send_text(thread_id, text, quote_comp, mention_ids, mention_all))
            else:
                await self._paced(lambda: self._send_text(thread_id, text, None, [], False))

        for comp in comps:
            if isinstance(comp, (Reply, At, AtAll)):
                continue
            if isinstance(comp, Plain):
                buf.append(comp.text)
                continue
            await flush()
            if isinstance(comp, Record):
                await self._send_voice_parts(thread_id, comp)
                continue
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

    async def _send_voice_parts(self, thread_id: str, comp: Record) -> None:
        """语音：超过 VOICE_MAX_SECONDS 按换气停顿切成几条，每条单独走发送节流。"""
        path = await comp.convert_to_file_path()
        # TTS 流式 wav 的头部长度是占位值（会被读成几万秒），守护进程转语音会 500；先用 ffmpeg 重写成规整 wav
        path = await to_wav(path) or path
        dur = await audio_duration_ms(path)
        too_long = self.split_long_voice and bool(dur and dur > VOICE_MAX_SECONDS * 1000)
        parts = await split_voice(path, dur) if too_long else []
        if too_long:
            logger.info(f"[wechat_agent] 语音 {dur / 1000:.0f}s 超长，切成 {len(parts) or 1} 段")
        try:
            for p in parts or [path]:
                await self._paced(lambda p=p: self._send_voice(thread_id, p))
        finally:
            for p in parts:
                try:
                    os.remove(p)
                except OSError:
                    pass

    async def _send_voice(self, thread_id: str, path: str) -> None:
        raw = Path(path).read_bytes()
        dur = await audio_duration_ms(path)
        name = os.path.basename(path)
        src = await self.client.upload(raw, name, _audio_ctype(name))
        job = await self.client.send_media(thread_id, "voice", src["id"], duration_ms=dur)
        await self._finish_job(job, "voice", timeout=300)

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


def _link_text(link: dict | None) -> str:
    """分享卡片（文章/视频/音乐/视频号）转文字；网址单独一行，解析插件能认出来。"""
    if not link:
        return ""
    head = " ".join(x for x in (link.get("title"), f"（{link['sourceName']}）" if link.get("sourceName") else "") if x)
    lines = [f"[分享] {head}".rstrip(), link.get("description") or "", link.get("url") or ""]
    return "\n".join(x for x in lines if x)


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
