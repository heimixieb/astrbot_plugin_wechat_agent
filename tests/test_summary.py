import unittest
from datetime import datetime, timedelta, timezone

from astrbot.core import astrbot_config

from astrbot_plugin_wechat_agent.main import format_history

astrbot_config["trace_enable"] = False
NOW = datetime(2026, 11, 4, 4, 0, tzinfo=timezone.utc)  # 北京时间 12:00


def m(minutes_ago, sender, mtype=1, body=""):
    return {"createdAt": (NOW - timedelta(minutes=minutes_ago)).isoformat(), "senderId": sender, "type": mtype, "body": body}


class FormatHistoryTest(unittest.TestCase):
    def test_orders_names_media_and_window(self):
        items = [
            m(5, "bot", body="我在呢"),
            m(10, "wxid_a", body="大肥鱼在吗"),
            m(8, "wxid_b", 3),
            m(60 * 25, "wxid_a", body="昨天的话"),  # 超过 24 小时
            m(7, "wxid_b", 10002),  # 系统消息，不要
        ]
        lines = format_history(items, {"wxid_a": "艾咪", "wxid_b": "乌云"}, "bot", NOW, bot_name="大肥鱼")
        self.assertEqual(lines, ["11:50 艾咪: 大肥鱼在吗", "11:52 乌云: [图片]", "11:55 大肥鱼: 我在呢"])

    def test_keeps_latest_200(self):
        items = [m(i, "wxid_a", body=f"第{i}条") for i in range(300)]
        lines = format_history(items, {}, "bot", NOW)
        self.assertEqual(len(lines), 200)
        self.assertTrue(lines[-1].endswith("第0条"))
