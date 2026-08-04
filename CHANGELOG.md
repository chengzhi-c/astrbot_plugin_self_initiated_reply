# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的格式。

## [0.9.0] - 2026-08-04

### 工程化（上帝类第二刀：会话状态收口）

- 移除主类两个纯转发委托（`_advance_session_generation` / `_generation_is_current`），全部调用点直连 `SessionGate.advance / is_current`。
- 运行成员判断（延迟取消守卫、patrol 循环、会话检查门卫、等待循环）统一走 `SessionGate.is_running`。
- 主动回复状态写入收进 `SessionState.record_proactive_attempt`（配额/冷却/历史条目的单点写入语义；UNKNOWN 投递只消耗配额不写历史）。
- 测试由 206 增至 207，行覆盖约 81%（门槛 72）。

## [0.8.1] - 2026-08-04

### 修复

- 白名单移除时唤醒仍挂起在会话运行释放上的延迟检查（此前可能永久悬挂）。
- `/selfreply check` 在非白名单会话执行后统一回收代次/锁/运行标记与释放事件（此前释放事件残留）。
- 配置回滚不再直接改写跨类私有字段：`SessionGate.snapshot()/restore()` 封装整表恢复。

### 工程化

- 修复两个测试弱点：白名单 ABA 测试移除掩盖每会话计数器变异的冗余推进；补白名单闸门行为守护测试（删除闸门变异实测变红）。
- webapi 边界测试补盲：webapi.py 行覆盖由 63% 提升至 100%，总行覆盖约 81%（门槛 72）。
- 测试由 167 增至 206，全部先红后绿（mutation 实测）验证。

## [0.8.0] - 2026-08-04

### 行为变更

- 隐私默认收紧：`log_reply_content` 默认值由 `true` 改为 `false`，回复内容默认不再写入日志。
- 只读指令（`status` / `list` / `help` / `debug` / `check`）不再使当前会话判断缓存失效。
- 白名单条目保存新增校验：超过 200 字符或含控制字符的条目将被拒绝。
- 高频成功路径日志由 INFO 降为 DEBUG，降低正常运行的日志噪音。

### 修复

- 运行中检查被 force cancel 时 `run_agent` 任务不再成为孤儿任务（request_stop + 显式收敛）。
- context 兜底发送不再被误记为 UNKNOWN 状态。
- `/selfreply on` / `off` 回滚时按原运行状态恢复 patrol / cleanup 任务。
- `_call_compat` 绑定失败触发的 minimal 重试不再导致双执行。
- 图片下载端口加入白名单，收窄 SSRF 面。
- force cancel 收敛路径补二次 cancel 兜底：`run_agent` 吞掉取消时不再残留孤儿任务。

### 工程化

- 拆分出 `webapi.py`，main.py 由 2815 行降至 2317 行。
- 引入 ruff / mypy / pre-commit / GitHub Actions CI（lint + 3 个 Python 版本测试 + wheel 构建 + 覆盖率工件）。
- 测试由基线 144 增至 162，行覆盖率约 78%（门槛 fail_under 72）。
- 常量与 E402 豁免收敛至 pyproject.toml / `_constants` 集中管理。
