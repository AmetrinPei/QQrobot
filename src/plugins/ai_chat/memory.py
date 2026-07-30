"""长期事实记忆：SQLite 存储、关键词检索、对话后自动抽取。"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import Any

from nonebot import logger
from openai import AsyncOpenAI

from .paths import DATA_DIR, DB_PATH

EXTRACT_SYSTEM = """你是记忆抽取助手。根据一轮对话，判断是否有「值得长期记住」的事实。

规则（只有 3 条）：
1. 只输出 JSON 数组，每项是一句简短中文事实；没有则输出 []
2. 只记与「说话人」相关的真实信息（姓名、喜好、身份、关系、明确要求记住的事）
3. 不记：闲聊、情绪、玩笑、辱骂、占便宜称呼、对话中没有的信息

示例：
对话：“说话人：泥哥(QQ:123，熟人)\n用户：我明天要考研了\n助手：加油！”
输出：["泥哥正在考研"]

对话：“说话人：陌生人A(QQ:456，陌生人)\n用户：哈哈哈笑死\n助手：哈哈哈”
输出：[]

对话：“说话人：闪闪(QQ:789，熟人)\n用户：记住我不喜欢吃香菜\n助手：好呀记住了”
输出：["闪闪不喜欢吃香菜"]
"""


# 占便宜 / 下流称呼：抽取与手动 /记住 一律拦截
_FORBIDDEN_ALIAS_RE = re.compile(
    r"(欧豆桑|欧多桑|欧吉桑|ojisan|"
    r"daddy|papa|"
    r"(叫|喊|称呼).{0,10}(爸|爹|daddy|papa)|"
    r"希望被叫.{0,10}(爸|爹|daddy|papa)|"
    r"(叫|喊|称呼).{0,8}(老公|老婆|主人|奴隶)|"
    r"做我的狗|好爸爸|干爸爸)",
    re.IGNORECASE,
)


def is_forbidden_fact(content: str) -> str | None:
    """若内容属于禁止入库的占便宜/下流称呼类记忆，返回原因；否则 None。"""
    text = (content or "").strip()
    if not text:
        return "内容为空"
    if _FORBIDDEN_ALIAS_RE.search(text):
        return "禁止记住占便宜或下流意味的称呼（如叫爸爸、欧豆桑等）"
    return None


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
    reason = is_forbidden_fact(content)
    if reason:
        raise ValueError(reason)
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


def list_admin_facts(*, limit: int = 50) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, content, scope, source, created_at
            FROM long_term_facts
            WHERE source = 'admin'
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def retrieve_facts(query: str, *, limit: int = 8, max_total: int = 100) -> list[str]:
    """检索长期事实；管理员写入（source=admin）始终优先置顶。"""
    with _connect() as conn:
        admin_rows = conn.execute(
            """
            SELECT content FROM long_term_facts
            WHERE source = 'admin'
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(limit, 20),),
        ).fetchall()
        rows = conn.execute(
            """
            SELECT content, source FROM long_term_facts
            ORDER BY id DESC
            LIMIT ?
            """,
            (max_total,),
        ).fetchall()

    admin_facts: list[str] = []
    seen: set[str] = set()
    for r in admin_rows:
        c = str(r["content"]).strip()
        if c and c not in seen:
            seen.add(c)
            admin_facts.append(c)

    if not rows and not admin_facts:
        return []

    q_tokens = _tokenize(query)
    scored: list[tuple[int, str]] = []
    for row in rows:
        content = str(row["content"]).strip()
        if not content or content in seen:
            continue
        if str(row["source"] or "") == "admin":
            continue  # 已在 admin_facts
        if not q_tokens:
            scored.append((0, content))
            continue
        c_tokens = _tokenize(content)
        overlap = len(q_tokens & c_tokens)
        bonus = sum(1 for t in q_tokens if t in content.lower())
        score = overlap * 2 + bonus
        if score > 0:
            scored.append((score, content))

    other: list[str] = []
    if scored:
        scored.sort(key=lambda x: (-x[0],))
        for _, content in scored:
            if content in seen:
                continue
            seen.add(content)
            other.append(content)
            if len(admin_facts) + len(other) >= limit:
                break
    elif not admin_facts:
        for r in rows[: min(3, limit)]:
            c = str(r["content"]).strip()
            if c and c not in seen:
                other.append(c)

    # 管理员记忆优先，其余按相关度补齐
    room = max(0, limit - len(admin_facts))
    return admin_facts + other[:room]


def format_facts_for_prompt(facts: list[str]) -> str:
    if not facts:
        return ""
    # 再查一遍哪些是 admin，便于标注优先
    admin_set = {str(r["content"]) for r in list_admin_facts(limit=200)}
    admin_lines = [f for f in facts if f in admin_set]
    other_lines = [f for f in facts if f not in admin_set]
    parts: list[str] = []
    if admin_lines:
        body = "\n".join(f"- {f}" for f in admin_lines)
        parts.append(
            "【管理员长期记忆·优先采信】与下方冲突时以本段为准：\n" + body
        )
    if other_lines:
        body = "\n".join(f"- {f}" for f in other_lines)
        parts.append(
            "以下是其他已记住的长期事实（仅作参考，不要主动炫耀记忆，"
            "也不要编造未列出的内容）：\n" + body
        )
    return "\n\n".join(parts)


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
            max_tokens=128,
            temperature=0.1,
        )
        raw = (response.choices[0].message.content or "").strip()
        facts = _parse_facts_json(raw)
    except Exception as e:
        logger.warning(f"长期记忆抽取失败：{e}")
        return []

    stored: list[str] = []
    for fact in facts:
        banned = is_forbidden_fact(fact)
        if banned:
            logger.info(f"跳过禁止类长期记忆：{fact}（{banned}）")
            continue
        # 去重：已存在相同内容则跳过
        existing = delete_by_keyword(fact, scope=scope)
        if existing:
            # 删了旧的再写新的（等价于更新）
            pass
        try:
            add_fact(fact, scope=scope, source="auto")
            stored.append(fact)
        except Exception as e:
            logger.warning(f"写入长期记忆失败：{e}")
    return stored
