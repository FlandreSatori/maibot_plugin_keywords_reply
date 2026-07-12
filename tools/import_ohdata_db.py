#!/usr/bin/env python3
"""将 OhData ``database.db`` 的 ``outerheaven`` 表导入为 MaiBot 关键词插件 ``keywords.json``。

映射规则（测试阶段，可按需调整）::

- ``完整匹配``  -> ``command_triggered``（整条消息精确匹配）
- ``关键词匹配`` -> ``auto_detect``（消息包含即触发）
- ``正则表达式`` -> ``auto_detect`` + ``regex=true``
- ``answer`` 中 ``|`` 分隔多条回复；``&`` 表示同一条回复内的多段组合
- `probability` -> entry ``probability``（`|` 拆出的多条回复 ``weight`` 固定为 100）
- ``at=真`` -> ``require_at_bot=true``
- CQ 码转为 images/records/faces 文件名引用；``{CQ:time,...}`` 条件标签会剥离

用法::

    python tools/import_ohdata_db.py
    python tools/import_ohdata_db.py --db ../../ohdata/ohdata/database.db --out ../keywords.json
"""

from __future__ import annotations

import argparse
import configparser
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
TOOLS = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from modules.store import KeywordsStore  # noqa: E402
from ohdata_db import DEFAULT_DB, OhDataDatabase  # noqa: E402

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / "keywords.json"
DEFAULT_INI = WORKSPACE_ROOT / "ohdata" / "ohdata" / "分群.ini"

_CQ_PATTERN = re.compile(r"\[CQ:([a-z_]+)(?:,([^\]]*))?\]", re.IGNORECASE)
_TIME_COND_PATTERN = re.compile(r"\{CQ:time,[^\}]+\}", re.IGNORECASE)
_KV_PATTERN = re.compile(r"([a-z_]+)=([^,]+)", re.IGNORECASE)

RULE_COMMAND = "完整匹配"
RULE_DETECT = "关键词匹配"
RULE_REGEX = "正则表达式"

SKIP_QUESTION_PREFIXES = (
    "^(拉黑",
    "^修改问题",
    "^添加问题",
    "^解锁问题",
    "^锁定问题",
    "^删除问题",
    "^查询问题",
)


def parse_cq_params(raw: str) -> Dict[str, str]:
    params: Dict[str, str] = {}
    if not raw:
        return params
    for m in _KV_PATTERN.finditer(raw):
        params[m.group(1).lower()] = m.group(2)
    return params


def strip_time_conditions(text: str) -> str:
    return _TIME_COND_PATTERN.sub("", text or "").strip()


def parse_answer_variant(raw: str, probability: int) -> dict:
    entry = KeywordsStore.empty_entry()
    entry["weight"] = 100
    entry["probability"] = max(0, min(100, int(probability or 100)))

    text = strip_time_conditions(raw)
    if not text:
        return entry

    parts: List[dict] = []
    segments = [p.strip() for p in text.split("&") if p.strip()]

    for part in segments:
        cursor = 0
        for m in _CQ_PATTERN.finditer(part):
            if m.start() > cursor:
                chunk = part[cursor : m.start()].strip()
                if chunk:
                    parts.append({"type": "text", "text": chunk})
            cq_type = m.group(1).lower()
            params = parse_cq_params(m.group(2) or "")
            if cq_type == "image":
                file_name = params.get("file", "").strip()
                if file_name:
                    parts.append({"type": "image", "file": file_name})
            elif cq_type in {"record", "voice"}:
                file_name = params.get("file", "").strip()
                if file_name:
                    parts.append({"type": "voice", "file": file_name})
            elif cq_type == "face":
                face_id = params.get("id", "").strip()
                if face_id.isdigit():
                    parts.append({"type": "face", "id": int(face_id)})
            elif cq_type == "at":
                user_id = params.get("qq", params.get("id", "")).strip()
                if user_id:
                    parts.append(
                        {
                            "type": "at",
                            "user_id": user_id,
                            "nickname": params.get("name", ""),
                            "all": False,
                        }
                    )
            cursor = m.end()
        if cursor < len(part):
            tail = part[cursor:].strip()
            if tail:
                parts.append({"type": "text", "text": tail})

    if parts:
        entry["parts"] = parts
    return entry


def entry_has_payload(entry: dict) -> bool:
    return KeywordsStore.entry_has_payload(entry)


