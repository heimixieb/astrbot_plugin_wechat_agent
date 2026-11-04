import asyncio
import io
import os
import shutil
import subprocess
import tempfile
import unittest
import wave

from PIL import Image as PILImage

from astrbot.api.event import MessageChain
from astrbot.api.message_components import At, Image, Plain, Record, Reply
from astrbot.api.platform import MessageType
from astrbot.core.message.components import AtAll
from astrbot.core import astrbot_config

# 测试别往真实的消息追踪库 data/logs/traces.db 写假记录（只改本进程内存，不存盘）
astrbot_config["trace_enable"] = False

from astrbot_plugin_wechat_agent.wechat_adapter import WeChatAgentAdapter
from astrbot_plugin_wechat_agent.wechat_client import WeChatApiError, WeChatClient
from astrbot_plugin_wechat_agent.wechat_media import pick_cuts, prepare_image_bytes
from astrbot_plugin_wechat_agent.tests.fake_daemon import ACC, TOKEN, FakeDaemon, committed, msg

OWNER = "wxid_owner"
BOT = "wxid_bot"


def gif_bytes():
    frames = [PILImage.new("RGB", (8, 8), c) for c in ("red", "blue")]
    out = io.BytesIO()
    frames[0].save(out, format="GIF", save_all=True, append_images=frames[1:], duration=50, loop=0)
    return out.getvalue()


def wav_bytes(seconds=1.0):
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(b"\x00\x00" * int(16000 * seconds))
    return out.getvalue()


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.d = FakeDaemon()
        url = await self.d.start()
        self.tmp = tempfile.mkdtemp()
        self.token_file = os.path.join(self.tmp, "tok")
        open(self.token_file, "w").write(TOKEN + "\n")
        self.queue = asyncio.Queue()
        cfg = {"id": "wechat", "type": "wechat_agent", "enable": True, "base_url": url,
               "token_file": self.token_file, "account": "dafeiyu", "media_wait_seconds": 2,
               "send_min_interval": 0, "send_max_interval": 0}
        self.a = WeChatAgentAdapter(cfg, {}, self.queue)
        self.a._data_dir = os.path.join(self.tmp, "data")
        from pathlib import Path
        self.a._data_dir = Path(self.a._data_dir)
        self.a._cursor_file = self.a._data_dir / "cursor.txt"
        self.a._tmp_dir = Path(self.tmp) / "media"
        self.a._tmp_dir.mkdir(parents=True)
        await self.a.client.resolve_account()
        self.a.own_id = BOT

    async def asyncTearDown(self):
        await self.a.terminate()
        await self.d.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def feed(self, m, cursor=1):
        self.d.messages[m["id"]] = m
        return await self.a.handle_event(committed(cursor, m["id"], m["threadId"]))


