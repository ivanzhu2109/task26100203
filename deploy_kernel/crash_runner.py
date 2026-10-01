"""子进程场景执行器：在指定暂停点被杀死，用于崩溃恢复测试。

用法:
    python -m deploy_kernel.crash_runner --dir D --now N submit TASK DEV PKG s,t,p
    python -m deploy_kernel.crash_runner --dir D --now N drive TASK

结果序列简写: s=success, t=transient_failure, p=permanent_failure。
暂停点由环境变量 DEPLOY_PAUSE_AT 控制，命中时进程以退出码 42 死亡。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from deploy_kernel import (  # noqa: E402
    Kernel, SUCCESS, TRANSIENT_FAILURE, PERMANENT_FAILURE,
)
from deploy_kernel.pause import PAUSE_EXIT_CODE  # noqa: E402

_SHORT = {"s": SUCCESS, "t": TRANSIENT_FAILURE, "p": PERMANENT_FAILURE}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", required=True, help="账本目录")
    parser.add_argument("--now", required=True, type=int, help="注入时钟（秒）")
    parser.add_argument("op", choices=["submit", "drive"])
    parser.add_argument("args", nargs="*")
    ns = parser.parse_args(argv)

    central = os.path.join(ns.dir, "central.db")
    devices = {"dev1": os.path.join(ns.dir, "dev1.db")}
    kernel = Kernel(central, devices, clock=lambda: ns.now)
    try:
        if ns.op == "submit":
            task_id, device_id, package_id, outcomes = ns.args
            kernel.submit(task_id, device_id, package_id,
                          [_SHORT[o] for o in outcomes.split(",")])
        else:
            kernel.drive(ns.args[0])
    finally:
        kernel.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
