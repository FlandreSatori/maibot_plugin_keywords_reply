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
      "regex": false,
      "enabled": true,
      "mode": "whitelist" | "blacklist",
      "groups": ["123456"],
      "case_sensitive": false,           # 仅 auto_detect 会显式写入
      "entries": [ entry, ... ]
    }

每条 entry::

    {
      "text": "",
      "images": [{"file": "abc.jpg"}],   # data_dir/images 下的文件名
      "records": [{"file": "abc.amr"}],  # 语音，data_dir/records
      "ats": [{"user_id": "123", "nickname": "", "all": false}],
      "faces": [{"id": 1}],
      "emojis": [{"file": "abc.gif"}],   # 表情包（sticker）
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

# entry 中媒体键 -> 存储子目录
_MEDIA_DIRS = {
    "images": "images",
    "records": "records",
    "emojis": "emojis",
}

# 媒体类型默认后缀
_MEDIA_SUFFIX = {
    "images": ".jpg",
    "records": ".amr",
    "emojis": ".gif",
}


class KeywordsStore:
    """关键词/检测词数据与媒体文件的持久化管理。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_file = self.data_dir / "keywords.json"
        self.media_dirs: Dict[str, Path] = {
            key: self.data_dir / sub for key, sub in _MEDIA_DIRS.items()
        }
        self._save_lock = asyncio.Lock()
        self.data: Dict[str, List[dict]] = {"command_triggered": [], "auto_detect": []}
        self.data_version = 0

    def setup(self) -> None:
        """创建目录并加载数据。"""

        self.data_dir.mkdir(parents=True, exist_ok=True)
        for path in self.media_dirs.values():
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
                cfg.setdefault("regex", False)
                cfg.setdefault("enabled", True)
                cfg.setdefault("mode", "whitelist")
                cfg.setdefault("groups", [])
                cfg.setdefault("entries", [])
                for entry in cfg.get("entries", []):
                    if not isinstance(entry, dict):
                        continue
                    entry.setdefault("text", "")
                    entry.setdefault("images", [])
                    entry.setdefault("records", [])
                    entry.setdefault("ats", [])
                    entry.setdefault("faces", [])
                    entry.setdefault("emojis", [])
                    entry.setdefault("music_cards", [])
        return data

    async def save(self) -> None:
        """原子写入 JSON，并清理未引用的媒体文件。"""

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
                return
        self._cleanup_unused_media()

    def bump_version(self) -> None:
        """在外部直接修改 data 后手动递增版本号（用于刷新匹配缓存）。"""

        self.data_version += 1

    # ─── 媒体文件 ───────────────────────────────────────────────

    def save_media_bytes(self, media_key: str, raw: bytes, suffix: str = "") -> Optional[str]:
        """将二进制内容按内容哈希落盘，返回文件名（去重）。"""

        base_dir = self.media_dirs.get(media_key)
        if base_dir is None or not raw:
            return None
        ext = suffix or _MEDIA_SUFFIX.get(media_key, "")
        filename = hashlib.md5(raw).hexdigest() + ext
        target = base_dir / filename
        try:
            if not target.exists():
                with target.open("wb") as f:
                    f.write(raw)
            return filename
        except Exception as exc:
            logger.error(f"保存媒体失败({media_key}): {exc}")
            return None

    def save_media_base64(self, media_key: str, b64: str, suffix: str = "") -> Optional[str]:
        """将 base64 内容落盘，返回文件名。"""

        if not b64:
            return None
        try:
            raw = base64.b64decode(b64)
        except Exception as exc:
            logger.error(f"媒体 base64 解码失败({media_key}): {exc}")
            return None
        return self.save_media_bytes(media_key, raw, suffix=suffix)

    def read_media_base64(self, media_key: str, filename: str) -> Optional[str]:
        """读取媒体文件并返回 base64；文件缺失返回 None。"""

        base_dir = self.media_dirs.get(media_key)
        if base_dir is None or not filename:
            return None
        path = base_dir / os.path.basename(filename)
        if not path.is_file():
            logger.warning(f"媒体文件不存在: {path}")
            return None
        try:
            return base64.b64encode(path.read_bytes()).decode("utf-8")
        except Exception as exc:
            logger.error(f"读取媒体失败({media_key}): {exc}")
            return None

    def _collect_referenced_media(self) -> Dict[str, set]:
        refs: Dict[str, set] = {key: set() for key in _MEDIA_DIRS}
        for section in SECTIONS:
            for cfg in self.data.get(section, []):
                if not isinstance(cfg, dict):
                    continue
                for entry in cfg.get("entries", []):
                    if not isinstance(entry, dict):
                        continue
                    for key in _MEDIA_DIRS:
                        for item in entry.get(key, []):
                            name = (item or {}).get("file")
                            if name:
                                refs[key].add(os.path.basename(name))
        return refs

    def _cleanup_unused_media(self) -> None:
        try:
            referenced = self._collect_referenced_media()
            removed = 0
            for key, base_dir in self.media_dirs.items():
                if not base_dir.is_dir():
                    continue
                for name in os.listdir(base_dir):
                    full = base_dir / name
                    if not full.is_file():
                        continue
                    if name not in referenced[key]:
                        full.unlink()
                        removed += 1
            if removed:
                logger.info(f"已清理未引用媒体文件: {removed} 个")
        except Exception as exc:
            logger.error(f"清理未引用媒体文件失败: {exc}")

    # ─── entry 辅助 ─────────────────────────────────────────────

    @staticmethod
    def empty_entry() -> dict:
        return {
            "text": "",
            "images": [],
            "records": [],
            "ats": [],
            "faces": [],
            "emojis": [],
            "music_cards": [],
        }

    @staticmethod
    def entry_has_payload(entry: Optional[dict]) -> bool:
        entry = entry or {}
        return bool(
            (entry.get("text") or "").strip()
            or entry.get("images")
            or entry.get("records")
            or entry.get("ats")
            or entry.get("faces")
            or entry.get("emojis")
            or entry.get("music_cards")
        )

    @staticmethod
    def merge_entries(primary: dict, secondary: dict) -> dict:
        primary = primary or {}
        secondary = secondary or {}
        primary_text = (primary.get("text") or "").strip()
        secondary_text = (secondary.get("text") or "").strip()
        if primary_text and secondary_text:
            merged_text = f"{primary_text}\n{secondary_text}"
        else:
            merged_text = primary_text or secondary_text
        merged = {"text": merged_text}
        for key in ("images", "records", "ats", "faces", "emojis", "music_cards"):
            merged[key] = list(primary.get(key, [])) + list(secondary.get(key, []))
        return merged

    @staticmethod
    def summarize_entry(entry: dict, max_text: int = 30) -> str:
        text = (entry.get("text") or "").replace("\n", " ")
        summary = text[:max_text]
        if len(text) > max_text:
            summary += "..."
        placeholders = []
        if entry.get("images"):
            placeholders.append("[图片]")
        if entry.get("records"):
            placeholders.append("[语音]")
        if entry.get("emojis"):
            placeholders.append("[表情]")
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
        if entry.get("ats"):
            lines.append(f"[@ x{len(entry['ats'])}]")
        if entry.get("music_cards"):
            lines.append(f"[音乐 x{len(entry['music_cards'])}]")
        return "\n".join(lines).strip()