class InboundTests(Base):
    async def test_private_text(self):
        abm = await self.feed(msg("m1", OWNER, OWNER, 1, "你好呀"))
        self.assertEqual(abm.type, MessageType.FRIEND_MESSAGE)
        self.assertEqual(abm.message_str, "你好呀")
        self.assertEqual(abm.session_id, OWNER)
        ev = self.queue.get_nowait()
        self.assertEqual(ev.unified_msg_origin, f"wechat:FriendMessage:{OWNER}")  # 白名单格式
        self.assertEqual(ev.get_self_id(), BOT)

    async def test_group_at_self_and_prefix_strip(self):
        m = msg("m2", "g1@chatroom", "wxid_alice", 1, "@大肥鱼\u2005今天吃啥")
        m["mentions"].update(self=True, userIds=[BOT])
        abm = await self.feed(m)
        self.assertEqual(abm.type, MessageType.GROUP_MESSAGE)
        self.assertEqual(abm.group_id, "g1@chatroom")
        self.assertIsInstance(abm.message[0], At)
        self.assertEqual(str(abm.message[0].qq), BOT)  # 触发 AstrBot 的 @ 唤醒
        self.assertEqual(abm.message_str, "今天吃啥")
        self.assertEqual(abm.sender.nickname, "Alice")

    async def test_group_plain_not_at(self):
        abm = await self.feed(msg("m3", "g1@chatroom", "wxid_alice", 1, "大肥鱼 在吗"))
        self.assertFalse(any(isinstance(c, At) for c in abm.message))
        self.assertEqual(abm.message_str, "大肥鱼 在吗")  # 由 wake_prefix 规则唤醒

    async def test_at_all(self):
        m = msg("m3b", "g1@chatroom", "wxid_alice", 1, "大家好")
        m["mentions"].update(all=True)
        abm = await self.feed(m)
        self.assertTrue(any(isinstance(c, AtAll) for c in abm.message))

    async def test_quote_of_our_message_sets_reply_sender_self(self):
        m = msg("m4", OWNER, OWNER, 49, "对的", appMessageType=57,
                quote={"serverId": "123", "type": 1, "state": "embedded", "senderId": BOT,
                       "direction": "outgoing", "embeddedText": "我之前说的话"})
        abm = await self.feed(m)
        rep = next(c for c in abm.message if isinstance(c, Reply))
        self.assertEqual(str(rep.sender_id), BOT)  # AstrBot 引用唤醒规则
        self.assertEqual(rep.message_str, "我之前说的话")
        self.assertEqual(abm.message_str, "对的")

    async def test_quote_of_other_not_self(self):
        m = msg("m4b", "g1@chatroom", "wxid_alice", 49, "嗯", appMessageType=57,
                quote={"serverId": "5", "type": 1, "state": "embedded", "senderId": "wxid_bob", "embeddedText": "x"})
        abm = await self.feed(m)
        rep = next(c for c in abm.message if isinstance(c, Reply))
        self.assertEqual(str(rep.sender_id), "wxid_bob")

    async def test_ignored(self):
        out = msg("m5", OWNER, BOT, 1, "我发的", direction="outgoing")
        self.assertIsNone(await self.feed(out))
        self.d.messages["m6"] = msg("m6", OWNER, OWNER, 1, "历史")
        self.assertIsNone(await self.a.handle_event(committed(2, "m6", OWNER, origin="backfill")))
        self.assertIsNone(await self.feed(msg("m7", OWNER, OWNER, 10000, "系统消息")))
        self.assertIsNone(await self.feed(msg("m8", OWNER, OWNER, 49, "x", appMessageType=74)))
        self.assertTrue(self.queue.empty())

    async def test_receive_image(self):
        m = msg("m9", OWNER, OWNER, 3)
        self.d.assets["m9"] = [{"id": "a1", "state": "available", "variant": "original", "actualVariant": "original", "contentType": "image/gif", "kind": "image"}]
        self.d.content["a1"] = (gif_bytes(), "image/gif")
        abm = await self.feed(m)
        img = next(c for c in abm.message if isinstance(c, Image))
        path = await img.convert_to_file_path()
        self.assertTrue(os.path.getsize(path) > 10)
        self.assertTrue(path.endswith(".gif"))

    async def test_receive_voice_for_asr(self):
        m = msg("m10", OWNER, OWNER, 34)
        self.d.assets["m10"] = [
            {"id": "v_silk", "state": "available", "variant": "normal", "actualVariant": "normal", "contentType": "audio/silk", "kind": "voice"},
            {"id": "v_wav", "state": "available", "variant": "normal", "actualVariant": "normal", "contentType": "audio/wav", "kind": "voice"}]
        self.d.content["v_wav"] = (wav_bytes(), "audio/wav")
        self.d.content["v_silk"] = (b"#!SILK_V3xxxx", "audio/silk")
        abm = await self.feed(m)
        rec = next(c for c in abm.message if isinstance(c, Record))
        path = await rec.convert_to_file_path()
        self.assertTrue(path.endswith(".wav"))

    async def test_media_unavailable_degrades(self):
        abm = await self.feed(msg("m11", OWNER, OWNER, 3))  # 没有 assets
        self.assertIn("下载失败", abm.message_str)

    async def test_link_card_becomes_text_with_url_line(self):
        link = {"title": "猫猫视频", "description": "超可爱", "url": "https://b23.tv/abc", "sourceName": "哔哩哔哩"}
        abm = await self.feed(msg("m12", OWNER, OWNER, 49, "猫猫视频", appMessageType=5, link=link))
        self.assertEqual(abm.message_str, "[分享] 猫猫视频 （哔哩哔哩）\n超可爱\nhttps://b23.tv/abc")
        self.assertIn("https://b23.tv/abc", abm.message_str.splitlines())  # 网址单独一行

    async def test_forwarded_chat_becomes_text(self):
        abm = await self.feed(msg("m13", OWNER, OWNER, 49, "Alice: 吃了吗\nBob: 没", appMessageType=19))
        self.assertEqual(abm.message_str, "[聊天记录]\nAlice: 吃了吗\nBob: 没")

    async def test_quote_of_link_card_keeps_link(self):
        m = msg("m14", OWNER, OWNER, 49, "这个好看", appMessageType=57,
                quote={"serverId": "8", "type": 49, "senderId": "wxid_bob",
                       "embeddedLink": {"title": "文章", "url": "https://mp.weixin.qq.com/s/x"}})
        rep = next(c for c in (await self.feed(m)).message if isinstance(c, Reply))
        self.assertIn("https://mp.weixin.qq.com/s/x", rep.message_str)

    async def test_already_recalled_message_ignored(self):
        m = msg("m15", OWNER, OWNER, 1, "[Message recalled]", recall={"observedAt": "2026-11-04T00:00:00Z"})
        self.assertIsNone(await self.feed(m))


