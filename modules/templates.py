"""文本变量模板渲染与正则安全检查。"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict

_TEMPLATE_PATTERN = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

# MaiBot 入站预处理会把图片/表情/语音转成可读占位文本，不应作为关键词回复的正文保存。
_AUTO_MEDIA_TEXT_PATTERN = re.compile(
    r"\[(?:"
    r"图片[：:][^\]]*"
    r"|图片(?:\s*x\d+)?"
    r"|图片，识别中\.\.\.\.\.\."
    r"|表情[：:][^\]]*"
    r"|表情(?:\s*x\d+)?"
    r"|face(?:\s*x\d+)?"
    r"|mface(?:\s*x\d+)?"
    r"|表情包[：:][^\]]*"
    r"|表情包"
    r"|语音[：:][^\]]*"
    r"|语音消息[^\]]*"
    r"|视频[：:][^\]]*"
    r"|视频(?:\s*x\d+)?"
    r"|网易云音乐[^\]]*"
    r"|QQ音乐[^\]]*"
    r"|音乐[^\]]*"
    r")\]"
)

# 危险正则片段，用于粗略防止 ReDoS
_DANGEROUS_REGEX_PATTERNS = [
    r"\(\?\:",
    r"\(\?\!",
    r"\(\?\<",
    r"\*\+",
    r"\+\*",
    r"\*\*",
    r"\+\+",
    r"\((?:[^()]*[+*{][^()]*)\)\s*\+",
    r"\{[^{}]*\}[^{}]*\{[^{}]*\}",
]


def build_template_context(message: Dict[str, Any]) -> Dict[str, str]:
    """从 MaiBot 消息字典构建模板上下文。"""

    now = datetime.now()
    info = message.get("message_info", {}) if isinstance(message, dict) else {}
    user_info = info.get("user_info", {}) if isinstance(info, dict) else {}
    group_info = info.get("group_info", {}) if isinstance(info, dict) else {}
    group_info = group_info if isinstance(group_info, dict) else {}
    return {
        "user_id": str((user_info or {}).get("user_id", "") or ""),
        "user_name": str((user_info or {}).get("user_nickname", "") or ""),
        "group_id": str((group_info or {}).get("group_id", "") or ""),
        "self_id": str(info.get("self_id", "") or "") if isinstance(info, dict) else "",
        "platform": str(message.get("platform", "") or "") if isinstance(message, dict) else "",
        "message": str(message.get("processed_plain_text", "") or "") if isinstance(message, dict) else "",
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
    }


def render_template_text(text: str, message: Dict[str, Any], enabled: bool = True) -> str:
    """按上下文替换文本中的 ``{变量}`` 占位符。"""

    if not text or not enabled:
        return text
    context = build_template_context(message)
    return _TEMPLATE_PATTERN.sub(lambda m: context.get(m.group(1), m.group(0)), text)


def strip_auto_media_text(text: str) -> str:
    """移除 MaiBot 自动生成的图片/表情/语音占位描述。"""

    if not text:
        return ""
    cleaned = _AUTO_MEDIA_TEXT_PATTERN.sub("", text)
    return re.sub(r"\s+", " ", cleaned).strip()


def is_safe_regex(pattern: str) -> bool:
    """粗略判断正则是否安全（防 ReDoS）。"""

    if len(pattern) > 100:
        return False
    for dangerous in _DANGEROUS_REGEX_PATTERNS:
        if re.search(dangerous, pattern):
            return False
    return True
