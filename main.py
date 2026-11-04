import time
from datetime import datetime, timedelta, timezone

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

# 导入即注册 wechat_agent 平台适配器
from .wechat_adapter import WeChatAgentAdapter  # noqa: F401

CST = timezone(timedelta(hours=8))
MEDIA_TEXT = {3: "[图片]", 47: "[表情]", 34: "[语音]", 43: "[视频]", 48: "[位置]", 42: "[名片]"}
DEFAULTS = {
    "bot_name": "机器人",
    "summary_prompt": (
        "你是群里的「{bot_name}」。下面是群聊记录（时间 昵称: 内容）。"
        "请把它总结成 3 到 6 条要点，每条一行，用口语、简短，提到谁说的就用昵称；"
        "只总结记录里真有的内容，不编造，闲聊刷屏的部分一句带过；不要用 Markdown 符号。"
    ),
    "summary_hours": 24,
    "summary_limit": 200,
    "summary_cooldown": 300,  # 防刷、省钱
}


def format_history(
    items: list[dict], names: dict[str, str], own_id: str, now: datetime,
    bot_name: str = "机器人", hours: int = 24, limit: int = 200,
) -> list[str]:
    """守护进程的消息列表 → 按时间排好的「HH:MM 昵称: 内容」，只取最近 hours 小时、最多 limit 条。"""
    since = now - timedelta(hours=hours)
    lines = []
    for m in sorted(items, key=lambda m: str(m.get("createdAt") or "")):
        try:
            at = datetime.fromisoformat(str(m.get("createdAt")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if at < since:
            continue
        mtype = m.get("type")
        text = (m.get("body") or "").replace("\n", " ").strip() if mtype in (1, 49) else MEDIA_TEXT.get(mtype, "")
        if not text:
            continue
        sender = str(m.get("senderId") or "")
        who = bot_name if sender == own_id else names.get(sender, sender)
        lines.append(f"{at.astimezone(CST):%H:%M} {who}: {text[:200]}")
    return lines[-limit:]


class WeChatAgentPlugin(Star):
    """注册微信平台适配器；另提供微信群「总结群聊」指令。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.cfg = {**DEFAULTS, **(config or {})}
        self._last_summary: dict[str, float] = {}

    @filter.command("总结群聊", alias={"群聊总结", "群聊摘要"})
    async def summarize_group(self, event: AstrMessageEvent):
        """总结本群最近的聊天（默认 24 小时内、最多 200 条）。"""
        adapter = getattr(event, "platform", None)
        group_id = event.get_group_id()
        if event.get_platform_name() != "wechat_agent" or not group_id or adapter is None:
            return
        cooldown = int(self.cfg["summary_cooldown"])
        wait = cooldown - (time.monotonic() - self._last_summary.get(group_id, -cooldown))
        if wait > 0:
            yield event.plain_result(f"刚总结过呀，{int(wait // 60) + 1} 分钟后再来吧")
            return
        self._last_summary[group_id] = time.monotonic()
        bot_name = str(self.cfg["bot_name"])
        try:
            limit = int(self.cfg["summary_limit"])
            items = await adapter.client.thread_messages(group_id, limit=limit)
            names = {}
            for sender in {str(m.get("senderId") or "") for m in items}:
                if sender and sender != adapter.own_id:
                    names[sender] = await adapter._nickname(group_id, sender, True)
            lines = format_history(items, names, adapter.own_id, datetime.now(timezone.utc),
                                   bot_name, int(self.cfg["summary_hours"]), limit)
            if len(lines) < 5:
                yield event.plain_result("最近群里没聊几句，没什么好总结的～")
                return
            provider = self.context.get_using_provider(event.unified_msg_origin)
            prompt = str(self.cfg["summary_prompt"]).replace("{bot_name}", bot_name)
            resp = await provider.text_chat(prompt="\n".join(lines), system_prompt=prompt)
            yield event.plain_result((resp.completion_text or "").strip() or "唔，总结失败了，晚点再试试")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[wechat_agent] 群聊总结失败: {type(e).__name__}: {e}")
            self._last_summary.pop(group_id, None)
            yield event.plain_result("唔，总结失败了，晚点再试试")
