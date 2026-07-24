"""多段气泡发送：拆分回复并带短延迟，更像真人连发。"""

from __future__ import annotations

import asyncio
import random
import re
from collections.abc import Awaitable, Callable

from nonebot.adapters.onebot.v11 import Message, MessageSegment

BUBBLE_HINT = (
    "若确有必要拆成多条消息发送，用单独一行的 --- 分隔各段；"
    "群聊最多三段，私聊最多四段。"
    "闲聊一般仍只回一段一两句；认真帮忙时才按逻辑拆成多段，每段一个意思。"
)


def split_bubbles(
    reply: str,
    *,
    is_group: bool = False,
    max_group: int = 3,
    max_private: int = 4,
) -> list[str]:
    """把模型回复拆成多段；无分隔符时尽量保持单段。"""
    text = (reply or "").strip()
    if not text:
        return []

    parts: list[str] = []
    if "\n---\n" in text or text.startswith("---\n") or "\n---" in text:
        raw = re.split(r"\n\s*---\s*\n", text)
        parts = [p.strip() for p in raw if p.strip()]
    else:
        # 无显式分隔：私聊偶发按句号轻拆（仅当明显两句且都不长）
        parts = [text]

    # 去掉残留的纯 ---
    parts = [p for p in parts if p and p != "---"]

    limit = max_group if is_group else max_private
    if len(parts) > limit:
        # 超出部分合并到最后一段，避免刷屏
        head, tail = parts[: limit - 1], parts[limit - 1 :]
        parts = head + [" ".join(tail)]

    # 群聊硬闸：单段仍尽量短，但认真帮忙时放宽到 200 字
    cleaned: list[str] = []
    for p in parts:
        if is_group and len(p) > 200:
            p = p[:200] + "…"
        cleaned.append(p)
    return cleaned


def merge_for_memory(parts: list[str]) -> str:
    """写入短期记忆时合并为一次完整回复。"""
    return "\n".join(p.strip() for p in parts if p.strip())


async def send_bubbles(
    send: Callable[[str | Message], Awaitable[object]],
    reply: str,
    *,
    is_group: bool = False,
    at_user_id: int | None = None,
    delay_min: float = 0.4,
    delay_max: float = 1.5,
) -> str:
    """
    按段发送；返回合并后的完整文本（供记忆）。
    at_user_id 仅作用于第一段（群聊多人互动时）。
    """
    parts = split_bubbles(reply, is_group=is_group)
    if not parts:
        return ""

    for i, part in enumerate(parts):
        if i > 0:
            await asyncio.sleep(random.uniform(delay_min, delay_max))
        if i == 0 and at_user_id is not None and is_group:
            msg: str | Message = Message(
                [
                    MessageSegment.at(at_user_id),
                    MessageSegment.text(f" {part}"),
                ]
            )
        else:
            msg = part
        await send(msg)

    return merge_for_memory(parts)
