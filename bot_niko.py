"""尼古喵喵风格人设入口：独立 persona + 独立记忆目录。

与 bot.py 换皮启动，不要同时跑（默认同端口会冲突）。
用法：py bot_niko.py
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# 必须在 import nonebot / 加载插件之前设置
os.environ["QQBOT_PERSONA_FILE"] = str(ROOT / "persona_niko.txt")
os.environ["QQBOT_DATA_DIR"] = str(ROOT / "data" / "niko")

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

nonebot.init()

driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

nonebot.load_from_toml("pyproject.toml")

if __name__ == "__main__":
    nonebot.run()
