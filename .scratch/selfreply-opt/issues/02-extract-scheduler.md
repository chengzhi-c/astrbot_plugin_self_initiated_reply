# 02 — 拆分调度器（延迟/巡检/清理）

**What to build:** 把调度职责从主插件类迁出为独立模块：延迟检查（含静默等待）、巡检循环、事件/图片两类清理循环、后台任务登记与停止屏障。主插件只保留注册入口与生命周期接口，调度器自持代次与停止状态的检查。

**Blocked by:** 01 — 基线冻结与行为契约

**Status:** resolved

## Resolution (2026-08-06)

- 新增 `scheduler.py`（SessionScheduler，385 行）：延迟检查/巡检/图片与事件清理/
  任务时序全部迁入；业务动作经注入回调执行，状态容器经引用共享。
- main.py 2399 → 2017 行（-15.9%，略低于票内预估 20%：约 60 行委托壳保留
  既有调用面与测试引用面，07 收尾后移除）。
- 新增 `tests/test_session_scheduler.py`（14 用例）：脱离插件实例，仅注入
  设置/代次闸门/回调即可独立单测（验收项 1 达成）。
- 结构护栏更新（3 处，捕获力保持）：静默等待日志断言、cancel_delay 的
  is_running 守卫断言、`max_age_sec=image_age` 断言均指向 scheduler.py；
  `_delayed_check` 保留委托壳供测试直驱；phase5 常量改从 models 模块读取。
- 实测：363 tests 全绿（+14），整体 80.39%，scheduler.py 覆盖 71%，
  模块门槛 66 已加入 `scripts/coverage_gates.py`，ruff 全清。
