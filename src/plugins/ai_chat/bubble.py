"""多段气泡发送：拆分回复并带短延迟，更像真人连发。"""

from __future__ import annotations

import asyncio
import random
import re
from collections.abc import Awaitable, Callable

from nonebot.adapters.onebot.v11 import Message, MessageSegment

BUBBLE_HINT = (
    "发送方式：能拆就拆成多段连发，更像真人微信。"
    "闲聊、接话、吐槽：用单独一行的 --- 分隔各段；短句不要加句尾句号「。」。"
    "可偶尔带一个常见表情（🙂😂😅🙄😏），别每段都加。"
    "只有真正很长的完整罗列才不要拆，保持一整段；长说明可用句号。"
    "群聊最多三段，私聊最多四段；禁止圆括号动作描写。"
)

# 舞台动作：（歪头） / (翻白眼) 等；过长括号多半是正常说明，保留
_STAGE_DIR_RE = re.compile(r"[（(][^（）()\n]{1,24}[）)]")


def strip_stage_directions(text: str) -> str:
    """去掉模型爱加的括号动作，避免每句都（歪头）。"""
    if not text:
        return text
    cleaned = _STAGE_DIR_RE.sub("", text)
    # 收掉动作留下的多余空白
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r" *\n *", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


# 困倦口头禅
_SLEEPY_RE = re.compile(
    r"("
    r"好困[啊哦呢么嘛呀]?|"
    r"有点[儿]?困|有点想睡|好想睡|想睡觉|"
    r"没精神|没有精神|精疲力尽|熬不住|"
    r"困得不行|困死[了啦]|"
    r"困了[啊哦呢么嘛呀]?|"
    r"去睡觉了?|该睡觉了|本该睡觉|我去睡了|我先睡了|"
    r"打了个[哈呵]欠|打哈欠|打呵欠"
    r")"
)


def strip_sleepy_talk(text: str) -> str:
    """削弱「困了/想睡」口头禅；删光后给一句短接话。"""
    if not text:
        return text
    cleaned = _SLEEPY_RE.sub("", text)
    cleaned = re.sub(r"[，,]{2,}", "，", cleaned)
    cleaned = re.sub(r"[。！？]{2,}", "。", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = cleaned.strip(" ，,、")
    cleaned = cleaned.strip()
    if not cleaned or cleaned in ("。", "…", "...", "——", "哼", "哼。", "嗯。"):
        return "嗯"
    return cleaned


_LIST_ITEM_RE = re.compile(
    r"(?:^|\n)\s*(?:\d+[\.、\)）]|[-•·＊*])\s+\S",
    re.MULTILINE,
)
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?…])\s*")


def _is_long_listing(text: str) -> bool:
    """完整长罗列/教程/代码：保持单段，不拆气泡。"""
    if "```" in text:
        return True
    items = _LIST_ITEM_RE.findall(text)
    if len(items) >= 3 and len(text) >= 80:
        return True
    # 多行且偏长的步骤说明
    if text.count("\n") >= 5 and len(text) >= 180:
        return True
    return False


def _split_by_sentences(text: str, *, limit: int) -> list[str] | None:
    """短句闲聊：按句号拆成多段；不适合则返回 None。"""
    chunks = [p.strip() for p in _SENT_SPLIT_RE.split(text) if p and p.strip()]
    if len(chunks) < 2 or len(chunks) > limit:
        return None
    # 任一段过长 / 整体像一篇说明文 → 不拆
    if any(len(c) > 55 for c in chunks):
        return None
    if len(text) > 140:
        return None
    return chunks


def soften_chat_punctuation(text: str) -> str:
    """短句去掉句尾「。」，更像微信；长文/罗列不动。"""
    if not text:
        return text
    s = text.strip()
    if _is_long_listing(s):
        return s
    # 整段较短：去掉末尾句号（保留？！…~）
    if len(s) <= 40 and s.endswith("。"):
        s = s[:-1].rstrip()
    elif len(s) <= 80:
        # 多句短聊：句号改成停顿感更弱——仅去掉「行吧。」这类短分句尾部句号
        s = re.sub(r"(?<=[\u4e00-\u9fffA-Za-z0-9])。(?=\s*$)", "", s)
        s = re.sub(r"(?<=[\u4e00-\u9fffA-Za-z0-9])。(?=\s*\n)", "\n", s)
    return s.strip()


def polish_bubble_part(text: str) -> str:
    """单段发送前润色：去动作、去困倦口头禅、短句去句号。"""
    t = strip_stage_directions(text)
    t = strip_sleepy_talk(t)
    t = soften_chat_punctuation(t)
    return t


def split_bubbles(
    reply: str,
    *,
    is_group: bool = False,
    max_group: int = 3,
    max_private: int = 4,
) -> list[str]:
    """把模型回复拆成多段连发；长罗列保持单段。"""
    text = (reply or "").strip()
    if not text:
        return []

    limit = max_group if is_group else max_private
    parts: list[str] = []

    # 1) 显式 ---
    if re.search(r"\n\s*---\s*\n", text) or text.startswith("---\n"):
        raw = re.split(r"\n\s*---\s*\n", text)
        parts = [p.strip() for p in raw if p.strip() and p.strip() != "---"]
    # 2) 完整长罗列：不拆
    elif _is_long_listing(text):
        parts = [text]
    # 3) 空行分段
    elif "\n\n" in text:
        parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        if len(parts) < 2:
            parts = [text]
    # 4) 短句闲聊按句拆
    else:
        sent = _split_by_sentences(text, limit=limit)
        parts = sent if sent else [text]

    parts = [p for p in parts if p and p != "---"]
    if not parts:
        return []

    if len(parts) > limit:
        head, tail = parts[: limit - 1], parts[limit - 1 :]
        parts = head + ["\n".join(tail)]

    cleaned: list[str] = []
    for p in parts:
        if is_group and len(p) > 200 and not _is_long_listing(p):
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
    delay_min: float = 0.45,
    delay_max: float = 1.35,
) -> str:
    """
    按段发送；返回合并后的完整文本（供记忆）。
    at_user_id 仅作用于第一段（群聊多人互动时）。
    """
    parts = split_bubbles(reply, is_group=is_group)
    if not parts:
        return ""

    polished: list[str] = []
    for p in parts:
        q = polish_bubble_part(p)
        if q:
            polished.append(q)
    parts = polished
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
