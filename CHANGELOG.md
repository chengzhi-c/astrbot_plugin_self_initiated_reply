# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的格式。
小版本（0.8.1~0.8.8）迭代细节省略，详见 git 历史。

## [0.9.0] - 2026-08-07

### 缺陷修复

- **配置热更新分裂缺陷**：webapi 应用/回滚配置时整体替换 `plugin.settings`，五运行组件持有旧引用读到过期配置；新增 `Settings.apply` 原地写入保持对象身份（红灯测试实测捕获）。
- **delayed_check 早退路径 UnboundLocalError**：早退分支未绑定 `silence_event` 而 finally 回收引用它；绑定上移至 try 首行（补盲测试首测即红）。

### 内部结构（功能零变更）

- 模块并入：`unified_manager.py` → `webapi.py`、`events.py` → `utils.py`（行为与 backward-compat 字段不变）。
- 删死代码：2 个实测零锚点委托壳、main.py 死字段、零调用包装器；image 模块 19 处日志前缀硬编码收敛为 `PLUGIN_ID`。
- delivery 三处重复的工具直发记录调用提取为 `_record_direct_sends` 并补 confirmed 守卫。

### 质量护栏

- 三盲区补盲（scheduler/delivery/storage），总覆盖率 91.31% → 94.47%（569 tests）；门槛 fail_under 70→89。
- mypy 显式覆盖全源码；mutation 38/38 全击杀；wheel 内容断言实跑通过。

## [0.8.0] - 2026-08-04

### 行为变更

- 隐私默认收紧：`log_reply_content` 默认改为 `false`。
- 只读指令（status/list/help/debug/check）不再使当前会话判断缓存失效；高频成功路径日志降为 DEBUG。

### 修复与工程化

- 运行中检查被 force cancel 时 `run_agent` 不再成为孤儿任务；context 兜底发送不再误记 UNKNOWN；`_call_compat` 绑定失败重试不再双执行。
- 拆出 `webapi.py`（main.py 2815→2317 行）；引入 ruff/mypy/pre-commit/GitHub Actions CI；测试 144→162，覆盖率约 78%。
