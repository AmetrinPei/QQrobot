"""短期会话记忆 + 通讯录备注/亲疏（与长期事实共用 data/memory.db）。"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

# 与 memory.py 共用同一库文件
from .paths import DATA_DIR, DB_PATH

# 亲疏等级（简化为两级）
TIER_STRANGER = "陌生人"
TIER_FAMILIAR = "熟人"
VALID_TIERS = (TIER_STRANGER, TIER_FAMILIAR)
# 自动升档：互动达到阈值后陌生人→熟人
AUTO_BUMP_THRESHOLD = 6


def _ensure_contact_columns(conn: sqlite3.Connection) -> None:
    cols = {
        str(r[1])
        for r in conn.execute("PRAGMA table_info(contacts)").fetchall()
    }
    if "tier" not in cols:
        conn.execute(
            "ALTER TABLE contacts ADD COLUMN tier TEXT DEFAULT '陌生人'"
        )
    if "interact_count" not in cols:
        conn.execute(
            "ALTER TABLE contacts ADD COLUMN interact_count INTEGER DEFAULT 0"
        )
    if "note" not in cols:
        conn.execute("ALTER TABLE contacts ADD COLUMN note TEXT")
    if "last_private_at" not in cols:
        conn.execute(
            "ALTER TABLE contacts ADD COLUMN last_private_at REAL"
        )


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS short_sessions (
            session_key TEXT PRIMARY KEY,
            messages TEXT NOT NULL DEFAULT '[]',
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS contacts (
            user_id TEXT PRIMARY KEY,
            alias TEXT,
            nickname TEXT,
            last_seen REAL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            tier TEXT DEFAULT '陌生人',
            interact_count INTEGER DEFAULT 0,
            note TEXT,
            last_private_at REAL
        )
        """
    )
    _ensure_contact_columns(conn)
    conn.commit()
    return conn


def normalize_tier(raw: str | None) -> str:
    s = (raw or "").strip()
    if s in VALID_TIERS:
        return s
    # 旧等级兼容映射：朋友/家人 → 熟人
    familiar_words = (
        "熟人", "朋友", "家人", "好友", "亲人", "哥哥", "家里人",
        "friend", "family", "acquaintance",
    )
    if s.lower() in familiar_words:
        return TIER_FAMILIAR
    return TIER_STRANGER

def session_key_for(
    *,
    is_private: bool,
    user_id: int | str,
    group_id: int | str | None = None,
) -> str:
    if is_private:
        return f"private:{user_id}"
    if group_id is None:
        raise ValueError("群聊需要 group_id")
    return f"group:{group_id}"


