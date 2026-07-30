"""通用工具路由框架。

模型在第一轮回复中输出 [TOOL_CALL] 工具名 | 参数 触发外部工具；
主流程解析 → 路由执行 → 将结果注入上下文 → 第二轮模型调用生成最终回复。

新增工具只需：
1. 写一个 async handler(params: str) -> str
2. 调用 register_tool() 注册
3. 在 system prompt 中添加工具说明（TOOL_DESCRIPTIONS）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Awaitable, Callable

from nonebot import logger

# 工具处理函数签名：接收参数字符串，返回结果文本
ToolHandler = Callable[[str], Awaitable[str]]

# 全局工具注册表
_registry: dict[str, ToolHandler] = {}

# 触发标记
TOOL_TOKEN = "[TOOL_CALL]"


@dataclass
class ToolCall:
    """解析出的工具调用。"""

    name: str
    params: str


def register_tool(name: str, handler: ToolHandler) -> None:
    """注册一个工具处理器（同名覆盖）。"""
    _registry[name] = handler
    logger.info(f"工具已注册：{name}")


def get_registered_tools() -> list[str]:
    return list(_registry.keys())


def has_tool_call(text: str) -> bool:
    """模型输出是否包含工具调用指令。"""
    return TOOL_TOKEN in text


def parse_tool_call(text: str) -> ToolCall | None:
    """从模型输出中解析 [TOOL_CALL] 工具名 | 参数。

    兼容全角竖线 ｜；参数可省略。
    """
    m = re.search(
        rf"\[TOOL_CALL\]\s*(\w+)\s*(?:[|｜]\s*(.+?))?\s*$",
        text,
        re.MULTILINE,
    )
    if not m:
        return None
    return ToolCall(name=m.group(1), params=(m.group(2) or "").strip())


async def execute_tool(call: ToolCall, *, timeout: float = 15.0) -> str:
    """执行工具调用，返回结果文本。失败时返回 [工具出错] ... 而非抛异常。"""
    handler = _registry.get(call.name)
    if handler is None:
        return f"[工具出错] 未知工具「{call.name}」，请忽略此调用并直接回答。"
    try:
        import asyncio

        result = await asyncio.wait_for(handler(call.params), timeout=timeout)
        return result or "[工具返回为空] 未获取到有效信息，请凭自身知识回答。"
    except asyncio.TimeoutError:
        logger.warning(f"工具 {call.name} 执行超时（{timeout}s）")
        return f"[工具出错] {call.name} 执行超时，请凭自身知识回答。"
    except Exception as e:
        logger.warning(f"工具 {call.name} 执行异常：{e}")
        return f"[工具出错] {e}"


def format_tool_result_for_prompt(
    call: ToolCall,
    result: str,
) -> str:
    """将工具执行结果格式化为第二轮对话的 user 消息。"""
    return (
        f"[系统提示] 你刚才调用了工具「{call.name}」，参数：{call.params or '无'}\n"
        f"以下是工具返回的结果，请基于这些结果用自然语言回答用户。"
        f"如果结果不足以回答，请诚实说明。\n\n"
        f"{result}"
    )
