"""崩溃恢复测试：在可控暂停点杀死子进程，重开两份账本后继续推进。"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from deploy_kernel.center import CenterLedger
from deploy_kernel.device import DeviceAgent

CHILD = os.path.join(os.path.dirname(__file__), "crash_child.py")
OUTCOMES_SUCCESS = ["success", "success", "success"]
OUTCOMES_TEMP_THEN_SUCCESS = ["temp_failure", "success", "success"]


class RecoveryCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.center_path = os.path.join(self.tmp.name, "center.db")
        self.device_path = os.path.join(self.tmp.name, "device.db")
        self.clock_file = os.path.join(self.tmp.name, "clock.txt")
        self.set_clock(1000)

    def tearDown(self):
        self.tmp.cleanup()

    # ---- 工具 ----

    def set_clock(self, now):
        with open(self.clock_file, "w") as f:
            f.write(str(now))

    def run_child(self, crash_at=None, outcomes=OUTCOMES_SUCCESS, task_id="t1"):
        env = dict(os.environ)
        if crash_at:
            env["DEPLOY_CRASH_AT"] = crash_at
        else:
            env.pop("DEPLOY_CRASH_AT", None)
        return subprocess.run(
            [sys.executable, CHILD, self.center_path, self.device_path, "dev1",
             task_id, "pkg1", json.dumps(outcomes), self.clock_file],
            env=env, capture_output=True, text=True,
        )

    def center_task(self, task_id="t1"):
        c = CenterLedger(self.center_path)
        try:
            return c.get_task(task_id)
        finally:
            c.close()

    def device_state(self, task_id="t1"):
        d = DeviceAgent(self.device_path)
        try:
            return {
                "count": d.execution_count(task_id),
                "apps": d.installed_apps(),
                "receipts": [
                    d.get_receipt(row["attempt_id"]) for row in
                    d._db.execute(
                        "SELECT attempt_id FROM attempts WHERE task_id = ?", (task_id,)
                    ).fetchall()
                ],
            }
        finally:
            d.close()

    # ---- 用例 1：设备效果与回执事务提交前崩溃 ----

    def test_crash_before_device_commit(self):
        proc = self.run_child(crash_at="before_device_commit")
        self.assertEqual(proc.returncode, 42)

        # 中心已创建尝试并进入执行中；设备侧效果与回执都不存在（同生共死）
        task = self.center_task()
        self.assertEqual(task["state"], "running")
        self.assertEqual(task["attempt_no"], 1)
        attempt_id = task["attempt_id"]
        self.assertEqual(task["lease_expires_at"], 1060)
        dev = self.device_state()
        self.assertEqual(dev["count"], 0)
        self.assertEqual(dev["apps"], [])
        self.assertEqual(dev["receipts"], [])

        # 租约有效时重开账本推进：不得重复领取
        proc = self.run_child()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        task = self.center_task()
        self.assertEqual(task["state"], "running")
        self.assertEqual(task["attempt_id"], attempt_id)
        self.assertEqual(self.device_state()["count"], 0)

        # 租约到期后重投：沿用原 attempt_id，设备首次执行
        self.set_clock(1061)
        proc = self.run_child()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        task = self.center_task()
        self.assertEqual(task["state"], "installed")
        self.assertEqual(task["attempt_id"], attempt_id)
        self.assertEqual(task["attempt_no"], 1)
        dev = self.device_state()
        self.assertEqual(dev["count"], 1)  # 成功任务设备执行计数最终为 1
        self.assertEqual(dev["apps"], ["pkg1"])

    # ---- 用例 2：设备已提交、中心接受前崩溃 ----

    def test_crash_after_device_commit(self):
        proc = self.run_child(crash_at="after_device_commit")
        self.assertEqual(proc.returncode, 42)

        # 设备效果与回执已同事务落盘；中心尚未接受
        task = self.center_task()
        self.assertEqual(task["state"], "running")
        attempt_id = task["attempt_id"]
        dev = self.device_state()
        self.assertEqual(dev["count"], 1)
        self.assertEqual(dev["apps"], ["pkg1"])
        self.assertEqual(dev["receipts"][0]["outcome"], "success")

        # 租约内推进：不重投、不重复执行
        self.set_clock(1030)
        proc = self.run_child()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.device_state()["count"], 1)
        self.assertEqual(self.center_task()["state"], "running")

        # 租约到期后重投：取回原回执，中心接受，不多消耗尝试
        self.set_clock(1061)
        proc = self.run_child()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        task = self.center_task()
        self.assertEqual(task["state"], "installed")
        self.assertEqual(task["attempt_id"], attempt_id)
        self.assertEqual(task["attempt_no"], 1)
        dev = self.device_state()
        self.assertEqual(dev["count"], 1)  # 设备未重复执行
        self.assertEqual(dev["apps"], ["pkg1"])  # 已成功的效果未被判失败

    # ---- 用例 3：暂时失败的重试时间只由中心首次接受时刻决定 ----

    def test_temp_failure_retry_time_set_by_center_accept(self):
        proc = self.run_child(crash_at="after_device_commit",
                              outcomes=OUTCOMES_TEMP_THEN_SUCCESS)
        self.assertEqual(proc.returncode, 42)

        # 设备在 t=1000 提交了暂时失败回执，中心未接受：不得排定重试
        task = self.center_task()
        self.assertEqual(task["state"], "running")
        self.assertEqual(task["attempt_no"], 1)
        attempt_id = task["attempt_id"]

        # 中心在 t=1061 首次接受回执 -> 下次尝试最早 1961，而非 1000+900
        self.set_clock(1061)
        proc = self.run_child(outcomes=OUTCOMES_TEMP_THEN_SUCCESS)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        task = self.center_task()
        self.assertEqual(task["state"], "queued")
        self.assertEqual(task["next_attempt_at"], 1961)
        self.assertEqual(self.device_state()["count"], 1)  # 重投未重复执行

        # 1961 之前不能创建下一尝试
        self.set_clock(1960)
        proc = self.run_child(outcomes=OUTCOMES_TEMP_THEN_SUCCESS)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        task = self.center_task()
        self.assertEqual(task["state"], "queued")
        self.assertEqual(task["attempt_no"], 1)
        self.assertEqual(self.device_state()["count"], 1)

        # 到点后第 2 次尝试成功
        self.set_clock(1961)
        proc = self.run_child(outcomes=OUTCOMES_TEMP_THEN_SUCCESS)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        task = self.center_task()
        self.assertEqual(task["state"], "installed")
        self.assertEqual(task["attempt_no"], 2)
        self.assertNotEqual(task["attempt_id"], attempt_id)
        dev = self.device_state()
        self.assertEqual(dev["count"], 2)
        self.assertEqual(dev["apps"], ["pkg1"])


if __name__ == "__main__":
    unittest.main()
