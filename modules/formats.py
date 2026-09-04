"""关键词词库 JSON、CSV 与 SQLite 交换格式。"""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List

from .store import KeywordsStore, SECTIONS

CSV_FIELDS = (
    "section",
    "keyword",
    "aliases",
    "tags",
    "regex",
    "enabled",
    "mode",
    "groups",
    "require_at_bot",
    "case_sensitive",
    "entries_json",
    "extra_json",
)

# Excel 单个单元格上限为 32,767 个字符。留出余量，避免导入时发生截断。
_CSV_CELL_MAX_CHARS = 30_000

_RULE_FIELDS = {
    "keyword",
    "aliases",
    "tags",
    "regex",
    "enabled",
    "mode",
    "groups",
    "require_at_bot",
    "case_sensitive",
    "entries",
}

_SECTION_TO_CSV = {
    "command_triggered": "关键词",
    "auto_detect": "检测词",
}
_CSV_TO_SECTION = {
    "关键词": "command_triggered",
    "检测词": "auto_detect",
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _parse_json(value: Any, default: Any) -> Any:
    text = str(value or "").strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"无效 JSON 字段: {text[:80]}") from exc


def _parse_bool(value: Any, default: bool = False) -> bool:
    text = str(value or "").strip().casefold()
    if not text:
        return default
    if text in {"1", "true", "yes", "on", "是"}:
        return True
    if text in {"0", "false", "no", "off", "否"}:
        return False
    raise ValueError(f"无法识别布尔值: {value}")


def _parse_list_field(value: Any, *, separators: tuple[str, ...]) -> List[str]:
    """解析当前 CSV 约定的简单列表字段。"""

    text = str(value or "")
    if not text:
        return []
    pattern = "|".join(re.escape(separator) for separator in separators)
    items = re.split(pattern, text)
    result: List[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item or "").strip()
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            result.append(text)
    return result


def _iter_rules(data: Dict[str, List[dict]]) -> Iterable[tuple[str, int, dict]]:
    for section in SECTIONS:
        for position, rule in enumerate(data.get(section, [])):
            if isinstance(rule, dict):
                yield section, position, rule


def _rule_to_row(section: str, rule: dict, *, include_entries: bool = True) -> dict:
    extra = {key: value for key, value in rule.items() if key not in _RULE_FIELDS}
    row = {
        "section": _SECTION_TO_CSV.get(section, section),
        "keyword": str(rule.get("keyword") or ""),
        "aliases": "\n".join(str(alias).strip() for alias in (rule.get("aliases") or []) if str(alias).strip()),
        "tags": ",".join(str(tag).strip() for tag in (rule.get("tags") or []) if str(tag).strip()),
        "regex": "true" if rule.get("regex") else "false",
        "enabled": "true" if rule.get("enabled", True) else "false",
        "mode": str(rule.get("mode") or "whitelist"),
        "groups": _json(rule.get("groups") or []),
        "require_at_bot": "true" if rule.get("require_at_bot") else "false",
        "case_sensitive": "true" if rule.get("case_sensitive") else "false",
        "extra_json": _json(extra),
    }
    if include_entries:
        row["entries_json"] = _json(rule.get("entries") or [])
    return row


def _entries_json_from_row(row: dict) -> str:
    """读取旧版单列或新版分片 entries_json。"""

    if "entries_json" in row and row.get("entries_json") is not None:
        return str(row.get("entries_json") or "")
    pieces = []
    index = 1
    while True:
        key = f"entries_json_{index}"
        if key not in row:
            break
        pieces.append(str(row.get(key) or ""))
        index += 1
    return "".join(pieces)


def _row_to_rule(row: dict) -> tuple[str, dict]:
    section = _CSV_TO_SECTION.get(str(row.get("section") or ""), "")
    if section not in SECTIONS:
        raise ValueError(f"未知词条类型: {section}")
    extra = _parse_json(row.get("extra_json"), {})
    if not isinstance(extra, dict):
        raise ValueError("extra_json 必须是 JSON 对象")
    rule = dict(extra)
    rule.update(
        {
            "keyword": str(row.get("keyword") or "").strip(),
            "aliases": _parse_list_field(row.get("aliases"), separators=("\n",)),
            "tags": _parse_list_field(row.get("tags"), separators=(",",)),
            "regex": _parse_bool(row.get("regex")),
            "enabled": _parse_bool(row.get("enabled"), True),
            "mode": str(row.get("mode") or "whitelist").strip(),
            "groups": _parse_json(row.get("groups"), []),
            "require_at_bot": _parse_bool(row.get("require_at_bot")),
            "entries": _parse_json(_entries_json_from_row(row), []),
        }
    )
    if section == "auto_detect":
        rule["case_sensitive"] = _parse_bool(row.get("case_sensitive"))
    return section, rule


def export_json(data: Any) -> bytes:
    normalized = KeywordsStore.normalize(data)
    return json.dumps(normalized, ensure_ascii=False, indent=2).encode("utf-8")


def import_json(raw: bytes) -> Dict[str, List[dict]]:
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("JSON 文件编码或结构无效") from exc
    return KeywordsStore.normalize(data)


