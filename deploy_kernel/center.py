"""中心账本：任务状态机，独立 SQLite 文件。"""

import json
import sqlite3

QUEUED = "queued"
RUNNING = "running"
INSTALLED = "installed"
FAILED = "failed"

OUTCOME_SUCCESS = "success"
OUTCOME_TEMP_FAILURE = "temp_failure"
OUTCOME_PERM_FAILURE = "perm_failure"

TERMINAL_STATES = (INSTALLED, FAILED)

MAX_ATTEMPTS = 3
LEASE_SECONDS = 60
RETRY_DELAY_SECONDS = 900

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    package_id TEXT NOT NULL,
    outcomes TEXT NOT NULL,
    state TEXT NOT NULL,
    attempt_no INTEGER NOT NULL DEFAULT 0,
    attempt_id TEXT,
    lease_expires_at INTEGER,
    next_attempt_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS receipts (
    attempt_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    accepted_at INTEGER NOT NULL
);
"""


class CenterLedger:
    """中心账本，持久化任务与已接受回执。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    # ---- 任务 ----

    def submit_task(self, task_id, device_id, package_id, outcomes) -> None:
        """幂等提交：同 task_id 同参数只对应一个任务。"""
        row = self._db.execute(
            "SELECT device_id, package_id, outcomes FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        encoded = json.dumps(list(outcomes))
        if row is not None:
            if (row["device_id"], row["package_id"], row["outcomes"]) != (
                device_id,
                package_id,
                encoded,
            ):
                raise ValueError(f"task {task_id} already exists with different params")
            return
        with self._db:
            self._db.execute(
                "INSERT INTO tasks (task_id, device_id, package_id, outcomes, state)"
                " VALUES (?, ?, ?, ?, ?)",
                (task_id, device_id, package_id, encoded, QUEUED),
            )

    def get_task(self, task_id):
        row = self._db.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def create_attempt(self, task_id, attempt_id, now) -> None:
        """排队任务到期后创建新尝试：发 attempt_id、进入执行中、起租约。"""
        with self._db:
            self._db.execute(
                "UPDATE tasks SET state = ?, attempt_no = attempt_no + 1,"
                " attempt_id = ?, lease_expires_at = ?"
                " WHERE task_id = ? AND state = ?",
                (RUNNING, attempt_id, now + LEASE_SECONDS, task_id, QUEUED),
            )

    # ---- 回执 ----

    def receipt_accepted(self, attempt_id) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM receipts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        return row is not None

    def accept_receipt(self, task_id, attempt_id, attempt_no, outcome, now) -> bool:
        """接受回执并推进状态机。重复/迟到回执返回 False，不产生任何效果。"""
        if self.receipt_accepted(attempt_id):
            return False
        task = self.get_task(task_id)
        if task is None or task["state"] in TERMINAL_STATES:
            return False
        if task["attempt_id"] != attempt_id:
            return False
        if outcome == OUTCOME_SUCCESS:
            new_state, next_at = INSTALLED, task["next_attempt_at"]
        elif outcome == OUTCOME_PERM_FAILURE:
            new_state, next_at = FAILED, task["next_attempt_at"]
        elif outcome == OUTCOME_TEMP_FAILURE:
            if attempt_no >= MAX_ATTEMPTS:
                new_state, next_at = FAILED, task["next_attempt_at"]
            else:
                new_state, next_at = QUEUED, now + RETRY_DELAY_SECONDS
        else:
            raise ValueError(f"unknown outcome: {outcome}")
        with self._db:
            self._db.execute(
                "INSERT INTO receipts (attempt_id, task_id, attempt_no, outcome, accepted_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (attempt_id, task_id, attempt_no, outcome, now),
            )
            self._db.execute(
                "UPDATE tasks SET state = ?, next_attempt_at = ? WHERE task_id = ?",
                (new_state, next_at, task_id),
            )
        return True

    def get_receipts(self, task_id):
        rows = self._db.execute(
            "SELECT * FROM receipts WHERE task_id = ? ORDER BY accepted_at, attempt_no",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]
