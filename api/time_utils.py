"""
时间格式化工具 (time_utils)
===========================

【这个文件做什么】
把不同平台返回的"重置时间"统一格式化成给用户看的字符串。
不同平台用的时间格式不一样:
  - GLM:毫秒时间戳(nextResetTime,如 1718000000000)
  - Volcengine:秒时间戳(ResetTimestamp,如 1718000000),-1 表示无重置
  - Claude/ZenMux:ISO 8601 字符串(如 "2025-06-01T12:00:00Z")
所以提供三个函数分别处理,再加一个按"单位"自动派发的入口。

【格式化规则】(从原 glm_client._format_reset_time 沿用)
  - 当天重置:只显示 HH:MM
  - 跨天:    显示 YYYY-MM-DD HH:MM
  - 空/无效: 返回空串(UI 据此不显示重置时间)
"""
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def _format_dt(dt: datetime) -> str:
    """把 datetime 格式化:当天只显示时分,跨天显示完整日期。"""
    now = datetime.now()
    try:
        if dt.date() == now.date():
            return dt.strftime("%H:%M")
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception as e:
        logger.warning("格式化时间失败(%s)", type(e).__name__)
        return "未知时间"


def format_reset_time_ms(timestamp_ms: Optional[int]) -> str:
    """毫秒时间戳(GLM / Kimi 等)→ 字符串。空/0 返回空串。"""
    if not timestamp_ms:
        return ""
    try:
        return _format_dt(datetime.fromtimestamp(timestamp_ms / 1000))
    except Exception as e:
        logger.warning("解析毫秒时间戳失败(%s)", type(e).__name__)
        return "未知时间"


def format_reset_time_seconds(timestamp_sec: Optional[int]) -> str:
    """秒时间戳(Volcengine)→ 字符串。None / <=0(如 -1 表示无重置)返回空串。"""
    if timestamp_sec is None or timestamp_sec <= 0:
        return ""
    try:
        return _format_dt(datetime.fromtimestamp(timestamp_sec))
    except Exception as e:
        logger.warning("解析秒时间戳失败(%s)", type(e).__name__)
        return "未知时间"


def format_reset_time_iso(iso_str: Optional[str]) -> str:
    """ISO 8601 字符串(Claude/ZenMux)→ 字符串。空/解析失败返回空串。"""
    if not iso_str:
        return ""
    try:
        # datetime.fromisoformat 在 3.11+ 才认结尾的 Z,这里手动替换成 +00:00
        s = iso_str.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        # 带时区的转成本地时间再格式化,显示更直观
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return _format_dt(dt)
    except Exception as e:
        logger.warning("解析 ISO 时间失败(%s)", type(e).__name__)
        return ""


def format_reset_time(value) -> str:
    """自动识别重置时间:数字按毫秒(GLM/Kimi),字符串按 ISO(ZenMux/Claude)。

    不同平台字段类型不一,这个入口替你判断;Volcengine 的"秒"时间戳请直接用
    format_reset_time_seconds(它单独区分)。
    """
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return format_reset_time_ms(int(value))
    if isinstance(value, str):
        return format_reset_time_iso(value)
    return ""
