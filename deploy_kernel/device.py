"""设备账本：已安装应用集合、执行计数、回执，独立 SQLite 文件。"""

import json
import sqlite3

from .center import OUTCOME_SUCCESS

_SCHEMA = """
CREATE TABLE IF NOT EXISTS apps (
    package_id TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS attempts (
    attempt_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    package_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    receipt TEXT NOT NULL
);
"""


class DeviceAgent:
    """设备角色，通过本机函数调用参与协议，不执行系统安装命令。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def execute(self, attempt_id, task_id, package_id, attempt_no, outcomes) -> dict:
        """执行一次投递。效果与回执在同一事务中持久化；
        重复 attempt_id 只返回原回执，不再次产生效果。"""
        row = self._db.execute(
            "SELECT receipt FROM attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if row is not None:
            return json.loads(row["receipt"])
        outcome = list(outcomes)[attempt_no - 1]
        receipt = {
            "attempt_id": attempt_id,
            "task_id": task_id,
            "attempt_no": attempt_no,
            "outcome": outcome,
        }
        with self._db:
            if outcome == OUTCOME_SUCCESS:
                self._db.execute(
                    "INSERT OR IGNORE INTO apps (package_id) VALUES (?)", (package_id,)
                )
            self._db.execute(
                "INSERT INTO attempts (attempt_id, task_id, package_id, attempt_no, receipt)"
                " VALUES (?, ?, ?, ?, ?)",
                (attempt_id, task_id, package_id, attempt_no, json.dumps(receipt)),
            )
        return receipt

    def execution_count(self, task_id=None) -> int:
        if task_id is None:
            row = self._db.execute("SELECT COUNT(*) c FROM attempts").fetchone()
        else:
            row = self._db.execute(
                "SELECT COUNT(*) c FROM attempts WHERE task_id = ?", (task_id,)
            ).fetchone()
        return row["c"]

    def installed_apps(self):
        rows = self._db.execute("SELECT package_id FROM apps ORDER BY package_id").fetchall()
        return [r["package_id"] for r in rows]

    def get_receipt(self, attempt_id):
        row = self._db.execute(
            "SELECT receipt FROM attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        return json.loads(row["receipt"]) if row is not None else None
