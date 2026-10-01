"""部署内核：组合中心账本与设备角色，提供 submit/drive/inspect。

可控暂停点（供崩溃恢复测试）：设置环境变量 DEPLOY_CRASH_AT 为逗号分隔的
暂停点名称，内核执行到该点时以 os._exit 终止当前进程，模拟断电/崩溃。
暂停点：
  before_device_commit  设备效果与回执事务提交前
  after_device_commit   设备事务已提交、中心接受回执前
"""

import json
import os
import uuid

from .center import CenterLedger, QUEUED, TERMINAL_STATES
from .device import DeviceAgent

CRASH_POINTS = ("before_device_commit", "after_device_commit")


def _crash_points_from_env():
    raw = os.environ.get("DEPLOY_CRASH_AT", "")
    return {p.strip() for p in raw.split(",") if p.strip()}


class DeploymentKernel:
    """center_path: 中心账本文件；device_paths: {device_id: 设备账本文件}；
    clock: 调用方注入的时钟，() -> int 秒，重启不重置由调用方保证。"""

    def __init__(self, center_path: str, device_paths: dict, clock) -> None:
        self.clock = clock
        self.center = CenterLedger(center_path)
        self.devices = {dev: DeviceAgent(path) for dev, path in device_paths.items()}

    def close(self) -> None:
        self.center.close()
        for dev in self.devices.values():
            dev.close()

    # ---- 三个操作 ----

    def submit(self, task_id: str, device_id: str, package_id: str, outcomes) -> None:
        outcomes = list(outcomes)
        if len(outcomes) != 3:
            raise ValueError("outcomes must be a fixed sequence of length 3")
        if device_id not in self.devices:
            raise KeyError(f"unknown device: {device_id}")
        self.center.submit_task(task_id, device_id, package_id, outcomes)

    def drive(self, task_id: str) -> None:
        """推进任务：每次至多向设备投递一次并把回执交回中心。"""
        task = self.center.get_task(task_id)
        if task is None:
            raise KeyError(f"unknown task: {task_id}")
        if task["state"] in TERMINAL_STATES:
            return
        now = self.clock()
        just_created = False
        if task["state"] == QUEUED:
            if now < task["next_attempt_at"]:
                return
            self.center.create_attempt(task_id, uuid.uuid4().hex, now)
            task = self.center.get_task(task_id)
            just_created = True
        if not just_created and task["lease_expires_at"] > now:
            return  # 租约有效，不得重复领取
        receipt = self._deliver(task)
        self.center.accept_receipt(
            task_id,
            receipt["attempt_id"],
            receipt["attempt_no"],
            receipt["outcome"],
            self.clock(),
        )

    def inspect(self, task_id: str) -> dict:
        task = self.center.get_task(task_id)
        if task is None:
            raise KeyError(f"unknown task: {task_id}")
        task = dict(task)
        task["outcomes"] = json.loads(task["outcomes"])
        device = self.devices[task["device_id"]]
        return {
            "task": task,
            "receipts": self.center.get_receipts(task_id),
            "device": {
                "device_id": task["device_id"],
                "execution_count": device.execution_count(task_id),
                "installed": task["package_id"] in device.installed_apps(),
            },
        }

    # ---- 内部 ----

    def _deliver(self, task) -> dict:
        self._maybe_crash("before_device_commit")
        receipt = self.devices[task["device_id"]].execute(
            task["attempt_id"],
            task["task_id"],
            task["package_id"],
            task["attempt_no"],
            json.loads(task["outcomes"]),
        )
        self._maybe_crash("after_device_commit")
        return receipt

    @staticmethod
    def _maybe_crash(point: str) -> None:
        if point in _crash_points_from_env():
            os._exit(42)