def get_history(session_key: str) -> list[dict[str, str]]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT messages FROM short_sessions WHERE session_key = ?",
            (session_key,),
        ).fetchone()
    if not row:
        return []
    try:
        data = json.loads(row["messages"] or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "")
        content = str(item.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            out.append({"role": role, "content": content})
    return out


def append_turn(
    session_key: str,
    *,
    user_content: str,
    assistant_content: str,
    max_turns: int = 16,
) -> None:
    """追加一轮对话，只保留最近 max_turns 轮（每轮 user+assistant）。"""
    user_content = (user_content or "").strip()
    assistant_content = (assistant_content or "").strip()
    if not user_content and not assistant_content:
        return

    history = get_history(session_key)
    if user_content:
        history.append({"role": "user", "content": user_content})
    if assistant_content:
        history.append({"role": "assistant", "content": assistant_content})

    max_messages = max(2, max_turns * 2)
    if len(history) > max_messages:
        history = history[-max_messages:]

    now = time.time()
    payload = json.dumps(history, ensure_ascii=False)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO short_sessions (session_key, messages, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(session_key) DO UPDATE SET
                messages = excluded.messages,
                updated_at = excluded.updated_at
            """,
            (session_key, payload, now),
        )
        conn.commit()


def clear_session(session_key: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM short_sessions WHERE session_key = ?",
            (session_key,),
        )
        conn.commit()
        return (cur.rowcount or 0) > 0


def touch_contact(
    user_id: int | str,
    *,
    nickname: str | None = None,
    is_private: bool = False,
    bump_interact: bool = True,
) -> None:
    """自动记录与机器人说过话的人；可累计互动并慢升到熟人。"""
    uid = str(user_id)
    now = time.time()
    nick = (nickname or "").strip() or None
    with _connect() as conn:
        row = conn.execute(
            "SELECT user_id, tier, interact_count FROM contacts WHERE user_id = ?",
            (uid,),
        ).fetchone()
        if row:
            count = int(row["interact_count"] or 0)
            if bump_interact:
                count += 1
            tier = normalize_tier(row["tier"])
            # 自动升档：陌生人→熟人
            if bump_interact and count >= AUTO_BUMP_THRESHOLD and tier == TIER_STRANGER:
                tier = TIER_FAMILIAR
            sets = ["last_seen = ?", "updated_at = ?", "interact_count = ?", "tier = ?"]
            args: list[Any] = [now, now, count, tier]
            if nick:
                sets.append("nickname = ?")
                args.append(nick)
            if is_private:
                sets.append("last_private_at = ?")
                args.append(now)
            args.append(uid)
            conn.execute(
                f"UPDATE contacts SET {', '.join(sets)} WHERE user_id = ?",
                args,
            )
        else:
            conn.execute(
                """
                INSERT INTO contacts
                    (user_id, alias, nickname, last_seen, created_at, updated_at,
                     tier, interact_count, note, last_private_at)
                VALUES (?, NULL, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    uid,
                    nick,
                    now,
                    now,
                    now,
                    TIER_STRANGER,
                    1 if bump_interact else 0,
                    now if is_private else None,
                ),
            )
        conn.commit()


def set_alias(user_id: int | str, alias: str) -> None:
    uid = str(user_id)
    alias = alias.strip()
    if not alias:
        raise ValueError("备注为空")
    now = time.time()
    with _connect() as conn:
        row = conn.execute(
            "SELECT user_id FROM contacts WHERE user_id = ?", (uid,)
        ).fetchone()
        if row:
            conn.execute(
                """
                UPDATE contacts
                SET alias = ?, updated_at = ?, last_seen = COALESCE(last_seen, ?)
                WHERE user_id = ?
                """,
                (alias, now, now, uid),
            )
        else:
            conn.execute(
                """
                INSERT INTO contacts
                    (user_id, alias, nickname, last_seen, created_at, updated_at,
                     tier, interact_count)
                VALUES (?, ?, NULL, ?, ?, ?, ?, 0)
                """,
                (uid, alias, now, now, now, TIER_STRANGER),
            )
        conn.commit()


def clear_alias(user_id: int | str) -> bool:
    uid = str(user_id)
    with _connect() as conn:
        row = conn.execute(
            "SELECT alias FROM contacts WHERE user_id = ?", (uid,)
        ).fetchone()
        if not row or not row["alias"]:
            return False
        conn.execute(
            "UPDATE contacts SET alias = NULL, updated_at = ? WHERE user_id = ?",
            (time.time(), uid),
        )
        conn.commit()
        return True


def get_alias(user_id: int | str) -> str | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT alias FROM contacts WHERE user_id = ?",
            (str(user_id),),
        ).fetchone()
    if not row:
        return None
    alias = row["alias"]
    return str(alias).strip() if alias else None


def get_tier(user_id: int | str) -> str:
    with _connect() as conn:
        row = conn.execute(
            "SELECT tier FROM contacts WHERE user_id = ?",
            (str(user_id),),
        ).fetchone()
    if not row:
        return TIER_STRANGER
    tier = normalize_tier(row["tier"])
    return tier if tier in VALID_TIERS else TIER_STRANGER


def set_tier(user_id: int | str, tier: str) -> str:
    uid = str(user_id)
    tier_n = normalize_tier(tier)
    if tier_n not in VALID_TIERS:
        raise ValueError(f"无效等级，可选：{'/'.join(VALID_TIERS)}")
    now = time.time()
    with _connect() as conn:
        row = conn.execute(
            "SELECT user_id FROM contacts WHERE user_id = ?", (uid,)
        ).fetchone()
        if row:
            conn.execute(
                """
                UPDATE contacts
                SET tier = ?, updated_at = ?, last_seen = COALESCE(last_seen, ?)
                WHERE user_id = ?
                """,
                (tier_n, now, now, uid),
            )
        else:
            conn.execute(
                """
                INSERT INTO contacts
                    (user_id, alias, nickname, last_seen, created_at, updated_at,
                     tier, interact_count)
                VALUES (?, NULL, NULL, ?, ?, ?, ?, 0)
                """,
                (uid, now, now, now, tier_n),
            )
        conn.commit()
    return tier_n


