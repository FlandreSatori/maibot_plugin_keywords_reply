"""关键词/检测词匹配引擎。

- 命令触发（command_triggered）：整条消息首个 token 与关键词精确匹配（或正则 fullmatch）。
- 自动监听（auto_detect）：消息文本包含检测词（或正则 search）。

两种模式均支持：群聊黑白名单、大小写敏感、（检测词）触发冷却。
"""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Any, Callable, Dict, List, Optional

from .store import KeywordsStore

logger = logging.getLogger("plugin.maibot_plugin.keywords_reply")


def pick_weighted_entry(entries: List[dict]) -> Optional[dict]:
    """按 entry.weight 权重随机抽取一条回复。"""

    if not entries:
        return None
    if len(entries) == 1:
        return entries[0]

    weights = [KeywordsStore.normalize_entry_weight(entry) for entry in entries]
    total = sum(weights)
    if total <= 0:
        return random.choice(entries)
    return random.choices(entries, weights=weights, k=1)[0]


def entry_passes_probability(entry: dict) -> bool:
    """判定抽中的 entry 是否通过概率检查。"""

    probability = KeywordsStore.normalize_entry_probability(entry)
    if probability >= 100:
        return True
    if probability <= 0:
        return False
    return random.randint(1, 100) <= probability


def pick_entry_for_reply(entries: List[dict]) -> Optional[dict]:
    """先按权重抽取一条回复，再按该条 probability 决定是否回复。"""

    picked = pick_weighted_entry(entries)
    if not picked:
        return None
    if not entry_passes_probability(picked):
        logger.debug("已按权重选中回复，但未通过概率判定，跳过发送")
        return None
    return picked


def message_has_bot_mention(is_at: bool, is_mentioned: bool) -> bool:
    """判断消息是否包含对机器人的 @ 或平台级提及。"""

    return bool(is_at or is_mentioned)


