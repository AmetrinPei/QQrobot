"""轻量情绪/精力状态：持久化并注入 prompt。"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "memory.db"

VALID_MOODS = ("平静", "开心", "害羞", "委屈", "烦躁")
DEFAULT_MOOD = "平静"
DEFAULT_ENERGY = 70

_HAPPY_RE = re.compile(
    r"(哈哈|开心|喜欢你|真棒|好厉害|可爱|摸摸|抱抱|夸|谢谢你|爱你)"
)
_SHY_RE = re.compile(r"(好看|好帅|好可爱|喜欢米米|喜欢你|亲一下)")
_SAD_RE = re.compile(r"(笨蛋|去死|滚|讨厌你|闭嘴|傻|烦死|骂)")
# 对方反复问、或表示没听懂时触发烦躁
_ANNOYED_RE = re.compile(
    r"(又说|不是说了|不是刚说|刚不是|听不懂|没听懂|再说一遍|再问一遍|"
    r"讲了半天|说了半天|怎么还|还是不懂|又问|同一个问题|我刚才|"
    r"为什么又|不是已经|到底怎么)"
)


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_mood (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            mood TEXT NOT NULL DEFAULT '平静',
            energy INTEGER NOT NULL DEFAULT 70,
            last_trigger TEXT,
            updated_at REAL NOT NULL
        )
        """
    )
    row = conn.execute("SELECT id FROM bot_mood WHERE id = 1").fetchone()
    if not row:
        conn.execute(
            """
            INSERT INTO bot_mood (id, mood, energy, last_trigger, updated_at)
            VALUES (1, ?, ?, NULL, ?)
            """,
            (DEFAULT_MOOD, DEFAULT_ENERGY, time.time()),
        )
    conn.commit()
    return conn


def get_state() -> dict[str, Any]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT mood, energy, last_trigger, updated_at FROM bot_mood WHERE id = 1"
        ).fetchone()
    if not row:
        return {
            "mood": DEFAULT_MOOD,
            "energy": DEFAULT_ENERGY,
            "last_trigger": None,
            "updated_at": time.time(),
        }
    mood = str(row["mood"] or DEFAULT_MOOD)
    if mood not in VALID_MOODS:
        mood = DEFAULT_MOOD
    energy = int(row["energy"] if row["energy"] is not None else DEFAULT_ENERGY)
    energy = max(0, min(100, energy))
    return {
        "mood": mood,
        "energy": energy,
        "last_trigger": row["last_trigger"],
        "updated_at": float(row["updated_at"] or time.time()),
    }


def set_state(
    *,
    mood: str | None = None,
    energy: int | None = None,
    last_trigger: str | None = None,
) -> dict[str, Any]:
    cur = get_state()
    new_mood = mood if mood in VALID_MOODS else cur["mood"]
    new_energy = cur["energy"] if energy is None else max(0, min(100, int(energy)))
    trigger = last_trigger if last_trigger is not None else cur["last_trigger"]
    now = time.time()
    with _connect() as conn:
        conn.execute(
            """
            UPDATE bot_mood
            SET mood = ?, energy = ?, last_trigger = ?, updated_at = ?
            WHERE id = 1
            """,
            (new_mood, new_energy, trigger, now),
        )
        conn.commit()
    return get_state()


def _energy_label(energy: int) -> str:
    if energy >= 75:
        return "精力充沛"
    if energy >= 45:
        return "精力一般"
    if energy >= 25:
        return "有点没精神"
    return "很没精神"


def format_mood_for_prompt(state: dict[str, Any] | None = None) -> str:
    st = state or get_state()
    mood = st["mood"]
    energy = int(st["energy"])
    label = _energy_label(energy)
    extra = ""
    if st.get("last_trigger"):
        extra = f"\n最近心情触发：{st['last_trigger']}（不必主动提起）。"
    return (
        f"你当前的内在状态：心情「{mood}」，{label}（精力 {energy}/100）。\n"
        "请让语气轻微符合这个状态，但仍遵守人设；"
        "心情「烦躁」时语气可以更冲、毒舌可以更狠，但不骂脏话、不人身攻击；"
        "不要自我分析心情，不要说「我现在的状态是……」。"
        f"{extra}"
    )


def apply_time_drift(hour: int | None = None) -> dict[str, Any]:
    """按钟点缓慢调整精力（深夜略低、白天回升）；不再产生困倦情绪。"""
    from datetime import datetime

    h = datetime.now().hour if hour is None else hour
    st = get_state()
    energy = int(st["energy"])
    mood = st["mood"]

    if h >= 23 or h < 5:
        energy = max(25, energy - 5)
    elif 5 <= h < 9:
        energy = min(75, energy + 5)
    elif 9 <= h < 18:
        energy = min(95, energy + 3)
    else:
        energy = max(35, energy - 2)

    return set_state(mood=mood, energy=energy)


def update_after_turn(
    *,
    user_text: str,
    assistant_text: str = "",
    is_private: bool = False,
    relation_tier: str = "陌生人",
) -> dict[str, Any]:
    """根据本轮对话用轻量规则更新心情/精力。"""
    st = get_state()
    mood = st["mood"]
    energy = int(st["energy"])
    trigger: str | None = None
    text = (user_text or "").strip()

    # 自然消耗：每轮略降，私聊稍多一点互动回血
    if is_private:
        energy = min(100, energy + 1)
    else:
        energy = max(10, energy - 1)

    if _SAD_RE.search(text):
        mood = "委屈"
        energy = max(20, energy - 10)
        trigger = "被说了重话"
    elif _SHY_RE.search(text):
        mood = "害羞"
        energy = min(100, energy + 3)
        trigger = "被夸/亲近"
    elif _HAPPY_RE.search(text):
        mood = "开心"
        energy = min(100, energy + 6)
        trigger = "开心互动"
    elif _ANNOYED_RE.search(text):
        mood = "烦躁"
        energy = max(20, energy - 4)
        trigger = "对方反复问/听不懂"
    elif relation_tier in ("家人", "朋友") and is_private:
        # 熟人私聊温和回升
        if mood in ("委屈", "烦躁"):
            mood = "平静"
            trigger = "和熟人聊开了"
        energy = min(100, energy + 2)

    return set_state(mood=mood, energy=energy, last_trigger=trigger)
