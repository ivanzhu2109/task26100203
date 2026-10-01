"""崩溃恢复测试用的子进程入口。

用法：
  python3 tests/crash_child.py <center.db> <device.db> <device_id> \
      <task_id> <package_id> <outcomes_json> <clock_file>

时钟来自 clock_file（整数秒，文件持久存在 => 重启不重置）。
设置 DEPLOY_CRASH_AT 环境变量可在内核暂停点处 os._exit 模拟崩溃。
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from deploy_kernel import DeploymentKernel


def main() -> None:
    (center_path, device_path, device_id,
     task_id, package_id, outcomes_json, clock_file) = sys.argv[1:8]

    def clock():
        with open(clock_file) as f:
            return int(f.read().strip())

    kernel = DeploymentKernel(center_path, {device_id: device_path}, clock=clock)
    kernel.submit(task_id, device_id, package_id, json.loads(outcomes_json))
    kernel.drive(task_id)
    kernel.close()


if __name__ == "__main__":
    main()
