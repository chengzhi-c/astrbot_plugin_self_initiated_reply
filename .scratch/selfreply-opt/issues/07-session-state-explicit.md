# 07 — 会话状态显式化（FSM + 会话协作者）

**What to build:** 把散落在主插件上的六张隐式会话表（最近事件、事件时间、延迟任务、运行中检查、图片索引、运行时 UMO 映射）收敛为一个每会话协作者对象，状态转移显式化为 IDLE→OBSERVING→DECIDING→GENERATING→DELIVERING。会话失效只有单点入口；代次单调性（防 ABA）与只读视图守护保留。

**Blocked by:** 02 — 拆分调度器；03 — 拆分判断链路；04 — 拆分生成管线；05 — 拆分发送状态机；06 — 拆分白名单与消息接收

**Status:** resolved

## Resolution (2026-08-06)

- 新增 `session_coordinator.py`（SessionCoordinator + SessionPhase 枚举）：
  事件/时间/图片三表写点收敛（record_event/capture_images）、失效级联单点
  `invalidate`（推进代次 → 取消延迟 → clear 三表+阶段标记）、FSM 状态投影
  （显式标记 DECIDING/GENERATING/DELIVERING 优先，否则按运行标记/事件
  推导 OBSERVING/IDLE）、图片索引读取（过期/去重/sticker/上限，自
  `_recent_images_for` 迁入）、terminate 走 `reset_all` 级联清空。
- main 壳保留：`_invalidate_session`/`_clear_cached_event`/`_recent_images_for`
  委托壳（round3/round6 锚点与测试直连面不动）；on_message/check 命令写点
  收敛经 coordinator；`_check_session_locked` 按阶段标记 FSM。
- **ABA 缺陷实测修复**（R18 红灯先红后绿）：白名单移除后重加，运行中旧任务
  复活发送并记录（"已主动回复。"）。根因：prune 删除代次表项后
  `.get(umo, 0)` 与从未 advance 的会话 token=0 撞车，且无代次任务
  （patrol/手动）的 `is_current(None)` 恒放行。修复：`SessionGate.current`
  + `_check_session_locked` 在无代次调用且会话已有代次记录时绑定任务起始
  代次（防 ABA）。真实路径（on_message 已 advance）全部受保护。
- 新增 `tests/test_session_coordinator.py`（10 用例）+ `tests/test_red_light_round8.py`
  （3 用例：R18 ABA / R19 main 无散落表清理 / R20 FSM 可观测）。
- 变异实测（3 处，注入后变红、恢复后全绿）：invalidate 漏取消延迟任务 /
  clear 漏清图片 / clear 漏清阶段标记。
- 实测：452 tests 全绿（+13），整体 85.76%，session_coordinator.py 覆盖 96%，
  门槛 91 加入 `scripts/coverage_gates.py`，ruff/mypy 全清。main 2017 → 1201
  行（-40%）。
