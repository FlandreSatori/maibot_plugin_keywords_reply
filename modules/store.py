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
            "tags": ["Z", "问候"],             # 可选；默认按首字拼音归类
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
from collections import OrderedDict
from pathlib import Path
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

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

# 媒体编码缓存（base64）目录与内存上限
_ENCODE_CACHE_ROOT = "encode_cache"
_ENCODE_MEM_MAX_BYTES = 64 * 1024 * 1024

# 媒体类型默认后缀
_MEDIA_SUFFIX = {
    "images": ".jpg",
    "records": ".amr",
    "emojis": ".gif",
    "videos": ".mp4",
}

# GBK 编码区间对应的拼音首字母边界。无需额外依赖，适合插件启动时批量归类。
_PINYIN_INITIAL_BOUNDARIES = (
    (-20319, "A"), (-20284, "B"), (-19776, "C"), (-19219, "D"),
    (-18711, "E"), (-18527, "F"), (-18240, "G"), (-17923, "H"),
    (-17418, "J"), (-16475, "K"), (-16213, "L"), (-15641, "M"),
    (-15166, "N"), (-14923, "O"), (-14915, "P"), (-14631, "Q"),
    (-14150, "R"), (-14091, "S"), (-13319, "T"), (-12839, "W"),
    (-12557, "X"), (-11848, "Y"), (-11056, "Z"),
)


def normalize_tags(value: Any) -> List[str]:
    """把标签规范为去重字符串列表，保留用户输入顺序。"""

    if isinstance(value, str):
        items = re.split(r"[|\n,，]+", value)
    elif isinstance(value, list):
        items = value
    else:
        items = []
    result: List[str] = []
    seen: set[str] = set()
    for item in items:
        tag = str(item or "").strip()
        key = tag.casefold()
        if not tag or key in seen:
            continue
        seen.add(key)
        result.append(tag)
    return result


