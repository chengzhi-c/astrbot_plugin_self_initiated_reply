# 11 — 延迟链去轮询（事件驱动）

**What to build:** 把延迟检查中的两类轮询等待改为事件通知：静默等待（剩余静默时间）与运行释放等待（前次检查结束）。消息到达或运行结束时由协作者置位通知，唤醒后仍复查代次与静默；超时兜底保留防止丢失唤醒。

**Blocked by:** 07 — 会话状态显式化

**Status:** resolved（2026-08-06）

- [x] 等待路径不再有 sleep 轮询（sleep 仅保留在超时兜底与巡检间隔）
- [x] 唤醒后必复查代次与静默状态，语义与现状等价
- [x] 红灯测试：静默被新消息打断、运行中被新消息失效、取消竞态——先红后绿

## Resolution

### 改造

- `scheduler.py`：静默等待从 `sleep(silence_left + 0.1)` 轮询改为事件等待
  `wait_for(_silence_events[umo].wait(), timeout=silence_left + 0.1)`；
  新增 `notify_activity(umo)`（pop + set，通知被消费即移除，无残留）；
  唤醒后必复查 `_should_run`/代次/静默（原语义保留）；任务结束 finally 回收
  silence event。运行释放等待本就事件化（release_event）未动；
  sleep 仅剩：初始单次 delay、超时兜底、巡检/清理间隔
- `session_coordinator.py`：`record_event` 收敛为活动写点，新增注入回调
  `notify_silence`（与 cancel_delay 同款模式）——消息/命令到达即唤醒延迟链
- `main.py`：构造接线 `notify_silence=lambda umo: self._scheduler.notify_activity(umo)`

### 测试（先红后绿 + 变异实测）

- **红灯** `test_silence_interrupted_aborts_when_session_invalidated`：
  静默等待中会话失效（代次推进）→ 任务必须立即主动退出、不产生检查。
  初始红灯 = notify_activity 不存在（API 缺失）；轮询观测任务主动退出
  （不用 wait_for 收尾：其超时取消会被任务吸收，3.14 下 `task.cancelled()`
  失真，会掩盖"任务未被唤醒"——假绿灯）
- **回归** `test_silence_interrupted_restarts_full_silence_cycle`：
  打断后按最新状态重计完整静默（检查不得早于 最后消息 + min_silence）
- **回归** 运行中失效/取消竞态由既有测试锁定（non_force leaves running /
  force cancels / release event）
- **变异 3 处实测 KILLED**（固化 mutation_check.py 32 → 35 点）：
  notify 丢通知（pop 无 set）/ 唤醒后不复查代次 / 唤醒后不复查静默
- **关键教训（实测暴露）**：静默打断的"完成时刻"在单任务下与轮询恒等
  （都 = 最后消息 + 静默期）——完成时点断言无法区分事件化与轮询；
  可观测差异在**唤醒时点**（是否在打断后立即复查/退出），红灯测试必须
  观测"任务被唤醒后主动退出"，而非完成时刻
