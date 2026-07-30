"""表情包收集与发送：群聊中自动收集，视觉模型自动打标，AI 对话中按关键词调用发送。

存储结构：
  data/stickers/          ← 图片文件（按 URL hash 命名）
  data/stickers/index.db  ← SQLite 索引（URL hash、描述标签、来源群）

标签系统：
  - 收集时由视觉模型自动看图生成 2~5 个描述性关键词
  - 如「大狗」「吴京严肃」「一本正经」「奶龙」「翻白眼」等
  - 发送时 AI 输出任意关键词，系统模糊匹配最接近的表情包
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time

import httpx
from nonebot import logger
from nonebot.adapters.onebot.v11 import MessageEvent
from openai import AsyncOpenAI

from .paths import DATA_DIR

# 表情包存储目录
STICKER_DIR = DATA_DIR / "stickers"
STICKER_DB = STICKER_DIR / "index.db"

# 视觉模型打标 prompt
_TAG_PROMPT = (
    "请用 2~5 个简短中文词描述这张表情包的内容和风格，"
    "用空格分隔。包含：角色/动物、表情/动作、情绪、梗/场景。"
    "只输出关键词，不要其他文字。"
    "示例输出：大狗 搞笑 翻白眼 无语"
)

# 工具说明（注入 system prompt）
TOOL_DESCRIPTION = (
    "【可用工具：send_sticker】\n"
    "当你想用表情包回应时调用。\n"
    "调用方式：仅输出一行 [TOOL_CALL] send_sticker | 描述关键词\n"
    "规则：\n"
    "- 关键词要描述你想要的表情包内容，如：大狗、翻白眼、吴京严肃、奶龙、装傻\n"
    "- 系统会模糊匹配最接近的已收集表情包\n"
    "- 不要每条消息都发，只在适合用表情包的场合用\n"
    "- 没有合适关键词时不要调用，正常回复即可"
)

# 结果中的贴纸标记（主流程检测用）
STICKER_TOKEN = "[STICKER_FILE]"


def _ensure_db() -> sqlite3.Connection:
    STICKER_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(STICKER_DB)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stickers (
            url_hash TEXT PRIMARY KEY,
            file_path TEXT NOT NULL,
            tags TEXT NOT NULL DEFAULT '',
            source_group TEXT,
            created_at REAL NOT NULL
        )
        """
    )
    # 兼容旧表：如果有 tag 列没有 tags 列，做迁移
    cols = {r[1] for r in conn.execute("PRAGMA table_info(stickers)").fetchall()}
    if "tag" in cols and "tags" not in cols:
        conn.execute("ALTER TABLE stickers RENAME COLUMN tag TO tags")
        conn.commit()
    conn.commit()
    return conn


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def has_sticker(url: str) -> bool:
    """该 URL 是否已收录。"""
    with _ensure_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM stickers WHERE url_hash = ?", (_url_hash(url),)
        ).fetchone()
    return bool(row)


def get_sticker_url(event: MessageEvent) -> str | None:
    """从消息中提取第一张表情包的 URL。"""
    for seg in event.message:
        if seg.type != "image":
            continue
        data = seg.data or {}
        sub = str(data.get("sub_type", "0"))
        summary = str(data.get("summary") or "")
        is_sticker = sub in ("1", "sticker") or "表情" in summary
        if not is_sticker:
            continue
        url = (data.get("url") or data.get("file") or "").strip()
        if url.startswith(("http://", "https://")):
            return url
    return None


async def collect_sticker(
    url: str,
    *,
    source_group: str | None = None,
    timeout: float = 6.0,
) -> bool:
    """下载并收录一张表情包（去重）。成功返回 True。标签后续由 auto_tag 补充。"""
    h = _url_hash(url)
    with _ensure_db() as conn:
        if conn.execute(
            "SELECT 1 FROM stickers WHERE url_hash = ?", (h,)
        ).fetchone():
            return False

    ext = _guess_ext(url)
    file_path = STICKER_DIR / f"{h}{ext}"
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            if len(resp.content) > 2 * 1024 * 1024:
                logger.info(f"表情包过大，跳过 url={url[:60]}")
                return False
            file_path.write_bytes(resp.content)
    except Exception as e:
        logger.warning(f"下载表情包失败 {url[:60]}：{e}")
        return False

    with _ensure_db() as conn:
        conn.execute(
            """
            INSERT INTO stickers (url_hash, file_path, tags, source_group, created_at)
            VALUES (?, ?, '', ?, ?)
            """,
            (h, str(file_path), source_group, time.time()),
        )
        conn.commit()
    logger.info(f"收录表情包 file={file_path.name}")
    return True


