"""批量规范化 keywords.json（补全字段、清洗占位符、旧格式转 parts[]）。

用法::

    python tools/migrate_to_parts.py [keywords.json 路径]

默认读取插件数据目录下的 keywords.json。
加 ``--dry-run`` 只统计不写入；加 ``--keep-legacy`` 迁移后保留 text/images 等旧字段。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.store import KeywordsStore  # noqa: E402

DEFAULT_DATA_FILES = [
    ROOT / "keywords.json",
    ROOT.parents[1] / "data" / "plugins" / "maibot_plugin.keywords_reply" / "keywords.json",
]


def resolve_data_file(path: str | None) -> Path:
    if path:
        target = Path(path).expanduser().resolve()
        if not target.exists():
            raise SystemExit(f"文件不存在: {target}")
        return target
    for candidate in DEFAULT_DATA_FILES:
        if candidate.exists():
            return candidate
    raise SystemExit("未找到 keywords.json，请通过参数指定路径。")


def count_entries(data: dict) -> int:
    total = 0
    for section in ("command_triggered", "auto_detect"):
        for rule in data.get(section, []) or []:
            if isinstance(rule, dict):
                total += len(rule.get("entries", []) or [])
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="批量规范化 keywords.json（字段补全 + 旧格式迁移）")
    parser.add_argument("data_file", nargs="?", help="keywords.json 路径")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写回文件")
    parser.add_argument("--keep-legacy", action="store_true", help="迁移后保留 text/images 等旧字段")
    args = parser.parse_args()

    data_file = resolve_data_file(args.data_file)
    raw = json.loads(data_file.read_text(encoding="utf-8"))
    before = json.dumps(raw, ensure_ascii=False, sort_keys=True)
    data = KeywordsStore.normalize(raw)

    migrated_rules = 0
    migrated_entries = 0
    for section in ("command_triggered", "auto_detect"):
        for rule in data.get(section, []):
            if not isinstance(rule, dict):
                continue
            rule_changed = False
            for entry in rule.get("entries", []):
                if not isinstance(entry, dict):
                    continue
                if KeywordsStore.migrate_entry_to_parts(entry, clear_legacy=not args.keep_legacy):
                    migrated_entries += 1
                    rule_changed = True
            if rule_changed:
                migrated_rules += 1

    after = json.dumps(data, ensure_ascii=False, sort_keys=True)
    changed = before != after

    print(f"数据文件: {data_file}")
    print(f"词条总数: {count_entries(data)} 个 entry")
    print(f"迁移为 parts[]: {migrated_rules} 条规则 / {migrated_entries} 个 entry")
    print(f"数据有变化: {'是' if changed else '否'}")
    if args.dry_run:
        print("dry-run 模式，未写入。")
        return

    if not changed:
        print("已是规范化格式，无需写入。")
        return

    payload = json.dumps(data, ensure_ascii=False, indent=2)
    data_file.write_text(payload + "\n", encoding="utf-8")
    print("已保存。请在群聊执行 /重载词库 使插件读取最新数据。")


if __name__ == "__main__":
    main()