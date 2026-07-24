"""消息图片解析 + 硅基流动视觉模型描述。"""

from __future__ import annotations

from nonebot import logger
from nonebot.adapters.onebot.v11 import MessageEvent
from openai import AsyncOpenAI

DESCRIBE_PROMPT = (
    "请客观简洁地用中文描述图片内容：主体、文字、场景、情绪氛围。"
    "不要猜测未看见的细节；看不清就说看不清。"
)


def extract_image_urls(event: MessageEvent, *, max_images: int = 3) -> list[str]:
    """从 OneBot 消息段提取可访问的图片 URL（NapCat 通常带 url）。"""
    urls: list[str] = []
    for seg in event.message:
        if seg.type != "image":
            continue
        data = seg.data or {}
        candidates = [
            data.get("url"),
            data.get("file"),
        ]
        for raw in candidates:
            if not raw or not isinstance(raw, str):
                continue
            url = raw.strip()
            if url.startswith(("http://", "https://", "data:image")):
                if url not in urls:
                    urls.append(url)
                break
        if len(urls) >= max_images:
            break
    return urls


def message_has_image(event: MessageEvent) -> bool:
    return any(seg.type == "image" for seg in event.message)


def is_sticker_like(event: MessageEvent) -> bool:
    """动画表情 / 表情包（NapCat 常见 sub_type=1 或 summary 含「表情」）。"""
    for seg in event.message:
        if seg.type != "image":
            continue
        data = seg.data or {}
        sub = str(data.get("sub_type", "0"))
        if sub in ("1", "sticker"):
            return True
        summary = str(data.get("summary") or "")
        if "表情" in summary:
            return True
    return False


def is_image_followup_candidate(event: MessageEvent) -> bool:
    """群聊跟进：带图，且几乎没有有效文字（表情包/纯图常见）。"""
    if not message_has_image(event):
        return False
    text = (event.get_plaintext() or "").strip()
    # 去掉纯空白；极短口头禅可视为跟图
    if not text:
        return True
    if len(text) <= 8 and is_sticker_like(event):
        return True
    return False


async def describe_images(
    client: AsyncOpenAI,
    *,
    model: str,
    image_urls: list[str],
    user_hint: str = "",
) -> str:
    """调用视觉模型得到图片描述；失败返回空字符串。"""
    if not image_urls:
        return ""

    content: list[dict] = []
    for url in image_urls:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": url, "detail": "auto"},
            }
        )
    hint = user_hint.strip() or "请描述这些图片。"
    content.append(
        {
            "type": "text",
            "text": f"{DESCRIBE_PROMPT}\n用户补充：{hint}",
        }
    )

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": content}],
            max_tokens=512,
            temperature=0.2,
        )
        return (response.choices[0].message.content or "").strip()
    except Exception as e:
        logger.warning(f"视觉模型调用失败：{e}")
        return ""


def build_user_content_with_vision(
    text: str,
    image_description: str,
) -> str:
    """把识图结果并入交给文本模型的用户内容。"""
    text = (text or "").strip()
    desc = (image_description or "").strip()
    if desc and text:
        return f"{text}\n\n[图片内容描述]\n{desc}"
    if desc:
        return f"[用户发来图片]\n[图片内容描述]\n{desc}"
    return text
