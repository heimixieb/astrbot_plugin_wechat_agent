from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain

if TYPE_CHECKING:  # pragma: no cover
    from .wechat_adapter import WeChatAgentAdapter


class WeChatAgentMessageEvent(AstrMessageEvent):
    def __init__(self, message_str, message_obj, platform_meta, session_id, platform: "WeChatAgentAdapter"):
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self.platform = platform
        # 原生消息（用于引用回复）
        self.native_message_id: str = str(message_obj.message_id)
        self.native_thread_id: str = getattr(message_obj, "native_thread_id", session_id)

    async def send(self, message: MessageChain) -> None:
        if not message or not message.chain:
            return
        await self.platform.send_chain(
            self.native_thread_id, message, reply_message_id=self.native_message_id
        )
        await super().send(message)

    async def send_streaming(self, generator, use_fallback: bool = False):
        # 微信不支持流式：聚合后一次发出；非文本段单独发
        buffer = ""
        async for chain in generator:
            if not isinstance(chain, MessageChain):
                continue
            for comp in chain.chain:
                if isinstance(comp, Plain):
                    buffer += comp.text
                else:
                    if buffer.strip():
                        await self.send(MessageChain([Plain(buffer)]))
                        buffer = ""
                    await self.send(MessageChain(chain=[comp]))
                    await asyncio.sleep(0.8)
        if buffer.strip():
            await self.send(MessageChain([Plain(buffer)]))
        return await super().send_streaming(_empty(), use_fallback)


async def _empty():
    if False:  # pragma: no cover
        yield None