def parse_answer_field(answer: str, probability: int) -> List[dict]:
    variants = [v.strip() for v in (answer or "").split("|") if v.strip()]
    entries: List[dict] = []
    for variant in variants:
        entry = parse_answer_variant(variant, probability)
        if entry_has_payload(entry):
            entries.append(entry)
    return entries


def should_skip_row(row: dict) -> bool:
    question = (row.get("question") or "").strip()
    answer = (row.get("answer") or "").strip()
    if not question:
        return True
    if row.get("rule") == RULE_REGEX and not answer:
        return True
    if question.startswith(SKIP_QUESTION_PREFIXES):
        return True
    return False


def map_section(rule: str, regex: bool) -> str:
    if rule == RULE_COMMAND and not regex:
        return "command_triggered"
    return "auto_detect"


def load_enabled_groups(ini_path: Path) -> List[str]:
    if not ini_path.exists():
        return []
    parser = configparser.ConfigParser()
    parser.read(ini_path, encoding="gb18030")
    if not parser.has_section("分群"):
        return []
    groups = []
    for group_id, status in parser.items("分群"):
        if status.strip() in {"开", "真", "true", "1", "on"}:
            groups.append(group_id.strip())
    return groups


def convert_row(row: dict, groups: List[str]) -> Optional[dict]:
    if should_skip_row(row):
        return None

    rule_type = (row.get("rule") or "").strip()
    regex = rule_type == RULE_REGEX
    question = (row.get("question") or "").strip()
    probability = int(row.get("probability") or 100)
    entries = parse_answer_field(row.get("answer") or "", probability)
    if not entries:
        return None

    require_at = str(row.get("at") or "").strip() in {"真", "true", "1", "yes"}
    cfg = {
        "keyword": question,
        "regex": regex,
        "enabled": True,
        "require_at_bot": require_at,
        "entries": entries,
    }

    if groups:
        cfg["mode"] = "whitelist"
        cfg["groups"] = groups
    else:
        cfg["mode"] = "blacklist"
        cfg["groups"] = []

    section = map_section(rule_type, regex)
    if section == "auto_detect":
        cfg["case_sensitive"] = False
    return {"section": section, "rule": cfg}


def import_database(
    db_path: Path,
    *,
    out_path: Path,
    ini_path: Path,
    merge: bool = False,
) -> Dict[str, Any]:
    db = OhDataDatabase(db_path)
    groups = load_enabled_groups(ini_path)

    result = {"command_triggered": [], "auto_detect": []}
    stats = {
        "total_rows": 0,
        "imported": 0,
        "skipped": 0,
        "by_rule": {},
        "groups": groups,
    }

    rows = db.list_rows("outerheaven", limit=1_000_000)
    stats["total_rows"] = len(rows)

    for row in rows:
        rule_name = row.get("rule") or "unknown"
        stats["by_rule"][rule_name] = stats["by_rule"].get(rule_name, 0) + 1
        converted = convert_row(row, groups)
        if not converted:
            stats["skipped"] += 1
            continue
        result[converted["section"]].append(converted["rule"])
        stats["imported"] += 1

    if merge and out_path.exists():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception:
            existing = {"command_triggered": [], "auto_detect": []}
        result["command_triggered"] = existing.get("command_triggered", []) + result["command_triggered"]
        result["auto_detect"] = existing.get("auto_detect", []) + result["auto_detect"]

    normalized = KeywordsStore.normalize(result)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    stats["output"] = str(out_path)
    stats["command_triggered"] = len(normalized["command_triggered"])
    stats["auto_detect"] = len(normalized["auto_detect"])
    return stats


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="导入 OhData database.db 到 keywords.json")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--ini", default=str(DEFAULT_INI))
    parser.add_argument("--merge", action="store_true", help="与已有 keywords.json 合并")
    parser.add_argument(
        "--deploy",
        action="store_true",
        help="导入后同时复制到 maibot_plugin.keywords_reply/keywords.json（MaiBot 数据目录）",
    )
    args = parser.parse_args(argv)

    stats = import_database(
        Path(args.db),
        out_path=Path(args.out),
        ini_path=Path(args.ini),
        merge=args.merge,
    )
    if args.deploy:
        import shutil

        deploy_target = WORKSPACE_ROOT / "maibot_plugin.keywords_reply" / "keywords.json"
        deploy_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(args.out), deploy_target)
        stats["deployed_to"] = str(deploy_target)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
