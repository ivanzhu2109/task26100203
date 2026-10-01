"""可控暂停点：用于测试模拟崩溃。"""

import os

PAUSE_ENV = "DEPLOY_PAUSE_AT"
PAUSE_EXIT_CODE = 42

# 暂停点名称
DEVICE_BEFORE_COMMIT = "device_before_commit"   # 设备事务已写未提交
DEVICE_AFTER_COMMIT = "device_after_commit"     # 设备已提交，中心尚未接受
CENTRAL_BEFORE_ACCEPT = "central_before_accept"  # 中心接受回执事务之前


def pausepoint(name: str) -> None:
    """若环境变量 DEPLOY_PAUSE_AT 等于 name，则立即终止当前进程（模拟掉电）。

    使用 os._exit 绕过一切清理，未提交的 SQLite 事务会回滚，
    已提交的事务则已持久化，与真实崩溃语义一致。
    """
    if os.environ.get(PAUSE_ENV) == name:
        os._exit(PAUSE_EXIT_CODE)
