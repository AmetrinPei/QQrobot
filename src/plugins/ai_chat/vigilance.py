"""攻击意图警惕：仅针对攻击者本人；首回后重复话题静默，直到解除。"""

from __future__ import annotations

import re
import time
from typing import Any

# 人身攻击 / 威胁 / 挑拨身份 / 占便宜下流
_ATTACK_RE = re.compile(
    r"("
    r"去死|找死|滚啊|滚开|傻逼|傻b|沙比|煞笔|sb\b|nmsl|cnm|操你|草你|肏|"
    r"脑残|白痴|智障|废物|垃圾货|蠢猪|有病吧|去死吧|"
    r"假弟弟|假的弟弟|你是假|冒充哥哥|取代卡比|取代.*哥|"
    r"叫爸爸|叫爹|欧豆桑|欧多桑|欧吉桑|叫老公|叫老婆|"
    r"弄死你|打死你|弄死|人肉你"
    r")",
    re.IGNORECASE,
)

# 话题缓和 / 结束挑衅 → 解除对该人的警惕
_DEESCALATE_RE = re.compile(
    r"("
    r"抱歉|对不起|不好意思|开个玩笑|开玩笑的|逗你玩|"
    r"不闹了|不说了|不提了|换个话题|聊点别的|"
    r"没事了|和解|别生气|原谅我|我不该"
    r")"
)

# 仍在延续攻击话题（即使没有脏字）
_ATTACK_TOPIC_RE = re.compile(
    r"(假弟弟|假的|冒充|取代|叫爸爸|欧豆桑|欧多桑|欧吉桑)"
)

ALERT_TIMEOUT_SEC = 900  # 无新攻击 15 分钟后自动解除
CALM_STREAK_CLEAR = 3  # 连续若干条非攻击且非延续话题 → 视为话题结束

# 仅按 user_id 隔离，绝不影响其他用户
_alerts: dict[str, dict[str, Any]] = {}


def _uid(user_id: int | str) -> str:
    return str(user_id)


def detect_attack(text: str) -> str | None:
    """若文本含攻击意图，返回简短标签；否则 None。"""
    s = (text or "").strip()
    if not s:
        return None
    if _ATTACK_RE.search(s):
        return "攻击/挑衅/占便宜"
    return None


def detect_deescalate(text: str) -> bool:
    s = (text or "").strip()
    if not s:
        return False
    return bool(_DEESCALATE_RE.search(s))


def _continues_attack_topic(text: str) -> bool:
    return bool(_ATTACK_TOPIC_RE.search(text or ""))


def mark_alert(user_id: int | str, *, reason: str) -> None:
    """标记该用户警惕。若已警惕且已回应过，保留 replied，避免重复开骂又恢复理会。"""
    uid = _uid(user_id)
    now = time.time()
    prev = _alerts.get(uid)
    if prev and time.time() - float(prev["last_attack"]) <= ALERT_TIMEOUT_SEC:
        prev["reason"] = reason
        prev["last_attack"] = now
        prev["calm_streak"] = 0
        # 保留 replied
        return
    _alerts[uid] = {
        "reason": reason,
        "since": now,
        "last_attack": now,
        "calm_streak": 0,
        "replied": False,
    }


def clear_alert(user_id: int | str) -> None:
    _alerts.pop(_uid(user_id), None)


def is_alert(user_id: int | str) -> bool:
    uid = _uid(user_id)
    st = _alerts.get(uid)
    if not st:
        return False
    if time.time() - float(st["last_attack"]) > ALERT_TIMEOUT_SEC:
        _alerts.pop(uid, None)
        return False
    return True


def get_alert(user_id: int | str) -> dict[str, Any] | None:
    if not is_alert(user_id):
        return None
    return dict(_alerts[_uid(user_id)])


def mark_replied(user_id: int | str) -> None:
    """首轮已回应：之后同一攻击话题静默，直到解除。"""
    uid = _uid(user_id)
    st = _alerts.get(uid)
    if not st:
        return
    st["replied"] = True
    st["last_attack"] = time.time()


def has_replied(user_id: int | str) -> bool:
    st = get_alert(user_id)
    return bool(st and st.get("replied"))


def update_on_message(user_id: int | str, text: str) -> bool:
    """根据本轮文本更新该用户警惕状态。返回结束后是否仍警惕。"""
    attack = detect_attack(text)
    if attack:
        mark_alert(user_id, reason=attack)
        return True

    if not is_alert(user_id):
        return False

    if detect_deescalate(text):
        clear_alert(user_id)
        return False

    uid = _uid(user_id)
    st = _alerts[uid]
    if _continues_attack_topic(text):
        st["last_attack"] = time.time()
        st["calm_streak"] = 0
        return True

    st["calm_streak"] = int(st.get("calm_streak") or 0) + 1
    if st["calm_streak"] >= CALM_STREAK_CLEAR:
        clear_alert(user_id)
        return False
    return True


def should_ignore_message(user_id: int | str, text: str) -> bool:
    """已对该人的攻击话题回应过：在解除前不再理会（道歉等解除语除外）。"""
    if not is_alert(user_id):
        return False
    if not has_replied(user_id):
        return False
    if detect_deescalate(text):
        return False
    return True


def should_skip_long_term_memory(user_id: int | str, text: str) -> bool:
    """仅该用户：攻击当轮或仍在警惕中 → 禁止写入长期记忆。"""
    if detect_attack(text):
        return True
    return is_alert(user_id)


def format_vigilance_for_prompt(user_id: int | str) -> str:
    st = get_alert(user_id)
    if not st:
        return ""
    reason = st.get("reason") or "攻击意图"
    return (
        f"【警惕标识·仅当前说话人】对方（仅此人）存在「{reason}」类行为。"
        "不要迁怒群里其他人；其他人仍按正常亲疏互动。\n"
        "对本说话人在该话题结束前：\n"
        "- 禁止亲密、撒娇、示好、卖萌讨好；\n"
        "- 语气冷静克制，短拒即可，不主动接梗；\n"
        "- 不接受下流/占便宜称呼，不被挑拨带节奏；\n"
        "- 本轮不写进长期记忆（系统已拦截）。\n"
        "对方道歉、明确不闹了或话题已切换后，仅对此人恢复正常。"
    )


def effective_relation_tier(user_id: int | str, real_tier: str) -> str:
    """仅警惕中的该用户按陌生人克制；其他人不受影响。"""
    if is_alert(user_id):
        return "陌生人"
    return real_tier
