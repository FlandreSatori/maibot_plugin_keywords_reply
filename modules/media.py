"""富媒体捕获与发送段构建。

- 捕获：从触发消息 / 引用消息的 ``raw_message`` 段中提取图片、语音、表情、At，
  并把二进制内容落盘（通过 :class:`KeywordsStore`）。
- 发送：把存储的 entry 转换为 ``ctx.send.hybrid`` / ``ctx.send.forward`` 使用的消息段。

MaiBot 出站消息段格式（见 host message_utils）::

    {"type": "text", "content": "..."}
    {"type": "image", "content": <base64>}
    {"type": "emoji", "content": <base64>}
    {"type": "voice", "content": <base64>}
    {"type": "at", "data": {"target_user_id": "123", "target_user_nickname": "昵称"}}
    {"type": "reply", "data": {"target_message_id": "..."}}
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from .store import KeywordsStore
from .templates import render_template_text

logger = logging.getLogger("plugin.foolllll.keywords-reply")

_MENTION_PATTERN = re.compile(r"\[@\s*(\d+)\]")


def extract_inline_ats(text: str) -> tuple[str, List[dict]]:
    """从文本中解析 ``[@12345]`` 转义 At，返回 (剩余文本, ats)。"""

    if not text:
        return "", []
    ats: List[dict] = []
    parts: List[str] = []
    last = 0
    for m in _MENTION_PATTERN.finditer(text):
        if m.start() > last:
            parts.append(text[last:m.start()])
        ats.append({"user_id": m.group(1), "nickname": "", "all": False})
        last = m.end()
    if last < len(text):
        parts.append(text[last:])
    return "".join(parts), ats


def _segment_data(seg: dict) -> Any:
    return seg.get("data")


def build_entry_from_segments(
    segments: List[dict],
    store: KeywordsStore,
    include_text: bool = True,
) -> dict:
    """把 raw_message 段解析为一条 entry（媒体落盘）。"""

    entry = store.empty_entry()
    text_parts: List[str] = []

    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        seg_type = str(seg.get("type", "")).strip().lower()

        if seg_type == "text":
            # 仅在需要文本时解析文本与内联 [@]，避免与命令解析器重复提取。
            if include_text:
                content = str(_segment_data(seg) or "")
                if content:
                    plain, inline_ats = extract_inline_ats(content)
                    if plain:
                        text_parts.append(plain)
                    entry["ats"].extend(inline_ats)
        elif seg_type == "at":
            data = _segment_data(seg) or {}
            if isinstance(data, dict):
                uid = str(data.get("target_user_id", "") or "").strip()
                nickname = str(data.get("target_user_nickname", "") or "")
                if uid:
                    entry["ats"].append({"user_id": uid, "nickname": nickname, "all": uid.lower() == "all"})
        elif seg_type in ("image", "emoji", "voice"):
            b64 = str(seg.get("binary_data_base64", "") or "")
            media_key = {"image": "images", "emoji": "emojis", "voice": "records"}[seg_type]
            if b64:
                filename = store.save_media_base64(media_key, b64)
                if filename:
                    entry[media_key].append({"file": filename})
        elif seg_type == "face":
            data = _segment_data(seg) or {}
            if isinstance(data, dict) and data.get("id") is not None:
                entry["faces"].append({"id": data.get("id")})
        elif seg_type == "dict":
            # QQ 原生表情等以 DictComponent 透传，尽力识别 face
            data = _segment_data(seg) or {}
            if isinstance(data, dict) and str(data.get("type", "")).lower() == "face":
                face_data = data.get("data", data)
                face_id = face_data.get("id") if isinstance(face_data, dict) else None
                if face_id is not None:
                    entry["faces"].append({"id": face_id})

    if include_text:
        entry["text"] = "".join(text_parts).strip()
    return entry


def _extract_reply_target_id(message: Dict[str, Any]) -> Optional[str]:
    """从消息中解析被引用消息的 ID。"""

    if not isinstance(message, dict):
        return None
    for seg in message.get("raw_message", []) or []:
        if isinstance(seg, dict) and str(seg.get("type", "")).lower() == "reply":
            data = seg.get("data")
            if isinstance(data, dict):
                target = str(data.get("target_message_id", "") or "").strip()
                if target:
                    return target
            elif data:
                return str(data).strip()
    reply_to = message.get("reply_to")
    if reply_to:
        return str(reply_to).strip()
    return None


async def capture_media_from_trigger(ctx: Any, store: KeywordsStore, message: Dict[str, Any]) -> dict:
    """从触发命令的消息中捕获媒体（不含文本，文本由命令解析器处理）。

    命令消息在 Command 处理器中不含二进制数据，需通过 get_by_id 重新拉取。
    """

    empty = store.empty_entry()
    if not isinstance(message, dict):
        return empty
    message_id = str(message.get("message_id", "") or "").strip()
    stream_id = str(message.get("session_id", "") or "").strip()
    if not message_id:
        # 退化：直接使用手头 segments（可能已含二进制，如事件场景）
        return build_entry_from_segments(message.get("raw_message", []), store, include_text=False)
    try:
        full = await ctx.message.get_by_id(message_id, stream_id=stream_id, include_binary_data=True)
    except Exception as exc:
        logger.warning(f"拉取触发消息媒体失败: {exc}")
        full = None
    segments = _segments_of(full) or message.get("raw_message", [])
    return build_entry_from_segments(segments, store, include_text=False)


async def capture_from_reply(ctx: Any, store: KeywordsStore, message: Dict[str, Any]) -> dict:
    """从被引用消息中捕获完整内容（文本 + 媒体）。"""

    empty = store.empty_entry()
    target_id = _extract_reply_target_id(message)
    if not target_id:
        return empty
    stream_id = str(message.get("session_id", "") or "").strip() if isinstance(message, dict) else ""
    try:
        quoted = await ctx.message.get_by_id(target_id, stream_id=stream_id, include_binary_data=True)
    except Exception as exc:
        logger.warning(f"拉取引用消息失败: message_id={target_id}, error={exc}")
        return empty
    segments = _segments_of(quoted)
    if not segments:
        return empty
    return build_entry_from_segments(segments, store, include_text=True)


def _segments_of(message: Any) -> List[dict]:
    """从 get_by_id 返回值中取出 raw_message 段列表。"""

    if isinstance(message, dict):
        segments = message.get("raw_message")
        if isinstance(segments, list):
            return segments
    return []


# ─── 发送段构建 ────────────────────────────────────────────────


def build_send_segments(
    entry: dict,
    store: KeywordsStore,
    message: Dict[str, Any],
    *,
    quote: bool = False,
    enable_template: bool = True,
    render: bool = True,
) -> List[dict]:
    """把一条 entry 转换为 ctx.send.hybrid 使用的消息段列表。"""

    segments: List[dict] = []

    if quote and isinstance(message, dict):
        target_id = str(message.get("message_id", "") or "").strip()
        if target_id:
            segments.append({"type": "reply", "data": {"target_message_id": target_id}})

    text = entry.get("text") or ""
    if text:
        if render:
            text = render_template_text(text, message, enabled=enable_template)
        segments.append({"type": "text", "content": text})

    for at in entry.get("ats", []):
        uid = "all" if at.get("all") else str(at.get("user_id", "") or "")
        if uid:
            segments.append(
                {
                    "type": "at",
                    "data": {"target_user_id": uid, "target_user_nickname": str(at.get("nickname", "") or "")},
                }
            )

    for face in entry.get("faces", []):
        # 尽力发送 QQ 原生表情；不受支持的适配器会降级忽略。
        segments.append({"type": "face", "data": {"id": face.get("id")}})

    for img in entry.get("images", []):
        b64 = store.read_media_base64("images", (img or {}).get("file", ""))
        if b64:
            segments.append({"type": "image", "content": b64})

    for emoji in entry.get("emojis", []):
        b64 = store.read_media_base64("emojis", (emoji or {}).get("file", ""))
        if b64:
            segments.append({"type": "emoji", "content": b64})

    for voice in entry.get("records", []):
        b64 = store.read_media_base64("records", (voice or {}).get("file", ""))
        if b64:
            segments.append({"type": "voice", "content": b64})

    return segments


def build_forward_messages(
    entries: List[dict],
    store: KeywordsStore,
    message: Dict[str, Any],
    *,
    self_id: str = "0",
    nickname: str = "关键词回复",
    enable_template: bool = True,
) -> List[dict]:
    """把多条 entry 转换为 ctx.send.forward 使用的转发节点列表。"""

    messages: List[dict] = []
    for entry in entries:
        segments = build_send_segments(
            entry,
            store,
            message,
            quote=False,
            enable_template=enable_template,
        )
        if segments:
            messages.append({"user_id": str(self_id), "nickname": nickname, "segments": segments})
    return messages
