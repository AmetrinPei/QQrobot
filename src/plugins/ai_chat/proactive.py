"""群聊定时主动插话 + 可选的私聊低频关心。"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Callable, Deque

from nonebot import get_bots, logger
from nonebot.adapters.onebot.v11 import Bot
from openai import AsyncOpenAI

from . import bot_control as botctl
from . import bubble
from . import mood
from . import session as sess
from . import time_context as tctx

NO_REPLY_TOKEN = "[NO_REPLY]"

PROACTIVE_SYSTEM_NOTE = (
    "这是群聊「定时旁听」：下面是最近一段时间群友的聊天摘录，"
    "不是在单独 @ 你。你可以选择：\n"
    "- 能自然轻轻接一句、不抢戏、不总结发言：按人设回一句短话\n"
    "- 插不上嘴、话题不合适、或接了会很尬：只输出 "
    f"{NO_REPLY_TOKEN} ，不要输出其他任何内容\n"
    "注意：不要连续追问，不要长篇，不要假装听懂没出现的内容。"
)

PRIVATE_CARE_NOTE = (
    "这是「私聊主动关心」：你很久没和对方私聊了，可以主动发一句极短的关心或打招呼。"
    "要求：\n"
    "- 只回一句话，不要追问连环问题，不要解释为什么突然说话\n"
    "- 语气符合人设与当前心情；若觉得不合适，只输出 "
    f"{NO_REPLY_TOKEN}\n"
    "- 不要提「系统」「定时任务」"
)


@dataclass
class GroupChatLine:
    user_id: int
    name: str
    text: str
    has_image: bool
    ts: float
    message_id: int | None = None


_buffers: dict[int, Deque[GroupChatLine]] = defaultdict(
    lambda: deque(maxlen=50)
)
_last_consume_ts: dict[int, float] = {}
_last_bot_speak_ts: dict[int, float] = {}
_last_private_care_ts: dict[int, float] = {}
_task: asyncio.Task | None = None
_private_task: asyncio.Task | None = None


def record_group_line(
    group_id: int,
    *,
    user_id: int,
    name: str,
    text: str,
    has_image: bool = False,
    message_id: int | None = None,
) -> None:
    text = (text or "").strip()
    if not text and not has_image:
        return
    display = text if text else "[图片/表情]"
    _buffers[int(group_id)].append(
        GroupChatLine(
            user_id=int(user_id),
            name=(name or "未知").strip() or "未知",
            text=display[:200],
            has_image=has_image,
            ts=time.time(),
            message_id=message_id,
        )
    )


def mark_bot_spoke(group_id: int) -> None:
    _last_bot_speak_ts[int(group_id)] = time.time()


def bot_recently_spoke_in_group(group_id: int, window_seconds: int) -> bool:
    """机器人是否在 window_seconds 秒内在该群说过话。"""
    last = _last_bot_speak_ts.get(int(group_id), 0.0)
    return (time.time() - last) < window_seconds


def _new_lines_since(group_id: int, since_ts: float) -> list[GroupChatLine]:
    return [line for line in _buffers[int(group_id)] if line.ts > since_ts]


def _format_lines(lines: list[GroupChatLine]) -> str:
    out: list[str] = []
    for line in lines:
        tag = "（含图）" if line.has_image else ""
        out.append(f"{line.name}(QQ:{line.user_id}){tag}：{line.text}")
    return "\n".join(out)


def _pick_bot() -> Bot | None:
    bots = get_bots()
    for bot in bots.values():
        if isinstance(bot, Bot):
            return bot
    return None


def _build_system(system_prompt: str, extra_note: str) -> str:
    mood.apply_time_drift()
    parts = [
        system_prompt,
        tctx.format_time_context(),
        mood.format_mood_for_prompt(),
        bubble.BUBBLE_HINT,
        extra_note,
    ]
    return "\n\n".join(parts)


async def run_proactive_tick(
    client: AsyncOpenAI,
    *,
    model: str,
    system_prompt: str,
    interval_hint_sec: int,
    min_messages: int,
    cooldown_sec: int,
    max_context: int,
) -> None:
    """对每个有足够新消息的群尝试主动插一句。"""
    if botctl.is_paused():
        return
    bot = _pick_bot()
    if bot is None:
        return

    now = time.time()
    group_ids = list(_buffers.keys())
    for gid in group_ids:
        since = _last_consume_ts.get(gid, 0.0)
        lines = _new_lines_since(gid, since)
        if len(lines) < min_messages:
            continue

        last_speak = _last_bot_speak_ts.get(gid, 0.0)
        if now - last_speak < cooldown_sec:
            _last_consume_ts[gid] = now
            continue

        context_lines = lines[-max_context:]
        transcript = _format_lines(context_lines)
        user_payload = (
            f"最近约 {max(1, interval_hint_sec // 60)} 分钟内的群聊摘录：\n"
            f"{transcript}\n\n"
            "若能自然接一句就回一句；插不上嘴只输出 "
            f"{NO_REPLY_TOKEN}。"
        )

        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": _build_system(
                            system_prompt, PROACTIVE_SYSTEM_NOTE
                        ),
                    },
                    {"role": "user", "content": user_payload},
                ],
                max_tokens=256,
                temperature=0.7,
            )
            reply = (response.choices[0].message.content or "").strip()
        except Exception as e:
            logger.warning(f"群 {gid} 主动插话调用失败：{e}")
            _last_consume_ts[gid] = now
            continue

        _last_consume_ts[gid] = now

        if not reply or reply == NO_REPLY_TOKEN or reply.startswith(NO_REPLY_TOKEN):
            logger.info(f"群 {gid} 主动插话：保持沉默")
            continue

        if len(reply) > 200:
            reply = reply[:200] + "…"

        try:
            async def _send(msg):
                await bot.send_group_msg(group_id=gid, message=msg)

            merged = await bubble.send_bubbles(
                _send, reply, is_group=True, delay_min=0.35, delay_max=1.0
            )
            mark_bot_spoke(gid)
            try:
                sk = sess.session_key_for(
                    is_private=False, user_id=0, group_id=gid
                )
                sess.append_turn(
                    sk,
                    user_content="[群聊旁听后主动接话]",
                    assistant_content=merged or reply,
                    max_turns=16,
                )
            except Exception:
                pass
            logger.info(f"群 {gid} 主动插话：{merged or reply}")
        except Exception as e:
            logger.warning(f"群 {gid} 主动插话发送失败：{e}")


async def run_private_care_tick(
    client: AsyncOpenAI,
    *,
    model: str,
    system_prompt: str,
    idle_seconds: int,
    cooldown_seconds: int,
    min_tier: str,
    whitelist: set[str],
) -> None:
    """对久未私聊的高关系用户，偶尔发一句轻量关心。"""
    if botctl.is_paused():
        return
    bot = _pick_bot()
    if bot is None:
        return

    now = time.time()
    candidates = sess.list_private_care_candidates(
        min_tier=min_tier,
        idle_seconds=float(idle_seconds),
        limit=8,
    )
    if whitelist:
        candidates = [
            c for c in candidates if str(c["user_id"]) in whitelist
        ]

    for c in candidates:
        uid = int(c["user_id"])
        last_care = _last_private_care_ts.get(uid, 0.0)
        if now - last_care < cooldown_seconds:
            continue

        alias = (c.get("alias") or c.get("nickname") or "朋友").strip()
        tier = sess.normalize_tier(c.get("tier"))
        name_line = f"{alias}(QQ:{uid})"
        user_payload = (
            f"对方：{name_line}\n"
            f"关系：{tier}\n"
            f"{sess.relation_prompt_note(tier)}\n"
            "请决定是否发一句极短关心；不合适就输出 "
            f"{NO_REPLY_TOKEN}。"
        )

        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": _build_system(
                            system_prompt, PRIVATE_CARE_NOTE
                        ),
                    },
                    {"role": "user", "content": user_payload},
                ],
                max_tokens=128,
                temperature=0.7,
            )
            reply = (response.choices[0].message.content or "").strip()
        except Exception as e:
            logger.warning(f"私聊关心调用失败 uid={uid}：{e}")
            _last_private_care_ts[uid] = now
            continue

        _last_private_care_ts[uid] = now

        if not reply or reply == NO_REPLY_TOKEN or reply.startswith(NO_REPLY_TOKEN):
            logger.info(f"私聊关心 uid={uid}：保持沉默")
            continue

        if len(reply) > 120:
            reply = reply[:120] + "…"

        try:
            async def _send(msg, _uid=uid):
                await bot.send_private_msg(user_id=_uid, message=msg)

            merged = await bubble.send_bubbles(
                _send, reply, is_group=False, delay_min=0.4, delay_max=1.2
            )
            try:
                sk = sess.session_key_for(is_private=True, user_id=uid)
                sess.append_turn(
                    sk,
                    user_content="[主动关心]",
                    assistant_content=merged or reply,
                    max_turns=16,
                )
                sess.mark_private_seen(uid)
            except Exception:
                pass
            logger.info(f"私聊关心 uid={uid}：{merged or reply}")
            # 一轮只关心一个人，避免连发
            break
        except Exception as e:
            logger.warning(f"私聊关心发送失败 uid={uid}：{e}")


async def proactive_loop(
    client: AsyncOpenAI,
    *,
    model: str,
    system_prompt_loader: Callable[[], str],
    interval_sec: int,
    min_messages: int,
    cooldown_sec: int,
    max_context: int,
) -> None:
    logger.info(
        f"群聊主动插话已启动：每 {interval_sec}s 检查，"
        f"至少 {min_messages} 条新消息才考虑开口"
    )
    while True:
        try:
            await asyncio.sleep(max(30, interval_sec))
            await run_proactive_tick(
                client,
                model=model,
                system_prompt=system_prompt_loader(),
                interval_hint_sec=interval_sec,
                min_messages=min_messages,
                cooldown_sec=cooldown_sec,
                max_context=max_context,
            )
        except asyncio.CancelledError:
            logger.info("群聊主动插话任务已停止")
            raise
        except Exception as e:
            logger.warning(f"群聊主动插话循环异常：{e}")


async def private_care_loop(
    client: AsyncOpenAI,
    *,
    model: str,
    system_prompt_loader: Callable[[], str],
    interval_sec: int,
    idle_seconds: int,
    cooldown_seconds: int,
    min_tier: str,
    whitelist: set[str],
) -> None:
    logger.info(
        f"私聊主动关心已启动：每 {interval_sec}s 检查，"
        f"闲置≥{idle_seconds}s 且关系≥{min_tier}"
    )
    while True:
        try:
            await asyncio.sleep(max(60, interval_sec))
            await run_private_care_tick(
                client,
                model=model,
                system_prompt=system_prompt_loader(),
                idle_seconds=idle_seconds,
                cooldown_seconds=cooldown_seconds,
                min_tier=min_tier,
                whitelist=whitelist,
            )
        except asyncio.CancelledError:
            logger.info("私聊主动关心任务已停止")
            raise
        except Exception as e:
            logger.warning(f"私聊主动关心循环异常：{e}")


def start_proactive_task(
    client: AsyncOpenAI,
    *,
    model: str,
    system_prompt_loader: Callable[[], str],
    interval_sec: int,
    min_messages: int,
    cooldown_sec: int,
    max_context: int,
) -> None:
    global _task
    if _task and not _task.done():
        return
    _task = asyncio.create_task(
        proactive_loop(
            client,
            model=model,
            system_prompt_loader=system_prompt_loader,
            interval_sec=interval_sec,
            min_messages=min_messages,
            cooldown_sec=cooldown_sec,
            max_context=max_context,
        )
    )


def start_private_care_task(
    client: AsyncOpenAI,
    *,
    model: str,
    system_prompt_loader: Callable[[], str],
    interval_sec: int,
    idle_seconds: int,
    cooldown_seconds: int,
    min_tier: str,
    whitelist: set[str],
) -> None:
    global _private_task
    if _private_task and not _private_task.done():
        return
    _private_task = asyncio.create_task(
        private_care_loop(
            client,
            model=model,
            system_prompt_loader=system_prompt_loader,
            interval_sec=interval_sec,
            idle_seconds=idle_seconds,
            cooldown_seconds=cooldown_seconds,
            min_tier=min_tier,
            whitelist=whitelist,
        )
    )
