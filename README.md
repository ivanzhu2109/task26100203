# 可恢复的软件部署内核

Python + SQLite 实现。中心账本与每台设备的账本分别存入独立 SQLite 文件；
设备角色通过本机函数调用参与协议，不执行任何系统安装命令；无后台线程，
时间由调用方注入的时钟（整数秒）提供。

## 结构

- `deploy_kernel/center.py` — 中心账本：任务状态机（排队/执行中/已安装/失败）、
  60 秒租约、900 秒重试间隔、回执只接受一次。
- `deploy_kernel/device.py` — 设备账本：已安装应用集合、执行计数、回执；
  效果与回执在同一事务持久化，重复 `attempt_id` 只返回原回执。
- `deploy_kernel/kernel.py` — 组合层，提供 `submit` / `drive` / `inspect`。
- `tests/` — 自动化测试（进程内行为测试 + 子进程崩溃恢复测试）。

## 用法

```python
from deploy_kernel import DeploymentKernel

clock = lambda: 1000  # 调用方注入，重启不重置由调用方保证
k = DeploymentKernel("center.db", {"dev1": "device-dev1.db"}, clock=clock)
k.submit("task-1", "dev1", "pkg-1", ["temp_failure", "success", "success"])
k.drive("task-1")            # 每次至多投递一次并把回执交回中心
info = k.inspect("task-1")   # 任务状态 + 设备执行情况
k.close()
```

## 可控暂停点（崩溃模拟）

设置环境变量 `DEPLOY_CRASH_AT`（逗号分隔），内核在对应暂停点 `os._exit`：

- `before_device_commit` — 设备效果与回执事务提交前
- `after_device_commit` — 设备事务已提交、中心接受回执前

## 运行测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：两个暂停点分别杀子进程后重开账本推进至租约到期重投（沿用原
`attempt_id`、效果与回执同生共死、执行不重复、成功任务计数为 1、重试时间
只由中心首次接受时刻决定）；连续 3 次暂时失败穿插重启与重复投递（900 秒
前不产生新尝试、最终失败、计数恰为 3、不会出现第 4 次尝试）。