class OutboundTests(Base):
    async def test_text_private(self):
        await self.a.send_chain(OWNER, MessageChain([Plain("你好"), Plain("呀")]))
        self.assertEqual(self.d.sends, [{"threadId": OWNER, "text": "你好呀"}])
        self.assertTrue(self.d.send_headers[0].get("Idempotency-Key"))

    async def test_group_quote_and_mention(self):
        target = msg("m20", "g1@chatroom", "wxid_alice", 1, "hi", serverId="777")
        self.d.messages["m20"] = target
        chain = MessageChain([Reply(id="m20"), At(qq="wxid_alice"), Plain("来啦")])
        await self.a.send_chain("g1@chatroom", chain)
        body = self.d.sends[0]
        self.assertEqual(body["quote"], {"messageId": "m20", "serverId": "777"})
        self.assertEqual(body["mentions"], {"userIds": ["wxid_alice"]})
        self.assertEqual(body["kind"], "text")

    async def test_private_ignores_at(self):
        await self.a.send_chain(OWNER, MessageChain([At(qq="wxid_x"), Plain("x")]))
        self.assertEqual(self.d.sends, [{"threadId": OWNER, "text": "x"}])

    async def test_quote_rejected_falls_back(self):
        self.d.messages["m21"] = msg("m21", OWNER, OWNER, 1, "q", serverId="9")
        self.d.reject_quote = True
        await self.a.send_chain(OWNER, MessageChain([Reply(id="m21"), Plain("回复")]))
        self.assertEqual(self.d.sends, [{"threadId": OWNER, "text": "回复"}])

    async def test_gif_sent_as_png_first_frame(self):
        p = os.path.join(self.tmp, "x.gif")
        open(p, "wb").write(gif_bytes())
        await self.a.send_chain(OWNER, MessageChain([Image.fromFileSystem(p)]))
        up = self.d.uploads[0]
        self.assertEqual(up["ctype"], "image/png")
        self.assertTrue(up["name"].endswith(".png"))
        self.assertEqual(up["data"][:8], b"\x89PNG\r\n\x1a\n")
        self.assertFalse(PILImage.open(io.BytesIO(up["data"])).is_animated if hasattr(PILImage.open(io.BytesIO(up["data"])), "is_animated") else False)
        s = self.d.sends[0]
        self.assertEqual((s["kind"], s["sourceId"], s["threadId"]), ("image", "src1", OWNER))

    async def test_png_and_jpeg_untouched(self):
        buf = io.BytesIO(); PILImage.new("RGB", (4, 4)).save(buf, "JPEG")
        data, name, ct = prepare_image_bytes(buf.getvalue(), "a.jpg")
        self.assertEqual((data, ct, name), (buf.getvalue(), "image/jpeg", "a.jpg"))

    async def test_voice_sent_with_duration(self):
        p = os.path.join(self.tmp, "tts.wav")
        open(p, "wb").write(wav_bytes(1.0))
        await self.a.send_chain(OWNER, MessageChain([Record.fromFileSystem(p)]))
        s = self.d.sends[0]
        self.assertEqual(s["kind"], "voice")
        self.assertTrue(900 <= s["durationMs"] <= 1100, s)
        self.assertEqual(self.d.uploads[0]["ctype"], "audio/wav")

    async def test_long_voice_split_under_limit(self):
        p = os.path.join(self.tmp, "long.wav")
        open(p, "wb").write(wav_bytes(130.0))
        await self.a.send_chain(OWNER, MessageChain([Record.fromFileSystem(p)]))
        durs = [s["durationMs"] for s in self.d.sends]
        self.assertEqual([s["kind"] for s in self.d.sends], ["voice"] * 3, durs)
        self.assertTrue(all(d <= 55_500 for d in durs), durs)
        self.assertTrue(129_000 <= sum(durs) <= 131_000, durs)
        self.assertFalse([f for f in os.listdir(self.tmp) if "_part" in f])  # 切出来的临时文件删掉了

    def test_pick_cuts_prefers_breath_pause(self):
        r = lambda xs: [round(x, 3) for x in xs]  # noqa: E731
        self.assertEqual(pick_cuts(50, []), [])
        self.assertEqual(r(pick_cuts(70, [(40.0, 40.6)])), [40.3])  # 切在停顿中点
        self.assertEqual(r(pick_cuts(70, [(10.0, 10.5)])), [55.0])  # 停顿太靠前：硬切
        cuts = pick_cuts(200, [(50.0, 51.0), (100.0, 100.4)])
        self.assertEqual(r(cuts[:2]), [50.5, 100.2])
        self.assertTrue(all(b - a <= 55 for a, b in zip([0, *cuts], [*cuts, 200])))

    async def test_mixed_order(self):
        p = os.path.join(self.tmp, "x.gif")
        open(p, "wb").write(gif_bytes())
        await self.a.send_chain(OWNER, MessageChain([Plain("看"), Image.fromFileSystem(p), Plain("好看吗")]))
        self.assertEqual([s.get("text") or s.get("kind") for s in self.d.sends], ["看", "image", "好看吗"])

    async def test_failed_job_raises_needs_review_not(self):
        self.d.job_state = "needs_review"
        p = os.path.join(self.tmp, "x.png")
        PILImage.new("RGB", (4, 4)).save(p)
        await self.a.send_chain(OWNER, MessageChain([Image.fromFileSystem(p)]))  # 不抛错也不重发
        self.assertEqual(len(self.d.sends), 1)
        self.d.job_state = "failed"
        with self.assertRaises(WeChatApiError):
            await self.a.send_chain(OWNER, MessageChain([Image.fromFileSystem(p)]))

    async def test_send_by_session(self):
        from astrbot.core.platform.message_session import MessageSession
        s = MessageSession("wechat", MessageType.FRIEND_MESSAGE, OWNER)
        await self.a.send_by_session(s, MessageChain([Plain("主动消息")]))
        self.assertEqual(self.d.sends[0]["text"], "主动消息")


