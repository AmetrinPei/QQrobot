"""AI 绘图工具：通过 SiliconFlow 图像生成 API 生成图片并发送。

工具协议：模型输出 [TOOL_CALL] image_gen | 描述
主流程执行后检测到 [IMAGE_URL] 标记 → 发送图片消息。
"""

from __future__ import annotations

import httpx
from nonebot import logger

# 图片生成 API（SiliconFlow 兼容 OpenAI images/generations）
_DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
_DEFAULT_MODEL = "black-forest-labs/FLUX.1-schnell"

# 工具说明（注入 system prompt）
TOOL_DESCRIPTION = (
    "【可用工具：image_gen】\n"
    "当用户明确要求你画图、生成图片、画一个xxx时调用。\n"
    "调用方式：仅输出一行 [TOOL_CALL] image_gen | 英文画面描述\n"
    "规则：\n"
    "- 描述用英文，简洁具体（主体、风格、色调）\n"
    "- 用户只是随口说「画个饼」之类的玩笑不要调用\n"
    "- 每轮最多调用一次"
)

# 结果中的图片 URL 标记（主流程检测用）
IMAGE_URL_TOKEN = "[IMAGE_URL]"


async def handle(params: str) -> str:
    """工具处理函数：生成图片，返回含 [IMAGE_URL] 的结果文本。"""
    prompt = params.strip()
    if not prompt:
        return "[工具出错] 绘图描述为空。"

    # 延迟导入，避免未启用时加载配置
    from . import get_image_gen_config

    api_key, base_url, model = get_image_gen_config()
    if not api_key:
        return "[工具出错] 未配置 SILICONFLOW_API_KEY，无法生成图片。"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{base_url}/images/generations",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "prompt": prompt,
                    "image_size": "1024x1024",
                    "num_inference_steps": 20,
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.TimeoutException:
        logger.warning(f"图片生成超时 prompt={prompt!r}")
        return "[工具出错] 图片生成超时，请稍后再试。"
    except Exception as e:
        logger.warning(f"图片生成失败：{e}")
        return f"[工具出错] 图片生成失败：{e}"

    # 解析返回的图片 URL
    images = data.get("images") or data.get("data") or []
    url = ""
    if images:
        first = images[0]
        if isinstance(first, dict):
            url = first.get("url") or first.get("b64_json", "")
        elif isinstance(first, str):
            url = first

    if not url:
        return "[工具出错] 未能获取生成的图片。"

    return (
        f"{IMAGE_URL_TOKEN} {url}\n"
        f"图片已生成（描述：{prompt}）。请自然地告诉用户你画好了。"
    )


def extract_image_url_from_result(tool_result: str) -> str | None:
    """从工具结果中提取图片 URL（供主流程发送）。"""
    if IMAGE_URL_TOKEN not in tool_result:
        return None
    for line in tool_result.split("\n"):
        if IMAGE_URL_TOKEN in line:
            url = line.replace(IMAGE_URL_TOKEN, "").strip()
            if url.startswith("http"):
                return url
    return None