async def auto_tag_sticker(
    client: AsyncOpenAI,
    model: str,
    url: str,
) -> str:
    """用视觉模型为表情包生成描述性标签，并写入数据库。返回标签字符串。"""
    h = _url_hash(url)
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": url, "detail": "auto"}},
                        {"type": "text", "text": _TAG_PROMPT},
                    ],
                }
            ],
            max_tokens=64,
            temperature=0.2,
        )
        tags = (response.choices[0].message.content or "").strip()
    except Exception as e:
        logger.warning(f"表情包自动打标失败：{e}")
        return ""

    # 清理：只保留中文词和英文词，去多余空格
    import re
    tags = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9\s]", " ", tags)
    tags = " ".join(tags.split())[:120]  # 限制总长度

    if tags:
        with _ensure_db() as conn:
            conn.execute(
                "UPDATE stickers SET tags = ? WHERE url_hash = ?",
                (tags, h),
            )
            conn.commit()
        logger.info(f"表情包打标 hash={h} tags={tags}")
    return tags


def get_random_sticker(keyword: str | None = None) -> str | None:
    """按关键词模糊匹配表情包，返回本地文件路径。

    匹配策略：
    1. tags 包含该关键词的随机取一张
    2. 没匹配到 → 从全库随机取
    """
    with _ensure_db() as conn:
        if keyword:
            rows = conn.execute(
                "SELECT file_path FROM stickers "
                "WHERE tags LIKE ? ORDER BY RANDOM() LIMIT 1",
                (f"%{keyword}%",),
            ).fetchall()
        else:
            rows = []
        if not rows:
            rows = conn.execute(
                "SELECT file_path FROM stickers ORDER BY RANDOM() LIMIT 1"
            ).fetchall()
    if not rows:
        return None
    path = rows[0]["file_path"]
    if os.path.isfile(path):
        return path
    return None


def sticker_count() -> int:
    with _ensure_db() as conn:
        row = conn.execute("SELECT COUNT(*) as c FROM stickers").fetchone()
    return row["c"] if row else 0


def list_sticker_tags(*, limit: int = 50) -> list[str]:
    """列出所有已有的 tags（去重，供调试用）。"""
    with _ensure_db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT tags FROM stickers WHERE tags != '' LIMIT ?",
            (limit,),
        ).fetchall()
    return [r["tags"] for r in rows]


# ---------- 工具处理器 ----------


async def handle(params: str) -> str:
    """tool_router 工具处理函数：根据关键词模糊匹配一张表情包。"""
    keyword = params.strip() or None
    file_path = get_random_sticker(keyword)
    if not file_path:
        return "[工具出错] 表情包库为空，还没有收集到表情包。"
    return (
        f"{STICKER_TOKEN} {file_path}\n"
        f"已选好表情包（关键词：{keyword or '随机'}）。"
    )


def extract_sticker_file(tool_result: str) -> str | None:
    """从工具结果中提取表情包文件路径。"""
    if STICKER_TOKEN not in tool_result:
        return None
    for line in tool_result.split("\n"):
        if STICKER_TOKEN in line:
            path = line.replace(STICKER_TOKEN, "").strip()
            if path and os.path.isfile(path):
                return path
    return None


# ---------- 内部工具 ----------


def _guess_ext(url: str) -> str:
    lower = url.lower().split("?")[0]
    if lower.endswith(".gif"):
        return ".gif"
    if lower.endswith(".webp"):
        return ".webp"
    if lower.endswith(".png"):
        return ".png"
    return ".jpg"
