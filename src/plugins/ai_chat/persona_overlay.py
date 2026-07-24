"""管理员人设/形象覆盖层：持久化并在 system prompt 中优先于本地 persona.txt。"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from .paths import DATA_DIR, DB_PATH, PERSONA_FILE


def _ensure_columns(conn: sqlite3.Connection) -> None:
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(persona_overlay)")}
    if "appearance" not in cols:
        conn.execute(
            "ALTER TABLE persona_overlay ADD COLUMN appearance TEXT NOT NULL DEFAULT ''"
        )


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS persona_overlay (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            content TEXT NOT NULL DEFAULT '',
            appearance TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL,
            updated_by TEXT
        )
        """
    )
    _ensure_columns(conn)
    row = conn.execute("SELECT id FROM persona_overlay WHERE id = 1").fetchone()
    if not row:
        conn.execute(
            """
            INSERT INTO persona_overlay (id, content, appearance, updated_at, updated_by)
            VALUES (1, '', '', ?, NULL)
            """,
            (time.time(),),
        )
    conn.commit()
    return conn


def get_overlay() -> dict[str, Any]:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT content, appearance, updated_at, updated_by
            FROM persona_overlay WHERE id = 1
            """
        ).fetchone()
    if not row:
        return {
            "content": "",
            "appearance": "",
            "updated_at": None,
            "updated_by": None,
        }
    return {
        "content": str(row["content"] or ""),
        "appearance": str(row["appearance"] or ""),
        "updated_at": float(row["updated_at"]) if row["updated_at"] else None,
        "updated_by": row["updated_by"],
    }


def set_overlay(
    content: str | None = None,
    *,
    appearance: str | None = None,
    updated_by: str | int | None = None,
) -> dict[str, Any]:
    cur = get_overlay()
    new_content = cur["content"] if content is None else (content or "").strip()
    new_appearance = (
        cur["appearance"] if appearance is None else (appearance or "").strip()
    )
    now = time.time()
    by = str(updated_by) if updated_by is not None else cur.get("updated_by")
    with _connect() as conn:
        conn.execute(
            """
            UPDATE persona_overlay
            SET content = ?, appearance = ?, updated_at = ?, updated_by = ?
            WHERE id = 1
            """,
            (new_content, new_appearance, now, by),
        )
        conn.commit()
    return get_overlay()


def append_overlay(extra: str, *, updated_by: str | int | None = None) -> dict[str, Any]:
    cur = get_overlay()["content"].strip()
    add = (extra or "").strip()
    if not add:
        return get_overlay()
    merged = f"{cur}\n{add}".strip() if cur else add
    return set_overlay(content=merged, updated_by=updated_by)


def clear_overlay(*, updated_by: str | int | None = None) -> dict[str, Any]:
    return set_overlay(content="", appearance="", updated_by=updated_by)


def clear_persona(*, updated_by: str | int | None = None) -> dict[str, Any]:
    return set_overlay(content="", updated_by=updated_by)


def clear_appearance(*, updated_by: str | int | None = None) -> dict[str, Any]:
    return set_overlay(appearance="", updated_by=updated_by)


def format_overlay_for_prompt(overlay: dict[str, Any] | None = None) -> str:
    """管理员覆盖层：明确优先于本地 persona 文件。"""
    st = overlay or get_overlay()
    appearance = (st.get("appearance") or "").strip()
    text = (st.get("content") or "").strip()
    if not appearance and not text:
        return ""
    parts = [
        "【管理员修订·最高优先】",
        "以下由管理员设定。若与上文本地人设文件冲突，**一律以本段为准**"
        "（本地文件仅补充未覆盖部分）：",
    ]
    if appearance:
        parts.append(f"## 形象（管理员）\n{appearance}")
    if text:
        parts.append(f"## 人设修订（管理员）\n{text}")
    return "\n".join(parts)


def load_base_persona() -> str:
    if PERSONA_FILE.is_file():
        text = PERSONA_FILE.read_text(encoding="utf-8").strip()
        if text:
            return text
    return "你是一个有用的QQ助手，回答简洁清晰。"


def load_full_persona() -> str:
    """本地人设 + 管理员覆盖（覆盖段在后，并声明优先）。"""
    base = load_base_persona()
    overlay = format_overlay_for_prompt()
    if not overlay:
        return base
    return f"{base}\n\n{overlay}"
