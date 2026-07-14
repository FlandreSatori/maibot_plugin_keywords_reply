"""关键词/检测词匹配引擎。

- ``command_triggered``：消息以触发词开头（触发词可含空格；其后可有参数）。
- ``auto_detect``：消息中包含触发词。
- 每条规则支持 ``keyword`` + ``aliases[]`` 多别名。
"""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("plugin.maibot_plugin.keywords_reply")


def rule_triggers(cfg: dict) -> List[str]:
    """返回规则的全部触发词（主词 + 别名），去重且保持顺序。"""

    primary = str(cfg.get("keyword", "") or "").strip()
    raw_aliases = cfg.get("aliases") or []
    if isinstance(raw_aliases, str):
        alias_items = [raw_aliases]
    elif isinstance(raw_aliases, list):
        alias_items = raw_aliases
    else:
        alias_items = []

    out: List[str] = []
    seen: set[str] = set()
    for item in [primary, *alias_items]:
        text = str(item or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def normalize_aliases(value: Any) -> List[str]:
    """把别名字段规范为去重字符串列表。"""

    if isinstance(value, str):
        parts = re.split(r"[|\n,，]+", value)
    elif isinstance(value, list):
        parts = value
    else:
        parts = []
    out: List[str] = []
    seen: set[str] = set()
    for item in parts:
        text = str(item or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def parse_trigger_field_and_body(args: str) -> tuple[List[str], str]:
    """解析命令参数中的首个触发词字段与剩余正文。

    支持 ``词 回复``、``\"含 空格\" 回复``。别名请用「添加别名」单独追加，不再用 ``|`` 串联。
    """

    s = str(args or "")
    n = len(s)
    i = 0
    while i < n and s[i].isspace():
        i += 1
    if i >= n:
        return [], ""

    if s[i] in "\"'":
        quote = s[i]
        i += 1
        start = i
        while i < n and s[i] != quote:
            i += 1
        field = s[start:i]
        if i < n and s[i] == quote:
            i += 1
    else:
        start = i
        while i < n and not s[i].isspace():
            i += 1
        field = s[start:i]

    while i < n and s[i].isspace():
        i += 1
    field = field.strip()
    body = s[i:].strip()
    if body and len(body) >= 2 and body[0] == body[-1] and body[0] in "\"'":
        body = body[1:-1].strip()
    return ([field] if field else []), body


def parse_selector_and_alias(args: str) -> tuple[str, str]:
    """解析 ``<序号/内容> <别名>``，别名可带空格或引号。"""

    fields, body = parse_trigger_field_and_body(args)
    selector = fields[0] if fields else ""
    return selector, body


def trigger_matches_command(
    text: str,
    trigger: str,
    *,
    case_sensitive: bool,
    is_regex: bool,
    compiled: Optional[re.Pattern[str]] = None,
) -> bool:
    """判断消息是否由该触发词作为命令词命中（允许后续参数）。"""

    text = str(text or "")
    trigger = str(trigger or "")
    if not text or not trigger:
        return False

    if is_regex:
        pattern = compiled
        if pattern is None:
            return False
        match = pattern.match(text)
        if match is None:
            return False
        end = match.end()
        return end == len(text) or text[end : end + 1].isspace()

    left = text if case_sensitive else text.casefold()
    right = trigger if case_sensitive else trigger.casefold()
    if left == right:
        return True
    return left.startswith(right + " ")


def trigger_matches_detect(
    text: str,
    trigger: str,
    *,
    case_sensitive: bool,
    is_regex: bool,
    compiled: Optional[re.Pattern[str]] = None,
) -> tuple[bool, bool]:
    """判断检测词是否命中，返回 (是否命中, 是否整句精确匹配)。"""

    text = str(text or "")
    trigger = str(trigger or "")
    if not text or not trigger:
        return False, False

    if is_regex:
        pattern = compiled
        if pattern is None:
            return False, False
        return bool(pattern.search(text)), False

    if case_sensitive:
        hay, needle = text, trigger
        exact = text == trigger
    else:
        hay, needle = text.casefold(), trigger.casefold()
        exact = text.casefold() == trigger.casefold()
    if needle not in hay:
        return False, False
    return True, exact


def pick_weighted_entry(entries: List[dict]) -> Optional[dict]:
    """按 weight 加权随机抽取一条 entry。"""

    if not entries:
        return None
    weights = []
    for entry in entries:
        try:
            w = int(entry.get("weight", 100) or 100)
        except (TypeError, ValueError):
            w = 100
        weights.append(max(0, w))
    if sum(weights) <= 0:
        return random.choice(entries)
    return random.choices(entries, weights=weights, k=1)[0]


def entry_passes_probability(entry: dict) -> bool:
    """按 probability（0-100）判定是否实际发送。"""

    try:
        p = int(entry.get("probability", 100) or 100)
    except (TypeError, ValueError):
        p = 100
    p = max(0, min(100, p))
    if p >= 100:
        return True
    if p <= 0:
        return False
    return random.randint(1, 100) <= p


def pick_entry_for_reply(entries: List[dict]) -> Optional[dict]:
    """加权抽取后再过概率门。"""

    picked = pick_weighted_entry(entries)
    if picked is None:
        return None
    if not entry_passes_probability(picked):
        return None
    return picked


def message_has_bot_mention(is_at: bool, is_mentioned: bool) -> bool:
    return bool(is_at or is_mentioned)


class Matcher:
    """关键词 / 检测词匹配器。"""

    def __init__(self, store: Any, get_settings: Callable[[], Dict[str, Any]]) -> None:
        self.store = store
        self.get_settings = get_settings
        self._last_triggered: Dict[str, float] = {}
        self._regex_cache: Dict[str, Optional[re.Pattern[str]]] = {}

    def _compiled(self, pattern: str) -> Optional[re.Pattern[str]]:
        if pattern in self._regex_cache:
            return self._regex_cache[pattern]
        try:
            compiled: Optional[re.Pattern[str]] = re.compile(pattern)
        except re.error:
            compiled = None
        self._regex_cache[pattern] = compiled
        return compiled

    def is_enabled_in_group(self, cfg: dict, group_id: str) -> bool:
        return self._group_allows(cfg, group_id)

    def _group_allows(self, cfg: dict, group_id: str) -> bool:
        if not cfg.get("enabled", True):
            return False
        mode = cfg.get("mode", "whitelist")
        groups = [str(g) for g in (cfg.get("groups") or [])]
        if mode == "blacklist":
            return group_id not in groups
        if groups:
            return group_id in groups
        if not group_id:
            return False
        return True

    def _should_forward(self, entries: List[dict], group_id: str) -> bool:
        del group_id
        settings = self.get_settings()
        return bool(settings.get("qq_forward_all_replies")) and len(entries) > 1

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
        settings = self.get_settings()
        case_sensitive = bool(settings.get("case_sensitive", False))

        candidates: List[tuple[int, dict, str]] = []
        for cfg in self.store.data.get("command_triggered", []):
            if not cfg.get("enabled", True):
                continue
            if cfg.get("require_at_bot") and not message_has_bot_mention(is_at, is_mentioned):
                continue
            if not self._group_allows(cfg, group_id):
                continue
            is_regex = bool(cfg.get("regex", False))
            for trigger in rule_triggers(cfg):
                compiled = self._compiled(trigger) if is_regex else None
                if not trigger_matches_command(
                    text,
                    trigger,
                    case_sensitive=case_sensitive,
                    is_regex=is_regex,
                    compiled=compiled,
                ):
                    continue
                candidates.append((len(trigger), cfg, trigger))

        if not candidates:
            return None
        candidates.sort(key=lambda item: (-item[0],))
        _, cfg, hit = candidates[0]
        entries = cfg.get("entries") or []
        if not entries:
            return None
        logger.info(f"关键词触发: {hit} (群: {group_id})")
        if self._should_forward(entries, group_id):
            return {"payload": list(entries), "rule": cfg, "trigger": hit}
        picked = pick_entry_for_reply(entries)
        if not picked:
            return None
        return {"payload": picked, "rule": cfg, "trigger": hit}

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
            if not cfg.get("enabled", True):
                continue
            if cfg.get("require_at_bot") and not message_has_bot_mention(is_at, is_mentioned):
                continue
            is_regex = bool(cfg.get("regex", False))
            hit = ""
            is_exact = False
            for trigger in rule_triggers(cfg):
                compiled = self._compiled(trigger) if is_regex else None
                matched, exact = trigger_matches_detect(
                    text,
                    trigger,
                    case_sensitive=global_case_sensitive,
                    is_regex=is_regex,
                    compiled=compiled,
                )
                if matched:
                    hit = trigger
                    is_exact = exact
                    break
            if not hit:
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

            logger.info(f"检测词触发: {hit} (群: {group_id})")
            if self._should_forward(entries, group_id):
                return {"payload": list(entries), "rule": cfg, "trigger": hit}
            picked = pick_entry_for_reply(entries)
            if not picked:
                return None
            return {"payload": picked, "rule": cfg, "trigger": hit}
        return None


# ─── 序号/内容查找与群聊开关（供命令处理复用） ────────────────────


def find_indices(data: List[dict], param: str, case_sensitive: bool = False) -> List[int]:
    """支持 ``1,2-5`` 形式的序号，或按关键词/别名内容匹配，返回索引列表。"""

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
    needle = param if case_sensitive else param.casefold()
    for i, cfg in enumerate(data):
        for trigger in rule_triggers(cfg):
            if cfg.get("regex", False) or case_sensitive:
                equal = trigger == param
            else:
                equal = trigger.casefold() == needle
            if equal:
                matched.append(i)
                break
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
    triggers = rule_triggers(cfg)
    keyword = triggers[0] if triggers else str(cfg.get("keyword", "") or "")
    alias_hint = ""
    if len(triggers) > 1:
        alias_hint = f" (+{len(triggers) - 1}别名)"
    return f"【{index}】 {keyword}{alias_hint}{regex_str}{describe_status_brief(cfg)}{reply_hint}"
