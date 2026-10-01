# 可恢复的软件部署内核

Python + SQLite 实现。中心账本与每台设备的账本分别存入独立 SQLite 文件；
设备角色通过本机函数调用参与协议，不执行任何系统安装命令。

## 结构

- `deploy_kernel/central.py` — 中心账本：任务（排队/执行中/已安装/失败）与尝试记录
- `deploy_kernel/device.py` — 设备账本：已安装应用集合、执行计数、回执（同事务持久化）
- `deploy_kernel/kernel.py` — `Kernel`：组合两类账本，提供 `submit` / `drive` / `inspect`
- `deploy_kernel/pause.py` — 可控暂停点（模拟掉电）
- `deploy_kernel/crash_runner.py` — 子进程场景执行器，供崩溃恢复测试使用
- `tests/test_crash_recovery.py` — 自动化测试

## 协议要点

- 时间来自调用方注入的时钟（整数秒），无后台线程；重启不重置。
- 首次投递创建全局唯一 `attempt_id` 与 60 秒租约；租约有效不得重复领取，
  到期后重投但沿用同一 `attempt_id` 与尝试序号。
- 设备首次执行某尝试时执行计数加一，成功还将应用加入已安装集合；
  效果与回执在同一事务中持久化，重复 `attempt_id` 只返回原回执。
- 成功 → 已安装；永久失败 → 失败；暂时失败 → 回到排队，
  下一尝试最早在中心首次接受该回执的时刻 +900 秒创建；最多 3 次逻辑尝试。
- 每次尝试的回执只被中心接受一次；重复/迟到回执不移动重试时间、
  不创建额外尝试、不回退终态。

## 使用

```python
from deploy_kernel import Kernel

now = [1000]  # 注入时钟
kernel = Kernel(
    central_path="central.db",
    device_paths={"dev1": "dev1.db"},
    clock=lambda: now[0],
)
kernel.submit("task1", "dev1", "pkg-a",
              ["success", "success", "success"])
kernel.drive("task1")          # 推进：创建尝试、投递、中心接受回执
print(kernel.inspect("task1")) # 查看任务状态与设备执行情况
kernel.close()
```

## 运行测试

```bash
python3 -m pytest tests -q
```

测试通过子进程 + 环境变量 `DEPLOY_PAUSE_AT` 提供可控暂停点：

- `device_before_commit` — 设备效果与回执事务提交前
- `device_after_commit` — 设备提交后、返回前
- `central_before_accept` — 中心接受回执事务之前

命中暂停点的子进程以退出码 42 死亡（`os._exit`，模拟掉电），
随后测试重新打开两份账本继续推进，核验崩溃恢复语义。
