"""联网搜索工具：通过 DuckDuckGo HTML 搜索获取实时信息。

无需 API Key，开箱即用。若需更稳定的结果可后续切换 Bing / SerpAPI。
"""

from __future__ import annotations

import re
from html import unescape

import httpx
from nonebot import logger

# DuckDuckGo HTML 搜索端点（无需 JS 渲染）
_SEARCH_URL = "https://html.duckduckgo.com/html/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# 工具说明（注入 system prompt，让模型知道何时调用）
TOOL_DESCRIPTION = (
    "【可用工具：web_search】\n"
    "当用户的问题涉及时效性信息（近期新闻、天气、赛事比分、价格变动、"
    "热点事件、实时数据等），而你的训练数据无法覆盖时，请调用联网搜索。\n"
    "调用方式：仅输出一行 [TOOL_CALL] web_search | 搜索关键词\n"
    "规则：\n"
    "- 只输出上面这一行，不要附带任何解释、问候或其他文字\n"
    "- 关键词要精准简洁（中文问题用中文关键词）\n"
    "- 日常闲聊、情感交流、你确定知道答案的问题 → 不要调用，正常回复\n"
    "- 每轮对话最多调用一次\n"
    "- 系统会自动搜索并把结果返回给你，届时你再基于结果自然地回答"
)


async def search_web(
    query: str,
    *,
    max_results: int = 5,
    timeout: float = 10.0,
) -> str:
    """执行 DuckDuckGo 搜索，返回格式化文本。失败返回错误提示。"""
    if not query.strip():
        return "[搜索失败] 关键词为空。"

    for attempt in range(2):
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                headers=_HEADERS,
                follow_redirects=True,
            ) as client:
                resp = await client.post(_SEARCH_URL, data={"q": query, "kl": "cn-zh"})
                resp.raise_for_status()
                results = _parse_html_results(resp.text, max_results)

            if not results:
                return f"[搜索完成] 未找到与「{query}」相关的结果。"
            return _format_results(query, results)

        except httpx.TimeoutException:
            logger.warning(f"联网搜索超时 attempt={attempt + 1} query={query!r}")
        except Exception as e:
            logger.warning(f"联网搜索异常 attempt={attempt + 1}：{e}")

    return f"[搜索失败] 网络请求超时或异常，无法获取「{query}」的搜索结果。请凭自身知识回答。"


# ---------- 内部解析 ----------


def _parse_html_results(html: str, max_results: int) -> list[dict[str, str]]:
    """从 DuckDuckGo HTML 响应中提取搜索结果。"""
    results: list[dict[str, str]] = []

    # 每条结果在 <div class="result ..."> ... </div> 中
    blocks = re.findall(
        r'<div[^>]*class="[^"]*result\b[^"]*"[^>]*>(.*?)</div>\s*</div>',
        html,
        re.DOTALL,
    )
    # 备用：更宽松地匹配 result__a + result__snippet
    if not blocks:
        blocks = re.findall(
            r'(<a[^>]*class="[^"]*result__a[^"]*".*?</a>.*?'
            r'class="[^"]*result__snippet[^"]*".*?</[atd])',
            html,
            re.DOTALL,
        )

    for block in blocks:
        if len(results) >= max_results:
            break

        # 标题 + 链接
        a_match = re.search(
            r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
            block,
            re.DOTALL,
        )
        if not a_match:
            continue

        url = unescape(a_match.group(1)).strip()
        title = _strip_tags(a_match.group(2)).strip()

        # DuckDuckGo 有时用跳转链接 //duckduckgo.com/l/?uddg=ENCODED_URL
        if "uddg=" in url:
            from urllib.parse import parse_qs, urlparse

            qs = parse_qs(urlparse(url).query)
            real = qs.get("uddg", [""])[0]
            if real:
                url = real

        # 摘要
        snip_match = re.search(
            r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</[atd]',
            block,
            re.DOTALL,
        )
        snippet = _strip_tags(snip_match.group(1)).strip() if snip_match else ""

        if title:
            results.append({"title": title, "url": url, "snippet": snippet})

    return results


def _strip_tags(html: str) -> str:
    """去除 HTML 标签并反转义实体。"""
    text = re.sub(r"<[^>]+>", "", html)
    return unescape(text)


def _format_results(query: str, results: list[dict[str, str]]) -> str:
    """将搜索结果格式化为注入 prompt 的文本。"""
    lines = [f"以下是关于「{query}」的联网搜索结果（仅供参考，注意时效性）：\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}")
        if r["snippet"]:
            lines.append(f"   摘要：{r['snippet']}")
        if r["url"]:
            lines.append(f"   来源：{r['url']}")
        lines.append("")
    return "\n".join(lines)
