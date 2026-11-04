"""通话桥接：假守护进程（控制/媒体 WS）+ 假豆包 WS 的离线测试。"""
import asyncio
import base64
import json
import os
import unittest

from aiohttp import WSMsgType, web

from astrbot.core import astrbot_config

from astrbot_plugin_wechat_agent.wechat_call import CallBridge, downsample_24k_to_16k
from astrbot_plugin_wechat_agent.wechat_client import WeChatClient

astrbot_config["trace_enable"] = False
ACC, OWNER = "acc1", "wxid_owner"


class FakeServers:
    def __init__(self, caller):
        self.caller = caller
        self.control_in: list[dict] = []
        self.media_in = bytearray()
        self.media_json: list[dict] = []
        self.llm_in: list[dict] = []
        self.ctrl_ws = None
        self.answered = asyncio.Event()
        self.audio_back = asyncio.Event()
        self.speech_up = asyncio.Event()

    async def control(self, req):
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        self.ctrl_ws = ws
        await ws.send_json({"type": "hello", "clientId": "c1", "capabilities": {"directAudio": True}})
        async for msg in ws:
            ev = json.loads(msg.data)
            self.control_in.append(ev)
            if ev["type"] == "call.ready":
                await ws.send_json({"type": "call.offered", "offer": {
                    "callId": "k1", "revision": 3, "kind": "direct", "callType": "voice", "callerId": self.caller}})
            elif ev["type"] == "call.answer":
                await ws.send_json({"type": "command.result", "requestId": ev["requestId"], "status": "accepted",
                                    "callId": "k1", "ownerToken": "ot", "code": None})
                await ws.send_json({"type": "call.state", "call": {
                    "callId": "k1", "revision": 5, "state": "connected", "ownedByThisClient": True}})
                self.answered.set()
            elif ev["type"] == "call.reject":
                self.answered.set()
        return ws

    async def media(self, req):
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        attach = await ws.receive_json()
        assert attach == {"type": "media.attach", "clientId": "c1", "ownerToken": "ot"}, attach
        fmt = {"sampleRate": 16000, "channels": 1, "frameMs": 20}
        await ws.send_json({"type": "media.ready",
                            "sendTrack": {"id": 1, "generation": 1, "direction": "send", "format": fmt},
                            "receiveTrack": {"id": 2, "generation": 1, "direction": "receive", "format": fmt}})
        await ws.send_bytes(b"\x01\x02" * 1600)  # 对方说话：100ms 非静音 PCM
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                self.media_in.extend(msg.data)
                self.audio_back.set()
            else:
                self.media_json.append(json.loads(msg.data))
        return ws

    async def llm(self, req):
        assert req.headers.get("X-Api-Key") == "k-test"
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        async for msg in ws:
            ev = json.loads(msg.data)
            self.llm_in.append(ev)
            if ev["type"] == "session.create":
                await ws.send_json({"type": "session.created", "session": {"id": "s1"}})
            elif ev["type"] == "speech_text_buffer.commit":
                await ws.send_json({"type": "response.output_audio.started", "response_id": "r1"})
                await ws.send_json({"type": "response.output_audio.delta", "response_id": "r1",
                                    "delta": base64.b64encode(b"\x10\x00" * 2400).decode()})  # 24k 100ms
                await ws.send_json({"type": "response.output_audio.done", "response_id": "r1"})
            elif ev["type"] == "input_audio_buffer.append" and base64.b64decode(ev["audio"]).startswith(b"\x01\x02"):
                self.speech_up.set()
            elif ev["type"] == "session.close":
                await ws.close()
        return ws


class CallBridgeTest(unittest.IsolatedAsyncioTestCase):
    async def start(self, caller):
        self.f = FakeServers(caller)
        app = web.Application()
        app.router.add_get(f"/v1/accounts/{ACC}/calls/socket", self.f.control)
        app.router.add_get(f"/v1/accounts/{ACC}/calls/k1/media", self.f.media)
        app.router.add_get("/llm", self.f.llm)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        os.environ["DOUBAO_TEST_KEY"] = "k-test"
        client = WeChatClient(f"http://127.0.0.1:{port}", token="t")
        client.account_id = ACC
        self.client = client
        self.b = CallBridge(client, {"call_allow": [OWNER], "call_api_key_env": "DOUBAO_TEST_KEY",
                                     "call_doubao_url": f"ws://127.0.0.1:{port}/llm"})
        self.task = asyncio.create_task(self.b.run())

    async def asyncTearDown(self):
        await self.b.stop()
        self.task.cancel()
        await self.client.close()
        await self.runner.cleanup()

    async def test_owner_call_bridges_audio_both_ways_and_hangs_up(self):
        await self.start(OWNER)
        await asyncio.wait_for(self.f.answered.wait(), 5)
        await asyncio.wait_for(self.f.audio_back.wait(), 5)  # 豆包的声音到了微信
        await asyncio.wait_for(self.f.speech_up.wait(), 5)  # 对方的声音到了豆包
        create = next(e for e in self.f.llm_in if e["type"] == "session.create")["session"]
        self.assertEqual(create["audio"]["output"]["voice"], "zh_female_xiaohe_jupiter_bigtts")
        self.assertIn("打微信语音电话", create["instructions"])
        self.assertEqual(self.f.media_json[0], {"type": "audio.start", "generation": 1})
        self.assertAlmostEqual(len(self.f.media_in), 3200, delta=8)  # 24k 100ms → 16k 100ms
        # 对方挂断：会话任务结束，不再发 hangup
        await self.f.ctrl_ws.send_json({"type": "call.state", "call": {
            "callId": "k1", "revision": 6, "state": "ended", "ownedByThisClient": True}})
        await asyncio.sleep(0.2)
        self.assertEqual(self.b._sessions, {})
        self.assertFalse(any(e["type"] == "call.hangup" for e in self.f.control_in))

    async def test_stranger_call_is_rejected(self):
        await self.start("wxid_stranger")
        await asyncio.wait_for(self.f.answered.wait(), 5)
        types = [e["type"] for e in self.f.control_in]
        self.assertIn("call.reject", types)
        self.assertNotIn("call.answer", types)


class DownsampleTest(unittest.TestCase):
    def test_downsample_ratio(self):
        out, _ = downsample_24k_to_16k(b"\x00\x00" * 2400, None)
        self.assertAlmostEqual(len(out), 3200, delta=8)
