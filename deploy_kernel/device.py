"""设备侧账本：持久化已安装应用集合、执行计数与回执。"""

import sqlite3

from .pause import pausepoint, DEVICE_BEFORE_COMMIT, DEVICE_AFTER_COMMIT

_SCHEMA = """
CREATE TABLE IF NOT EXISTS installed_apps (
    package_id TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS executions (
    task_id TEXT PRIMARY KEY,
    count   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS receipts (
    attempt_id TEXT PRIMARY KEY,
    task_id    TEXT NOT NULL,
    package_id TEXT NOT NULL,
    outcome    TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
"""


class DeviceLedger:
    """每台设备一个独立的 SQLite 文件。"""

    def __init__(self, path: str, device_id: str) -> None:
        self.device_id = device_id
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def deliver(self, attempt_id: str, task_id: str, package_id: str,
                outcome: str, now: int) -> dict:
        """执行一次投递。

        效果（执行计数、已安装集合）与回执在同一事务中持久化；
        重复收到同一 attempt_id 只返回原回执，不再产生效果。
        """
        existing = self.receipt_for(attempt_id)
        if existing is not None:
            return existing
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO receipts(attempt_id, task_id, package_id, outcome, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (attempt_id, task_id, package_id, outcome, now),
            )
            conn.execute(
                "INSERT INTO executions(task_id, count) VALUES (?, 1)"
                " ON CONFLICT(task_id) DO UPDATE SET count = count + 1",
                (task_id,),
            )
            if outcome == "success":
                conn.execute(
                    "INSERT OR IGNORE INTO installed_apps(package_id) VALUES (?)",
                    (package_id,),
                )
            pausepoint(DEVICE_BEFORE_COMMIT)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        pausepoint(DEVICE_AFTER_COMMIT)
        return self.receipt_for(attempt_id)

    def execution_count(self, task_id: str) -> int:
        row = self._conn.execute(
            "SELECT count FROM executions WHERE task_id = ?", (task_id,)
        ).fetchone()
        return row["count"] if row else 0

    def installed_apps(self) -> list:
        rows = self._conn.execute(
            "SELECT package_id FROM installed_apps ORDER BY package_id"
        ).fetchall()
        return [r["package_id"] for r in rows]

    def receipt_for(self, attempt_id: str) -> dict:
        row = self._conn.execute(
            "SELECT * FROM receipts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            return None
        return {
            "attempt_id": row["attempt_id"],
            "task_id": row["task_id"],
            "device_id": self.device_id,
            "package_id": row["package_id"],
            "outcome": row["outcome"],
            "created_at": row["created_at"],
        }

    def close(self) -> None:
        self._conn.close()
