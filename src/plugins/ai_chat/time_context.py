"""本地时间与作息倾向，注入到 system prompt。"""

from __future__ import annotations

from datetime import datetime

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _period_and_hint(hour: int) -> tuple[str, str]:
    """返回时段名与作息倾向短句。"""
    if 5 <= hour < 9:
        return "清晨", "语气可以短一点，正常闲聊即可。"
    if 9 <= hour < 12:
        return "上午", "语气正常。"
    if 12 <= hour < 14:
        return "中午", "闲聊仍简短即可。"
    if 14 <= hour < 18:
        return "下午", "语气正常。"
    if 18 <= hour < 22:
        return "傍晚到晚上", "可以更放松一点，仍不抢戏。"
    if 22 <= hour < 24:
        return "晚上", "回复可以短一点，但不要说自己困了、想睡。"
    # 0–4
    return "深夜", "回复极短即可；严禁主动说困了、想睡、去睡觉。"


def format_time_context(*, now: datetime | None = None) -> str:
    """供 system prompt 使用的时间与作息说明。"""
    dt = now or datetime.now()
    weekday = _WEEKDAYS[dt.weekday()]
    period, hint = _period_and_hint(dt.hour)
    clock = f"{dt.year}年{dt.month}月{dt.day}日"
    time_str = dt.strftime("%H:%M")
    return (
        f"当前本地时间：{clock} {weekday} {time_str}（{period}）。\n"
        f"作息倾向：{hint}\n"
        "可偶尔说「晚了」之类，但不要每句报时，更不要把「困了」当口头禅，也不要编造日程。"
    )
