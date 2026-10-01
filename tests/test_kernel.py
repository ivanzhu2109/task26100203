"""内核行为测试（进程内，注入可变时钟）。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from deploy_kernel import DeploymentKernel


class Clock:
    def __init__(self, now=0):
        self.now = now

    def __call__(self):
        return self.now


class KernelCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.center_path = os.path.join(self.tmp.name, "center.db")
        self.device_path = os.path.join(self.tmp.name, "device.db")
        self.clock = Clock(1000)
        self.kernel = self._open()

    def tearDown(self):
        self.kernel.close()
        self.tmp.cleanup()

    def _open(self):
        return DeploymentKernel(
            self.center_path, {"dev1": self.device_path}, clock=self.clock
        )

    def reopen(self):
        self.kernel.close()
        self.kernel = self._open()

    def test_submit_idempotent(self):
        self.kernel.submit("t1", "dev1", "pkg1", ["success"] * 3)
        self.kernel.submit("t1", "dev1", "pkg1", ["success"] * 3)
        self.assertEqual(self.kernel.inspect("t1")["task"]["state"], "queued")
        with self.assertRaises(ValueError):
            self.kernel.submit("t1", "dev1", "pkg2", ["success"] * 3)

    def test_success_flow(self):
        self.kernel.submit("t1", "dev1", "pkg1", ["success"] * 3)
        self.kernel.drive("t1")
        info = self.kernel.inspect("t1")
        self.assertEqual(info["task"]["state"], "installed")
        self.assertEqual(info["device"]["execution_count"], 1)
        self.assertTrue(info["device"]["installed"])
        self.kernel.drive("t1")  # 终态不再产生尝试
        self.assertEqual(self.kernel.inspect("t1")["device"]["execution_count"], 1)

    def test_perm_failure_ends_immediately(self):
        self.kernel.submit("t1", "dev1", "pkg1", ["perm_failure"] * 3)
        self.kernel.drive("t1")
        info = self.kernel.inspect("t1")
        self.assertEqual(info["task"]["state"], "failed")
        self.assertEqual(info["device"]["execution_count"], 1)
        self.clock.now += 10000
        self.kernel.drive("t1")
        self.assertEqual(self.kernel.inspect("t1")["device"]["execution_count"], 1)

    def test_three_temp_failures_with_restarts_and_redelivery(self):
        self.kernel.submit("t1", "dev1", "pkg1", ["temp_failure"] * 3)

        # 第 1 次尝试，t=1000 被中心接受 -> 下次最早 1900
        self.kernel.drive("t1")
        info = self.kernel.inspect("t1")
        self.assertEqual(info["task"]["state"], "queued")
        self.assertEqual(info["task"]["next_attempt_at"], 1900)
        attempt1 = info["task"]["attempt_id"]

        # 900 秒未到，不能创建下一尝试
        self.clock.now = 1899
        self.kernel.drive("t1")
        info = self.kernel.inspect("t1")
        self.assertEqual(info["task"]["attempt_no"], 1)
        self.assertEqual(info["device"]["execution_count"], 1)

        # 到点创建第 2 次尝试；租约内重复 drive 不重投
        self.clock.now = 1900
        self.kernel.drive("t1")
        info = self.kernel.inspect("t1")
        self.assertEqual(info["task"]["state"], "queued")
        self.assertEqual(info["task"]["next_attempt_at"], 2800)
        self.assertEqual(info["device"]["execution_count"], 2)
        attempt2 = info["task"]["attempt_id"]
        self.assertNotEqual(attempt1, attempt2)

        # 重启后继续
        self.reopen()

        # 第 3 次尝试前，先模拟一次"租约内重复投递不生效"：
        self.clock.now = 2800
        self.kernel.drive("t1")  # 创建第 3 次尝试并接受回执 -> failed
        info = self.kernel.inspect("t1")
        self.assertEqual(info["task"]["state"], "failed")
        self.assertEqual(info["task"]["attempt_no"], 3)
        self.assertEqual(info["device"]["execution_count"], 3)

        # 时间继续推进也不会出现第 4 次尝试
        self.clock.now += 100000
        self.reopen()
        self.kernel.drive("t1")
        info = self.kernel.inspect("t1")
        self.assertEqual(info["task"]["attempt_no"], 3)
        self.assertEqual(info["device"]["execution_count"], 3)
        self.assertEqual(len(info["receipts"]), 3)

    def test_late_duplicate_receipt_does_not_move_retry_time(self):
        self.kernel.submit("t1", "dev1", "pkg1", ["temp_failure", "success", "success"])
        self.clock.now = 1000
        self.kernel.drive("t1")  # 第 1 次暂时失败，next=1900
        info = self.kernel.inspect("t1")
        self.assertEqual(info["task"]["next_attempt_at"], 1900)
        attempt1 = info["task"]["attempt_id"]

        # 中心崩溃前已接受回执；这里模拟"迟到回执"：租约到期后重投同一 attempt，
        # 但任务已回到排队，旧回执不得被再次接受、不得移动重试时间。
        self.clock.now = 1500
        device = self.kernel.devices["dev1"]
        receipt = device.execute(attempt1, "t1", "pkg1", 1,
                                 ["temp_failure", "success", "success"])
        accepted = self.kernel.center.accept_receipt(
            "t1", receipt["attempt_id"], receipt["attempt_no"],
            receipt["outcome"], 1500)
        self.assertFalse(accepted)
        info = self.kernel.inspect("t1")
        self.assertEqual(info["task"]["next_attempt_at"], 1900)
        self.assertEqual(info["device"]["execution_count"], 1)  # 设备未重复执行

        # 到点重试成功
        self.clock.now = 1900
        self.kernel.drive("t1")
        info = self.kernel.inspect("t1")
        self.assertEqual(info["task"]["state"], "installed")
        self.assertEqual(info["device"]["execution_count"], 2)


if __name__ == "__main__":
    unittest.main()
