# 05 — 拆分发送状态机（delivery）

**What to build:** 把投递与记录迁出为独立模块：装饰钩子调用、代次复核（前/后/发送中）、发送结果分类、UNKNOWN 语义、主动状态记录（冷却、日配额、观察窗口、历史条目）。对外暴露两个入口："投递一次回复"与"记录一次尝试"。

**Blocked by:** 01 — 基线冻结与行为契约

**Status:** resolved

## Resolution (2026-08-06)

- 新增 `delivery.py`（DeliveryRunner，387 行）：发送前门卫、装饰钩子调用与
  代次复核（前/后/发送中）、事件发送与 context 兜底发送、发送结果分类、
  UNKNOWN 语义（不自动重试/不触发 after-send 钩子/仍消耗冷却与日配额并推进
  观察窗口）、主动状态记录，全部迁入。
- 日志随迁：skip before send / proactive reply sent（if/else 双分支）/
  event send completed 等锚点移至 delivery.py，round6 相应测试改读
  delivery.py（先例：02 拆分时 wait silence 日志随迁 scheduler.py）。
- main.py 保留 `_deliver_session_reply`/`_send_reply`/`_record_proactive_state`
  委托壳（round5 替换 `plugin._send_reply` 仍生效，因 delivery 内部经注入的
  `send_reply` 回调运行时查找实例方法）。main 2017 → 1283 行（-36%）。
- `test_storage_and_umo` 结构锚点（装饰钩子后必须复核代次）改解析
  delivery.py；round5 测试的 `main.SendOutcome` 改为动态包 models 导入
  （`main.SendStatus` 已随删壳移除，不再导出）。
- 新增 `tests/test_delivery_runner.py`（17 用例）：假门卫/发送器/钩子/存储
  独立单测，覆盖验收项 1/2/3 与门卫分支、send_reply 内部状态机
  （after-send 触发条件、context 兜底、stale SUPPRESSED）。
- 变异实测（3 处，注入后变红、恢复后全绿）：
  1) UNKNOWN 分支丢弃记录 → test_deliver_unknown_consumes_state_without_retry 变红
  2) unconfirmed 不推进观察窗口 → test_record_unconfirmed_sets_state_fields +
     round5 r7 双路变红（交叉验证）
  3) suppressed/失败分支丢弃直发记录 → test_deliver_suppressed_with_directs_records 变红
- 实测：423 tests 全绿（+17），整体 83.77%，delivery.py 覆盖 65%，
  模块门槛 60 已加入 `scripts/coverage_gates.py`，ruff/mypy 全清。
