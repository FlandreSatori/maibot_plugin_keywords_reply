#!/usr/bin/env python3
"""OhData / Outer Heaven 词库 SQLite 读写工具。

默认数据库路径：``ohdata/ohdata/database.db``（相对 Maibot 工作区根目录）。

用法::

    python tools/ohdata_db.py inspect
    python tools/ohdata_db.py list --table outerheaven --limit 5
    python tools/ohdata_db.py get --id 2
    python tools/ohdata_db.py write-test
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = WORKSPACE_ROOT / "ohdata" / "ohdata" / "database.db"

TABLES = ("manager", "shenhe", "custom", "outerheaven")


@dataclass
class OuterHeavenRow:
    id: int
    rule: str
    priority: int
    question: str
    answer: str
    probability: int
    condition: int
    at: str
    lock: str
    change: str


class OhDataDatabase:
    """对 database.db 的只读/读写封装。"""

    def __init__(self, db_path: Path | str = DEFAULT_DB) -> None:
        self.db_path = Path(db_path)
        if not self.db_path.exists():
            raise FileNotFoundError(f"数据库不存在: {self.db_path}")

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def inspect(self) -> Dict[str, Any]:
        with self.connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name")
            tables = []
            for name, sql in cur.fetchall():
                cur.execute(f'PRAGMA table_info("{name}")')
                cols = [dict(r) for r in cur.fetchall()]
                cur.execute(f'SELECT COUNT(*) AS c FROM "{name}"')
                count = cur.fetchone()["c"]
                tables.append({"name": name, "sql": sql, "columns": cols, "count": count})
            return {"path": str(self.db_path), "tables": tables}

    def list_rows(self, table: str, *, limit: int = 20, offset: int = 0) -> List[Dict[str, Any]]:
        if table not in TABLES:
            raise ValueError(f"未知表: {table}")
        with self.connect() as conn:
            cur = conn.cursor()
            cur.execute(f'SELECT * FROM "{table}" ORDER BY id LIMIT ? OFFSET ?', (limit, offset))
            return [dict(r) for r in cur.fetchall()]

    def get_outerheaven(self, row_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM outerheaven WHERE id = ?", (row_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def count_outerheaven_by_rule(self) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT rule, COUNT(*) AS count FROM outerheaven GROUP BY rule ORDER BY count DESC"
            )
            return [dict(r) for r in cur.fetchall()]

    def insert_outerheaven(
        self,
        *,
        rule: str,
        question: str,
        answer: str,
        priority: int = 1,
        probability: int = 100,
        condition: int = 0,
        at: str = "假",
        lock: str = "假",
        change: str = "假",
    ) -> int:
        with self.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO outerheaven
                    (rule, priority, question, answer, probability, condition, at, lock, change)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (rule, priority, question, answer, probability, condition, at, lock, change),
            )
            conn.commit()
            return int(cur.lastrowid)

    def update_outerheaven(self, row_id: int, **fields: Any) -> bool:
        allowed = {
            "rule",
            "priority",
            "question",
            "answer",
            "probability",
            "condition",
            "at",
            "lock",
            "change",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        assignments = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [row_id]
        with self.connect() as conn:
            cur = conn.cursor()
            cur.execute(f"UPDATE outerheaven SET {assignments} WHERE id = ?", values)
            conn.commit()
            return cur.rowcount > 0

    def delete_outerheaven(self, row_id: int) -> bool:
        with self.connect() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM outerheaven WHERE id = ?", (row_id,))
            conn.commit()
            return cur.rowcount > 0

    def upsert_manager(self, qq: int, call: str) -> int:
        with self.connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM manager WHERE qq = ?", (qq,))
            row = cur.fetchone()
            if row:
                cur.execute("UPDATE manager SET call = ? WHERE id = ?", (call, row["id"]))
                conn.commit()
                return int(row["id"])
            cur.execute("INSERT INTO manager (qq, call) VALUES (?, ?)", (qq, call))
            conn.commit()
            return int(cur.lastrowid)

    def list_managers(self) -> List[Dict[str, Any]]:
        return self.list_rows("manager", limit=10_000)

    def list_pending_reviews(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self.list_rows("shenhe", limit=limit)


def run_write_test(db: OhDataDatabase) -> Dict[str, Any]:
    """插入一条测试记录，更新后再删除，验证读写链路。"""
    marker = "__maibot_import_test__"
    new_id = db.insert_outerheaven(
        rule="关键词匹配",
        question=marker,
        answer="test",
        probability=1,
    )
    row = db.get_outerheaven(new_id)
    updated = db.update_outerheaven(new_id, answer="updated", probability=2)
    row2 = db.get_outerheaven(new_id)
    deleted = db.delete_outerheaven(new_id)
    gone = db.get_outerheaven(new_id)
    return {
        "inserted_id": new_id,
        "inserted_row": row,
        "updated": updated,
        "updated_row": row2,
        "deleted": deleted,
        "still_exists": gone is not None,
        "ok": bool(row and updated and row2 and row2["answer"] == "updated" and deleted and gone is None),
    }


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="OhData database.db 读写工具")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="database.db 路径")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("inspect", help="查看表结构与行数")
    p_list = sub.add_parser("list", help="列出表数据")
    p_list.add_argument("--table", default="outerheaven", choices=TABLES)
    p_list.add_argument("--limit", type=int, default=10)
    p_list.add_argument("--offset", type=int, default=0)

    p_get = sub.add_parser("get", help="按 id 读取 outerheaven")
    p_get.add_argument("--id", type=int, required=True)

    sub.add_parser("stats", help="outerheaven 规则分布")
    sub.add_parser("write-test", help="执行读写自检（会临时插入并删除一条记录）")

    args = parser.parse_args(argv)
    db = OhDataDatabase(args.db)

    if args.cmd == "inspect":
        _print_json(db.inspect())
    elif args.cmd == "list":
        _print_json(db.list_rows(args.table, limit=args.limit, offset=args.offset))
    elif args.cmd == "get":
        row = db.get_outerheaven(args.id)
        if not row:
            print(f"未找到 id={args.id}")
            return 1
        _print_json(row)
    elif args.cmd == "stats":
        _print_json(db.count_outerheaven_by_rule())
    elif args.cmd == "write-test":
        _print_json(run_write_test(db))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
