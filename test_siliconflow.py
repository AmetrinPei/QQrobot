"""硅基流动 API 本地测通脚本。

密钥从 .env.dev 读取，不要把 sk- 写进本文件。

用法：
  py test_siliconflow.py
"""

from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
import os

load_dotenv(Path(__file__).with_name(".env.dev"))

SILICONFLOW_API_KEY = os.getenv("SILICONFLOW_API_KEY", "")
BASE_URL = os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1")
MODEL = os.getenv("SILICONFLOW_MODEL", "deepseek-ai/DeepSeek-V4-Pro")


def main() -> None:
    if not SILICONFLOW_API_KEY or "粘贴" in SILICONFLOW_API_KEY:
        raise SystemExit("请先在 .env.dev 里填写 SILICONFLOW_API_KEY")

    client = OpenAI(api_key=SILICONFLOW_API_KEY, base_url=BASE_URL)

    print(f"模型: {MODEL}")
    print("请求中...\n")

    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": "你是一个简洁的助手。"},
            {"role": "user", "content": "用一句话介绍你自己，并确认已连通。"},
        ],
        max_tokens=256,
        temperature=0.7,
    )

    message = response.choices[0].message
    reasoning = getattr(message, "reasoning_content", None)
    if reasoning:
        print("--- 思考过程 ---")
        print(reasoning)
        print()

    print("--- 回复 ---")
    print(message.content or "(空回复)")
    print("\n测通成功。")


if __name__ == "__main__":
    main()
