# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的格式。

## [0.8.7] - 2026-08-06

### 工程

- **优化套件收口**（ticket 01-14）：scheduler / decision / generation / delivery / whitelist / events 六模块拆分；SessionGate / SessionState 会话状态显式化；变异矩阵扩至 38 点全击杀；事件驱动静默等待替代轮询；主动回复记录落盘合并写；宿主私有符号单源收敛（runtime_adapter 契约断言 + compat_check 锁定模式）；可观测性（INFO 白名单守卫、任务/会话泄漏告警、/status 调试面板导出）。行为契约基线不变。
- **镜像重复清零**（交付复审）：回调 Protocol、历史补全、超时/取消优雅停止、事件结果回收、静默剩余时间、Provider 控件、数值默认值等七处双形状收敛为单源；`_last_decisions` 随会话回收、跳过决策原因入调试面板；host 符号表数据驱动单源。
- **前端修复**：判断模型 Provider 与两个识图 Provider 统一入 `createProviderControl` 工厂，删除手写同形实现。

## [0.8.6] - 2026-08-04

### 修复

- **README 中文图片引用修正**（0.8.5 发布后补丁）：双语 README 的预览图 URL 改用原始 UTF-8 字符（GitHub raw CDN 不认百分号编码的中文文件名，实测编码 404 / 原始字符 200）。

## [0.8.5] - 2026-08-04

### 修复

- **README 图片 404**：恢复 `assets/审判之司.png`、`assets/慈爱之惠.png`（0.8.4 发布前 56faab4 误删——"零引用"检查未识别 URL 编码形式的引用，GitHub 渲染 404；已从历史恢复）。引用形式同步修正：GitHub raw CDN 对中文文件名只认原始 UTF-8 字符、不认百分号编码（实测编码形式 404 / 原始字符 200），双语 README 改用原始字符 URL。

## [0.8.4] - 2026-08-04

### 修复

- **越权取消**：写指令的会话取消移入权限校验之后，无权限用户不再能打断在途主动回复。
- **webapi 静默吞字段**：13 个 schema 键补全 + 未知键 fail loud（400 列出未知键）。
- **denylist 死条目清理**（4.23.3 实测校准）+ **私有 API 导入守卫**（缺失即可诊断）。
- **覆盖率口径修正**：生产口径 omit tests/，历史 81.62% 为假绿灯；图片解析文件 IO 改 `asyncio.to_thread` 防事件循环阻塞。

### 工程化

- **质量门禁**：红绿灯 R13-R17、mutation 11→19 全击杀、compat_check 扩展、CI 矩阵 +3.13/3.14、mypy 18 文件零问题。
- **补盲**：adapters 37%→100%、recorder_bridge 38%→99%，总覆盖 75.79%，门槛 65→70。
- **SessionGate 只读视图**（MappingProxyType）+ bridge 缓存复用 + flaky 测试事件驱动化。
- **五轮独立审查**（两轴子代理 + 三份盲审），假断言清零。

## [0.8.3] - 2026-08-04

### 修复（文档）

- README.en.md 版本徽章同步到当前版本（三方审查遗漏项）。

### 工程化（行为零变更）

- 版本一致性守卫扩展：metadata.yaml / models.py / 中英双语 README 徽章四处同源（双语 badge 先红后绿验证）。
- 新增资源泄漏回归套件（`tests/test_resource_leaks.py`）：多会话取消后 `_background_tasks`/`_delay_tasks`/`_running_check_tasks`/`_session_release`/`_running_sessions` 全部回基线、常驻任务不误杀、terminate 幂等；两个泄漏变异实测红→绿。
- `SessionGate.restore` 运行集恢复锚定测试（restore 删 running 表变异实测红→绿）。
- `_cancel_delay_task(force=True)` 运行中检查任务取消分支锚定测试（force 分支失效变异实测红→绿，mutation 巡检发现该缺口）。
- 变异检测制度化（`scripts/mutation_check.py`）：首批 11 个历史实测变异点全击杀，锚定串漂移即报错，恢复逐字节校验；挂 CI nightly。
- 宿主兼容冒烟（`scripts/compat_check.py`）+ 多版本 AstrBot 兼容矩阵 CI（4.23.3 硬门禁，latest 预警）。
- 宿主下限声明 `>=4.26.1` → `>=4.23.3`：逐版实测（compat_check 符号+签名校验），4.23.3/4.23.6/4.24.0/4.25.0 通过，4.23.2 及更早因 `run_agent` 缺 `buffer_intermediate_messages` 参数失败。
- 覆盖率门槛 72 → 76（实测 81.62%，按实测-5% 校准）。

## [0.8.2] - 2026-08-04

### 工程化（上帝类第二刀：会话状态收口，行为零变更）

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