class LoopTests(Base):
    async def test_first_run_skips_history_then_reconnects(self):
        # 历史事件 1..3 不应被处理；随后连接 #1 推 4 后断开，连接 #2 应从 4 续传并收到 5
        for i in (1, 2, 3):
            self.d.events.append(committed(i, f"h{i}", OWNER))
        for mid, c in (("n4", 4), ("n5", 5)):
            self.d.messages[mid] = msg(mid, OWNER, OWNER, 1, f"新消息{c}")
        self.d.stream_script = [
            [committed(4, "n4", OWNER), "DROP"],
            [committed(5, "n5", OWNER)],
        ]
        self.a._sleep_orig = self.a._sleep
        async def fast(s): await self.a._sleep_orig(min(s, 0.05))
        self.a._sleep = fast
        task = asyncio.create_task(self.a._run())
        got = []
        for _ in range(2):
            ev = await asyncio.wait_for(self.queue.get(), 10)
            got.append(ev.message_str)
        self.a._stop.set()
        await asyncio.wait_for(task, 5)
        self.assertEqual(got, ["新消息4", "新消息5"])
        self.assertEqual(self.d.stream_calls[0], "3")
        self.assertEqual(self.d.stream_calls[1], "4")
        self.assertEqual(self.a._cursor_file.read_text(), "5")
        self.assertIn("4", self.d.acks)

    async def test_waits_when_unauthenticated(self):
        self.d.authenticated = False
        async def fast(s): await asyncio.sleep(0.05)
        self.a._sleep = fast
        task = asyncio.create_task(self.a._run())
        await asyncio.sleep(0.3)
        self.assertFalse(task.done())
        self.assertEqual(self.d.stream_calls, [])
        self.d.authenticated = True
        self.d.messages["u1"] = msg("u1", OWNER, OWNER, 1, "登录后")
        self.d.stream_script = [[committed(1, "u1", OWNER)]]
        ev = await asyncio.wait_for(self.queue.get(), 10)
        self.assertEqual(ev.message_str, "登录后")
        self.a._stop.set()
        await asyncio.wait_for(task, 5)

    async def test_bad_token_reports_error_and_retries(self):
        open(self.token_file, "w").write("wrong")
        with self.assertRaises(WeChatApiError) as cm:
            await self.a.client.resolve_account()
        self.assertEqual(cm.exception.status, 401)
        open(self.token_file, "w").write(TOKEN)  # token 文件热更新
        await self.a.client.resolve_account()

    async def test_daemon_down_does_not_crash_loop(self):
        await self.d.stop()
        async def fast(s): await asyncio.sleep(0.05)
        self.a._sleep = fast
        task = asyncio.create_task(self.a._run())
        await asyncio.sleep(0.4)
        self.assertFalse(task.done())
        self.a._stop.set()
        await asyncio.wait_for(task, 5)
        # 避免 tearDown 再次 stop 报错
        self.d.runner = type("R", (), {"cleanup": staticmethod(lambda: asyncio.sleep(0))})()


