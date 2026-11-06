"""微信语音通话 ⇄ 豆包端到端实时语音（全双工 Seeduplex）桥接。

守护进程（agent-wechat ≥1.4.0）：
  控制 WS  /v1/accounts/{a}/calls/socket      JSON：hello / call.offered / call.state / command.result / track.updated
  媒体 WS  /v1/accounts/{a}/calls/{id}/media   先 media.attach → media.ready → audio.start，之后双向裸 PCM16LE 16k 单声道
豆包：wss://openspeech.bytedance.com/api/v3/duplex/realtime/dialogue，头 X-Api-Key
  上行 input_audio_buffer.append（base64，20ms/640 字节，严格实时节奏）；下行 response.output_audio.delta（pcm_s16le 24k）
"""

from __future__ import annotations

import asyncio
import audioop  # Python 3.13 起移出标准库，requirements.txt 里用 audioop-lts 补上
import base64
import json
import os
import time
import uuid
from pathlib import Path

import aiohttp

from astrbot.api import logger

DOUBAO_URL = "wss://openspeech.bytedance.com/api/v3/duplex/realtime/dialogue"
FRAME = 640  # 20ms @ 16k s16le mono
MAX_UPLINK_BACKLOG = FRAME * 20  # 上行积压超过 400ms 就丢旧的，保持低延迟
PROMPT_FILE = Path(__file__).with_name("call_prompt.md")


def downsample_24k_to_16k(pcm: bytes, state):
    """豆包 24k → 微信 16k；state 跨块保持连续。"""
    if len(pcm) % 2:
        pcm = pcm[:-1]
    return audioop.ratecv(pcm, 2, 1, 24000, 16000, state)


