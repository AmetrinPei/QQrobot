"""长期事实记忆：SQLite 存储、关键词检索、对话后自动抽取。"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from nonebot import logger
from openai import AsyncOpenAI

ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "memory.db"

EXTRACT_SYSTEM = """你是记忆抽取助手。根据一轮对话，判断是否有「值得长期记住」的事实。
规则：
1. 只输出 JSON 数组，每项是一句简短中文事实；没有则输出 []
2. 最多 3 条；只记与说话人相关、或对方明确要求记住的内容
3. 不记：密码、验证码、隐私八卦、一时情绪、无意义闲聊
4. 不要复述人设；不要编造对话里没有的信息
示例输出：["皮皮塔米米喜欢猫","阿强下周有考试"]"""


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS long_term_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            scope TEXT NOT NULL DEFAULT 'global',
            source TEXT NOT NULL DEFAULT 'auto',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_facts_scope ON long_term_facts(scope)"
    )
    conn.commit()
    return conn


def add_fact(content: str, *, scope: str = "global", source: str = "manual") -> int:
    content = content.strip()
    if not content:
        raise ValueError("内容为空")
    now = time.time()
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO long_term_facts (content, scope, source, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (content, scope, source, now, now),
        )
        conn.commit()
        return int(cur.lastrowid)


def delete_by_keyword(keyword: str, *, scope: str | None = None) -> list[str]:
    keyword = keyword.strip()
    if not keyword:
        return []
    like = f"%{keyword}%"
    with _connect() as conn:
        if scope is None:
            rows = conn.execute(
                "SELECT id, content FROM long_term_facts WHERE content LIKE ?",
                (like,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, content FROM long_term_facts
                WHERE content LIKE ? AND scope = ?
                """,
                (like, scope),
            ).fetchall()
        ids = [int(r["id"]) for r in rows]
        deleted = [str(r["content"]) for r in rows]
        if ids:
            conn.executemany(
                "DELETE FROM long_term_facts WHERE id = ?",
                [(i,) for i in ids],
            )
            conn.commit()
        return deleted


def clear_all(*, scope: str | None = None) -> int:
    with _connect() as conn:
        if scope is None:
            cur = conn.execute("DELETE FROM long_term_facts")
        else:
            cur = conn.execute(
                "DELETE FROM long_term_facts WHERE scope = ?", (scope,)
            )
        conn.commit()
        return int(cur.rowcount or 0)


def list_facts(*, limit: int = 50, scope: str | None = None) -> list[dict[str, Any]]:
    with _connect() as conn:
        if scope is None:
            rows = conn.execute(
                """
                SELECT id, content, scope, source, created_at
                FROM long_term_facts
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, content, scope, source, created_at
                FROM long_term_facts
                WHERE scope = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (scope, limit),
            ).fetchall()
        return [dict(r) for r in rows]


def _tokenize(text: str) -> set[str]:
    parts = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9_]{2,}", text.lower())
    return set(parts)


def retrieve_facts(query: str, *, limit: int = 8, max_total: int = 100) -> list[str]:
    """按关键词重叠检索相关事实；无命中时返回最近若干条。"""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT content FROM long_term_facts
            ORDER BY id DESC
            LIMIT ?
            """,
            (max_total,),
        ).fetchall()
    if not rows:
        return []

    q_tokens = _tokenize(query)
    scored: list[tuple[int, str]] = []
    for row in rows:
        content = str(row["content"])
        if not q_tokens:
            scored.append((0, content))
            continue
        c_tokens = _tokenize(content)
        overlap = len(q_tokens & c_tokens)
        # 子串命中加分
        bonus = sum(1 for t in q_tokens if t in content.lower())
        score = overlap * 2 + bonus
        if score > 0:
            scored.append((score, content))

    if scored:
        scored.sort(key=lambda x: (-x[0],))
        seen: set[str] = set()
        out: list[str] = []
        for _, content in scored:
            if content in seen:
                continue
            seen.add(content)
            out.append(content)
            if len(out) >= limit:
                break
        return out

    # 无关键词命中：给一点近期记忆作旁路
    return [str(r["content"]) for r in rows[: min(3, limit)]]


def format_facts_for_prompt(facts: list[str]) -> str:
    if not facts:
        return ""
    lines = "\n".join(f"- {f}" for f in facts)
    return (
        "以下是你已记住的长期事实（仅作参考，不要主动炫耀记忆，"
        "也不要编造未列出的内容）：\n"
        f"{lines}"
    )


def _parse_facts_json(raw: str) -> list[str]:
    text = (raw or "").strip()
    if not text:
        return []
    # 允许模型包一层 markdown
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 尝试截取第一个数组
        m = re.search(r"\[[\s\S]*\]", text)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
    if not isinstance(data, list):
        return []
    facts: list[str] = []
    for item in data:
        if isinstance(item, str):
            s = item.strip()
            if s:
                facts.append(s)
        elif isinstance(item, dict) and "content" in item:
            s = str(item["content"]).strip()
            if s:
                facts.append(s)
        if len(facts) >= 3:
            break
    return facts


async def extract_and_store(
    client: AsyncOpenAI,
    *,
    model: str,
    speaker: str,
    user_text: str,
    assistant_text: str,
    scope: str = "global",
) -> list[str]:
    """对话后抽取事实并入库；失败时静默跳过。"""
    user_payload = (
        f"说话人：{speaker}\n"
        f"用户：{user_text}\n"
        f"助手：{assistant_text}"
    )
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": EXTRACT_SYSTEM},
                {"role": "user", "content": user_payload},
            ],
            max_tokens=256,
            temperature=0.2,
        )
        raw = (response.choices[0].message.content or "").strip()
        facts = _parse_facts_json(raw)
    except Exception as e:
        logger.warning(f"长期记忆抽取失败：{e}")
        return []

    stored: list[str] = []
    for fact in facts:
        try:
            add_fact(fact, scope=scope, source="auto")
            stored.append(fact)
        except Exception as e:
            logger.warning(f"写入长期记忆失败：{e}")
    return stored
