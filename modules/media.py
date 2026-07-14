"""富媒体捕获与发送段构建。

- 捕获：从触发消息 / 引用消息的 ``raw_message`` 段中提取图片、语音、表情、At，
  并把二进制内容落盘（通过 :class:`KeywordsStore`）。
- 发送：把存储的 entry 转换为 ``ctx.send.hybrid`` / ``ctx.send.forward`` 使用的消息段。

MaiBot 出站消息段格式（见 host message_utils）::

    {"type": "text", "content": "..."}
    {"type": "image", "content": <base64>}
    {"type": "emoji", "content": <base64>}
    {"type": "voice", "content": <base64>}
    {"type": "dict", "data": {"type": "video", "data": {"file": "base64://..."}}}
    {"type": "at", "data": {"target_user_id": "123", "target_user_nickname": "昵称"}}
    {"type": "reply", "data": {"target_message_id": "..."}}
"""

from __future__ import annotations

import base64
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib import request as urllib_request

from .store import KeywordsStore
from .templates import render_template_text, strip_auto_media_text

logger = logging.getLogger("plugin.maibot_plugin.keywords_reply")

_MENTION_PATTERN = re.compile(r"\[@\s*(\d+)\]")
_ONEBOT_MUSIC_PLATFORMS = frozenset({"163", "qq", "migu", "kugou", "kuwo"})
_NETEASE_URL_ID_PATTERN = re.compile(
    r"(?:https?://)?(?:y\.)?music\.163\.com[^\s]*[?&#]id=(\d+)",
    re.IGNORECASE,
)
_MUSIC_SHARE_TEXT_PATTERN = re.compile(
    r"^\s*\[(?:网易云音乐|QQ音乐)\]\s*.+?(?:\s*-\s*.+)?\s*$",
    re.MULTILINE,
)
_MUSIC_TEXT_PLACEHOLDER_PATTERN = re.compile(
    r"\[(?:网易云音乐|QQ音乐|音乐)[^\]]*\]",
    re.IGNORECASE,
)


_MGMT_COMMAND_PATTERN = re.compile(
    r"/(?:添加|编辑|删除|启用|禁用|查看)(?:关键词|检测词)(?:回复|别名)?(?:\s|$)"
)
_ADD_COMMAND_HEAD = re.compile(
    r"^/(?:添加)(?:关键词|检测词)\s+(?:-r\s+)?",
    re.IGNORECASE,
)
_ADD_REPLY_COMMAND_PREFIX = re.compile(
    r"^/(?:添加)(?:关键词|检测词)回复\s+\S+(?:\s+(?P<body>[\s\S]+))?$",
    re.IGNORECASE,
)
_EDIT_REPLY_COMMAND_PREFIX = re.compile(
    r"^/(?:编辑)(?:关键词|检测词)回复\s+\S+\s+\d+(?:\s+(?P<body>[\s\S]+))?$",
    re.IGNORECASE,
)
_PROCESSED_REPLY_QUOTE_PREFIX = re.compile(
    r"^\[(?:"
    r"回复了.+?的消息:\s*[^\]]*|"
    r"回复了一条消息，但原消息已无法访问|"
    r"引用[：:][^\]]*|"
    r"引用消息"
    r")\]\s*",
    re.DOTALL,
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
    """读取消息段负载，兼容 ``data`` 与 ``content`` 两种字段名。"""

    seg_type = str(seg.get("type", "")).strip().lower()
    if seg_type in {"image", "emoji", "voice"} and seg.get("content") is not None:
        return seg.get("content")
    if seg.get("data") is not None:
        return seg.get("data")
    return seg.get("content")


def _looks_like_base64(value: str) -> bool:
    """判断字符串是否像可用的 base64 媒体数据（排除 VLM/ASR 占位文本）。"""

    normalized = str(value or "").strip()
    if len(normalized) < 16 or len(normalized) % 4 != 0:
        return False
    if normalized.startswith(("[", "http://", "https://", "data:")):
        return False
    try:
        base64.b64decode(normalized, validate=True)
        return True
    except Exception:
        return False


def _extract_nested_media_segment(seg: dict) -> Optional[tuple[str, dict]]:
    """从 ``dict`` 包装段中解析嵌套的图片/表情/语音段（不含视频：视频仅本地加载）。"""

    if str(seg.get("type", "")).strip().lower() != "dict":
        return None
    data = seg.get("data")
    if not isinstance(data, dict):
        return None
    inner_type = str(data.get("type", "")).strip().lower()
    if inner_type in {"image", "emoji", "voice", "record"}:
        return inner_type, data
    return None


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


def _download_url_base64(url: str, *, timeout: int = 15) -> str:
    """尽力从 HTTP(S) URL 下载资源并转为 base64。"""

    normalized = str(url or "").strip()
    if not normalized.startswith(("http://", "https://")):
        return ""
    try:
        req = urllib_request.Request(
            normalized,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "*/*",
            },
        )
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            return base64.b64encode(resp.read()).decode("utf-8")
    except Exception as exc:
        logger.warning(f"下载媒体失败: {normalized[:120]} error={exc}")
        return ""


