# 06 — 拆分白名单与消息接收（whitelist + events）

**What to build:** 把白名单增删（含配置与状态的双写、失败回滚）与消息接收归一化（文本提取、忽略判定、会话键映射）迁出为独立模块。增删共用一个回滚路径，消除当前两处逐行重复的实现。命令入口的权限校验与写操作取消语义保持。

**Blocked by:** 01 — 基线冻结与行为契约

**Status:** resolved

## Resolution (2026-08-06)

- 新增 `whitelist.py`（WhitelistManager，150 行）：白名单替换（移出会话的
  失效/代次清理/会话回收/runtime 映射过滤）、增删共用单一回滚
  （`commit_change`：双写失败 → 恢复内存 → 重写 → 仍失败告警上抛）。
  add/remove 原两处逐行重复实现消除（-60 行）。
- 新增 `events.py`：`should_ignore_event` 纯函数（自消息/命令/纯图无识图/
  忽略名单/直接点名），main 保留委托壳（round3/test_vision 结构锚点不动）。
- main 壳保留：`_replace_whitelist`（test_main_runtime 直连 4 处不破）、
  `_add_whitelist_session`/`_remove_whitelist_session`（锁+停止检查保留在壳，
  权限校验与写操作取消语义不变）；`_add/remove_whitelist_session_locked`
  删除（round3 锚点改指向 whitelist.py 的 add/remove）。
- 新增 `tests/test_whitelist_manager.py`（9 用例）+ `tests/test_event_ignore.py`
  （7 用例）。
- 变异实测（2 处，注入后变红、恢复后全绿）：
  1) commit_change 丢失内存回滚 → add/remove 两个回滚测试双路变红
  2) replace 丢失 gate.prune → 会话回收测试变红
- 实测：439 tests 全绿（+16），整体 84.11%，whitelist.py/events.py 覆盖 100%，
  门槛 95 加入 `scripts/coverage_gates.py`，ruff/mypy 全清。main 2017 → 1209
  行（-40%）。