if __name__ == "__main__":
    unittest.main()


class PacingTest(Base):
    async def test_sends_are_spaced_and_capped(self):
        import time
        self.a.send_gap = (0.3, 0.3)
        self.a.send_per_min = 3
        stamps = []
        orig = self.a._send_text
        async def rec(*args):
            stamps.append(time.monotonic())
            await orig(*args)
        self.a._send_text = rec
        tasks = [asyncio.create_task(self.a.send_chain("wxid_x", MessageChain([Plain(c)]))) for c in "abcd"]
        await asyncio.sleep(1.5)
        self.assertEqual(len(stamps), 3)  # 第 4 条被每分钟上限挡住
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertTrue(all(g >= 0.29 for g in gaps), gaps)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def test_queued_text_polls_send_status(self):
        self.d.text_state = "queued"
        self.d.send_status_state = "sent"
        intent = await self.a.client.wait_send(await self.a.client.send_text("wxid_x", "hi"))
        self.assertEqual(intent["state"], "sent")


class NicknameTest(Base):
    async def test_falls_back_to_lookup_when_members_empty(self):
        self.d.member_items = []
        self.assertEqual(await self.a._nickname("g1@chatroom", "wxid_alice", True), "群里的Alice")
        self.assertEqual(await self.a._nickname("g1@chatroom", "wxid_bob", True), "Bob")
        self.assertEqual(await self.a._nickname("g1@chatroom", "wxid_zed", True), "wxid_zed")


class ForwardFlattenTest(Base):
    async def test_nodes_are_sent_as_plain_messages(self):
        from astrbot.api.message_components import Node, Nodes
        chain = MessageChain([Nodes([Node(content=[Plain("标题")]), Node(content=[Plain("简介")])])])
        await self.a.send_chain("wxid_x", chain)
        self.assertEqual([b["text"] for b in self.d.sends], ["标题\n简介"])


class QuotedImageTest(Base):
    async def test_quoted_image_becomes_reply_image(self):
        g = "g1@chatroom"
        orig = msg("img1", g, "wxid_bob", 3)
        self.d.messages["img1"] = orig
        self.d.assets["img1"] = [{"id": "a9", "state": "available", "variant": "original", "actualVariant": "original", "contentType": "image/gif", "kind": "image"}]
        self.d.content["a9"] = (gif_bytes(), "image/gif")
        m = msg("t1", g, OWNER, 1, "把这张改成水彩", quote={"serverId": orig["serverId"], "type": 3, "state": "embedded",
                 "senderId": "wxid_bob", "embeddedText": None, "embeddedImage": True})
        abm = await self.feed(m)
        reply = next(c for c in abm.message if isinstance(c, Reply))
        self.assertTrue(any(isinstance(c, Image) for c in reply.chain))