def _candidate_media_urls(seg: dict) -> List[str]:
    """从消息段中收集可用于下载的 URL / base64 引用。"""

    candidates: List[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        text_value = str(value or "").strip()
        if not text_value or text_value in seen:
            return
        seen.add(text_value)
        candidates.append(text_value)

    add(seg.get("url"))
    data = _segment_data(seg)
    if isinstance(data, str):
        add(data)
    elif isinstance(data, dict):
        add(data.get("url"))
        add(data.get("file"))
        add(data.get("path"))
        add(data.get("base64") or data.get("base64_data"))
    return candidates


def _resolve_segment_binary_base64(seg: dict, *, timeout: int = 15) -> str:
    """从消息段解析可用于落盘的 base64 数据。"""

    direct = str(seg.get("binary_data_base64", "") or "").strip()
    if direct:
        return direct

    for candidate in _candidate_media_urls(seg):
        if candidate.startswith(("http://", "https://")):
            downloaded = _download_url_base64(candidate, timeout=timeout)
            if downloaded:
                return downloaded
            continue
        if candidate.startswith("base64://"):
            return candidate[len("base64://") :]
        if candidate.startswith("data:") and ";base64," in candidate:
            return candidate.split(";base64,", 1)[1]
        if _looks_like_base64(candidate):
            return candidate
    return ""


def _normalize_music_card(payload: Any) -> Optional[dict]:
    """把 OneBot 音乐段负载规范化为 ``{platform, id, title, artist}``。"""

    if not isinstance(payload, dict):
        return None

    raw_type = str(payload.get("type") or payload.get("platform") or "").strip().lower()
    if raw_type == "music":
        inner = payload.get("data", payload)
        return _normalize_music_card(inner)

    song_id = str(payload.get("id") or payload.get("song_id") or payload.get("mid") or "").strip()
    if raw_type in _ONEBOT_MUSIC_PLATFORMS and song_id:
        return {
            "platform": raw_type,
            "id": song_id,
            "title": str(payload.get("title") or payload.get("name") or "").strip(),
            "artist": str(
                payload.get("author") or payload.get("artist") or payload.get("singer") or ""
            ).strip(),
        }
    return None


def _extract_music_from_text(text: str) -> Optional[dict]:
    """从文本或链接中尽力解析网易云音乐卡片信息。"""

    normalized = str(text or "").strip()
    if not normalized:
        return None

    url_match = _NETEASE_URL_ID_PATTERN.search(normalized)
    if url_match:
        return {"platform": "163", "id": url_match.group(1), "title": "", "artist": ""}
    return None


def _extract_music_from_segment(seg: dict) -> Optional[dict]:
    """从单个消息段提取 OneBot 音乐卡片。"""

    if not isinstance(seg, dict):
        return None

    seg_type = str(seg.get("type", "")).strip().lower()
    if seg_type == "music":
        return _normalize_music_card(_segment_data(seg))

    if seg_type == "dict":
        data = seg.get("data")
        if isinstance(data, dict):
            if str(data.get("type", "")).strip().lower() == "music":
                return _normalize_music_card(data.get("data", data))
            return _normalize_music_card(data)

    if seg_type == "text":
        return _extract_music_from_text(str(_segment_data(seg) or ""))

    return None


def _append_music_card(entry: dict, card: Optional[dict]) -> None:
    """向 entry 追加去重后的音乐卡片。"""

    if not card or not isinstance(entry, dict):
        return
    platform = str(card.get("platform") or "").strip()
    song_id = str(card.get("id") or "").strip()
    if not platform or not song_id:
        return

    cards = entry.setdefault("music_cards", [])
    for existing in cards:
        if (
            str(existing.get("platform") or "").strip() == platform
            and str(existing.get("id") or "").strip() == song_id
        ):
            return
    cards.append(
        {
            "platform": platform,
            "id": song_id,
            "title": str(card.get("title") or "").strip(),
            "artist": str(card.get("artist") or "").strip(),
        }
    )


_MUSIC_PLATFORM_ALIASES = {
    "netease": "163",
    "网易云": "163",
    "网易云音乐": "163",
    "qq音乐": "qq",
    "咪咕": "migu",
    "酷狗": "kugou",
    "酷我": "kuwo",
}


def normalize_music_platform(platform: str, *, default: str = "163") -> Optional[str]:
    """规范化 OneBot 音乐平台标识，未知值返回 ``None``。"""

    normalized = str(platform or "").strip().lower()
    if not normalized:
        return default if default in _ONEBOT_MUSIC_PLATFORMS else None
    if normalized in _ONEBOT_MUSIC_PLATFORMS:
        return normalized
    return _MUSIC_PLATFORM_ALIASES.get(normalized)


def build_music_card_entry(platform: str, song_id: str, store: KeywordsStore) -> dict:
    """根据平台与歌曲 ID 构建仅含音乐卡片的 entry。"""

    entry = store.empty_entry()
    _append_music_card(entry, {"platform": platform, "id": song_id, "title": "", "artist": ""})
    return entry


def supported_music_platforms_text() -> str:
    return "163, qq, migu, kugou, kuwo（默认 163）"


def _build_music_send_segment(card: dict) -> Optional[dict]:
    """构建 MaiBot/OneBot 可识别的音乐卡片发送段。"""

    platform = str(card.get("platform") or "").strip()
    song_id = str(card.get("id") or "").strip()
    if not platform or not song_id:
        return None
    return {
        "type": "dict",
        "data": {
            "type": "music",
            "data": {
                "type": platform,
                "id": song_id,
            },
        },
    }


def _append_media_segment(
    entry: dict,
    store: KeywordsStore,
    seg_type: str,
    seg: dict,
    *,
    to_cache: bool = False,
) -> None:
    """把单个媒体段写入 entry 对应列表。

    不处理视频：视频仅支持本地 ``videos/`` 文件，不从消息入站/引用拉取。
    """

    normalized_type = "voice" if seg_type == "record" else seg_type
    if normalized_type not in {"image", "emoji", "voice"}:
        return
    b64 = _resolve_segment_binary_base64(seg, timeout=15)
    media_key = {
        "image": "images",
        "emoji": "emojis",
        "voice": "records",
    }[normalized_type]
    if not b64:
        return
    filename = store.save_media_base64(media_key, b64, to_cache=to_cache)
    if filename:
        entry[media_key].append({"file": filename})


def _build_video_send_segment(b64: str) -> Optional[dict]:
    """构造 NapCat/OneBot 可识别的视频发送段（经 ``dict`` 包装；数据来自本地 ``videos/``）。"""

    normalized = str(b64 or "").strip()
    if not normalized:
        return None
    file_ref = normalized if normalized.startswith("base64://") else f"base64://{normalized}"
    return {"type": "dict", "data": {"type": "video", "data": {"file": file_ref}}}


def _strip_music_share_text(text: str) -> str:
    """当已保存音乐卡片时，移除 ``[网易云音乐] 歌名 - 歌手`` 类分享文本。"""

    cleaned = str(text or "").strip()
    if not cleaned:
        return ""
    cleaned = _MUSIC_TEXT_PLACEHOLDER_PATTERN.sub("", cleaned).strip()
    if _MUSIC_SHARE_TEXT_PATTERN.fullmatch(cleaned):
        return ""
    return re.sub(r"\s+", " ", cleaned).strip()


def _raw_segment_to_part_segment(
    seg: dict,
    store: KeywordsStore,
    *,
    include_text: bool = True,
    to_cache: bool = False,
) -> Optional[dict]:
    """把单条 raw_message 段转换为 ``parts.segments`` 内可存储的 segment dict。"""

    if not isinstance(seg, dict):
        return None

    seg_type = str(seg.get("type", "")).strip().lower()
    # 视频不从消息拉取，仅本地 videos/ 配置后发送
    if seg_type in {"video", "file"}:
        return None
    music_card = _extract_music_from_segment(seg)

    if seg_type == "text":
        if music_card:
            song_id = str(music_card.get("id") or "").strip()
            if song_id:
                return {
                    "type": "music",
                    "platform": str(music_card.get("platform") or "163"),
                    "id": song_id,
                }
            return None
        if not include_text:
            return None
        content = str(_segment_data(seg) or "")
        if not content:
            return None
        plain, inline_ats = extract_inline_ats(content)
        plain = strip_auto_media_text(plain)
        if not plain and not inline_ats:
            return None
        # At 段单独成 segment；文本段仅保留正文
        if plain:
            return {"type": "text", "text": plain}
        return None

    if seg_type == "at":
        data = _segment_data(seg) or {}
        if isinstance(data, dict):
            uid = str(data.get("target_user_id", "") or "").strip()
            nickname = str(data.get("target_user_nickname", "") or "")
            if uid:
                return {
                    "type": "at",
                    "user_id": uid,
                    "nickname": nickname,
                    "all": uid.lower() == "all",
                }
        return None

    if seg_type in ("image", "emoji", "voice", "record"):
        media_entry = store.empty_entry()
        _append_media_segment(media_entry, store, seg_type, seg, to_cache=to_cache)
        media_key = {
            "image": "images",
            "emoji": "emojis",
            "voice": "records",
        }["voice" if seg_type in {"voice", "record"} else seg_type]
        files = media_entry.get(media_key) or []
        if not files:
            return None
        part_type = "voice" if seg_type in {"voice", "record"} else seg_type
        return {"type": part_type, "file": str(files[-1].get("file") or "")}

    if seg_type == "music":
        if not music_card:
            return None
        song_id = str(music_card.get("id") or "").strip()
        if not song_id:
            return None
        return {
            "type": "music",
            "platform": str(music_card.get("platform") or "163"),
            "id": song_id,
        }

    face_id = _extract_face_id_from_segment(seg)
    if face_id is not None:
        return {"type": "face", "id": face_id}

    if seg_type == "dict":
        nested = _extract_nested_media_segment(seg)
        if nested:
            inner_type, inner_seg = nested
            return _raw_segment_to_part_segment(
                {"type": inner_type, "data": _segment_data(inner_seg)},
                store,
                include_text=include_text,
                to_cache=to_cache,
            )
        if music_card:
            song_id = str(music_card.get("id") or "").strip()
            if song_id:
                return {
                    "type": "music",
                    "platform": str(music_card.get("platform") or "163"),
                    "id": song_id,
                }
    return None


def build_entry_from_segments(
    segments: List[dict],
    store: KeywordsStore,
    include_text: bool = True,
    *,
    to_cache: bool = False,
) -> dict:
    """把 raw_message 段解析为 entry，保留同一条消息内的段顺序。"""

    entry = store.empty_entry()
    ordered_segments: List[dict] = []
    text_parts: List[str] = []

    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        seg_type = str(seg.get("type", "")).strip().lower()
        if seg_type in {"video", "file"}:
            continue
        part_segment = _raw_segment_to_part_segment(
            seg,
            store,
            include_text=include_text,
            to_cache=to_cache,
        )
        if part_segment:
            ordered_segments.append(part_segment)
            if part_segment.get("type") == "text":
                text_parts.append(str(part_segment.get("text") or ""))
            part_type = str(part_segment.get("type") or "").strip().lower()
            if part_type in {"image", "emoji", "voice"}:
                media_key = {
                    "image": "images",
                    "emoji": "emojis",
                    "voice": "records",
                }[part_type]
                file_name = str(part_segment.get("file") or "").strip()
                if file_name:
                    entry[media_key].append({"file": file_name})
                continue

        # 兼容旧字段与列表摘要：继续填充扁平媒体/At 列表
        music_card = _extract_music_from_segment(seg)
        if seg_type == "text" and include_text and not music_card:
            content = str(_segment_data(seg) or "")
            if content:
                plain, inline_ats = extract_inline_ats(content)
                entry["ats"].extend(inline_ats)
        elif seg_type == "at":
            data = _segment_data(seg) or {}
            if isinstance(data, dict):
                uid = str(data.get("target_user_id", "") or "").strip()
                nickname = str(data.get("target_user_nickname", "") or "")
                if uid:
                    entry["ats"].append({"user_id": uid, "nickname": nickname, "all": uid.lower() == "all"})
        elif seg_type in ("image", "emoji", "voice", "record"):
            _append_media_segment(entry, store, seg_type, seg, to_cache=to_cache)
        elif seg_type == "music":
            _append_music_card(entry, music_card)
        else:
            face_id = _extract_face_id_from_segment(seg)
            if face_id is not None:
                entry["faces"].append({"id": face_id})
            elif seg_type == "dict":
                nested = _extract_nested_media_segment(seg)
                if nested:
                    inner_type, inner_seg = nested
                    _append_media_segment(entry, store, inner_type, inner_seg, to_cache=to_cache)
                elif music_card:
                    _append_music_card(entry, music_card)

    if ordered_segments:
        entry["parts"] = [KeywordsStore.make_message_part(ordered_segments)]

    if include_text:
        entry["text"] = strip_auto_media_text("".join(text_parts).strip())
        if entry.get("music_cards"):
            entry["text"] = _strip_music_share_text(entry["text"])
    else:
        entry["text"] = ""
    return entry


def dedupe_entry_ats(ats: List[dict]) -> List[dict]:
    """按 user_id 去重 At，保留首次出现并尽量补齐 nickname。"""

    merged: Dict[str, dict] = {}
    order: List[str] = []
    for at in ats or []:
        if not isinstance(at, dict):
            continue
        if at.get("all"):
            key = "__all__"
        else:
            key = str(at.get("user_id", "") or "").strip()
        if not key:
            continue
        if key not in merged:
            merged[key] = dict(at)
            order.append(key)
            continue
        existing = merged[key]
        if not str(existing.get("nickname", "") or "").strip() and str(at.get("nickname", "") or "").strip():
            existing["nickname"] = at.get("nickname")
    return [merged[key] for key in order]


def strip_text_at_mentions(text: str, ats: List[dict]) -> str:
    """当已保存真实 At 时，移除文本里重复的 ``@昵称`` 字面量。"""

    cleaned = str(text or "")
    if not cleaned or not ats:
        return cleaned.strip()

    for at in ats:
        nickname = str(at.get("nickname", "") or "").strip()
        if nickname:
            cleaned = re.sub(rf"@{re.escape(nickname)}(?=\s|$|[，。！？,.!?])", "", cleaned)
            cleaned = cleaned.replace(f"@{nickname}", "")
    return re.sub(r"\s+", " ", cleaned).strip()


def _normalize_face_id(value: Any) -> Optional[int]:
    """把 QQ face id 规范化为非负整数；无效时返回 ``None``。"""

    try:
        face_id = int(value)
    except (TypeError, ValueError):
        return None
    if face_id < 0 or face_id > 99999:
        return None
    return face_id


def _is_bare_qq_face_dict(data: dict) -> bool:
    """判断 dict 段是否为 MaiBot 归一化后的 QQ face（仅剩 ``id`` 等少量字段）。"""

    if not isinstance(data, dict) or "id" not in data:
        return False
    allowed_keys = {"id", "type", "raw", "summary"}
    if not set(data.keys()).issubset(allowed_keys):
        return False
    inner_type = str(data.get("type", "")).strip().lower()
    if inner_type and inner_type not in {"face", "dict"}:
        return False
    return _normalize_face_id(data.get("id")) is not None


def _extract_face_id_from_segment(seg: dict) -> Optional[int]:
    """从 raw_message 段提取 QQ face id，兼容 OneBot 与 MaiBot ``DictComponent`` 格式。"""

    if not isinstance(seg, dict):
        return None

    seg_type = str(seg.get("type", "")).strip().lower()
    if seg_type == "face":
        data = _segment_data(seg)
        if isinstance(data, dict):
            normalized = _normalize_face_id(data.get("id"))
            if normalized is not None:
                return normalized
        if data is not None and not isinstance(data, dict):
            normalized = _normalize_face_id(data)
            if normalized is not None:
                return normalized
        return _normalize_face_id(seg.get("id"))

    if seg_type != "dict":
        return None

    data = seg.get("data")
    if not isinstance(data, dict):
        return None

    inner_type = str(data.get("type", "")).strip().lower()
    if inner_type == "face":
        payload = data.get("data", data)
        if isinstance(payload, dict):
            return _normalize_face_id(payload.get("id"))
        return _normalize_face_id(payload)

    if _is_bare_qq_face_dict(data):
        return _normalize_face_id(data.get("id"))
    return None


def sanitize_entry_faces(entry: dict) -> None:
    """就地清理 entry 中的 QQ face 字段，移除无效 id。"""

    if not isinstance(entry, dict):
        return

    cleaned_faces: List[dict] = []
    for face in entry.get("faces") or []:
        if not isinstance(face, dict):
            continue
        face_id = _normalize_face_id(face.get("id"))
        if face_id is not None:
            cleaned_faces.append({"id": face_id})
    entry["faces"] = cleaned_faces

    parts = entry.get("parts")
    if not isinstance(parts, list):
        return
    for part in parts:
        if not isinstance(part, dict):
            continue
        for seg in KeywordsStore.get_part_segments(part):
            if not isinstance(seg, dict) or str(seg.get("type") or "").strip().lower() != "face":
                continue
            face_id = _normalize_face_id(seg.get("id"))
            if face_id is None:
                seg.pop("id", None)
            else:
                seg["id"] = face_id


def _build_face_send_segment(face_id: int) -> dict:
    """构造 MaiBot 可透传的 QQ face 段（经 ``dict`` 包装保留 ``type``）。"""

    return {"type": "dict", "data": {"type": "face", "data": {"id": face_id}}}


def sanitize_entry_text(entry: dict, store: KeywordsStore) -> dict:
    """整理 entry：去重 At、剥离重复 @ 文本、移除 VLM 媒体占位描述、规范化 face。"""

    if not isinstance(entry, dict):
        return entry

    sanitize_entry_faces(entry)
    entry["ats"] = dedupe_entry_ats(entry.get("ats") or [])
    entry["text"] = strip_text_at_mentions(str(entry.get("text", "") or ""), entry["ats"])
    entry["text"] = strip_auto_media_text(str(entry.get("text", "") or ""))

    for part in entry.get("parts") or []:
        if not isinstance(part, dict):
            continue
        for seg in KeywordsStore.get_part_segments(part):
            if not isinstance(seg, dict) or str(seg.get("type") or "").strip().lower() != "text":
                continue
            seg["text"] = strip_auto_media_text(str(seg.get("text") or ""))

    has_media = bool(
        entry.get("images")
        or entry.get("records")
        or entry.get("emojis")
        or entry.get("videos")
        or entry.get("music_cards")
        or entry.get("faces")
        or any(
            KeywordsStore._segment_has_payload(seg)
            for part in entry.get("parts") or []
            if isinstance(part, dict)
            for seg in KeywordsStore.get_part_segments(part)
        )
    )
    if has_media and entry.get("music_cards"):
        entry["text"] = _strip_music_share_text(entry["text"])
        for part in entry.get("parts") or []:
            if isinstance(part, dict) and str(part.get("type") or "").strip().lower() == "text":
                part["text"] = _strip_music_share_text(str(part.get("text") or ""))
    _purge_management_command_text(entry)
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


def strip_processed_reply_quote_prefix(text: str) -> str:
    """剥离 MaiBot 在 ``processed_plain_text`` 中注入的引用说明前缀。

    覆盖常见形态：``[回复了…的消息: …]``、``[引用：…]``、``[引用消息]`` 等。
    """

    normalized = str(text or "").strip()
    if not normalized:
        return ""
    return _PROCESSED_REPLY_QUOTE_PREFIX.sub("", normalized, count=1).strip()


def _strip_processed_reply_quote_prefix(text: str) -> str:
    """兼容旧名。"""

    return strip_processed_reply_quote_prefix(text)


def _is_management_command_text(text: str) -> bool:
    """判断文本是否为本插件管理命令（含引用说明前缀后的命令行）。"""

    normalized = _strip_processed_reply_quote_prefix(str(text or "")).strip()
    if not normalized:
        return False
    return bool(re.match(r"^/(?:添加|编辑|删除|启用|禁用|查看|设置|重载)", normalized, re.IGNORECASE))


def _strip_reply_command_prefix(
    text: str,
    *,
    command_mode: str = "add",
    keyword: str = "",
) -> str:
    """从首段文本中剥离管理命令前缀，保留回复正文。"""

    from .matching import parse_trigger_field_and_body

    del keyword  # 触发词字段可能含空格/别名，统一走解析器

    normalized = _strip_processed_reply_quote_prefix(text)
    if not normalized:
        return ""

    if command_mode == "add":
        head = _ADD_COMMAND_HEAD.match(normalized)
        if head:
            _triggers, body = parse_trigger_field_and_body(normalized[head.end() :])
            return str(body or "").strip()

    prefix_patterns = {
        "add_reply": (_ADD_REPLY_COMMAND_PREFIX,),
        "edit_reply": (_EDIT_REPLY_COMMAND_PREFIX,),
    }
    for pattern in prefix_patterns.get(command_mode, ()):
        match = pattern.match(normalized)
        if match:
            return str(match.group("body") or "").strip()
    return normalized


def _purge_management_command_text(entry: dict) -> None:
    """移除误写入 entry 的管理命令文本（引用发命令时常见）。"""

    if not isinstance(entry, dict):
        return

    text = str(entry.get("text") or "").strip()
    if text and _is_management_command_text(text):
        entry["text"] = ""

    parts = entry.get("parts")
    if not isinstance(parts, list):
        return

    cleaned_parts: List[dict] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        kept_segments: List[dict] = []
        for seg in KeywordsStore.get_part_segments(part):
            if not isinstance(seg, dict):
                continue
            if str(seg.get("type") or "").strip().lower() == "text":
                seg_text = str(seg.get("text") or "").strip()
                if not seg_text or _is_management_command_text(seg_text):
                    continue
            kept_segments.append(seg)
        if kept_segments:
            cleaned_parts.append(KeywordsStore.make_message_part(kept_segments))
    entry["parts"] = cleaned_parts

def extract_reply_raw_segments_from_message(
    message: Dict[str, Any],
    *,
    command_mode: str = "",
    keyword: str = "",
) -> List[dict]:
    """从整条命令消息的 ``raw_message`` 提取回复段，保留文本与 QQ 表情的交错顺序。"""

    if not isinstance(message, dict):
        return []
    segments = message.get("raw_message", []) or []
    if not isinstance(segments, list) or not segments:
        return []

    reply_segments: List[dict] = []
    prefix_stripped = False

    for seg in segments:
        if not isinstance(seg, dict):
            continue
        seg_type = str(seg.get("type", "")).strip().lower()
        if seg_type == "reply":
            continue

        if seg_type == "text" and command_mode and not prefix_stripped:
            content = str(_segment_data(seg) or "")
            remainder = _strip_reply_command_prefix(
                content,
                command_mode=command_mode,
                keyword=keyword,
            )
            prefix_stripped = True
            if remainder and not _is_management_command_text(remainder):
                reply_segments.append({"type": "text", "data": remainder})
            continue

        reply_segments.append(seg)

    return reply_segments


def build_reply_entry_from_command_message(
    message: Dict[str, Any],
    store: KeywordsStore,
    *,
    command_mode: str = "",
    keyword: str = "",
    reply_text_fallback: str = "",
) -> dict:
    """优先从 ``raw_message`` 按序构建回复 entry；失败时回退到命令解析出的纯文本。"""

    reply_segments = extract_reply_raw_segments_from_message(
        message,
        command_mode=command_mode,
        keyword=keyword,
    )
    if reply_segments:
        entry = build_entry_from_segments(reply_segments, store, include_text=True)
        if store.entry_has_payload(entry):
            return entry

    entry = store.empty_entry()
    plain, inline_ats = extract_inline_ats(strip_auto_media_text(reply_text_fallback or ""))
    if _is_management_command_text(plain):
        plain = ""
    entry["text"] = plain.strip()
    entry["ats"].extend(inline_ats)
    return entry


def capture_media_from_message_dict(message: Dict[str, Any], store: KeywordsStore) -> dict:
    """从序列化消息字典捕获富媒体（用于 ``before_process`` 阶段，二进制尚未被主程序丢弃）。

    写入 ``media_cache/``，避免与词库永久媒体混用。
    """

    if not isinstance(message, dict):
        return store.empty_entry()
    return build_entry_from_segments(
        message.get("raw_message", []) or [],
        store,
        include_text=False,
        to_cache=True,
    )


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


async def capture_from_reply(
    ctx: Any,
    store: KeywordsStore,
    message: Dict[str, Any],
    *,
    media_cache: Optional[Dict[str, dict]] = None,
) -> dict:
    """从被引用消息中捕获完整内容（文本 + 媒体）。"""

    empty = store.empty_entry()
    target_id = _extract_reply_target_id(message)
    if not target_id:
        return empty

    cached_entry = empty
    if isinstance(media_cache, dict):
        cached = media_cache.get(target_id)
        if isinstance(cached, dict) and store.entry_has_payload(cached):
            cached_entry = cached

    stream_id = str(message.get("session_id", "") or "").strip() if isinstance(message, dict) else ""
    segments = await _fetch_message_segments(ctx, target_id, stream_id)
    fetched_entry = build_entry_from_segments(segments, store, include_text=True) if segments else empty
    merged = store.merge_entries(cached_entry, fetched_entry)
    return sanitize_entry_text(merged, store)


# ─── 发送段构建 ────────────────────────────────────────────────


def _append_reply_segment(segments: List[dict], message: Dict[str, Any]) -> None:
    if not isinstance(message, dict):
        return
    target_id = str(message.get("message_id", "") or "").strip()
    if target_id:
        segments.append({"type": "reply", "data": {"target_message_id": target_id}})


def _part_to_segments(
    part: dict,
    store: KeywordsStore,
    message: Dict[str, Any],
    *,
    enable_template: bool = True,
    render: bool = True,
) -> List[dict]:
    """把单个有序 part 转换为消息段列表。"""

    if not isinstance(part, dict):
        return []

    part_type = str(part.get("type") or "").strip().lower()
    segments: List[dict] = []

    if part_type == "text":
        text = str(part.get("text") or "")
        if text.strip():
            if render:
                text = render_template_text(text, message, enabled=enable_template)
            segments.append({"type": "text", "content": text})
        return segments

    if part_type == "at":
        uid = "all" if part.get("all") else str(part.get("user_id", "") or "")
        if uid:
            segments.append(
                {
                    "type": "at",
                    "data": {
                        "target_user_id": uid,
                        "target_user_nickname": str(part.get("nickname", "") or ""),
                    },
                }
            )
        return segments

    if part_type == "face":
        face_id = _normalize_face_id(part.get("id"))
        if face_id is not None:
            segments.append(_build_face_send_segment(face_id))
        return segments

    if part_type == "music":
        music_segment = _build_music_send_segment(
            {
                "platform": part.get("platform"),
                "id": part.get("id"),
                "title": part.get("title", ""),
                "artist": part.get("artist", ""),
            }
        )
        if music_segment:
            segments.append(music_segment)
        return segments

    media_key = {
        "image": "images",
        "emoji": "emojis",
        "voice": "records",
        "record": "records",
        "video": "videos",
    }.get(part_type)
    if media_key:
        b64 = store.read_media_base64(media_key, str(part.get("file") or ""))
        if b64:
            if part_type == "video":
                video_segment = _build_video_send_segment(b64)
                if video_segment:
                    segments.append(video_segment)
            else:
                segment_type = "voice" if part_type in {"voice", "record"} else part_type
                segments.append({"type": segment_type, "content": b64})
    return segments


def build_send_segments(
    entry: dict,
    store: KeywordsStore,
    message: Dict[str, Any],
    *,
    quote: bool = False,
    enable_template: bool = True,
    render: bool = True,
) -> List[dict]:
    """把一条 entry 转换为 ctx.send.hybrid 使用的消息段列表（单条消息）。"""

    segments: List[dict] = []
    if quote:
        _append_reply_segment(segments, message)

    parts = store.get_ordered_parts(entry)
    if parts:
        for message_part in parts:
            for part_segment in KeywordsStore.get_part_segments(message_part):
                segments.extend(
                    _part_to_segments(
                        part_segment,
                        store,
                        message,
                        enable_template=enable_template,
                        render=render,
                    )
                )
        return segments

    for at in entry.get("ats", []):
        uid = "all" if at.get("all") else str(at.get("user_id", "") or "")
        if uid:
            segments.append(
                {
                    "type": "at",
                    "data": {"target_user_id": uid, "target_user_nickname": str(at.get("nickname", "") or "")},
                }
            )

    text = entry.get("text") or ""
    if text:
        if render:
            text = render_template_text(text, message, enabled=enable_template)
        segments.append({"type": "text", "content": text})

    for face in entry.get("faces", []):
        face_id = _normalize_face_id((face or {}).get("id"))
        if face_id is not None:
            segments.append(_build_face_send_segment(face_id))

    for music in entry.get("music_cards", []):
        music_segment = _build_music_send_segment(music or {})
        if music_segment:
            segments.append(music_segment)

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

    for video in entry.get("videos", []):
        b64 = store.read_media_base64("videos", (video or {}).get("file", ""))
        video_segment = _build_video_send_segment(b64 or "")
        if video_segment:
            segments.append(video_segment)

    return segments


def build_ordered_send_batches(
    entry: dict,
    store: KeywordsStore,
    message: Dict[str, Any],
    *,
    quote: bool = False,
    enable_template: bool = True,
    render: bool = True,
) -> List[List[dict]]:
    """把 entry 拆成按顺序逐条发送的 hybrid 批次（每批对应一条聊天消息）。"""

    if not store.uses_ordered_parts(entry):
        segments = build_send_segments(
            entry,
            store,
            message,
            quote=quote,
            enable_template=enable_template,
            render=render,
        )
        return [segments] if segments else []

    batches: List[List[dict]] = []
    for index, message_part in enumerate(entry.get("parts", [])):
        if not isinstance(message_part, dict) or not KeywordsStore._part_has_payload(message_part):
            continue
        segments: List[dict] = []
        if quote and index == 0:
            _append_reply_segment(segments, message)
        for part_segment in KeywordsStore.get_part_segments(message_part):
            segments.extend(
                _part_to_segments(
                    part_segment,
                    store,
                    message,
                    enable_template=enable_template,
                    render=render,
                )
            )
        if segments:
            batches.append(segments)
    return batches


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
