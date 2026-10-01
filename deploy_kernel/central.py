"""中心账本：任务与尝试记录，存于独立 SQLite 文件。"""

import json
import sqlite3

from .pause import pausepoint, CENTRAL_BEFORE_ACCEPT

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id            TEXT PRIMARY KEY,
    device_id          TEXT NOT NULL,
    package_id         TEXT NOT NULL,
    outcomes           TEXT NOT NULL,           -- JSON 数组，长度 3
    status             TEXT NOT NULL,           -- queued/executing/installed/failed
    attempt_no         INTEGER NOT NULL DEFAULT 0,
    current_attempt_id TEXT,
    lease_expires      INTEGER,
    not_before         INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS attempts (
    attempt_id      TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL,
    attempt_no      INTEGER NOT NULL,
    created_at      INTEGER NOT NULL,
    lease_expires   INTEGER NOT NULL,
    receipt_outcome TEXT,
    accepted_at     INTEGER
);
"""

_TERMINAL = ("installed", "failed")


class CentralLedger:
    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def submit(self, task_id: str, device_id: str, package_id: str,
               outcomes: list) -> None:
        """幂等持久化部署任务；同 task_id 不同参数则报错。"""
        existing = self.get_task(task_id)
        if existing is not None:
            same = (
                existing["device_id"] == device_id
                and existing["package_id"] == package_id
                and existing["outcomes"] == list(outcomes)
            )
            if not same:
                raise ValueError(
                    f"task {task_id!r} already exists with different parameters"
                )
            return
        with self._conn:
            self._conn.execute(
                "INSERT INTO tasks(task_id, device_id, package_id, outcomes, status)"
                " VALUES (?, ?, ?, ?, 'queued')",
                (task_id, device_id, package_id, json.dumps(list(outcomes))),
            )

    def get_task(self, task_id: str) -> dict:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        return self._task_from_row(row) if row else None

    def create_attempt(self, task_id: str, attempt_id: str, attempt_no: int,
                       now: int, lease_seconds: int) -> None:
        lease_expires = now + lease_seconds
        with self._conn:
            self._conn.execute(
                "INSERT INTO attempts(attempt_id, task_id, attempt_no, created_at, lease_expires)"
                " VALUES (?, ?, ?, ?, ?)",
                (attempt_id, task_id, attempt_no, now, lease_expires),
            )
            self._conn.execute(
                "UPDATE tasks SET status='executing', attempt_no=?,"
                " current_attempt_id=?, lease_expires=? WHERE task_id=?",
                (attempt_no, attempt_id, lease_expires, task_id),
            )

    def accept_receipt(self, task_id: str, attempt_id: str, receipt: dict,
                       now: int, retry_delay: int, max_attempts: int) -> None:
        """中心首次接受回执时推进状态；重复/迟到回执直接忽略。"""
        pausepoint(CENTRAL_BEFORE_ACCEPT)
        with self._conn:
            task = self.get_task(task_id)
            if task is None or task["status"] in _TERMINAL:
                return  # 终态不可回退
            attempt = self._conn.execute(
                "SELECT * FROM attempts WHERE attempt_id = ? AND task_id = ?",
                (attempt_id, task_id),
            ).fetchone()
            if attempt is None or attempt["accepted_at"] is not None:
                return  # 每次尝试的回执只被接受一次
            outcome = receipt["outcome"]
            self._conn.execute(
                "UPDATE attempts SET receipt_outcome=?, accepted_at=? WHERE attempt_id=?",
                (outcome, now, attempt_id),
            )
            attempt_no = attempt["attempt_no"]
            if outcome == "success":
                new_status, not_before = "installed", task["not_before"]
            elif outcome == "permanent_failure":
                new_status, not_before = "failed", task["not_before"]
            elif attempt_no >= max_attempts:
                new_status, not_before = "failed", task["not_before"]
            else:
                new_status = "queued"
                not_before = now + retry_delay  # 只由中心首次接受时刻决定
            self._conn.execute(
                "UPDATE tasks SET status=?, not_before=? WHERE task_id=?",
                (new_status, not_before, task_id),
            )

    def attempts_of(self, task_id: str) -> list:
        rows = self._conn.execute(
            "SELECT * FROM attempts WHERE task_id = ? ORDER BY attempt_no",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> dict:
        task = dict(row)
        task["outcomes"] = json.loads(task["outcomes"])
        return task

    def close(self) -> None:
        self._conn.close()
