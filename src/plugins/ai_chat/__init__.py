"""硅基流动 AI 对话插件。

- 密钥/模型：项目根目录 .env.dev
- 人设与边界：项目根目录 persona.txt
- 短期记忆 + 通讯录/亲疏：data/memory.db；/备注 /关系 /忘记短期 /记忆列表
- 长期记忆：/记住 /忘掉 /长期记忆
- 拟人：时间感知、情绪状态、多段发送、群主动插话、可选私聊关心
- 识图：有图时先走视觉模型描述，再交给文本模型按人设回答
- 触发：群聊 @机器人；@ 后短时间内的跟进文字/表情（必要时才回）；
  回复机器人消息；私聊直接发消息。多人同时聊时回复会带 @
- 定时：每隔一段时间若群里有新消息，可主动轻轻接一句（插不上就沉默）
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from nonebot import get_driver, get_plugin_config, logger, on_message
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.plugin import PluginMetadata
from openai import AsyncOpenAI
from pydantic import BaseModel, Field, field_validator

from . import bubble
from . import memory as mem
from . import mood
from . import proactive as pro
from . import session as sess
from . import time_context as tctx
from . import vision as vis

ROOT = Path(__file__).resolve().parents[3]
PERSONA_FILE = ROOT / "persona.txt"

ADMIN_CMD_PREFIXES = (
    "/记住",
    "/忘掉",
    "/长期记忆",
    "/备注",
    "/关系",
    "/心情",
    "/忘记短期",
    "/记忆列表",
    "/说群",
    "/说私",
    "/说",
)

NO_REPLY_TOKEN = "[NO_REPLY]"

FOLLOWUP_JUDGE_NOTE = (
    "当前是群聊「跟进」消息：对方不久前 @ 过你或与你互动，但这条没有再次 @ 你。"
    "请判断是否在继续和你说话、是否需要你接话：\n"
    "- 需要接话：按人设正常简短回复（不要解释你的判断过程）\n"
    "- 明显在和别人聊、或与你无关的随口一句：只输出 "
    f"{NO_REPLY_TOKEN} ，不要输出其他任何内容"
)


class Config(BaseModel):
    siliconflow_api_key: str = Field(default="")
    siliconflow_base_url: str = Field(default="https://api.siliconflow.cn/v1")
    siliconflow_model: str = Field(default="deepseek-ai/DeepSeek-V4-Pro")
    siliconflow_vision_model: str = Field(
        default="Qwen/Qwen3-VL-8B-Instruct"
    )
    admin_qq: str = Field(
        default="",
        description="管理员 QQ，逗号分隔；仅他们可私聊使用记忆指令",
    )

    @field_validator("admin_qq", mode="before")
    @classmethod
    def _coerce_admin_qq(cls, v: object) -> str:
        if v is None:
            return ""
        return str(v).strip()

    memory_short_term_turns: int = Field(
        default=16,
        description="按会话保留最近 N 轮对话（私聊按用户，群聊按群）",
    )
    memory_long_term_enabled: bool = Field(default=True)
    memory_long_term_group: bool = Field(
        default=False,
        description="群聊是否自动抽取长期记忆（默认关闭，避免误记吹水）",
    )
    memory_long_term_retrieve: int = Field(default=8)
    memory_long_term_max: int = Field(default=200)
    group_followup_seconds: int = Field(
        default=180,
        description="群内与某人互动后，多少秒内其消息视为可能跟进",
    )
    group_followup_image_seconds: int | None = Field(
        default=None,
        description="已弃用，请用 group_followup_seconds",
    )
    group_at_when_active: int = Field(
        default=2,
        description="群内窗口内互动人数 >= 该值时，回复前加 @对方",
    )
    # 定时主动插话
    group_proactive_enabled: bool = Field(default=True)
    group_proactive_interval_seconds: int = Field(
        default=600,
        description="每隔多少秒检查一次群聊新消息并考虑插话",
    )
    group_proactive_min_messages: int = Field(
        default=2,
        description="窗口内至少多少条新消息才考虑主动开口",
    )
    group_proactive_cooldown_seconds: int = Field(
        default=120,
        description="机器人刚在该群说过话后，多少秒内不再主动插话",
    )
    group_proactive_max_context: int = Field(
        default=20,
        description="主动插话时最多带多少条近期消息给模型",
    )
    # 私聊主动关心（默认关闭）
    private_proactive_enabled: bool = Field(
        default=False,
        description="是否对高关系用户低频私聊关心",
    )
    private_proactive_interval_seconds: int = Field(
        default=3600,
        description="私聊关心检查间隔（秒）",
    )
    private_proactive_idle_seconds: int = Field(
        default=86400,
        description="多久未私聊才考虑主动关心（秒，默认 1 天）",
    )
    private_proactive_cooldown_seconds: int = Field(
        default=172800,
        description="同一人两次主动关心最短间隔（秒，默认 2 天）",
    )
    private_proactive_min_tier: str = Field(
        default="朋友",
        description="主动关心最低亲疏：熟人/朋友/家人",
    )
    private_proactive_whitelist: str = Field(
        default="",
        description="可选 QQ 白名单（逗号分隔）；非空则只关心名单内用户",
    )

    @field_validator("private_proactive_whitelist", mode="before")
    @classmethod
    def _coerce_whitelist(cls, v: object) -> str:
        if v is None:
            return ""
        return str(v).strip()

    def followup_window(self) -> int:
        # 兼容旧配置 GROUP_FOLLOWUP_IMAGE_SECONDS
        if self.group_followup_image_seconds is not None:
            return int(self.group_followup_image_seconds)
        return int(self.group_followup_seconds)

    def private_whitelist_set(self) -> set[str]:
        raw = (self.private_proactive_whitelist or "").strip()
        if not raw:
            return set()
        return {p.strip() for p in raw.split(",") if p.strip()}


__plugin_meta__ = PluginMetadata(
    name="硅基流动AI聊天",
    description="调用硅基流动模型回复 QQ 消息；支持短期/长期记忆、识图与拟人状态",
    usage=(
        "私聊直接发消息；群聊 @机器人；@ 后短时间内跟进消息必要时会接；"
        "多人同时聊时回复带 @；定时旁听群聊必要时主动接一句。"
        "人设见 persona.txt。"
        "管理员私聊：/备注 /关系 /心情 /忘记短期 /记忆列表 /记住 /忘掉 /长期记忆"
    ),
    config=Config,
)

config = get_plugin_config(Config)

client = AsyncOpenAI(
    api_key=config.siliconflow_api_key or "missing",
    base_url=config.siliconflow_base_url,
)


def load_system_prompt() -> str:
    if PERSONA_FILE.is_file():
        text = PERSONA_FILE.read_text(encoding="utf-8").strip()
        if text:
            return text
    return "你是一个有用的QQ助手，回答简洁清晰。"


def parse_admin_ids() -> set[str]:
    raw = (config.admin_qq or "").strip()
    if not raw:
        return set()
    return {p.strip() for p in raw.split(",") if p.strip()}


def is_admin(event: MessageEvent) -> bool:
    return str(event.user_id) in parse_admin_ids()


def raw_display_name(event: MessageEvent) -> str:
    sender = event.sender
    name = ""
    if sender is not None:
        name = (getattr(sender, "card", None) or "") or (
            getattr(sender, "nickname", None) or ""
        )
    return (name or "未知").strip()


def speaker_label(event: MessageEvent) -> str:
    """优先用通讯录备注，否则群名片/昵称；附带亲疏。"""
    alias = sess.get_alias(event.user_id)
    name = alias or raw_display_name(event)
    tier = sess.get_tier(event.user_id)
    return f"{name}(QQ:{event.user_id}，{tier})"


def build_system_prompt(
    *,
    scene: str,
    followup: bool = False,
    relation_tier: str | None = None,
) -> str:
    """组装完整 system：人设 + 时间 + 情绪 + 关系 + 气泡提示。"""
    mood.apply_time_drift()
    parts = [
        load_system_prompt(),
        tctx.format_time_context(),
        mood.format_mood_for_prompt(),
        bubble.BUBBLE_HINT,
        f"当前场景：{scene}。请按上述人设与边界回答。",
    ]
    if relation_tier:
        parts.append(sess.relation_prompt_note(relation_tier))
    if followup:
        parts.append(FOLLOWUP_JUDGE_NOTE)
    return "\n\n".join(parts)


def current_session_key(event: MessageEvent) -> str:
    if isinstance(event, PrivateMessageEvent):
        return sess.session_key_for(is_private=True, user_id=event.user_id)
    assert isinstance(event, GroupMessageEvent)
    return sess.session_key_for(
        is_private=False,
        user_id=event.user_id,
        group_id=event.group_id,
    )


def is_reply_to_me(event: MessageEvent) -> bool:
    """是否回复了机器人自己的消息。"""
    reply = getattr(event, "reply", None)
    if reply is None:
        return False
    sender = getattr(reply, "sender", None)
    if sender is None:
        return False
    try:
        return int(sender.user_id) == int(event.self_id)
    except (TypeError, ValueError):
        return str(sender.user_id) == str(event.self_id)


def message_at_others_only(event: GroupMessageEvent) -> bool:
    """消息里 @ 了别人且没有 @ 机器人 → 多半不是对机器人说。"""
    saw_other = False
    for seg in event.message:
        if seg.type != "at":
            continue
        qq = str((seg.data or {}).get("qq", "")).strip()
        if not qq or qq == "all":
            continue
        if qq == str(event.self_id):
            return False
        saw_other = True
    return saw_other


def is_group_followup(event: MessageEvent) -> bool:
    """近期互动过、未再 @ 机器人的跟进消息（文字或图）。"""
    if not isinstance(event, GroupMessageEvent):
        return False
    if event.is_tome() or is_reply_to_me(event):
        return False
    if message_at_others_only(event):
        return False
    if not sess.recently_group_interacted(
        event.group_id,
        event.user_id,
        window_seconds=config.followup_window(),
    ):
        return False
    text = (event.get_plaintext() or "").strip()
    has_img = vis.message_has_image(event)
    if not text and not has_img:
        return False
    return True


async def should_handle_message(event: MessageEvent) -> bool:
    """私聊一律；群聊 @ / 回复机器人 / 互动窗口内的跟进消息。"""
    if isinstance(event, PrivateMessageEvent):
        return True
    if event.is_tome():
        return True
    if is_reply_to_me(event):
        return True
    if is_group_followup(event):
        return True
    return False


chat = on_message(rule=should_handle_message, priority=10, block=True)


async def _is_group_message(event: MessageEvent) -> bool:
    return isinstance(event, GroupMessageEvent)


# 旁听所有群消息（不拦截），供定时主动插话用
group_watch = on_message(rule=_is_group_message, priority=99, block=False)


@group_watch.handle()
async def _collect_group_chat(event: GroupMessageEvent):
    if int(event.user_id) == int(event.self_id):
        return
    text = (event.get_plaintext() or "").strip()
    # 去掉纯 @ 机器人残留，旁听时仍保留内容
    text = re.sub(r"@\S+", "", text).strip()
    pro.record_group_line(
        int(event.group_id),
        user_id=int(event.user_id),
        name=raw_display_name(event),
        text=text,
        has_image=vis.message_has_image(event),
        message_id=getattr(event, "message_id", None),
    )


@get_driver().on_startup
async def _start_proactive_loop():
    if not config.siliconflow_api_key or "粘贴" in config.siliconflow_api_key:
        logger.warning("未配置 API Key，跳过主动任务")
        return
    # 启动时对齐一次作息精力
    try:
        mood.apply_time_drift()
    except Exception as e:
        logger.warning(f"初始化心情状态失败：{e}")

    if config.group_proactive_enabled:
        pro.start_proactive_task(
            client,
            model=config.siliconflow_model,
            system_prompt_loader=load_system_prompt,
            interval_sec=config.group_proactive_interval_seconds,
            min_messages=config.group_proactive_min_messages,
            cooldown_sec=config.group_proactive_cooldown_seconds,
            max_context=config.group_proactive_max_context,
        )
    else:
        logger.info("群聊主动插话已关闭（GROUP_PROACTIVE_ENABLED=false）")

    if config.private_proactive_enabled:
        pro.start_private_care_task(
            client,
            model=config.siliconflow_model,
            system_prompt_loader=load_system_prompt,
            interval_sec=config.private_proactive_interval_seconds,
            idle_seconds=config.private_proactive_idle_seconds,
            cooldown_seconds=config.private_proactive_cooldown_seconds,
            min_tier=config.private_proactive_min_tier,
            whitelist=config.private_whitelist_set(),
        )
    else:
        logger.info("私聊主动关心已关闭（PRIVATE_PROACTIVE_ENABLED=false）")


def _strip_at_text(event: MessageEvent, text: str) -> str:
    """去掉群聊里残留的 @机器人 纯文本。"""
    text = text.strip()
    if isinstance(event, GroupMessageEvent):
        text = re.sub(r"^@\S+\s*", "", text).strip()
    return text


def _is_no_reply(reply: str) -> bool:
    s = (reply or "").strip()
    if s == NO_REPLY_TOKEN:
        return True
    if re.fullmatch(r"\[NO_REPLY\][.。!！]*", s):
        return True
    return False


def should_at_in_group(event: GroupMessageEvent) -> bool:
    """多人同时互动时，回复带上 @对方。"""
    active = sess.count_recent_group_interactors(
        event.group_id,
        window_seconds=config.followup_window(),
    )
    return active >= max(2, config.group_at_when_active)


async def _handle_admin_say(
    bot: Bot, event: PrivateMessageEvent, text: str
) -> str | None:
    """
    /说群 群号 内容
    /说私 QQ号 内容
    /说 群 群号 内容
    /说 私 QQ号 内容
    内容原样由机器人发出。
    """
    target_type: str | None = None
    rest = ""

    if text.startswith("/说群"):
        target_type = "group"
        rest = text[len("/说群") :].strip()
    elif text.startswith("/说私"):
        target_type = "private"
        rest = text[len("/说私") :].strip()
    elif text == "/说" or text.startswith("/说 "):
        rest0 = text[len("/说") :].strip()
        if not rest0:
            return (
                "用法：\n"
                "/说群 群号 内容\n"
                "/说私 QQ号 内容\n"
                "或：/说 群 群号 内容\n"
                "或：/说 私 QQ号 内容"
            )
        parts = rest0.split(None, 1)
        kind = parts[0]
        rest = parts[1].strip() if len(parts) > 1 else ""
        if kind in ("群", "group", "g"):
            target_type = "group"
        elif kind in ("私", "私聊", "private", "p"):
            target_type = "private"
        else:
            return "用法：/说 群 群号 内容  或  /说 私 QQ号 内容"
    else:
        return None

    if not rest:
        if target_type == "group":
            return "用法：/说群 群号 内容"
        return "用法：/说私 QQ号 内容"

    parts = rest.split(None, 1)
    if len(parts) < 2:
        return "还要写上要说的内容哦。"
    target_id, content = parts[0].strip(), parts[1].strip()
    if not target_id.isdigit():
        return "群号/QQ号要是数字哦。"
    if not content:
        return "内容不能为空。"
    if len(content) > 1500:
        content = content[:1500] + "…"

    try:
        if target_type == "group":
            gid = int(target_id)
            await bot.send_group_msg(group_id=gid, message=content)
            pro.mark_bot_spoke(gid)
            try:
                sk = sess.session_key_for(is_private=False, user_id=0, group_id=gid)
                sess.append_turn(
                    sk,
                    user_content="[管理员指定发言]",
                    assistant_content=content,
                    max_turns=config.memory_short_term_turns,
                )
            except Exception:
                pass
            return f"已在群 {gid} 说：{content}"

        uid = int(target_id)
        await bot.send_private_msg(user_id=uid, message=content)
        try:
            sk = sess.session_key_for(is_private=True, user_id=uid)
            sess.append_turn(
                sk,
                user_content="[管理员指定发言]",
                assistant_content=content,
                max_turns=config.memory_short_term_turns,
            )
        except Exception:
            pass
        return f"已私聊 {uid} 说：{content}"
    except Exception as e:
        return f"发送失败：{e}"


async def handle_admin_memory_cmd(
    bot: Bot, event: PrivateMessageEvent, text: str
) -> str | None:
    """处理管理员记忆/代发指令；非指令返回 None。"""
    if not text.startswith("/"):
        return None
    if not is_admin(event):
        if text.startswith(ADMIN_CMD_PREFIXES):
            return "这个只有哥哥能用哦。"
        return None

    # ----- 代发：内容由管理员指定，原样发出 -----
    say = await _handle_admin_say(bot, event, text)
    if say is not None:
        return say

    if text == "/记忆列表" or text.startswith("/记忆列表 "):
        contacts = sess.list_contacts(limit=40, only_aliased=True)
        if not contacts:
            return "还没有备注过任何人。用法：/备注 QQ号 称呼"
        lines = [
            f"{i}. {c['user_id']} → {c['alias']}"
            + (f"（{c.get('tier') or '陌生人'}）" if c.get("tier") else "")
            + (f"（昵称:{c['nickname']}）" if c.get("nickname") else "")
            for i, c in enumerate(contacts, 1)
        ]
        body = "\n".join(lines)
        if len(body) > 1400:
            body = body[:1400] + "…"
        return f"通讯录备注（{len(contacts)} 人）：\n{body}"

    if text == "/心情" or text.startswith("/心情 "):
        arg = text[len("/心情") :].strip()
        if not arg:
            st = mood.get_state()
            trigger = st.get("last_trigger") or "无"
            return (
                f"当前心情：{st['mood']}，精力 {st['energy']}/100\n"
                f"最近触发：{trigger}"
            )
        if arg in ("重置", "reset"):
            mood.set_state(mood="平静", energy=70, last_trigger="管理员重置")
            return "已重置心情为平静、精力 70。"
        parts = arg.split(None, 1)
        m = parts[0]
        if m not in mood.VALID_MOODS:
            return f"用法：/心情\n或：/心情 平静|开心|害羞|委屈|烦躁 [精力0-100]\n或：/心情 重置"
        energy = None
        if len(parts) > 1 and parts[1].isdigit():
            energy = int(parts[1])
        mood.set_state(mood=m, energy=energy, last_trigger="管理员设定")
        st = mood.get_state()
        return f"已设定心情：{st['mood']}，精力 {st['energy']}/100"

    if text.startswith("/关系"):
        rest = text[len("/关系") :].strip()
        if not rest:
            return (
                "用法：/关系 QQ号 陌生人|熟人|朋友|家人\n"
                "查看：/关系 QQ号\n"
                f"可选等级：{'/'.join(sess.VALID_TIERS)}"
            )
        parts = rest.split(None, 1)
        uid = parts[0]
        if not uid.isdigit():
            return "QQ号要是数字哦。"
        if len(parts) == 1:
            c = sess.get_contact(uid)
            tier = sess.get_tier(uid)
            alias = (c or {}).get("alias") or (c or {}).get("nickname") or "（无备注）"
            count = (c or {}).get("interact_count") or 0
            return f"{uid} {alias}：关系={tier}，互动约 {count} 次"
        try:
            tier = sess.set_tier(uid, parts[1].strip())
        except ValueError as e:
            return str(e)
        return f"已设定：{uid} → {tier}"

    if text.startswith("/备注"):
        rest = text[len("/备注") :].strip()
        if not rest:
            return "用法：/备注 QQ号 称呼\n取消备注：/备注 清除 QQ号"
        parts = rest.split(None, 2)
        if parts[0] in ("清除", "删除", "取消") and len(parts) >= 2:
            uid = parts[1]
            if not uid.isdigit():
                return "QQ号要是数字哦。"
            if sess.clear_alias(uid):
                return f"已清除 {uid} 的备注。"
            return f"{uid} 本来就没有备注。"
        if len(parts) < 2:
            return "用法：/备注 QQ号 称呼"
        uid, alias = parts[0], parts[1]
        if len(parts) > 2:
            alias = f"{parts[1]} {parts[2]}".strip()
        if not uid.isdigit():
            return "QQ号要是数字哦。"
        sess.set_alias(uid, alias)
        return f"已备注：{uid} → {alias}"

    if text == "/忘记短期" or text.startswith("/忘记短期 "):
        arg = text[len("/忘记短期") :].strip()
        if not arg:
            key = sess.session_key_for(is_private=True, user_id=event.user_id)
            sess.clear_session(key)
            return "已清空咱们私聊的短期记忆。"
        m = re.match(r"^(?:群\s*)?(\d+)$", arg)
        if not m:
            return "用法：/忘记短期\n或：/忘记短期 群号"
        gid = m.group(1)
        key = sess.session_key_for(
            is_private=False, user_id=event.user_id, group_id=gid
        )
        existed = sess.clear_session(key)
        if existed:
            return f"已清空群 {gid} 的短期记忆。"
        return f"群 {gid} 本来就没有短期记忆。"

    if text == "/长期记忆" or text.startswith("/长期记忆 "):
        arg = text[len("/长期记忆") :].strip()
        if arg in ("清空", "清除", "clear"):
            n = mem.clear_all()
            return f"已清空长期记忆（{n} 条）。"
        facts = mem.list_facts(limit=30)
        if not facts:
            return "还没有长期记忆。"
        lines = [
            f"{i}. [{r['source']}] {r['content']}"
            for i, r in enumerate(facts, 1)
        ]
        body = "\n".join(lines)
        if len(body) > 1400:
            body = body[:1400] + "…"
        return f"长期记忆（最近 {len(facts)} 条）：\n{body}"

    if text.startswith("/记住"):
        content = text[len("/记住") :].strip()
        if not content:
            return "用法：/记住 内容"
        mem.add_fact(content, scope="global", source="manual")
        return f"记住了：{content}"

    if text.startswith("/忘掉"):
        keyword = text[len("/忘掉") :].strip()
        if not keyword:
            return "用法：/忘掉 关键词"
        deleted = mem.delete_by_keyword(keyword)
        if not deleted:
            return f"没有找到包含「{keyword}」的记忆。"
        preview = "；".join(deleted[:5])
        extra = f" 等共 {len(deleted)} 条" if len(deleted) > 5 else ""
        return f"已忘掉：{preview}{extra}"

    return None


@chat.handle()
async def handle_chat(bot: Bot, event: MessageEvent):
    if not config.siliconflow_api_key or "粘贴" in config.siliconflow_api_key:
        await chat.finish("未配置 SILICONFLOW_API_KEY，请在 .env.dev 中填写。")

    text = _strip_at_text(event, event.get_plaintext())
    image_urls = vis.extract_image_urls(event)
    followup = is_group_followup(event)
    followup_image = followup and vis.is_image_followup_candidate(event)

    if isinstance(event, PrivateMessageEvent) and text.startswith("/"):
        cmd_reply = await handle_admin_memory_cmd(bot, event, text)
        if cmd_reply is not None:
            await chat.finish(cmd_reply)

    if not text and not image_urls:
        if followup:
            await chat.finish()
        await chat.finish("说点什么吧~")

    # @ / 回复机器人时立刻记互动，便于后续跟进
    if isinstance(event, GroupMessageEvent) and (
        event.is_tome() or is_reply_to_me(event)
    ):
        sess.mark_group_interact(event.group_id, event.user_id)

    sess.touch_contact(
        event.user_id,
        nickname=raw_display_name(event),
        is_private=isinstance(event, PrivateMessageEvent),
    )

    # ----- 识图 -----
    image_description = ""
    if image_urls:
        image_description = await vis.describe_images(
            client,
            model=config.siliconflow_vision_model,
            image_urls=image_urls,
            user_hint=text,
        )
        if not image_description and not text:
            if followup or vis.is_sticker_like(event):
                user_content = (
                    "[用户发来表情包或图片，具体内容未能识别。"
                    "若刚才在和你聊、接一下比较自然，就按人设回一句；"
                    "若没什么可接的，只输出 "
                    f"{NO_REPLY_TOKEN} 。]"
                )
            else:
                await chat.finish("这张图我没看清……")
        else:
            user_content = vis.build_user_content_with_vision(
                text, image_description
            )
    else:
        user_content = vis.build_user_content_with_vision(text, image_description)

    if followup:
        if followup_image:
            user_content = (
                f"{user_content}\n"
                "（这是对方刚和你聊完后发来的图/表情，可视为对你说话；"
                f"若完全不必接，只输出 {NO_REPLY_TOKEN}。）"
            )
        else:
            user_content = f"{user_content}\n（{FOLLOWUP_JUDGE_NOTE}）"

    speaker = speaker_label(event)
    relation_tier = sess.get_tier(event.user_id)
    user_content_for_model = f"说话人：{speaker}\n{user_content}"

    sk = current_session_key(event)
    history = sess.get_history(sk)

    scene = "私聊" if isinstance(event, PrivateMessageEvent) else "群聊"
    full_system = build_system_prompt(
        scene=scene,
        followup=followup,
        relation_tier=relation_tier,
    )
    if config.memory_long_term_enabled:
        facts = mem.retrieve_facts(
            user_content,
            limit=config.memory_long_term_retrieve,
            max_total=config.memory_long_term_max,
        )
        facts_block = mem.format_facts_for_prompt(facts)
        if facts_block:
            full_system = f"{full_system}\n\n{facts_block}"

    messages: list[dict] = [{"role": "system", "content": full_system}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_content_for_model})

    try:
        response = await client.chat.completions.create(
            model=config.siliconflow_model,
            messages=messages,
            max_tokens=1024,
            temperature=0.7,
        )
    except Exception as e:
        await chat.finish(f"调用模型失败：{e}")

    reply = (response.choices[0].message.content or "").strip()
    if not reply:
        if followup:
            await chat.finish()
        await chat.finish("模型返回了空内容。")

    # 跟进消息：模型判断不必回则静默
    if followup and _is_no_reply(reply):
        logger.info(f"跟进消息判定无需回复 user={event.user_id}")
        await chat.finish()

    if len(reply) > 1500:
        reply = reply[:1500] + "…"

    is_group = isinstance(event, GroupMessageEvent)
    at_uid = None
    if is_group and should_at_in_group(event):  # type: ignore[arg-type]
        at_uid = int(event.user_id)

    try:
        merged = await bubble.send_bubbles(
            chat.send,
            reply,
            is_group=is_group,
            at_user_id=at_uid,
        )
    except Exception as e:
        logger.warning(f"多段发送失败，回退单条：{e}")
        merged = bubble.merge_for_memory(bubble.split_bubbles(reply, is_group=is_group)) or reply
        if is_group and at_uid is not None:
            await chat.send(
                Message(
                    [
                        MessageSegment.at(at_uid),
                        MessageSegment.text(f" {merged}"),
                    ]
                )
            )
        else:
            await chat.send(merged)

    try:
        sess.append_turn(
            sk,
            user_content=user_content_for_model,
            assistant_content=merged,
            max_turns=config.memory_short_term_turns,
        )
    except Exception as e:
        logger.warning(f"写入短期记忆失败：{e}")

    try:
        mood.update_after_turn(
            user_text=text or user_content,
            assistant_text=merged,
            is_private=isinstance(event, PrivateMessageEvent),
            relation_tier=relation_tier,
        )
    except Exception as e:
        logger.warning(f"更新心情失败：{e}")

    if is_group:
        sess.mark_group_interact(event.group_id, event.user_id)
        pro.mark_bot_spoke(int(event.group_id))

    should_extract = config.memory_long_term_enabled and (
        isinstance(event, PrivateMessageEvent) or config.memory_long_term_group
    )
    if should_extract and (text or image_description):
        extract_text = text or "[图片]"
        if image_description:
            extract_text = f"{extract_text}\n[图片描述]{image_description}"
        asyncio.create_task(
            _safe_extract(
                speaker=speaker,
                user_text=extract_text,
                assistant_text=merged,
            )
        )

    await chat.finish()


async def _safe_extract(*, speaker: str, user_text: str, assistant_text: str) -> None:
    try:
        stored = await mem.extract_and_store(
            client,
            model=config.siliconflow_model,
            speaker=speaker,
            user_text=user_text,
            assistant_text=assistant_text,
            scope="global",
        )
        if stored:
            logger.info(f"长期记忆新增 {len(stored)} 条：{stored}")
    except Exception as e:
        logger.warning(f"长期记忆后台任务失败：{e}")
