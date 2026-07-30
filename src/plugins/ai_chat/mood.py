"""轻量情绪状态：持久化并注入 prompt（已移除精力系统，仅保留心情标签）。"""

from __future__ import annotations

import re
import sqlite3
import time
from typing import Any

from .paths import DATA_DIR, DB_PATH

VALID_MOODS = ("平静", "开心", "害羞", "委屈", "烦躁")
DEFAULT_MOOD = "平静"

_HAPPY_RE = re.compile(
    r"(哈哈|开心|喜欢你|真棒|好厉害|可爱|摸摸|抱抱|夸|谢谢你|爱你)"
)
_SHY_RE = re.compile(r"(好看|好帅|好可爱|喜欢米米|喜欢你|亲一下)")
_SAD_RE = re.compile(r"(笨蛋|去死|滚|讨厌你|闭嘴|傻|烦死|骂)")
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
            VALUES (1, ?, 70, NULL, ?)
            """,
            (DEFAULT_MOOD, time.time()),
        )
    conn.commit()
    return conn


def get_state() -> dict[str, Any]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT mood, last_trigger, updated_at FROM bot_mood WHERE id = 1"
        ).fetchone()
    if not row:
        return {"mood": DEFAULT_MOOD, "last_trigger": None, "updated_at": time.time()}
    mood = str(row["mood"] or DEFAULT_MOOD)
    if mood not in VALID_MOODS:
        mood = DEFAULT_MOOD
    return {
        "mood": mood,
        "last_trigger": row["last_trigger"],
        "updated_at": float(row["updated_at"] or time.time()),
    }


def set_state(
    *,
    mood: str | None = None,
    energy: int | None = None,  # 保留参数兼容，忽略
    last_trigger: str | None = None,
) -> dict[str, Any]:
    cur = get_state()
    new_mood = mood if mood in VALID_MOODS else cur["mood"]
    trigger = last_trigger if last_trigger is not None else cur["last_trigger"]
    now = time.time()
    with _connect() as conn:
        conn.execute(
            """
            UPDATE bot_mood SET mood = ?, last_trigger = ?, updated_at = ?
            WHERE id = 1
            """,
            (new_mood, trigger, now),
        )
        conn.commit()
    return get_state()


def format_mood_for_prompt(state: dict[str, Any] | None = None) -> str:
    st = state or get_state()
    mood = st["mood"]
    extra = ""
    if st.get("last_trigger"):
        extra = f"\n最近心情触发：{st['last_trigger']}（不必主动提起）。"
    return (
        f"你当前心情：「{mood}」。\n"
        "语气轻微符合即可，仍遵守人设；"
        "「烦躁」时语气可以更冲、毒舌更狠，但不骂脏话；"
        "不要自我分析心情，不要说「我现在的状态是……」。"
        f"{extra}"
    )


def apply_time_drift(hour: int | None = None) -> dict[str, Any]:
    """已简化：不再按时间调整精力，直接返回当前状态。"""
    return get_state()


def update_after_turn(
    *,
    user_text: str,
    assistant_text: str = "",
    is_private: bool = False,
    relation_tier: str = "陌生人",
) -> dict[str, Any]:
    """根据本轮对话用轻量规则更新心情（无精力变动）。"""
    st = get_state()
    mood = st["mood"]
    trigger: str | None = None
    text = (user_text or "").strip()

    if _SAD_RE.search(text):
        mood = "委屈"
        trigger = "被说了重话"
    elif _SHY_RE.search(text):
        mood = "害羞"
        trigger = "被夸/亲近"
    elif _HAPPY_RE.search(text):
        mood = "开心"
        trigger = "开心互动"
    elif _ANNOYED_RE.search(text):
        mood = "烦躁"
        trigger = "对方反复追问"
    elif relation_tier == "熟人" and is_private:
        if mood in ("委屈", "烦躁"):
            mood = "平静"
            trigger = "和熟人聊开了"

    return set_state(mood=mood, last_trigger=trigger)