def export_csv(data: Any) -> bytes:
    normalized = KeywordsStore.normalize(data)
    rows = [_rule_to_row(section, rule, include_entries=False) for section, _, rule in _iter_rules(normalized)]
    entries_values = [_json(rule.get("entries") or []) for _, _, rule in _iter_rules(normalized)]
    entry_columns = max(
        1,
        max(
            ((len(value) + _CSV_CELL_MAX_CHARS - 1) // _CSV_CELL_MAX_CHARS for value in entries_values),
            default=0,
        ),
    )
    fieldnames = [field for field in CSV_FIELDS if field != "entries_json"]
    entries_at = fieldnames.index("extra_json")
    fieldnames[entries_at:entries_at] = [f"entries_json_{index}" for index in range(1, entry_columns + 1)]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for row, entries_json in zip(rows, entries_values):
        for index in range(entry_columns):
            start = index * _CSV_CELL_MAX_CHARS
            row[f"entries_json_{index + 1}"] = entries_json[start : start + _CSV_CELL_MAX_CHARS]
        writer.writerow(row)
    return output.getvalue().encode("utf-8-sig")


def import_csv(raw: bytes) -> Dict[str, List[dict]]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV 文件须使用 UTF-8 编码") from exc
    result: Dict[str, List[dict]] = {section: [] for section in SECTIONS}
    reader = csv.DictReader(io.StringIO(text, newline=""))
    required_fields = {"section", "keyword", "aliases", "tags", "entries_json_1"}
    if not reader.fieldnames or not required_fields.issubset(reader.fieldnames):
        missing = sorted(required_fields - set(reader.fieldnames or []))
        raise ValueError(f"CSV 缺少必要表头: {', '.join(missing)}")
    entry_columns = sorted(
        int(name.removeprefix("entries_json_"))
        for name in reader.fieldnames
        if name.startswith("entries_json_") and name.removeprefix("entries_json_").isdigit()
    )
    if entry_columns != list(range(1, len(entry_columns) + 1)):
        raise ValueError("CSV 的 entries_json_N 列必须从 entries_json_1 连续编号")
    expected_fields = {
        field for field in CSV_FIELDS if field != "entries_json"
    } | {f"entries_json_{index}" for index in entry_columns}
    unexpected = sorted(set(reader.fieldnames) - expected_fields)
    missing = sorted(expected_fields - set(reader.fieldnames))
    if unexpected or missing:
        details = []
        if missing:
            details.append(f"缺少: {', '.join(missing)}")
        if unexpected:
            details.append(f"未知: {', '.join(unexpected)}")
        raise ValueError(f"CSV 表头不符合预期（{'；'.join(details)}）")
    for line_number, row in enumerate(reader, start=2):
        try:
            section, rule = _row_to_rule(row)
        except ValueError as exc:
            raise ValueError(f"CSV 第 {line_number} 行: {exc}") from exc
        if rule["keyword"]:
            result[section].append(rule)
    return KeywordsStore.normalize(result)


def export_database(data: Any) -> bytes:
    normalized = KeywordsStore.normalize(data)
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "keywords.db"
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                """CREATE TABLE rules (
                    section TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    keyword TEXT NOT NULL,
                    aliases TEXT NOT NULL,
                    tags TEXT NOT NULL,
                    regex TEXT NOT NULL,
                    enabled TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    groups_json TEXT NOT NULL,
                    require_at_bot TEXT NOT NULL,
                    case_sensitive TEXT NOT NULL,
                    entries_json TEXT NOT NULL,
                    extra_json TEXT NOT NULL,
                    PRIMARY KEY (section, position)
                )"""
            )
            connection.execute("CREATE INDEX idx_rules_keyword ON rules(keyword)")
            for section, position, rule in _iter_rules(normalized):
                row = _rule_to_row(section, rule)
                connection.execute(
                    "INSERT INTO rules VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        section,
                        position,
                        row["keyword"],
                        row["aliases"],
                        row["tags"],
                        row["regex"],
                        row["enabled"],
                        row["mode"],
                        row["groups"],
                        row["require_at_bot"],
                        row["case_sensitive"],
                        row["entries_json"],
                        row["extra_json"],
                    ),
                )
            connection.commit()
        finally:
            connection.close()
        return path.read_bytes()


def import_database(raw: bytes) -> Dict[str, List[dict]]:
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "keywords.db"
        path.write_bytes(raw)
        try:
            connection = sqlite3.connect(path)
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """SELECT section, keyword, aliases, tags, regex, enabled, mode,
                          groups_json AS groups, require_at_bot, case_sensitive,
                          entries_json, extra_json
                   FROM rules ORDER BY section, position"""
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise ValueError("SQLite 词库无效或缺少 rules 表") from exc
        finally:
            if "connection" in locals():
                connection.close()
    result: Dict[str, List[dict]] = {section: [] for section in SECTIONS}
    for row in rows:
        section, rule = _row_to_rule(dict(row))
        if rule["keyword"]:
            result[section].append(rule)
    return KeywordsStore.normalize(result)


def export_data(data: Any, format_name: str) -> bytes:
    exporters = {"json": export_json, "csv": export_csv, "database": export_database, "db": export_database}
    exporter = exporters.get(str(format_name or "").casefold())
    if exporter is None:
        raise ValueError(f"不支持的导出格式: {format_name}")
    return exporter(data)


def import_data(raw: bytes, format_name: str) -> Dict[str, List[dict]]:
    importers = {"json": import_json, "csv": import_csv, "database": import_database, "db": import_database}
    importer = importers.get(str(format_name or "").casefold())
    if importer is None:
        raise ValueError(f"不支持的导入格式: {format_name}")
    return importer(raw)