class QuoteProbabilityTest(Base):
    def test_formula_matches_maibot(self):
        from astrbot_plugin_wechat_agent.wechat_adapter import quote_probability
        self.assertAlmostEqual(quote_probability(10, False, 5, 0), 0.2093 * 0.5, places=4)  # 目标就是最新一条
        self.assertAlmostEqual(quote_probability(10, False, 5, 8), 0.2093 * 1.5, places=4)  # 中间插了很多条
        self.assertAlmostEqual(quote_probability(80, False, 5, 1), 0.0861, places=4)
        self.assertAlmostEqual(quote_probability(10, True, 1, 1), 0.0118, places=4)

    async def test_group_reply_quotes_once_when_lucky(self):
        from unittest.mock import patch
        g = "g1@chatroom"
        await self.feed(msg("q1", g, "wxid_alice", 1, "今天去哪"), cursor=1)
        for i in range(3):
            await self.feed(msg(f"q1x{i}", g, "wxid_bob", 1, "插话"), cursor=2 + i)
        with patch("astrbot_plugin_wechat_agent.wechat_adapter.random.random", return_value=0.0):
            await self.a.send_chain(g, MessageChain([Plain("去公园")]), reply_message_id="q1")
            await self.a.send_chain(g, MessageChain([Plain("第二段")]), reply_message_id="q1")  # 分段第二条不再引用
        self.assertEqual(self.d.sends[0]["quote"]["messageId"], "q1")
        self.assertNotIn("quote", self.d.sends[1])

    async def test_no_quote_when_unlucky_or_private(self):
        from unittest.mock import patch
        g = "g1@chatroom"
        await self.feed(msg("q2", g, "wxid_alice", 1, "在吗"))
        self.d.messages["p1"] = msg("p1", OWNER, OWNER, 1, "在吗")
        with patch("astrbot_plugin_wechat_agent.wechat_adapter.random.random", return_value=0.99):
            await self.a.send_chain(g, MessageChain([Plain("在")]), reply_message_id="q2")
        with patch("astrbot_plugin_wechat_agent.wechat_adapter.random.random", return_value=0.0):
            await self.a.send_chain(OWNER, MessageChain([Plain("在")]), reply_message_id="p1")
        self.assertTrue(all("quote" not in s for s in self.d.sends))


class NoTypoTest(Base):
    async def test_reply_sent_as_is(self):
        from unittest.mock import patch
        with patch("astrbot_plugin_wechat_agent.wechat_adapter.random.random", return_value=0.0):
            await self.a.send_chain(OWNER, MessageChain([Plain("我在家的时候")]), reply_message_id="p9")
        self.assertEqual([s["text"] for s in self.d.sends], ["我在家的时候"])  # 打错字功能已去掉，原样发


class AvatarTest(Base):
    async def test_cached_avatar_no_refresh(self):
        self.d.avatars["wxid_alice"] = b"JPEG-A"
        self.assertEqual(await self.a.client.avatar("wxid_alice"), b"JPEG-A")
        self.assertEqual(self.d.avatar_refreshes, [])

    async def test_refreshes_when_not_cached(self):
        self.d.avatar_remote["wxid_bob"] = b"JPEG-B"
        self.assertEqual(await self.a.client.avatar("wxid_bob"), b"JPEG-B")
        self.assertEqual(self.d.avatar_refreshes, ["wxid_bob"])

    async def test_unknown_returns_none(self):
        # 昵称不是 wxid：刷新后仍取不到，轮询完返回 None（约 5 秒）
        self.assertIsNone(await self.a.client.avatar("艾咪"))


class MentionOthersTest(Base):
    async def test_at_others_becomes_at_component(self):
        m = msg("m_at", "g1@chatroom", "wxid_carol", 1, "@大肥鱼\u2005摸 @Alice\u2005")
        m["mentions"].update(self=True, userIds=["wxid_alice", BOT])
        abm = await self.feed(m)
        ats = [c for c in abm.message if isinstance(c, At)]
        self.assertEqual([str(a.qq) for a in ats], [BOT, "wxid_alice"])  # 自己只出现一次，且在最前
        self.assertEqual(ats[1].name, "Alice")


class StaleMessageTest(Base):
    async def test_old_message_is_skipped(self):
        m = msg("old1", OWNER, OWNER, 1, "几个小时前的话", createdAt="2026-01-01T00:00:00Z")
        self.assertIsNone(await self.feed(m))
        self.assertTrue(self.queue.empty())

    async def test_fresh_message_is_handled(self):
        self.assertIsNotNone(await self.feed(msg("new1", OWNER, OWNER, 1, "刚说的话")))


class RecallTest(Base):
    async def test_recalled_before_reply_is_not_sent(self):
        await self.feed(msg("r1", OWNER, OWNER, 1, "发错了", serverId="4242"))
        ev = self.queue.get_nowait()
        await self.a.handle_event({"cursor": "2", "kind": "message.recalled",
                                   "data": {"threadId": OWNER, "serverId": "4242"}})
        await ev.send(MessageChain([Plain("收到")]))
        self.assertEqual(self.d.sends, [])

    async def test_not_recalled_is_sent(self):
        await self.feed(msg("r2", OWNER, OWNER, 1, "在吗", serverId="4343"))
        ev = self.queue.get_nowait()
        await self.a.handle_event({"cursor": "2", "kind": "message.recalled",
                                   "data": {"threadId": OWNER, "messageId": "other", "serverId": "1"}})
        await ev.send(MessageChain([Plain("在")]))
        self.assertEqual(self.d.sends[0]["text"], "在")
