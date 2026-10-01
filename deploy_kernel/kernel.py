"""部署内核：把中心账本与设备账本组合成 submit/drive/inspect 三个操作。"""

import uuid

from .central import CentralLedger
from .device import DeviceLedger

LEASE_SECONDS = 60
RETRY_DELAY_SECONDS = 900
MAX_ATTEMPTS = 3

_TERMINAL = ("installed", "failed")


class Kernel:
    """clock 为调用方注入的时钟：无参可调用对象，返回整数秒。"""

    def __init__(self, central_path: str, device_paths: dict, clock) -> None:
        self._central = CentralLedger(central_path)
        self._device_paths = dict(device_paths)
        self._devices = {}
        self._clock = clock

    def submit(self, task_id: str, device_id: str, package_id: str,
               outcomes: list) -> None:
        outcomes = list(outcomes)
        if len(outcomes) != MAX_ATTEMPTS:
            raise ValueError("outcomes must contain exactly 3 entries")
        if device_id not in self._device_paths:
            raise ValueError(f"unknown device {device_id!r}")
        self._central.submit(task_id, device_id, package_id, outcomes)

    def drive(self, task_id: str) -> None:
        """推进任务：每次至多向设备投递一次并把回执交回中心。

        未到执行时间或任务已结束时不产生新尝试；租约有效时不重复领取；
        租约到期后重投，沿用同一 attempt_id 与尝试序号。
        """
        now = int(self._clock())
        task = self._central.get_task(task_id)
        if task is None:
            raise KeyError(f"unknown task {task_id!r}")
        status = task["status"]
        if status in _TERMINAL:
            return
        if status == "queued":
            if now < task["not_before"]:
                return
            attempt_no = task["attempt_no"] + 1
            attempt_id = uuid.uuid4().hex
            self._central.create_attempt(
                task_id, attempt_id, attempt_no, now, LEASE_SECONDS
            )
        else:  # executing
            if now < task["lease_expires"]:
                return  # 租约有效，不得重复领取
            attempt_no = task["attempt_no"]
            attempt_id = task["current_attempt_id"]
        device = self._device(task["device_id"])
        outcome = task["outcomes"][attempt_no - 1]
        receipt = device.deliver(
            attempt_id, task_id, task["package_id"], outcome, now
        )
        self._central.accept_receipt(
            task_id, attempt_id, receipt, now, RETRY_DELAY_SECONDS, MAX_ATTEMPTS
        )

    def inspect(self, task_id: str) -> dict:
        task = self._central.get_task(task_id)
        if task is None:
            raise KeyError(f"unknown task {task_id!r}")
        device = self._device(task["device_id"])
        return {
            "task_id": task_id,
            "device_id": task["device_id"],
            "package_id": task["package_id"],
            "outcomes": task["outcomes"],
            "status": task["status"],
            "attempt_no": task["attempt_no"],
            "current_attempt_id": task["current_attempt_id"],
            "lease_expires": task["lease_expires"],
            "not_before": task["not_before"],
            "attempts": self._central.attempts_of(task_id),
            "device": {
                "execution_count": device.execution_count(task_id),
                "installed_apps": device.installed_apps(),
            },
        }

    def _device(self, device_id: str) -> DeviceLedger:
        if device_id not in self._devices:
            self._devices[device_id] = DeviceLedger(
                self._device_paths[device_id], device_id
            )
        return self._devices[device_id]

    def close(self) -> None:
        self._central.close()
        for device in self._devices.values():
            device.close()
        self._devices.clear()
