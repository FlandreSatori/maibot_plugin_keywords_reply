"""合并回复内容相同的词条为别名（CLI）。

用法::

    python tools/merge_duplicate_rules.py --data-file ".../keywords.json"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.merge_rules import merge_keywords_file  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="合并回复相同的关键词/检测词为别名")
    parser.add_argument(
        "--data-file",
        default=str(ROOT.parent.parent / "data" / "plugins" / "maibot_plugin.keywords_reply" / "keywords.json"),
        help="keywords.json 路径",
    )
    args = parser.parse_args()
    result = merge_keywords_file(Path(args.data_file))
    print(f"backup -> {result['backup']}")
    for section, stats in (result.get("report") or {}).items():
        print(
            f"{section}: {stats['before']} -> {stats['after']} "
            f"(merged_groups={stats['groups_merged']}, removed={stats['rules_removed']}, "
            f"aliases_added≈{stats['aliases_added']})"
        )
        for ex in stats.get("examples") or []:
            more = "" if ex["alias_total"] <= 12 else f" ...(+{ex['alias_total'] - 12})"
            print(f"  e.g. keep={ex['keep']!r} aliases={ex['aliases']}{more} from={ex['merged_from']}")
    print(f"wrote {result['path']}")


if __name__ == "__main__":
    main()
