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

import base64
import logging
import re
from typing import Any, Dict, List, Optional
from urllib import request as urllib_request

from .store import KeywordsStore
from .templates import render_template_text, strip_auto_media_text

logger = logging.getLogger("plugin.foolllll.keywords-reply")

_MENTION_PATTERN = re.compile(r"\[@\s*(\d+)\]")
_MGMT_COMMAND_PATTERN = re.compile(
    r"/(?:添加|编辑|删除|启用|禁用|查看)(?:关键词|检测词)(?:回复)?(?:\s|$)"
)


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


def _unwrap_message_payload(raw: Any) -> Optional[Dict[str, Any]]:
    """兼容 ``get_by_id`` 直接返回消息体或 ``{success, message}`` 包装。"""

    if not isinstance(raw, dict):
        return None
    if isinstance(raw.get("raw_message"), list):
        return raw
    if raw.get("success") is False:
        return None
    nested = raw.get("message")
    if isinstance(nested, dict):
        return nested
    return None


def _segments_of(message: Any) -> List[dict]:
    """从消息字典中取出 ``raw_message`` 段列表。"""

    payload = _unwrap_message_payload(message)
    if not payload:
        return []
    segments = payload.get("raw_message")
    return segments if isinstance(segments, list) else []


def _download_url_base64(url: str) -> str:
    """尽力从 HTTP(S) URL 下载资源并转为 base64。"""

    normalized = str(url or "").strip()
    if not normalized.startswith(("http://", "https://")):
        return ""
    try:
        with urllib_request.urlopen(normalized, timeout=15) as resp:
            return base64.b64encode(resp.read()).decode("utf-8")
    except Exception as exc:
        logger.warning(f"下载媒体失败: {normalized[:120]} error={exc}")
        return ""


def _resolve_segment_binary_base64(seg: dict) -> str:
    """从消息段解析可用于落盘的 base64 数据。"""

    direct = str(seg.get("binary_data_base64", "") or "").strip()
    if direct:
        return direct

    data = _segment_data(seg)
    if isinstance(data, str):
        normalized = data.strip()
        if normalized.startswith(("http://", "https://")):
            return _download_url_base64(normalized)
        if normalized.startswith("base64://"):
            return normalized[len("base64://") :]
        if normalized.startswith("data:") and ";base64," in normalized:
            return normalized.split(";base64,", 1)[1]
    return ""


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
            b64 = _resolve_segment_binary_base64(seg)
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
            data = _segment_data(seg) or {}
            if isinstance(data, dict) and str(data.get("type", "")).lower() == "face":
                face_data = data.get("data", data)
                face_id = face_data.get("id") if isinstance(face_data, dict) else None
                if face_id is not None:
                    entry["faces"].append({"id": face_id})

    if include_text:
        entry["text"] = strip_auto_media_text("".join(text_parts).strip())
    else:
        entry["text"] = ""
    return entry


def sanitize_entry_text(entry: dict, store: KeywordsStore) -> dict:
    """若已捕获到富媒体，则移除 MaiBot 自动生成的占位描述文本。"""

    if not isinstance(entry, dict):
        return entry
    has_media = bool(entry.get("images") or entry.get("records") or entry.get("emojis"))
    if has_media:
        entry["text"] = strip_auto_media_text(str(entry.get("text", "") or ""))
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


async def _fetch_message_segments(ctx: Any, message_id: str, stream_id: str) -> List[dict]:
    """通过 ``get_by_id`` 拉取消息段；失败时返回空列表。"""

    if not message_id:
        return []
    try:
        raw = await ctx.message.get_by_id(
            message_id,
            stream_id=stream_id,
            chat_id=stream_id,
            include_binary_data=True,
        )
    except Exception as exc:
        logger.warning(f"get_by_id 失败: message_id={message_id}, error={exc}")
        return []
    return _segments_of(raw)


def extract_management_command_text(message: Dict[str, Any]) -> str:
    """从入站消息原始段提取管理命令文本（不依赖 ``processed_plain_text``）。"""

    if not isinstance(message, dict):
        return ""
    parts: List[str] = []
    for seg in message.get("raw_message", []) or []:
        if not isinstance(seg, dict) or str(seg.get("type", "")).strip().lower() != "text":
            continue
        text = str(seg.get("data", "") or "").strip()
        if text:
            parts.append(text)
    return " ".join(parts).strip()


def is_management_command_message(message: Dict[str, Any]) -> bool:
    """判断消息是否包含本插件的管理命令。"""

    text = extract_management_command_text(message)
    return bool(text and _MGMT_COMMAND_PATTERN.search(text))


def capture_media_from_message_dict(message: Dict[str, Any], store: KeywordsStore) -> dict:
    """从序列化消息字典捕获富媒体（用于 ``before_process`` 阶段，二进制尚未被主程序丢弃）。"""

    if not isinstance(message, dict):
        return store.empty_entry()
    return build_entry_from_segments(message.get("raw_message", []) or [], store, include_text=False)


async def capture_media_from_trigger(ctx: Any, store: KeywordsStore, message: Dict[str, Any]) -> dict:
    """从触发命令的消息中捕获媒体（不含文本，文本由命令解析器处理）。"""

    empty = store.empty_entry()
    if not isinstance(message, dict):
        return empty

    stream_id = str(message.get("session_id", "") or "").strip()
    local_segments = message.get("raw_message", []) or []
    local_entry = build_entry_from_segments(local_segments, store, include_text=False)
    if store.entry_has_payload(local_entry):
        return local_entry

    message_id = str(message.get("message_id", "") or "").strip()
    if not message_id:
        return empty

    segments = await _fetch_message_segments(ctx, message_id, stream_id)
    if segments:
        return build_entry_from_segments(segments, store, include_text=False)
    return empty


async def capture_from_reply(ctx: Any, store: KeywordsStore, message: Dict[str, Any]) -> dict:
    """从被引用消息中捕获完整内容（文本 + 媒体）。"""

    empty = store.empty_entry()
    target_id = _extract_reply_target_id(message)
    if not target_id:
        return empty
    stream_id = str(message.get("session_id", "") or "").strip() if isinstance(message, dict) else ""
    segments = await _fetch_message_segments(ctx, target_id, stream_id)
    if not segments:
        return empty
    return build_entry_from_segments(segments, store, include_text=True)


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
