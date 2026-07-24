"""机器人运行开关：暂停后除管理员启停/管理指令外不回复。"""

from __future__ import annotations

import time
from pathlib import Path

from .paths import DATA_DIR

_FLAG = DATA_DIR / "bot_paused.flag"


def is_paused() -> bool:
    try:
        return _FLAG.is_file()
    except OSError:
        return False


def set_paused(paused: bool) -> bool:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if paused:
        _FLAG.write_text(str(time.time()), encoding="utf-8")
    elif _FLAG.is_file():
        _FLAG.unlink()
    return is_paused()


def status_text() -> str:
    return "已停止（仅管理员指令可用）" if is_paused() else "运行中"