class Matcher:
    """封装匹配所需的缓存与冷却状态。"""

    def __init__(self, store: KeywordsStore, get_settings: Callable[[], Dict[str, Any]]) -> None:
        self.store = store
        self.get_settings = get_settings
        self._regex_cache: Dict[tuple, Optional[re.Pattern]] = {}
        self._regex_cache_version = -1
        self._last_triggered: Dict[str, float] = {}

    def _compiled(self, pattern: str) -> Optional[re.Pattern]:
        if self._regex_cache_version != self.store.data_version:
            self._regex_cache.clear()
            self._regex_cache_version = self.store.data_version
        cached = self._regex_cache.get(pattern)
        if cached is not None:
            return cached
        try:
            compiled = re.compile(pattern)
            self._regex_cache[pattern] = compiled
            return compiled
        except Exception as exc:
            logger.error(f"正则编译失败({pattern}): {exc}")
            self._regex_cache[pattern] = None
            return None

    @staticmethod
    def is_enabled_in_group(cfg: dict, group_id: str) -> bool:
        if not cfg.get("enabled", True):
            return False
        mode = cfg.get("mode", "whitelist")
        groups = cfg.get("groups", [])
        if mode == "whitelist":
            return group_id in groups
        return group_id not in groups

    def _group_allows(self, cfg: dict, group_id: str) -> bool:
        mode = cfg.get("mode", "whitelist")
        groups = cfg.get("groups", [])
        if mode == "whitelist":
            return group_id in groups
        return group_id not in groups

    def _should_forward(self, entries: List[dict], group_id: str) -> bool:
        settings = self.get_settings()
        if not settings.get("qq_forward_all_replies", False):
            return False
        if len(entries or []) <= 1:
            return False
        if not group_id:
            return False
        return True

    # ─── 命令触发 ──────────────────────────────────────────────

    def match_command(
        self,
        text: str,
        group_id: str,
        *,
        is_at: bool = False,
        is_mentioned: bool = False,
    ) -> Optional[dict]:
        text = (text or "").strip()
        if not text or not group_id:
            return None
        potential = text.split()[0]
        settings = self.get_settings()
        case_sensitive = bool(settings.get("case_sensitive", False))

        for cfg in self.store.data.get("command_triggered", []):
            keyword = cfg.get("keyword")
            if not keyword or not cfg.get("enabled", True):
                continue
            if cfg.get("require_at_bot") and not message_has_bot_mention(is_at, is_mentioned):
                continue
            if cfg.get("regex", False):
                compiled = self._compiled(keyword)
                if compiled is None or not compiled.fullmatch(potential):
                    continue
            else:
                left, right = (potential, keyword) if case_sensitive else (potential.lower(), keyword.lower())
                if left != right:
                    continue
            if not self._group_allows(cfg, group_id):
                continue
            entries = cfg.get("entries") or []
            if not entries:
                return None
            logger.info(f"关键词触发: {potential} (群: {group_id})")
            if self._should_forward(entries, group_id):
                return {"payload": list(entries), "rule": cfg}
            picked = pick_entry_for_reply(entries)
            if not picked:
                return None
            return {"payload": picked, "rule": cfg}
        return None

    # ─── 自动监听 ──────────────────────────────────────────────

    def match_detect(
        self,
        text: str,
        group_id: str,
        session_id: str,
        *,
        is_at: bool = False,
        is_mentioned: bool = False,
    ) -> Optional[dict]:
        text = (text or "").strip()
        if not text or not group_id:
            return None
        settings = self.get_settings()
        global_case_sensitive = bool(settings.get("case_sensitive", False))
        cooldown = int(settings.get("cooldown", 0) or 0)
        ignore_cd_exact = bool(settings.get("ignore_cooldown_on_exact_match", False))
        now = time.time()

        for cfg in self.store.data.get("auto_detect", []):
            keyword = cfg.get("keyword")
            if not keyword or not cfg.get("enabled", True):
                continue
            if cfg.get("require_at_bot") and not message_has_bot_mention(is_at, is_mentioned):
                continue
            is_regex = cfg.get("regex", False)

            if is_regex:
                compiled = self._compiled(keyword)
                if compiled is None or not compiled.search(text):
                    continue
                is_exact = False
            else:
                if global_case_sensitive:
                    hay, needle = text, keyword
                    is_exact = text == keyword
                else:
                    hay, needle = text.lower(), keyword.lower()
                    is_exact = text.lower() == keyword.lower()
                if needle not in hay:
                    continue

            if not self._group_allows(cfg, group_id):
                continue

            entries = cfg.get("entries") or []
            if not entries:
                continue

            skip_cooldown = ignore_cd_exact and is_exact and not is_regex
            if not skip_cooldown and cooldown > 0 and session_id in self._last_triggered:
                elapsed = now - self._last_triggered[session_id]
                if elapsed < cooldown:
                    logger.debug(f"检测词冷却中 (session: {session_id})，剩余 {cooldown - elapsed:.1f}s")
                    continue

            if cooldown > 0 and not skip_cooldown:
                self._last_triggered[session_id] = now

            logger.info(f"检测词触发: {keyword} (群: {group_id})")
            if self._should_forward(entries, group_id):
                return {"payload": list(entries), "rule": cfg}
            picked = pick_entry_for_reply(entries)
            if not picked:
                return None
            return {"payload": picked, "rule": cfg}
        return None


# ─── 序号/内容查找与群聊开关（供命令处理复用） ────────────────────


def find_indices(data: List[dict], param: str, case_sensitive: bool = False) -> List[int]:
    """支持 ``1,2-5`` 形式的序号，或按关键词内容匹配，返回索引列表。"""

    if not data:
        return []
    try:
        indices: List[int] = []
        for part in param.split(","):
            if "-" in part:
                start, end = map(int, part.split("-"))
                indices.extend(range(start - 1, end))
            else:
                indices.append(int(part) - 1)
        valid = [i for i in indices if 0 <= i < len(data)]
        if valid:
            return valid
    except ValueError:
        pass

    matched: List[int] = []
    for i, cfg in enumerate(data):
        keyword = cfg.get("keyword", "")
        is_regex = cfg.get("regex", False)
        if is_regex or case_sensitive:
            equal = keyword == param
        else:
            equal = keyword.lower() == param.lower()
        if equal:
            matched.append(i)
    return matched