class CallBridge:
    def __init__(self, client, cfg: dict) -> None:
        self.client = client  # WeChatClient（复用 base_url / token / account_id）
        self.allow = {str(x) for x in (cfg.get("call_allow") or [])}  # 含 "*" = 所有好友
        self.voice = str(cfg.get("call_voice") or "zh_female_xiaohe_jupiter_bigtts")
        self.speech_rate = int(cfg.get("call_speech_rate", 0))  # 豆包 speech_rate：-50~100，0 正常，100 = 2 倍速
        self.greeting = str(cfg.get("call_greeting") or "喂？你好呀～")
        self.prompt_file = Path(str(cfg.get("call_prompt_file") or "") or PROMPT_FILE)
        self.max_seconds = float(cfg.get("call_max_minutes", 10)) * 60
        self.key_env = str(cfg.get("call_api_key_env") or "DOUBAO_REALTIME_API_KEY")
        self.doubao_url = str(cfg.get("call_doubao_url") or DOUBAO_URL)
        self._stop = asyncio.Event()
        self._ctrl: aiohttp.ClientWebSocketResponse | None = None
        self._client_id = ""
        self._owner: dict[str, str] = {}  # callId -> ownerToken
        self._sessions: dict[str, asyncio.Task] = {}
        self._pending_answer: dict[str, str] = {}  # requestId -> callId
        self._generation: dict[str, asyncio.Queue] = {}  # callId -> 新 send generation
        self._revision: dict[str, int] = {}  # callId -> 最新 revision（挂断要带）
        self._retry = 5.0  # 控制连接断开后多久重连
        self._no_audio_logged = False

    # --------------------------------------------------------------- control
    def _ws_url(self, suffix: str) -> str:
        base = self.client.base_url.replace("http://", "ws://").replace("https://", "wss://")
        return f"{base}/{self.client._acc(suffix)}"

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                if not self.client.account_id:
                    await asyncio.sleep(5)
                    continue
                async with self.client.session().ws_connect(
                    self._ws_url("calls/socket"), headers=self.client._headers(), heartbeat=15
                ) as ws:
                    self._ctrl = ws
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._on_control(json.loads(msg.data))
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[wechat_call] 通话控制连接中断: {type(e).__name__}: {e}")
            self._ctrl = None
            await asyncio.sleep(self._retry)
            self._retry = 5.0

    async def stop(self) -> None:
        self._stop.set()
        for t in self._sessions.values():
            t.cancel()
        if self._ctrl is not None:
            await self._ctrl.close()

    async def _cmd(self, body: dict) -> str:
        rid = body.setdefault("requestId", uuid.uuid4().hex)
        if self._ctrl is not None and not self._ctrl.closed:
            await self._ctrl.send_json(body)
        return rid

    async def _on_control(self, ev: dict) -> None:
        t = ev.get("type")
        if t == "hello":
            self._client_id = str(ev.get("clientId") or "")
            if (ev.get("capabilities") or {}).get("directAudio"):
                await self._cmd({"type": "call.ready", "ready": True,
                                 "format": {"sampleRate": 16000, "channels": 1, "frameMs": 20},
                                 "scope": {"kind": "all"}})
                self._no_audio_logged = False
                allow = "所有好友" if "*" in self.allow else (sorted(self.allow) or "无")
                logger.info(f"[wechat_call] 语音通话已就绪，允许来电: {allow}")
            else:
                # 微信掉线时守护进程也这样回；重新登录后不会再发 hello，所以断开重连、隔一阵再查
                if not self._no_audio_logged:
                    self._no_audio_logged = True
                    logger.warning("[wechat_call] 守护进程现在接不了电话（capabilities 无 directAudio，微信没登录时也这样），每 30 秒重查一次")
                self._retry = 30.0
                if self._ctrl is not None:
                    await self._ctrl.close()
        elif t == "call.offered":
            offer = ev.get("offer") or {}
            cid, caller = str(offer.get("callId")), str(offer.get("callerId"))
            if offer.get("callType") != "voice" or offer.get("kind", "direct") != "direct":
                reason = "不是一对一语音"
            elif not ("*" in self.allow or caller in self.allow):
                reason = "不在 call_allow"
            elif self._owner or self._pending_answer:
                reason = "正在和别人通话"
            else:
                reason = ""
            if not reason:
                logger.info(f"[wechat_call] 接听来电 {caller}")
                rid = await self._cmd({"type": "call.answer", "callId": cid,
                                       "expectedRevision": offer.get("revision")})
                self._pending_answer[rid] = cid
            else:
                logger.info(f"[wechat_call] 拒接来电 {caller}（{reason}）")
                await self._cmd({"type": "call.reject", "callId": cid, "expectedRevision": offer.get("revision")})
        elif t == "command.result":
            cid = self._pending_answer.pop(str(ev.get("requestId")), None)
            if cid and ev.get("status") in ("accepted", "replayed") and ev.get("ownerToken"):
                self._owner[cid] = str(ev["ownerToken"])
            elif cid:
                logger.warning(f"[wechat_call] 接听失败: {ev.get('code')}")
        elif t == "call.state":
            call = ev.get("call") or {}
            cid, state = str(call.get("callId")), call.get("state")
            self._revision[cid] = int(call.get("revision") or 0)
            if state == "connected" and call.get("ownedByThisClient") and cid in self._owner and cid not in self._sessions:
                self._generation[cid] = asyncio.Queue()
                self._sessions[cid] = asyncio.create_task(self._session(cid))
            elif state in ("ending", "ended", "uncertain"):
                task = self._sessions.pop(cid, None)
                if task:
                    task.cancel()
                self._owner.pop(cid, None)
                self._generation.pop(cid, None)
                if state != "ending":
                    self._revision.pop(cid, None)
                    logger.info(f"[wechat_call] 通话结束 ({state})")
        elif t == "track.updated":
            q = self._generation.get(str(ev.get("callId")))
            track = ev.get("track") or {}
            if q is not None and track.get("direction") == "send":
                q.put_nowait(int(track.get("generation") or 0))
        elif t == "call.error":
            logger.warning(f"[wechat_call] 守护进程报错: {ev.get('code')}")

    # --------------------------------------------------------------- session
    async def _session(self, cid: str) -> None:
        key = os.environ.get(self.key_env, "")
        http = self.client.session()
        started = time.monotonic()
        try:
            if not key:
                raise RuntimeError(f"缺少环境变量 {self.key_env}")
            async with http.ws_connect(self._ws_url(f"calls/{cid}/media"), headers=self.client._headers()) as media, \
                    http.ws_connect(self.doubao_url, headers={"X-Api-Key": key}, heartbeat=20) as llm:
                await media.send_json({"type": "media.attach", "clientId": self._client_id, "ownerToken": self._owner[cid]})
                ready = await media.receive_json(timeout=10)
                if ready.get("type") != "media.ready":
                    raise RuntimeError(f"媒体通道未就绪: {ready}")
                gen = int(ready["sendTrack"]["generation"])
                await media.send_json({"type": "audio.start", "generation": gen})
                await llm.send_json({"type": "session.create", "session": {
                    "model": "1.2.6.1",
                    "instructions": self.prompt_file.read_text(encoding="utf-8") if self.prompt_file.exists() else "",
                    "audio": {"input": {"format": {"type": "pcm", "rate": 16000}},
                              "output": {"format": {"type": "pcm_s16le", "rate": 24000}, "voice": self.voice,
                                         "speech_rate": self.speech_rate}}}})
                logger.info("[wechat_call] 通话已接通，豆包会话建立中")
                uplink = bytearray()
                tasks = [
                    asyncio.create_task(self._media_in(media, uplink)),
                    asyncio.create_task(self._pace_up(llm, uplink)),
                    asyncio.create_task(self._llm_down(cid, llm, media, gen)),
                ]
                done, pending = await asyncio.wait(tasks, timeout=self.max_seconds, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                for t in done:
                    if t.exception():
                        logger.warning(f"[wechat_call] 通话通道结束: {t.exception()}")
                try:
                    await llm.send_json({"type": "session.close"})
                except Exception:  # noqa: BLE001
                    pass
        except asyncio.CancelledError:
            return  # 对方挂断：call.state ending 取消了本任务
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[wechat_call] 通话出错: {type(e).__name__}: {e}")
        logger.info(f"[wechat_call] 挂断（时长 {time.monotonic() - started:.0f} 秒）")
        await self._cmd({"type": "call.hangup", "callId": cid, "expectedRevision": self._revision.get(cid, 0)})

    @staticmethod
    async def _media_in(media, uplink: bytearray) -> None:
        async for msg in media:
            if msg.type == aiohttp.WSMsgType.BINARY:
                uplink.extend(msg.data)
                if len(uplink) > MAX_UPLINK_BACKLOG:
                    del uplink[: len(uplink) - FRAME * 10]
            elif msg.type == aiohttp.WSMsgType.TEXT:
                ev = json.loads(msg.data)
                if ev.get("type") == "call.error":
                    raise RuntimeError(f"媒体通道报错 {ev.get('code')}")
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break

    @staticmethod
    async def _pace_up(llm, uplink: bytearray) -> None:
        """豆包要求严格 20ms 节奏：按时钟每 20ms 发一包，没收到声音就补静音。"""
        silence = b"\x00" * FRAME
        t0 = time.monotonic()
        n = 0
        while True:
            if len(uplink) >= FRAME:
                chunk = bytes(uplink[:FRAME])
                del uplink[:FRAME]
            else:
                chunk = silence
            await llm.send_json({"type": "input_audio_buffer.append", "audio": base64.b64encode(chunk).decode()})
            n += 1
            delay = t0 + n * 0.02 - time.monotonic()
            if delay < -0.2:  # 卡顿后不追发，重新对齐时钟
                t0, n = time.monotonic(), 0
            await asyncio.sleep(max(0.0, delay))

    async def _llm_down(self, cid: str, llm, media, gen: int) -> None:
        state = None
        current = ""  # 最近一段回答的 response_id
        speaking = False
        dropped: set[str] = set()
        async for msg in llm:
            if msg.type != aiohttp.WSMsgType.TEXT:
                if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
                continue
            ev = json.loads(msg.data)
            t = ev.get("type")
            if t == "session.created":
                await llm.send_json({"type": "speech_text_buffer.commit", "text": self.greeting})
            elif t == "response.output_audio.started":
                current, speaking = str(ev.get("response_id") or ""), True
            elif t == "response.output_audio.delta":
                if str(ev.get("response_id") or current) in dropped:
                    continue
                pcm, state = downsample_24k_to_16k(base64.b64decode(ev.get("delta") or ""), state)
                for i in range(0, len(pcm), 60000):
                    await media.send_bytes(pcm[i:i + 60000])
            elif t == "response.output_audio.done":
                speaking = False
            elif t == "conversation.item.input_audio_transcription.started" and speaking:
                # 对方开口打断：丢掉这段回答剩下的声音，清空守护进程里排队的音频
                dropped.add(current)
                speaking = False
                await llm.send_json({"type": "response.cancel"})
                await self._cmd({"type": "call.clear_audio", "callId": cid})
                q = self._generation.get(cid)
                if q is not None:
                    try:
                        gen = await asyncio.wait_for(q.get(), 3)
                        await media.send_json({"type": "audio.start", "generation": gen})
                    except asyncio.TimeoutError:
                        pass
                state = None
            elif t == "conversation.item.input_audio_transcription.completed":
                logger.info(f"[wechat_call] 对方: {str(ev.get('transcript') or '')[:80]}")
            elif t == "response.output_text.done":
                logger.info(f"[wechat_call] 机器人: {str(ev.get('text') or '')[:80]}")
            elif t in ("error", "session.closed"):
                if t == "error":
                    logger.warning(f"[wechat_call] 豆包报错: {json.dumps(ev, ensure_ascii=False)[:300]}")
                break
