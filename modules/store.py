"""关键词回复数据存储层。

负责在 ``ctx.paths.data_dir`` 下维护可外部编辑的 ``keywords.json``，
以及图片/语音/表情等媒体文件（以 base64 解码后落盘，条目仅保存文件名）。

数据结构::

    {
      "command_triggered": [ rule, ... ],
      "auto_detect": [ rule, ... ]
    }

其中每条 rule::

    {
      "keyword": "关键词",
      "aliases": ["别名1", "别名2"],     # 可选；与 keyword 等价触发
      "regex": false,
      "enabled": true,
      "mode": "whitelist" | "blacklist",
      "groups": ["123456"],
      "case_sensitive": false,           # 仅 auto_detect 会显式写入
      "require_at_bot": false,           # 为 true 时须 @ 机器人或命中 is_mentioned 才触发
      "entries": [ entry, ... ]
    }

每条 entry::

    {
      "text": "",
      "weight": 100,                     # 多回复时按权重随机抽取
      "probability": 100,                # 抽中后实际回复的概率（0-100）
      "images": [{"file": "abc.jpg"}],   # data_dir/images 下的文件名
      "records": [{"file": "abc.amr"}],  # 语音，data_dir/records
      "ats": [{"user_id": "123", "nickname": "", "all": false}],
      "faces": [{"id": 1}],
      "emojis": [{"file": "abc.gif"}],   # 表情包（sticker）
      "videos": [{"file": "abc.mp4"}],   # 视频，data_dir/videos
      "music_cards": [{"platform": "163", "id": "28481103", "title": "", "artist": ""}]
    }
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("plugin.maibot_plugin.keywords_reply")

SECTIONS = ("command_triggered", "auto_detect")

# entry 中媒体键 -> 存储子目录（词库永久媒体，永不自动清理）
_MEDIA_DIRS = {
    "images": "images",
    "records": "records",
    "emojis": "emojis",
    "videos": "videos",
}

# before_process 入站缓存子目录（与永久媒体隔离；不含 videos：视频仅本地加载）
_MEDIA_CACHE_DIRS = {
    "images": "images",
    "records": "records",
    "emojis": "emojis",
}
_MEDIA_CACHE_ROOT = "media_cache"
# media_cache 滚动上限：总文件数超过该值时按 mtime 删最旧的
_MEDIA_CACHE_MAX_FILES = 100

# 媒体类型默认后缀
_MEDIA_SUFFIX = {
    "images": ".jpg",
    "records": ".amr",
    "emojis": ".gif",
    "videos": ".mp4",
}


class KeywordsStore:
    """关键词/检测词数据与媒体文件的持久化管理。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_file = self.data_dir / "keywords.json"
        self.media_dirs: Dict[str, Path] = {
            key: self.data_dir / sub for key, sub in _MEDIA_DIRS.items()
        }
        self.cache_root = self.data_dir / _MEDIA_CACHE_ROOT
        self.cache_dirs: Dict[str, Path] = {
            key: self.cache_root / sub for key, sub in _MEDIA_CACHE_DIRS.items()
        }
        self._save_lock = asyncio.Lock()
        self.data: Dict[str, List[dict]] = {"command_triggered": [], "auto_detect": []}
        self.data_version = 0

    def setup(self) -> None:
        """创建目录并加载数据。"""

        self.data_dir.mkdir(parents=True, exist_ok=True)
        for path in self.media_dirs.values():
            path.mkdir(parents=True, exist_ok=True)
        for path in self.cache_dirs.values():
            path.mkdir(parents=True, exist_ok=True)
        self.data = self.normalize(self._load())

    # ─── 加载 / 保存 ────────────────────────────────────────────

    def _load(self) -> dict:
        if self.data_file.exists():
            try:
                with self.data_file.open("r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as exc:
                logger.error(f"加载关键词数据失败: {exc}")
        return {"command_triggered": [], "auto_detect": []}

    @staticmethod
    def normalize(data: Any) -> Dict[str, List[dict]]:
        """补全缺省字段，保证数据结构完整。"""

        from .matching import normalize_aliases

        if not isinstance(data, dict):
            return {"command_triggered": [], "auto_detect": []}
        data.setdefault("command_triggered", [])
        data.setdefault("auto_detect", [])
        for section in SECTIONS:
            rules = data.get(section)
            if not isinstance(rules, list):
                data[section] = []
                continue

            for cfg in rules:
                if not isinstance(cfg, dict):
                    continue
                cfg.setdefault("keyword", "")
                primary = str(cfg.get("keyword", "") or "").strip()
                cfg["keyword"] = primary
                aliases = normalize_aliases(cfg.get("aliases"))
                # 别名不得与主词重复
                primary_key = primary.casefold()
                cfg["aliases"] = [a for a in aliases if a.casefold() != primary_key]
                cfg.setdefault("regex", False)
                cfg.setdefault("enabled", True)
                cfg.setdefault("mode", "whitelist")
                cfg.setdefault("groups", [])
                cfg.setdefault("entries", [])
                cfg.setdefault("require_at_bot", False)
                if section == "auto_detect":
                    cfg.setdefault("case_sensitive", False)
                for entry in cfg.get("entries", []):
                    if not isinstance(entry, dict):
                        continue
                    has_probability = "probability" in entry
                    entry.setdefault("weight", 100)
                    if not has_probability:
                        KeywordsStore._migrate_legacy_entry_chance(entry)
                    entry.setdefault("probability", 100)
                    entry.setdefault("parts", [])
                    entry.setdefault("text", "")
                    entry.setdefault("images", [])
                    entry.setdefault("records", [])
                    entry.setdefault("ats", [])
                    entry.setdefault("faces", [])
                    entry.setdefault("emojis", [])
                    entry.setdefault("videos", [])
                    entry.setdefault("music_cards", [])
                    KeywordsStore._sanitize_entry_fields(entry)
        return data

    @staticmethod
    def _sanitize_entry_fields(entry: dict) -> None:
        """加载时规范化 entry 字段，避免无效 face id 或占位文本残留。"""

        from .media import sanitize_entry_faces
        from .templates import strip_auto_media_text

        sanitize_entry_faces(entry)
        entry["text"] = strip_auto_media_text(str(entry.get("text", "") or ""))
        for part in entry.get("parts") or []:
            if not isinstance(part, dict):
                continue
            for seg in KeywordsStore.get_part_segments(part):
                if str(seg.get("type") or "").strip().lower() == "text":
                    seg["text"] = strip_auto_media_text(str(seg.get("text") or ""))

    @staticmethod
    def migrate_entry_to_parts(entry: dict, *, clear_legacy: bool = True) -> bool:
        """把仍使用旧扁平字段的 entry 转为 ``parts[]`` 格式。

        返回 ``True`` 表示发生了迁移。迁移后若 ``parts`` 含多条有效内容，
        触发时将按条分开发送（与旧版单条混合消息不同）。
        """

        if not isinstance(entry, dict) or KeywordsStore.uses_ordered_parts(entry):
            return False
        if not KeywordsStore.entry_has_payload(entry):
            return False

        parts = KeywordsStore.legacy_entry_to_parts(entry)
        if not parts:
            return False

        entry["parts"] = parts
        KeywordsStore._sanitize_entry_fields(entry)
        if clear_legacy:
            entry["text"] = ""
            for key in ("images", "records", "ats", "faces", "emojis", "videos", "music_cards"):
                entry[key] = []
        return True

    @staticmethod
    def _migrate_legacy_entry_chance(entry: dict) -> None:
        """兼容旧数据：将 <=100 的 weight 迁移为触发概率，权重重置为 100。"""

        try:
            legacy_weight = int(entry.get("weight", 100))
        except (TypeError, ValueError):
            legacy_weight = 100
        legacy_weight = max(0, legacy_weight)
        if legacy_weight <= 100:
            entry["probability"] = legacy_weight
            entry["weight"] = 100

    async def save(self) -> None:
        """原子写入 JSON。永久媒体目录（images/records/emojis/videos）永不自动清理。"""

        async with self._save_lock:
            self.data_version += 1
            tmp_file = self.data_file.with_suffix(".json.tmp")
            try:
                payload = json.dumps(self.data, ensure_ascii=False, indent=2)
                with tmp_file.open("w", encoding="utf-8") as f:
                    f.write(payload)
                os.replace(tmp_file, self.data_file)
            except Exception as exc:
                logger.error(f"保存关键词数据失败: {exc}")
                try:
                    if tmp_file.exists():
                        tmp_file.unlink()
                except Exception:
                    pass

    def bump_version(self) -> None:
        """在外部直接修改 data 后手动递增版本号（用于刷新匹配缓存）。"""

        self.data_version += 1

    # ─── 媒体文件 ───────────────────────────────────────────────

    def save_media_bytes(
        self,
        media_key: str,
        raw: bytes,
        suffix: str = "",
        *,
        to_cache: bool = False,
    ) -> Optional[str]:
        """将二进制内容按内容哈希落盘，返回文件名（去重）。

        ``to_cache=True`` 写入 ``media_cache/``；否则写入永久 ``images/records/emojis/videos/``。
        """

        dirs = self.cache_dirs if to_cache else self.media_dirs
        base_dir = dirs.get(media_key)
        if base_dir is None or not raw:
            return None
        ext = suffix or _MEDIA_SUFFIX.get(media_key, "")
        filename = hashlib.md5(raw).hexdigest() + ext
        target = base_dir / filename
        try:
            base_dir.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                with target.open("wb") as f:
                    f.write(raw)
            if to_cache:
                self._trim_media_cache()
            return filename
        except Exception as exc:
            logger.error(f"保存媒体失败({media_key}, cache={to_cache}): {exc}")
            return None

    def save_media_base64(
        self,
        media_key: str,
        b64: str,
        suffix: str = "",
        *,
        to_cache: bool = False,
    ) -> Optional[str]:
        """将 base64 内容落盘，返回文件名。"""

        if not b64:
            return None
        try:
            raw = base64.b64decode(b64)
        except Exception as exc:
            logger.error(f"媒体 base64 解码失败({media_key}): {exc}")
            return None
        return self.save_media_bytes(media_key, raw, suffix=suffix, to_cache=to_cache)

    def _iter_media_cache_files(self) -> List[Path]:
        """列出 media_cache 下全部普通文件。"""

        files: List[Path] = []
        for base_dir in self.cache_dirs.values():
            if not base_dir.is_dir():
                continue
            try:
                for name in os.listdir(base_dir):
                    path = base_dir / name
                    if path.is_file():
                        files.append(path)
            except Exception as exc:
                logger.warning(f"扫描媒体缓存目录失败: {base_dir} error={exc}")
        return files

    def _trim_media_cache(self, max_files: int = _MEDIA_CACHE_MAX_FILES) -> None:
        """media_cache 超过上限时按修改时间滚动删除最旧文件。"""

        limit = max(1, int(max_files))
        try:
            files = self._iter_media_cache_files()
            overflow = len(files) - limit
            if overflow <= 0:
                return
            files.sort(key=lambda path: path.stat().st_mtime)
            removed = 0
            for path in files[:overflow]:
                try:
                    path.unlink()
                    removed += 1
                except Exception as exc:
                    logger.warning(f"删除过期媒体缓存失败: {path} error={exc}")
            if removed:
                logger.info(f"媒体缓存滚动清理: 删除 {removed} 个最旧文件（上限 {limit}）")
        except Exception as exc:
            logger.error(f"媒体缓存滚动清理失败: {exc}")

    def promote_media_from_cache(self, media_key: str, filename: str) -> Optional[str]:
        """把 ``media_cache`` 中的文件复制到永久媒体目录（同名），返回文件名。"""

        name = os.path.basename(str(filename or "").strip())
        if not name or media_key not in _MEDIA_DIRS:
            return None
        if media_key not in self.cache_dirs:
            # 无入站缓存目录的类型（如 videos）：仅校验永久目录是否已有文件
            permanent_path = self.media_dirs[media_key] / name
            return name if permanent_path.is_file() else None
        cache_path = self.cache_dirs[media_key] / name
        permanent_path = self.media_dirs[media_key] / name
        try:
            self.media_dirs[media_key].mkdir(parents=True, exist_ok=True)
            if permanent_path.is_file():
                return name
            if cache_path.is_file():
                permanent_path.write_bytes(cache_path.read_bytes())
                return name
        except Exception as exc:
            logger.error(f"提升缓存媒体失败({media_key}/{name}): {exc}")
            return None
        return None

    def promote_entry_media(self, entry: Optional[dict]) -> dict:
        """把 entry 引用的缓存媒体提升到永久目录，供词库长期保存。"""

        entry = entry or {}
        if not isinstance(entry, dict):
            return entry

        for key in _MEDIA_DIRS:
            items = entry.get(key) or []
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and item.get("file"):
                    promoted = self.promote_media_from_cache(key, str(item.get("file") or ""))
                    if promoted:
                        item["file"] = promoted

        for part in entry.get("parts") or []:
            if not isinstance(part, dict):
                continue
            for seg in KeywordsStore.get_part_segments(part):
                if not isinstance(seg, dict):
                    continue
                seg_type = str(seg.get("type") or "").strip().lower()
                media_key = {
                    "image": "images",
                    "voice": "records",
                    "record": "records",
                    "emoji": "emojis",
                    "video": "videos",
                }.get(seg_type)
                if not media_key:
                    continue
                name = str(seg.get("file") or "").strip()
                if not name:
                    continue
                promoted = self.promote_media_from_cache(media_key, name)
                if promoted:
                    seg["file"] = promoted
        return entry

    def read_media_base64(self, media_key: str, filename: str) -> Optional[str]:
        """读取媒体文件并返回 base64；优先永久目录，其次缓存目录。"""

        if media_key not in _MEDIA_DIRS or not filename:
            return None
        name = os.path.basename(filename)
        candidates = [
            self.media_dirs[media_key] / name,
            self.cache_dirs[media_key] / name,
        ]
        for path in candidates:
            if not path.is_file():
                continue
            try:
                return base64.b64encode(path.read_bytes()).decode("utf-8")
            except Exception as exc:
                logger.error(f"读取媒体失败({media_key}): {exc}")
                return None
        logger.warning(f"媒体文件不存在: {self.media_dirs[media_key] / name}")
        return None

    # ─── entry 辅助 ─────────────────────────────────────────────

    @staticmethod
    def empty_entry() -> dict:
        return {
            "text": "",
            "weight": 100,
            "probability": 100,
            "parts": [],
            "images": [],
            "records": [],
            "ats": [],
            "faces": [],
            "emojis": [],
            "videos": [],
            "music_cards": [],
        }

    @staticmethod
    def is_message_part(part: Optional[dict]) -> bool:
        """是否为「一条聊天消息」part（内含可合并发送的 ``segments``）。"""

        return isinstance(part, dict) and isinstance(part.get("segments"), list)

    @staticmethod
    def get_part_segments(part: Optional[dict]) -> List[dict]:
        """展开 part 为有序 segment 列表。

        - ``{"segments": [...]}``：一条消息内的多段富文本；
        - ``{"type": "text", ...}`` 等：兼容旧数据，视为仅含一段的消息。
        """

        if not isinstance(part, dict):
            return []
        nested = part.get("segments")
        if isinstance(nested, list):
            return [seg for seg in nested if isinstance(seg, dict) and KeywordsStore._segment_has_payload(seg)]
        if KeywordsStore._segment_has_payload(part):
            return [part]
        return []

    @staticmethod
    def _segment_has_payload(part: Optional[dict]) -> bool:
        if not isinstance(part, dict):
            return False
        part_type = str(part.get("type") or "").strip().lower()
        if part_type == "text":
            return bool((part.get("text") or "").strip())
        if part_type == "image":
            return bool((part.get("file") or "").strip())
        if part_type in {"voice", "record"}:
            return bool((part.get("file") or "").strip())
        if part_type == "emoji":
            return bool((part.get("file") or "").strip())
        if part_type == "video":
            return bool((part.get("file") or "").strip())
        if part_type == "music":
            return bool(str(part.get("id") or "").strip())
        if part_type == "face":
            return part.get("id") is not None
        if part_type == "at":
            return bool(str(part.get("user_id") or "").strip()) or bool(part.get("all"))
        return False

    @staticmethod
    def _part_has_payload(part: Optional[dict]) -> bool:
        return bool(KeywordsStore.get_part_segments(part))

    @staticmethod
    def make_message_part(segments: List[dict]) -> dict:
        """把多条 segment 打包为「一条聊天消息」part。"""

        cleaned = [seg for seg in segments if isinstance(seg, dict) and KeywordsStore._segment_has_payload(seg)]
        return {"segments": cleaned}

    @staticmethod
    def uses_ordered_parts(entry: Optional[dict]) -> bool:
        """entry 是否显式使用 ``parts`` 列表（每个 part 对应一条聊天消息）。"""

        entry = entry or {}
        parts = entry.get("parts")
        if not isinstance(parts, list) or not parts:
            return False
        return any(KeywordsStore._part_has_payload(part) for part in parts if isinstance(part, dict))

    @staticmethod
    def legacy_entry_to_parts(entry: Optional[dict]) -> List[dict]:
        """把旧版扁平 entry 转为单条消息的 ``parts``（段顺序与旧版 hybrid 拼装一致）。"""

        entry = entry or {}
        segments: List[dict] = []
        text = (entry.get("text") or "").strip()
        if text:
            segments.append({"type": "text", "text": text})
        for at in entry.get("ats", []):
            if not isinstance(at, dict):
                continue
            segments.append(
                {
                    "type": "at",
                    "user_id": str(at.get("user_id", "") or ""),
                    "nickname": str(at.get("nickname", "") or ""),
                    "all": bool(at.get("all")),
                }
            )
        for face in entry.get("faces", []):
            if isinstance(face, dict) and face.get("id") is not None:
                segments.append({"type": "face", "id": face.get("id")})
        for music in entry.get("music_cards", []):
            if not isinstance(music, dict):
                continue
            song_id = str(music.get("id") or "").strip()
            if song_id:
                segments.append(
                    {
                        "type": "music",
                        "platform": str(music.get("platform") or "163"),
                        "id": song_id,
                    }
                )
        for img in entry.get("images", []):
            if isinstance(img, dict) and (img.get("file") or "").strip():
                segments.append({"type": "image", "file": str(img.get("file") or "").strip()})
        for emoji in entry.get("emojis", []):
            if isinstance(emoji, dict) and (emoji.get("file") or "").strip():
                segments.append({"type": "emoji", "file": str(emoji.get("file") or "").strip()})
        for voice in entry.get("records", []):
            if isinstance(voice, dict) and (voice.get("file") or "").strip():
                segments.append({"type": "voice", "file": str(voice.get("file") or "").strip()})
        for video in entry.get("videos", []):
            if isinstance(video, dict) and (video.get("file") or "").strip():
                segments.append({"type": "video", "file": str(video.get("file") or "").strip()})
        if not segments:
            return []
        return [KeywordsStore.make_message_part(segments)]

    @staticmethod
    def get_ordered_parts(entry: Optional[dict]) -> List[dict]:
        """返回 entry 的消息级 ``parts``；无 ``parts`` 时从旧字段推导为单条消息。"""

        entry = entry or {}
        if KeywordsStore.uses_ordered_parts(entry):
            return [
                part
                for part in entry.get("parts", [])
                if isinstance(part, dict) and KeywordsStore._part_has_payload(part)
            ]
        return KeywordsStore.legacy_entry_to_parts(entry)

    @staticmethod
    def entry_has_payload(entry: Optional[dict]) -> bool:
        entry = entry or {}
        if KeywordsStore.uses_ordered_parts(entry):
            return True
        return bool(
            (entry.get("text") or "").strip()
            or entry.get("images")
            or entry.get("records")
            or entry.get("ats")
            or entry.get("faces")
            or entry.get("emojis")
            or entry.get("videos")
            or entry.get("music_cards")
        )

    @staticmethod
    def merge_entries(primary: dict, secondary: dict) -> dict:
        primary = primary or {}
        secondary = secondary or {}
        if KeywordsStore.uses_ordered_parts(primary) or KeywordsStore.uses_ordered_parts(secondary):
            merged_parts = KeywordsStore.get_ordered_parts(primary) + KeywordsStore.get_ordered_parts(secondary)
            merged = KeywordsStore.empty_entry()
            merged["weight"] = primary.get("weight", secondary.get("weight", 100))
            merged["probability"] = primary.get("probability", secondary.get("probability", 100))
            merged["parts"] = merged_parts
            return merged

        primary_text = (primary.get("text") or "").strip()
        secondary_text = (secondary.get("text") or "").strip()
        if primary_text and secondary_text:
            merged_text = f"{primary_text}\n{secondary_text}"
        else:
            merged_text = primary_text or secondary_text
        merged = {"text": merged_text}
        for key in ("images", "records", "ats", "faces", "emojis", "videos", "music_cards"):
            merged[key] = list(primary.get(key, [])) + list(secondary.get(key, []))
        return merged

    @staticmethod
    def normalize_entry_probability(entry: Optional[dict]) -> int:
        """将 entry 回复概率规范化为 0-100 整数。"""

        if not isinstance(entry, dict):
            return 100
        try:
            probability = int(entry.get("probability", 100))
        except (TypeError, ValueError):
            return 100
        return max(0, min(100, probability))

    @staticmethod
    def normalize_entry_weight(entry: Optional[dict]) -> int:
        """将 entry 权重规范化为非负整数。"""

        if not isinstance(entry, dict):
            return 100
        try:
            weight = int(entry.get("weight", 100))
        except (TypeError, ValueError):
            return 100
        return max(0, weight)

    @staticmethod
    def summarize_entry(entry: dict, max_text: int = 30) -> str:
        text = (entry.get("text") or "").replace("\n", " ")
        summary = text[:max_text]
        if len(text) > max_text:
            summary += "..."
        placeholders = []
        weight = KeywordsStore.normalize_entry_weight(entry)
        if weight != 100:
            placeholders.append(f"[权重:{weight}]")
        probability = KeywordsStore.normalize_entry_probability(entry)
        if probability != 100:
            placeholders.append(f"[概率:{probability}%]")
        if entry.get("images"):
            placeholders.append("[图片]")
        if entry.get("records"):
            placeholders.append("[语音]")
        if entry.get("emojis"):
            placeholders.append("[表情]")
        if entry.get("videos"):
            placeholders.append("[视频]")
        if entry.get("ats"):
            placeholders.append("[@]")
        if entry.get("faces"):
            placeholders.append("[表情ID]")
        if entry.get("music_cards"):
            placeholders.append("[音乐]")
        if not summary:
            return "".join(placeholders) or "[空]"
        return f"{summary}{''.join(placeholders)}"

    @staticmethod
    def placeholder_text(entry: dict) -> str:
        lines = []
        if entry.get("text"):
            lines.append(entry["text"])
        if entry.get("images"):
            lines.append(f"[图片 x{len(entry['images'])}]")
        if entry.get("records"):
            lines.append(f"[语音 x{len(entry['records'])}]")
        if entry.get("emojis"):
            lines.append(f"[表情 x{len(entry['emojis'])}]")
        if entry.get("videos"):
            lines.append(f"[视频 x{len(entry['videos'])}]")
        if entry.get("ats"):
            lines.append(f"[@ x{len(entry['ats'])}]")
        if entry.get("music_cards"):
            lines.append(f"[音乐 x{len(entry['music_cards'])}]")
        return "\n".join(lines).strip()
