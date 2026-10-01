"""崩溃恢复测试：通过子进程 + 可控暂停点模拟掉电。"""

import os
import subprocess
import sys

import pytest

from deploy_kernel import Kernel, LEASE_SECONDS, RETRY_DELAY_SECONDS
from deploy_kernel.pause import (
    PAUSE_ENV, PAUSE_EXIT_CODE, DEVICE_BEFORE_COMMIT, CENTRAL_BEFORE_ACCEPT,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TASK = "task1"
DEV = "dev1"
PKG = "pkg-a"


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def run_phase(workdir, now, *op_args, pause_at=None):
    """在子进程中执行一个阶段；命中暂停点时进程以 42 退出（模拟掉电）。"""
    env = dict(os.environ)
    if pause_at is None:
        env.pop(PAUSE_ENV, None)
    else:
        env[PAUSE_ENV] = pause_at
    cmd = [
        sys.executable, "-m", "deploy_kernel.crash_runner",
        "--dir", str(workdir), "--now", str(now), *op_args,
    ]
    result = subprocess.run(cmd, env=env, cwd=REPO_ROOT,
                            capture_output=True, text=True)
    expected = PAUSE_EXIT_CODE if pause_at else 0
    assert result.returncode == expected, (
        f"rc={result.returncode} stderr={result.stderr}"
    )


def open_kernel(workdir, clock):
    return Kernel(
        central_path=os.path.join(str(workdir), "central.db"),
        device_paths={DEV: os.path.join(str(workdir), "dev1.db")},
        clock=clock,
    )


@pytest.fixture
def workdir(tmp_path):
    return tmp_path


def test_crash_before_device_commit(workdir):
    """设备效果与回执事务提交前崩溃：效果与回执同生共死，重投不重复执行。"""
    run_phase(workdir, 1000, "submit", TASK, DEV, PKG, "s,s,s")
    # 第一次投递：设备事务写入后、提交前掉电
    run_phase(workdir, 1000, "drive", TASK, pause_at=DEVICE_BEFORE_COMMIT)

    clock = Clock(1000)
    kernel = open_kernel(workdir, clock)
    state = kernel.inspect(TASK)
    assert state["status"] == "executing"
    assert state["attempt_no"] == 1
    attempt_id = state["current_attempt_id"]
    assert state["lease_expires"] == 1000 + LEASE_SECONDS
    # 效果与回执同生共死：两者都不存在
    assert state["device"]["execution_count"] == 0
    assert kernel._device(DEV).receipt_for(attempt_id) is None

    # 租约有效时不得重复领取
    clock.now = 1000 + LEASE_SECONDS - 1
    kernel.drive(TASK)
    assert kernel.inspect(TASK)["device"]["execution_count"] == 0
    kernel.close()

    # 重启（重新打开两份账本），租约到期后重投
    clock.now = 1000 + LEASE_SECONDS + 1
    kernel = open_kernel(workdir, clock)
    kernel.drive(TASK)
    state = kernel.inspect(TASK)
    assert state["status"] == "installed"
    assert state["current_attempt_id"] == attempt_id  # 沿用原 attempt_id
    assert state["attempt_no"] == 1
    assert state["device"]["execution_count"] == 1    # 执行不重复
    assert state["device"]["installed_apps"] == [PKG]
    assert state["attempts"][0]["accepted_at"] == clock.now
    kernel.close()


def test_crash_after_device_commit_before_central_accept(workdir):
    """设备已提交效果与回执、中心尚未接受时崩溃：重投取回原回执。"""
    run_phase(workdir, 1000, "submit", TASK, DEV, PKG, "s,s,s")
    run_phase(workdir, 1000, "drive", TASK, pause_at=CENTRAL_BEFORE_ACCEPT)

    clock = Clock(1000)
    kernel = open_kernel(workdir, clock)
    state = kernel.inspect(TASK)
    attempt_id = state["current_attempt_id"]
    # 设备侧效果与回执已持久化，中心侧尚未接受
    assert state["status"] == "executing"
    assert state["device"]["execution_count"] == 1
    assert kernel._device(DEV).receipt_for(attempt_id)["outcome"] == "success"
    assert state["attempts"][0]["accepted_at"] is None
    kernel.close()

    # 重启并推进至租约到期后重投：取回原回执，中心不多消耗尝试
    clock.now = 1000 + LEASE_SECONDS + 1
    kernel = open_kernel(workdir, clock)
    kernel.drive(TASK)
    state = kernel.inspect(TASK)
    assert state["status"] == "installed"
    assert state["current_attempt_id"] == attempt_id
    assert state["attempt_no"] == 1
    assert state["device"]["execution_count"] == 1  # 成功任务执行计数最终为 1
    assert state["device"]["installed_apps"] == [PKG]
    kernel.close()


def test_transient_retry_time_set_by_central_accept(workdir):
    """暂时失败的重试时间只由中心首次接受回执的时刻确定。"""
    run_phase(workdir, 1000, "submit", TASK, DEV, PKG, "t,s,s")
    # 设备 1000 时刻已提交回执，中心未接受即崩溃
    run_phase(workdir, 1000, "drive", TASK, pause_at=CENTRAL_BEFORE_ACCEPT)
    # 租约到期后重投，取回原回执（不重复执行），中心接受前再次崩溃
    run_phase(workdir, 1000 + LEASE_SECONDS + 1, "drive", TASK,
              pause_at=CENTRAL_BEFORE_ACCEPT)

    clock = Clock(2000)
    kernel = open_kernel(workdir, clock)
    state = kernel.inspect(TASK)
    attempt_id = state["current_attempt_id"]
    assert state["device"]["execution_count"] == 1  # 两次重投均未重复执行
    kernel.drive(TASK)  # 中心在 2000 首次接受回执
    state = kernel.inspect(TASK)
    assert state["status"] == "queued"
    assert state["current_attempt_id"] == attempt_id
    # 重试时间 = 中心首次接受时刻 2000 + 900，而非设备提交时刻 1000
    assert state["not_before"] == 2000 + RETRY_DELAY_SECONDS
    kernel.close()

    # 重启：900 秒之前不能创建下一尝试
    clock.now = 2000 + RETRY_DELAY_SECONDS - 1
    kernel = open_kernel(workdir, clock)
    kernel.drive(TASK)
    assert kernel.inspect(TASK)["attempt_no"] == 1
    # 到达重试时间：创建第 2 次尝试并成功
    clock.now = 2000 + RETRY_DELAY_SECONDS
    kernel.drive(TASK)
    state = kernel.inspect(TASK)
    assert state["status"] == "installed"
    assert state["attempt_no"] == 2
    assert state["current_attempt_id"] != attempt_id
    assert state["device"]["execution_count"] == 2
    kernel.close()


def test_three_transient_failures_with_restarts(workdir):
    """连续 3 次暂时失败，穿插重启与重复投递，最终失败且无第 4 次尝试。"""
    run_phase(workdir, 1000, "submit", TASK, DEV, PKG, "t,t,t")

    clock = Clock(1000)
    kernel = open_kernel(workdir, clock)
    kernel.drive(TASK)  # 第 1 次尝试，中心 1000 接受
    state = kernel.inspect(TASK)
    assert state["status"] == "queued"
    assert state["not_before"] == 1000 + RETRY_DELAY_SECONDS
    kernel.close()

    # 重启：900 秒之前推进无效果
    clock.now = 1000 + RETRY_DELAY_SECONDS - 1
    kernel = open_kernel(workdir, clock)
    kernel.drive(TASK)
    assert kernel.inspect(TASK)["attempt_no"] == 1
    kernel.close()

    # 第 2 次尝试：设备提交后、中心接受前崩溃（重复投递场景）
    run_phase(workdir, 1000 + RETRY_DELAY_SECONDS, "drive", TASK,
              pause_at=CENTRAL_BEFORE_ACCEPT)
    clock.now = 1000 + RETRY_DELAY_SECONDS
    kernel = open_kernel(workdir, clock)
    state = kernel.inspect(TASK)
    attempt2 = state["current_attempt_id"]
    assert state["attempt_no"] == 2
    assert state["device"]["execution_count"] == 2
    # 租约有效时重投无效果
    kernel.drive(TASK)
    assert kernel.inspect(TASK)["device"]["execution_count"] == 2
    kernel.close()

    # 重启，租约到期后重投：沿用 attempt2，取回原回执
    clock.now = 1000 + RETRY_DELAY_SECONDS + LEASE_SECONDS + 1
    kernel = open_kernel(workdir, clock)
    kernel.drive(TASK)
    state = kernel.inspect(TASK)
    assert state["current_attempt_id"] == attempt2
    assert state["device"]["execution_count"] == 2  # 重复投递不重复执行
    assert state["status"] == "queued"
    accept2 = 1000 + RETRY_DELAY_SECONDS + LEASE_SECONDS + 1
    assert state["not_before"] == accept2 + RETRY_DELAY_SECONDS

    # 第 3 次尝试：到达重试时间前无新尝试
    clock.now = accept2 + RETRY_DELAY_SECONDS - 1
    kernel.drive(TASK)
    assert kernel.inspect(TASK)["attempt_no"] == 2
    clock.now = accept2 + RETRY_DELAY_SECONDS
    kernel.drive(TASK)
    state = kernel.inspect(TASK)
    assert state["status"] == "failed"  # 第 3 次暂时失败进入失败
    assert state["attempt_no"] == 3
    assert state["device"]["execution_count"] == 3  # 执行计数恰为 3
    kernel.close()

    # 重启后继续推进时间，也不会出现第 4 次尝试
    clock.now = accept2 + RETRY_DELAY_SECONDS + 100000
    kernel = open_kernel(workdir, clock)
    kernel.drive(TASK)
    state = kernel.inspect(TASK)
    assert state["status"] == "failed"
    assert state["attempt_no"] == 3
    assert state["device"]["execution_count"] == 3
    kernel.close()


def test_permanent_failure_ends_task(workdir):
    clock = Clock(500)
    kernel = open_kernel(workdir, clock)
    kernel.submit(TASK, DEV, PKG, ["permanent_failure"] * 3)
    kernel.drive(TASK)
    state = kernel.inspect(TASK)
    assert state["status"] == "failed"
    assert state["device"]["execution_count"] == 1
    clock.now += 100000
    kernel.drive(TASK)
    assert kernel.inspect(TASK)["device"]["execution_count"] == 1
    kernel.close()


def test_submit_idempotent(workdir):
    clock = Clock(0)
    kernel = open_kernel(workdir, clock)
    kernel.submit(TASK, DEV, PKG, ["success"] * 3)
    kernel.submit(TASK, DEV, PKG, ["success"] * 3)  # 相同参数重复提交
    assert kernel.inspect(TASK)["status"] == "queued"
    with pytest.raises(ValueError):
        kernel.submit(TASK, DEV, "other-pkg", ["success"] * 3)
    kernel.close()