def apply_group_toggle(cfg: dict, enable: bool, args: List[str], current_group_id: str) -> tuple[bool, str]:
    """应用启用/禁用群聊逻辑，返回 (是否成功, 描述或错误信息)。"""

    if enable:
        if not args:
            if current_group_id:
                if cfg["mode"] == "blacklist":
                    if current_group_id in cfg["groups"]:
                        cfg["groups"].remove(current_group_id)
                else:
                    if current_group_id not in cfg["groups"]:
                        cfg["groups"].append(current_group_id)
                cfg["enabled"] = True
                return True, f"当前群聊 ({current_group_id})"
            return False, "当前不在群聊中，请指定群号或使用'全局'参数。"
        if args[0] == "全局":
            cfg["enabled"] = True
            cfg["mode"] = "blacklist"
            cfg["groups"] = []
            return True, "全局 (所有群聊)"
        if cfg["mode"] != "whitelist":
            cfg["mode"] = "whitelist"
            cfg["groups"] = []
        for gid in args:
            if not gid.isdigit():
                return False, f"群号格式错误: {gid}"
            if gid not in cfg["groups"]:
                cfg["groups"].append(gid)
        cfg["enabled"] = True
        return True, ", ".join(args)

    # 禁用
    if not args:
        if current_group_id:
            if cfg["mode"] != "blacklist":
                cfg["mode"] = "blacklist"
                cfg["groups"] = []
            if current_group_id not in cfg["groups"]:
                cfg["groups"].append(current_group_id)
            cfg["enabled"] = True
            return True, f"当前群聊 ({current_group_id})"
        cfg["enabled"] = False
        return True, "全局"
    if args[0] == "全局":
        cfg["enabled"] = False
        return True, "全局"
    if cfg["mode"] != "blacklist":
        cfg["mode"] = "blacklist"
        cfg["groups"] = []
    for gid in args:
        if not gid.isdigit():
            return False, f"群号格式错误: {gid}"
        if gid not in cfg["groups"]:
            cfg["groups"].append(gid)
    cfg["enabled"] = True
    return True, ", ".join(args)


def describe_status(cfg: dict) -> str:
    """生成词条群聊状态的简短描述。"""

    enabled = cfg.get("enabled", True)
    mode = cfg.get("mode", "whitelist")
    groups = cfg.get("groups", [])
    at_hint = "；需@机器人" if cfg.get("require_at_bot") else ""
    if not enabled:
        return f"全局禁用{at_hint}"
    if mode == "blacklist":
        base = "全局启用" if not groups else f"黑名单模式 (禁用群: {', '.join(groups)})"
        return f"{base}{at_hint}"
    base = "未在任何群聊启用" if not groups else f"白名单模式 (允许群: {', '.join(groups)})"
    return f"{base}{at_hint}"


def describe_status_brief(cfg: dict) -> str:
    """列表用的方括号状态标记。"""

    enabled = cfg.get("enabled", True)
    mode = cfg.get("mode", "whitelist")
    groups = cfg.get("groups", [])
    at_hint = " [@]" if cfg.get("require_at_bot") else ""
    if not enabled:
        return f" [全局禁用]{at_hint}"
    if mode == "blacklist":
        return (" [全局启用]" if not groups else f" [黑名单:{','.join(groups)}]") + at_hint
    return (" [未启用]" if not groups else f" [白名单:{','.join(groups)}]") + at_hint


_LIST_PAGE_ARG_PATTERN = re.compile(r"^(?:第)?(\d+)(?:页)?$", re.IGNORECASE)


def parse_list_page_arg(args: str) -> int:
    """解析列表翻页参数，如 ``2``、``第2页``。"""

    text = (args or "").strip()
    if not text:
        return 1
    matched = _LIST_PAGE_ARG_PATTERN.fullmatch(text)
    if matched:
        return max(1, int(matched.group(1)))
    return 1


def paginate_slice(items: List[Any], page: int, page_size: int) -> tuple[List[Any], int, int, int]:
    """对列表分页，返回 (当前页数据, 当前页码, 总页数, 总条数)。"""

    total = len(items)
    if total == 0:
        return [], 1, 1, 0
    page_size = max(1, int(page_size or 1))
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(1, int(page or 1)), total_pages)
    start = (page - 1) * page_size
    return items[start : start + page_size], page, total_pages, total


def format_list_page_hint(label: str, page: int, total_pages: int, total: int) -> str:
    """生成分页列表页脚提示。"""

    if total_pages <= 1:
        return ""
    next_page = page + 1 if page < total_pages else 1
    return (
        f"\n\n第 {page}/{total_pages} 页，共 {total} 条。"
        f"翻页：/查看{label}列表 {next_page}"
        + (f"（末页可输入 /查看{label}列表 1 回到首页）" if page == total_pages else "")
    )


def summarize_rule_for_list(cfg: dict, index: int) -> str:
    """生成词条列表单行摘要（不含各条回复详情，避免消息过大）。"""

    regex_str = " [正则]" if cfg.get("regex", False) else ""
    entry_count = len(cfg.get("entries") or [])
    reply_hint = f" · {entry_count}条回复" if entry_count != 1 else ""
    keyword = str(cfg.get("keyword", "") or "")
    return f"【{index}】 {keyword}{regex_str}{describe_status_brief(cfg)}{reply_hint}"