def default_tag_for_keyword(keyword: Any) -> str:
    """返回默认分组：英文不分大小写归入 A-Z，中文拼音归入小写 a-z。"""

    text = str(keyword or "").strip()
    if not text:
        return "其它"
    first = text[0]
    if "A" <= first <= "Z" or "a" <= first <= "z":
        return first.upper()
    try:
        encoded = first.encode("gbk")
    except UnicodeEncodeError:
        return "其它"
    if len(encoded) != 2:
        return "其它"
    code = encoded[0] * 256 + encoded[1] - 65536
    initial = ""
    for boundary, letter in _PINYIN_INITIAL_BOUNDARIES:
        if code < boundary:
            break
        initial = letter
    return initial.lower() if initial else "其它"


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
        # base64 编码缓存：磁盘持久化 + 内存 LRU；按使用频率淘汰
        self.encode_cache_root = self.data_dir / _ENCODE_CACHE_ROOT
        self.encode_cache_enabled = True
        self.encode_cache_max_bytes = 500 * 1024 * 1024
        self._b64_mem: "OrderedDict[str, str]" = OrderedDict()
        self._b64_mem_bytes = 0
        self._encode_index: Dict[str, Dict[str, float]] = {}
        self._encode_index_dirty = False
        self._encode_index_flushed_at = 0.0
        self._encode_lock = threading.Lock()
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
        self.encode_cache_root.mkdir(parents=True, exist_ok=True)
        self._load_encode_index()
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
                had_tags = "tags" in cfg
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
                if had_tags:
                    tags = normalize_tags(cfg.get("tags"))
                    # 兼容上一版自动标签：英文旧的小写标签升为大写，中文旧的大写拼音改为小写。
                    default_tag = default_tag_for_keyword(primary)
                    if len(tags) == 1 and tags[0].casefold() == default_tag.casefold():
                        tags = [default_tag]
                    cfg["tags"] = tags
                else:
                    cfg["tags"] = [default_tag_for_keyword(primary)]
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
        """原子写入 JSON（序列化与写盘在工作线程执行，避免阻塞事件循环）。

        永久媒体目录（images/records/emojis/videos）永不自动清理。
        """

        async with self._save_lock:
            self.data_version += 1
            tmp_file = self.data_file.with_suffix(".json.tmp")
            try:
                payload = await asyncio.to_thread(self._serialize_data)
                await asyncio.to_thread(self._write_atomic, tmp_file, self.data_file, payload)
            except Exception as exc:
                logger.error(f"保存关键词数据失败: {exc}")
                try:
                    if tmp_file.exists():
                        tmp_file.unlink()
                except Exception:
                    pass

    def _serialize_data(self) -> str:
        try:
            return json.dumps(self.data, ensure_ascii=False, indent=2)
        except RuntimeError:
            # 并发修改导致的极端情况：重试一次
            return json.dumps(self.data, ensure_ascii=False, indent=2)

    @staticmethod
    def _write_atomic(tmp_file: Path, final_path: Path, payload: str) -> None:
        with tmp_file.open("w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp_file, final_path)

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
        """读取媒体文件并返回 base64。

        读取顺序：内存 LRU → 磁盘编码缓存（encode_cache/）→ 源文件（永久目录 → 缓存目录）。
        ``videos`` 无入站缓存目录，只读永久 ``videos/``。
        """

        if media_key not in _MEDIA_DIRS or not filename:
            return None
        name = os.path.basename(filename)
        if not name:
            return None
        return self._read_encoded_cached(media_key, name)

    # ─── 编码缓存（base64 持久化，按使用频率保留） ───────────────

    def configure_encode_cache(
        self,
        *,
        enabled: bool = True,
        max_bytes: int = 500 * 1024 * 1024,
    ) -> None:
        """由插件根据配置项设置编码缓存开关与磁盘上限（``0`` 表示不限制）。"""

        self.encode_cache_enabled = bool(enabled)
        self.encode_cache_max_bytes = max(0, int(max_bytes))

    def flush_encode_index(self) -> None:
        """强制落盘编码缓存索引（卸载时调用，避免频率统计丢失）。"""

        self._save_encode_index()

    async def preload_media(self, entries: Any) -> None:
        """发送前预热编码缓存（线程池执行，避免阻塞事件循环）。"""

        if not self.encode_cache_enabled:
            return
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, (list, tuple)):
            return
        pending: List[Tuple[str, str]] = []
        seen: set[Tuple[str, str]] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for media_key, name in KeywordsStore.entry_media_files(entry):
                item = (media_key, name)
                if item not in seen:
                    seen.add(item)
                    pending.append(item)
        if pending:
            await asyncio.to_thread(self._preload_sync, pending)

    def _preload_sync(self, pending: List[Tuple[str, str]]) -> None:
        for media_key, name in pending:
            self._read_encoded_cached(media_key, name)

    @staticmethod
    def entry_media_files(entry: Optional[dict]) -> List[Tuple[str, str]]:
        """收集 entry 引用的全部媒体文件 ``(media_key, filename)``，含 parts 内嵌段。"""

        out: List[Tuple[str, str]] = []
        if not isinstance(entry, dict):
            return out
        for key in _MEDIA_DIRS:
            items = entry.get(key) or []
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict):
                    name = str(item.get("file") or "").strip()
                    if name:
                        out.append((key, name))
        for part in entry.get("parts") or []:
            for seg in KeywordsStore.get_part_segments(part):
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
                if name:
                    out.append((media_key, name))
        return out

    def _read_encoded_cached(self, media_key: str, name: str) -> Optional[str]:
        key = f"{media_key}/{name}"
        if self.encode_cache_enabled:
            with self._encode_lock:
                b64 = self._b64_mem.get(key)
                if b64 is not None:
                    self._b64_mem.move_to_end(key)
                    self._touch_index_locked(key, len(b64))
                    return b64
            cached = self._read_encode_entry(media_key, name)
            if cached is not None:
                with self._encode_lock:
                    self._b64_mem_put_locked(key, cached)
                self._touch_index(key, len(cached))
                self._maybe_flush_index()
                return cached
        path = self._resolve_media_path(media_key, name)
        if path is None:
            logger.warning(f"媒体文件不存在: {self.media_dirs[media_key] / name}")
            return None
        try:
            raw = path.read_bytes()
        except Exception as exc:
            logger.error(f"读取媒体失败({media_key}): {exc}")
            return None
        b64 = base64.b64encode(raw).decode("utf-8")
        if self.encode_cache_enabled:
            self._write_encode_entry(media_key, name, b64)
            self._touch_index(key, len(b64))
            self._prune_encode_cache()
            self._maybe_flush_index()
        return b64

    def _resolve_media_path(self, media_key: str, name: str) -> Optional[Path]:
        """定位源文件：优先永久目录，其次入站缓存目录。"""

        permanent = self.media_dirs[media_key] / name
        if permanent.is_file():
            return permanent
        cache_dir = self.cache_dirs.get(media_key)
        if cache_dir is not None:
            cached = cache_dir / name
            if cached.is_file():
                return cached
        return None

    def _b64_mem_put_locked(self, key: str, b64: str) -> None:
        """写入内存 LRU（调用方需持有 ``_encode_lock``），超限淘汰最旧条目。"""

        size = len(b64)
        if size > _ENCODE_MEM_MAX_BYTES:
            return
        old = self._b64_mem.pop(key, None)
        if old is not None:
            self._b64_mem_bytes -= len(old)
        self._b64_mem[key] = b64
        self._b64_mem_bytes += size
        while self._b64_mem_bytes > _ENCODE_MEM_MAX_BYTES:
            _, evicted = self._b64_mem.popitem(last=False)
            self._b64_mem_bytes -= len(evicted)

    def _touch_index(self, key: str, size: int) -> None:
        with self._encode_lock:
            self._touch_index_locked(key, size)

    def _touch_index_locked(self, key: str, size: int) -> None:
        """更新索引命中频率（调用方需持有 ``_encode_lock``）。"""

        info = self._encode_index.get(key)
        if info is None:
            self._encode_index[key] = {"hits": 1, "last": time.time(), "size": int(size)}
        else:
            info["hits"] = int(info.get("hits", 0)) + 1
            info["last"] = time.time()
            info["size"] = int(size)
        self._encode_index_dirty = True

    def _encode_entry_path(self, media_key: str, name: str) -> Path:
        return self.encode_cache_root / media_key / f"{name}.b64"

    def _read_encode_entry(self, media_key: str, name: str) -> Optional[str]:
        path = self._encode_entry_path(media_key, name)
        if not path.is_file():
            return None
        try:
            b64 = path.read_text(encoding="utf-8")
        except Exception as exc:
            logger.warning(f"读取编码缓存失败({media_key}/{name}): {exc}")
            return None
        # 轻量校验：长度非 4 倍数视为损坏，回退源文件
        if not b64 or len(b64) % 4 != 0:
            return None
        return b64

    def _write_encode_entry(self, media_key: str, name: str, b64: str) -> None:
        path = self._encode_entry_path(media_key, name)
        tmp = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(b64, encoding="utf-8")
            os.replace(tmp, path)
        except Exception as exc:
            logger.warning(f"写入编码缓存失败({media_key}/{name}): {exc}")
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass

    def _load_encode_index(self) -> None:
        """加载编码缓存索引并做一致性清理（孤儿文件/丢失条目）。"""

        index_path = self.encode_cache_root / "index.json"
        index: Dict[str, Dict[str, float]] = {}
        if index_path.is_file():
            try:
                raw = json.loads(index_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    index = {
                        str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)
                    }
            except Exception as exc:
                logger.warning(f"加载编码缓存索引失败: {exc}")
        self._encode_index = index
        self._encode_index_dirty = False
        self._encode_index_flushed_at = time.time()
        self._reconcile_encode_cache()

    def _reconcile_encode_cache(self) -> None:
        """启动时清理：删除索引外的孤儿缓存文件，移除文件已丢失的索引条目。"""

        try:
            if not self.encode_cache_root.is_dir():
                return
            known = set(self._encode_index.keys())
            for sub in self.encode_cache_root.iterdir():
                if not sub.is_dir():
                    continue
                for path in sub.iterdir():
                    if not path.is_file():
                        continue
                    if path.name.endswith(".tmp"):
                        try:
                            path.unlink()
                        except Exception:
                            pass
                        continue
                    name = path.name[:-4] if path.name.endswith(".b64") else path.name
                    key = f"{sub.name}/{name}"
                    if key not in known:
                        try:
                            path.unlink()
                        except Exception:
                            pass
            stale: List[str] = []
            for key in list(self._encode_index.keys()):
                try:
                    media_key, name = key.split("/", 1)
                except ValueError:
                    stale.append(key)
                    continue
                if not self._encode_entry_path(media_key, name).is_file():
                    stale.append(key)
            for key in stale:
                self._encode_index.pop(key, None)
            if stale:
                self._encode_index_dirty = True
        except Exception as exc:
            logger.warning(f"编码缓存一致性清理失败: {exc}")

    def _save_encode_index(self) -> None:
        with self._encode_lock:
            if not self._encode_index_dirty:
                return
            payload = json.dumps(self._encode_index, ensure_ascii=False)
            self._encode_index_dirty = False
            self._encode_index_flushed_at = time.time()
        index_path = self.encode_cache_root / "index.json"
        tmp = index_path.with_name("index.json.tmp")
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, index_path)
        except Exception as exc:
            with self._encode_lock:
                self._encode_index_dirty = True
            logger.warning(f"保存编码缓存索引失败: {exc}")

    def _maybe_flush_index(self, interval: float = 60.0) -> None:
        """索引按 60 秒节流机会式落盘，避免高频写。"""

        if not self._encode_index_dirty:
            return
        if time.time() - self._encode_index_flushed_at < interval:
            return
        self._save_encode_index()

    def _prune_encode_cache(self) -> int:
        """磁盘编码缓存超上限时按 (命中次数, 最近使用) 淘汰最冷条目。"""

        limit = self.encode_cache_max_bytes
        if limit <= 0:
            return 0
        with self._encode_lock:
            total = sum(int(info.get("size", 0)) for info in self._encode_index.values())
            if total <= limit:
                return 0
            items = sorted(
                self._encode_index.items(),
                key=lambda kv: (kv[1].get("hits", 0), kv[1].get("last", 0.0)),
            )
            victims: List[str] = []
            for key, info in items:
                if total <= limit:
                    break
                total -= int(info.get("size", 0))
                victims.append(key)
            for key in victims:
                self._encode_index.pop(key, None)
            self._encode_index_dirty = True
        removed = 0
        for key in victims:
            try:
                media_key, name = key.split("/", 1)
                self._encode_entry_path(media_key, name).unlink(missing_ok=True)
                removed += 1
            except Exception:
                pass
        return removed

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
    def _summarize_segment(seg: dict, *, max_text: int) -> str:
        """单段摘要：文本截断；媒体显示 ``[文件名]``。"""

        if not isinstance(seg, dict):
            return ""
        seg_type = str(seg.get("type") or "").strip().lower()
        if seg_type == "text":
            text = str(seg.get("text") or "").replace("\n", " ").strip()
            if not text:
                return ""
            if len(text) > max_text:
                return text[:max_text] + "..."
            return text
        if seg_type in {"image", "emoji", "voice", "record", "video"}:
            name = str(seg.get("file") or "").strip()
            return f"[{name}]" if name else f"[{seg_type}]"
        if seg_type == "at":
            if seg.get("all"):
                return "[@全体]"
            uid = str(seg.get("user_id") or "").strip()
            nick = str(seg.get("nickname") or "").strip()
            if nick:
                return f"[@{nick}]"
            return f"[@{uid}]" if uid else "[@]"
        if seg_type == "face":
            face_id = seg.get("id")
            return f"[表情ID:{face_id}]" if face_id is not None else "[表情ID]"
        if seg_type == "music":
            platform = str(seg.get("platform") or "").strip() or "?"
            song_id = str(seg.get("id") or "").strip()
            return f"[音乐:{platform}/{song_id}]" if song_id else "[音乐]"
        return ""

    @staticmethod
    def summarize_entry(entry: dict, max_text: int = 30) -> str:
        """列表/详情用的单条回复摘要。

        优先遍历 ``parts``；媒体显示为 ``[xxxxxxxx.jpg]`` / ``[xxxxxx.silk]`` 等，避免纯媒体词条显示成 ``[空]``。
        """

        entry = entry or {}
        meta: List[str] = []
        weight = KeywordsStore.normalize_entry_weight(entry)
        if weight != 100:
            meta.append(f"[权重:{weight}]")
        probability = KeywordsStore.normalize_entry_probability(entry)
        if probability != 100:
            meta.append(f"[概率:{probability}%]")

        body_parts: List[str] = []
        for part in KeywordsStore.get_ordered_parts(entry):
            for seg in KeywordsStore.get_part_segments(part):
                piece = KeywordsStore._summarize_segment(seg, max_text=max_text)
                if piece:
                    body_parts.append(piece)

        # 旧扁平字段兜底（无 parts / 迁移未完成时）
        if not body_parts:
            text = (entry.get("text") or "").replace("\n", " ").strip()
            if text:
                body_parts.append(text[:max_text] + ("..." if len(text) > max_text else ""))
            for key, label in (
                ("images", None),
                ("records", None),
                ("emojis", None),
                ("videos", None),
            ):
                for item in entry.get(key) or []:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("file") or "").strip()
                    body_parts.append(f"[{name}]" if name else f"[{label or key}]")
            for at in entry.get("ats") or []:
                if isinstance(at, dict):
                    nick = str(at.get("nickname") or "").strip()
                    uid = str(at.get("user_id") or "").strip()
                    body_parts.append(f"[@{nick or uid}]" if (nick or uid) else "[@]")
            for face in entry.get("faces") or []:
                if isinstance(face, dict) and face.get("id") is not None:
                    body_parts.append(f"[表情ID:{face.get('id')}]")
            for card in entry.get("music_cards") or []:
                if not isinstance(card, dict):
                    continue
                platform = str(card.get("platform") or "").strip() or "?"
                song_id = str(card.get("id") or "").strip()
                if song_id:
                    body_parts.append(f"[音乐:{platform}/{song_id}]")

        body = "".join(body_parts)
        if not body:
            return "".join(meta) or "[空]"
        return f"{body}{''.join(meta)}"

    @staticmethod
    def placeholder_text(entry: dict) -> str:
        lines: List[str] = []
        for part in KeywordsStore.get_ordered_parts(entry or {}):
            for seg in KeywordsStore.get_part_segments(part):
                piece = KeywordsStore._summarize_segment(seg, max_text=200)
                if piece:
                    lines.append(piece)
        if lines:
            return "\n".join(lines).strip()

        entry = entry or {}
        if entry.get("text"):
            lines.append(str(entry["text"]))
        for key in ("images", "records", "emojis", "videos"):
            for item in entry.get(key) or []:
                if isinstance(item, dict) and item.get("file"):
                    lines.append(f"[{item['file']}]")
        if entry.get("ats"):
            lines.append(f"[@ x{len(entry['ats'])}]")
        if entry.get("music_cards"):
            lines.append(f"[音乐 x{len(entry['music_cards'])}]")
        return "\n".join(lines).strip()
