"""合并回复内容相同的词条（关键词并入 aliases）。"""

from __future__ import annotations

import json
import shutil
from collections import defaultdict
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

SECTIONS = ("command_triggered", "auto_detect")


def _canon(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _canon(value[k]) for k in sorted(value.keys())}
    if isinstance(value, list):
        return [_canon(x) for x in value]
    if isinstance(value, str):
        return value.strip()
    return value


def entries_fingerprint(entries: Any) -> str:
    return json.dumps(_canon(entries if isinstance(entries, list) else []), ensure_ascii=False, separators=(",", ":"))


def rule_fingerprint(rule: dict, section: str) -> tuple:
    """仅在行为一致（除关键词/别名外）时合并。"""

    groups = rule.get("groups") or []
    if not isinstance(groups, list):
        groups = []
    groups_key = tuple(sorted(str(g).strip() for g in groups if str(g).strip()))
    fp: tuple = (
        entries_fingerprint(rule.get("entries")),
        bool(rule.get("regex", False)),
        bool(rule.get("require_at_bot", False)),
        bool(rule.get("enabled", True)),
        str(rule.get("mode") or "whitelist"),
        groups_key,
    )
    if section == "auto_detect":
        fp = fp + (bool(rule.get("case_sensitive", False)),)
    return fp


def collect_triggers(rule: dict) -> List[str]:
    primary = str(rule.get("keyword") or "").strip()
    raw = rule.get("aliases") or []
    if isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, list):
        items = raw
    else:
        items = []
    out: List[str] = []
    seen: set[str] = set()
    for item in [primary, *items]:
        text = str(item or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def merge_section(rules: List[dict], section: str) -> Tuple[List[dict], Dict[str, Any]]:
    buckets: Dict[tuple, List[dict]] = defaultdict(list)
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        buckets[rule_fingerprint(rule, section)].append(rule)

    merged: List[dict] = []
    stats: Dict[str, Any] = {
        "before": len(rules),
        "after": 0,
        "groups_merged": 0,
        "rules_removed": 0,
        "aliases_added": 0,
        "examples": [],
    }

    seen_fp: set[tuple] = set()
    ordered_fps: List[tuple] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        fp = rule_fingerprint(rule, section)
        if fp not in seen_fp:
            seen_fp.add(fp)
            ordered_fps.append(fp)

    for fp in ordered_fps:
        group = buckets[fp]
        if len(group) == 1:
            rule = deepcopy(group[0])
            triggers = collect_triggers(rule)
            rule["keyword"] = triggers[0] if triggers else str(rule.get("keyword") or "")
            rule["aliases"] = triggers[1:]
            merged.append(rule)
            continue

        stats["groups_merged"] += 1
        stats["rules_removed"] += len(group) - 1

        base = deepcopy(group[0])
        all_triggers: List[str] = []
        seen: set[str] = set()
        for rule in group:
            for trigger in collect_triggers(rule):
                key = trigger.casefold()
                if key in seen:
                    continue
                seen.add(key)
                all_triggers.append(trigger)

        primary = all_triggers[0] if all_triggers else str(base.get("keyword") or "")
        aliases = all_triggers[1:]
        old_triggers = collect_triggers(base)
        old_alias_count = max(0, len(old_triggers) - 1)
        stats["aliases_added"] += max(0, len(aliases) - old_alias_count)

        base["keyword"] = primary
        base["aliases"] = aliases
        merged.append(base)

        if len(stats["examples"]) < 8:
            stats["examples"].append(
                {
                    "keep": primary,
                    "aliases": aliases[:12],
                    "alias_total": len(aliases),
                    "merged_from": len(group),
                }
            )

    stats["after"] = len(merged)
    return merged, stats


def merge_keywords_data(data: Any) -> Tuple[Dict[str, List[dict]], Dict[str, Any]]:
    """合并两段词库，返回 (新数据, 分节统计)。"""

    if not isinstance(data, dict):
        data = {"command_triggered": [], "auto_detect": []}
    out: Dict[str, List[dict]] = {
        "command_triggered": list(data.get("command_triggered") or []),
        "auto_detect": list(data.get("auto_detect") or []),
    }
    report: Dict[str, Any] = {}
    for section in SECTIONS:
        rules = out.get(section) or []
        if not isinstance(rules, list):
            rules = []
        merged, stats = merge_section(rules, section)
        out[section] = merged
        report[section] = stats
    return out, report


def backup_keywords_file(data_file: Path) -> Path:
    """复制 keywords.json 为带时间戳的备份文件。"""

    data_file = Path(data_file)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = data_file.with_name(f"keywords.json.bak_merge_{stamp}")
    shutil.copy2(data_file, backup)
    return backup


def merge_keywords_file(data_file: Path) -> Dict[str, Any]:
    """备份并原地合并 keywords.json，返回结果摘要。"""

    data_file = Path(data_file)
    if not data_file.is_file():
        raise FileNotFoundError(f"词库文件不存在: {data_file}")

    backup = backup_keywords_file(data_file)
    raw = json.loads(data_file.read_text(encoding="utf-8"))
    merged, report = merge_keywords_data(raw)
    text = json.dumps(merged, ensure_ascii=False, indent=2) + "\n"
    tmp = data_file.with_suffix(".json.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(data_file)
    return {
        "ok": True,
        "backup": str(backup.resolve()),
        "path": str(data_file.resolve()),
        "report": report,
        "data": merged,
        "keyword_count": len(merged.get("command_triggered", [])),
        "detect_count": len(merged.get("auto_detect", [])),
    }