def get_contact(user_id: int | str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT user_id, alias, nickname, tier, interact_count,
                   note, last_seen, last_private_at
            FROM contacts WHERE user_id = ?
            """,
            (str(user_id),),
        ).fetchone()
    return dict(row) if row else None


def mark_private_seen(user_id: int | str) -> None:
    uid = str(user_id)
    now = time.time()
    with _connect() as conn:
        row = conn.execute(
            "SELECT user_id FROM contacts WHERE user_id = ?", (uid,)
        ).fetchone()
        if row:
            conn.execute(
                """
                UPDATE contacts
                SET last_private_at = ?, last_seen = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (now, now, now, uid),
            )
        else:
            conn.execute(
                """
                INSERT INTO contacts
                    (user_id, alias, nickname, last_seen, created_at, updated_at,
                     tier, interact_count, last_private_at)
                VALUES (?, NULL, NULL, ?, ?, ?, ?, 0, ?)
                """,
                (uid, now, now, now, TIER_STRANGER, now),
            )
        conn.commit()


def list_private_care_candidates(
    *,
    min_tier: str = TIER_FAMILIAR,
    idle_seconds: float,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """久未私聊、且亲疏达标的联系人，供低频主动关心。"""
    need_familiar = normalize_tier(min_tier) == TIER_FAMILIAR
    cutoff = time.time() - max(0.0, idle_seconds)
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT user_id, alias, nickname, tier, last_private_at, last_seen
            FROM contacts
            ORDER BY COALESCE(last_private_at, 0) ASC
            LIMIT 200
            """
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        tier = normalize_tier(r["tier"])
        if need_familiar and tier != TIER_FAMILIAR:
            continue
        last_p = r["last_private_at"]
        # 从未私聊过：用 last_seen 判断是否「认识但久没聊」
        ref = float(last_p) if last_p is not None else float(r["last_seen"] or 0)
        if ref > cutoff:
            continue
        out.append(dict(r))
        if len(out) >= limit:
            break
    return out


def relation_prompt_note(tier: str) -> str:
    """按亲疏给模型语气提示（简化为两级）。"""
    if normalize_tier(tier) == TIER_FAMILIAR:
        return (
            "对方是熟人：语气自然随意一点，可以接话、开玩笑，"
            "不必过分客气，但仍遵守人设。"
        )
    return "对方是陌生人：客气、克制，不主动套近乎。"


def list_contacts(*, limit: int = 50, only_aliased: bool = True) -> list[dict[str, Any]]:
    with _connect() as conn:
        if only_aliased:
            rows = conn.execute(
                """
                SELECT user_id, alias, nickname, last_seen, tier, interact_count
                FROM contacts
                WHERE alias IS NOT NULL AND TRIM(alias) != ''
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT user_id, alias, nickname, last_seen, tier, interact_count
                FROM contacts
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

# ----- 群聊：近期与某人互动（用于 @ 后跟进文字/表情）-----
_recent_group_interact: dict[str, float] = {}


def _group_user_key(group_id: int | str, user_id: int | str) -> str:
    return f"group:{group_id}:user:{user_id}"


def mark_group_interact(group_id: int | str, user_id: int | str) -> None:
    """标记该用户刚在本群与机器人互动过。"""
    _recent_group_interact[_group_user_key(group_id, user_id)] = time.time()
    # 防止无限增长：偶尔清理过旧条目
    if len(_recent_group_interact) > 2000:
        cutoff = time.time() - 3600
        stale = [k for k, t in _recent_group_interact.items() if t < cutoff]
        for k in stale:
            _recent_group_interact.pop(k, None)


def recently_group_interacted(
    group_id: int | str,
    user_id: int | str,
    *,
    window_seconds: int = 180,
) -> bool:
    """该用户是否在窗口内刚与机器人聊过（含被回复）。"""
    ts = _recent_group_interact.get(_group_user_key(group_id, user_id))
    if ts is None:
        return False
    return (time.time() - ts) <= max(0, window_seconds)


def count_recent_group_interactors(
    group_id: int | str,
    *,
    window_seconds: int = 180,
) -> int:
    """本群窗口内仍算「在和机器人互动」的人数。"""
    prefix = f"group:{group_id}:user:"
    now = time.time()
    window = max(0, window_seconds)
    n = 0
    for key, ts in _recent_group_interact.items():
        if not key.startswith(prefix):
            continue
        if now - ts <= window:
            n += 1
    return n
