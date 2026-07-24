"""人设文件与记忆库目录：可由环境变量切换（见 bot_niko.py）。"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

_persona = os.environ.get("QQBOT_PERSONA_FILE", "").strip()
PERSONA_FILE = Path(_persona) if _persona else ROOT / "persona.txt"

_data = os.environ.get("QQBOT_DATA_DIR", "").strip()
DATA_DIR = Path(_data) if _data else ROOT / "data"
DB_PATH = DATA_DIR / "memory.db"
