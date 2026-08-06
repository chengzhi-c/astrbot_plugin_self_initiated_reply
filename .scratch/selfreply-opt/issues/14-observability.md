# 14 — 可观测性（结构化日志 + 泄漏告警）

**What to build:** 关键事件（巡检、决策、发送、白名单变更）输出结构化日志，遵循噪音纪律：高频成功路径 DEBUG，INFO 仅保留异常与运维状态。后台任务数与代次表规模达到阈值时告警，调试面板可导出会话级运行状态。

**Blocked by:** 07 — 会话状态显式化

**Status:** resolved（2026-08-06）

- [x] 日志分级纪律经红灯测试验证（无新增 INFO 高频噪音）
- [x] 后台任务数/代次表泄漏告警有对应红灯测试
- [x] 调试面板导出内容覆盖：代次、运行中集合、任务数、缓存规模、最近裁决原因

## Resolution

### 日志分级守卫（tests/test_observability.py）

- ast 解析 13 个业务模块的全部 `logger.info(` 消息模板 → 双向锁定：
  新增模板不在白名单即红（防新增 INFO 噪音）；白名单条目在代码中
  消失即红（防僵尸条目）
- 白名单 15 项全部为异常/运维语义（代次失效抑制、未确认记录、资源清理
  结果、白名单管理、启动/终止横幅）；高频成功路径（生成/发送/巡检通过/
  清理例程）均为 DEBUG，无需改动

### 泄漏告警（scheduler.py + models.py）

- 共享常量：`LEAK_WARN_TASK_THRESHOLD = 100`（延迟+运行中检查+后台任务
  合计）、`LEAK_WARN_SESSION_THRESHOLD = 1500`（代次表规模，> 白名单上限
  1000 即泄漏）
- `cleanup_events_if_needed` 周期末尾 `_warn_leaks_if_needed`：超阈值
  logger.warning（运维状态，随 1 小时清理周期低频）；`_leak_warned` 标记
  防重复——回落清除标记后再次超标可重新告警
- scheduler 构造新增 `background_tasks` 引用参数（main 装配传入）
- 红灯测试 4 项：任务阈值告警 / 会话阈值告警 / 不重复告警+回落后重告警 /
  正常规模静默

### 调试面板导出（webapi.py `_api_status` + main.py）

- `/status` 扩展：gate（代次快照 + 运行中集合）、tasks（delay/
  running_check/background 计数）、caches（事件/图片事件/会话数）、
  last_decisions（每会话最近裁决：at/trigger/should_reply/reason）
- main `_decide_session_reply` 记录 `_last_decisions`（仅内存不落盘）
- test_api_status 扩展断言新字段

### 终验

- 522 passed、覆盖率门槛 PASS、ruff/mypy 全清
- 变异矩阵 38/38 KILLED（scheduler 构造改动后全量重跑）
- 真实宿主 compat_check 不受影响（runtime_adapter 未改）
- 交付自查：移除 `_warn_leaks_if_needed` 未使用参数
