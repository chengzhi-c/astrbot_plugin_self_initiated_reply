# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的格式。

## [0.9.0] - 2026-08-07

三要素极致优化（可维护/最轻量/高质量，经《主动回复方案审核.md》交叉复审修订版）；
命令组、Web API 路由、行为契约、状态文件格式全部不变。

### 缺陷修复

- **配置热更新分裂缺陷（轴 A）**：webapi 应用/回滚配置时对 `plugin.settings` 整体替换，
  而 decision/generation/delivery/scheduler/whitelist 五组件构造时各存旧引用，
  热更新后组件读过期配置（533 基线全绿不暴露）；新增 `Settings.apply` 原地写入保持
  对象身份，红灯测试 `test_config_hot_reload.py` 先行实测捕获后修复转绿。
- **delayed_check 早退路径 UnboundLocalError（轴 D1 补盲实测捕获的潜伏缺陷）**：
  延迟后插件停用/代次失效的早退不会绑定 `silence_event`，而 finally 回收引用它；
  绑定上移至 try 首行，补盲测试首测即红。

### 内部结构（最轻量 + 可维护，功能零变更）

- **B1** 删 main.py 死字段 `_invalid_quiet_hours_logged`（decision.py 在用副本不受影响）。
- **B2** 删生产零调用的 `storage.save_sessions` 包装器，测试改用生产同形两步写。
- **B3** `unified_manager.py` 并入 `webapi.py`：`/unified/overview` 路由与全部
  backward-compat 别名字段原样保留。
- **B4** `events.py` 并入 `utils.py`：`should_ignore_event` 行为不变，委托壳保留
  （test_regressions RL-4 与 test_vision 两处行为契约锚点依赖）。
- **C'** 删 2 个实测零锚点委托壳：`_local_gate`（直连 DecisionMaker.local_gate，
  调用点统一 force 关键字形态）与 `_queue_state_save`（异步闭包直连
  DebouncedStateSaver.mark_dirty）；其余 18 壳 + 4 视图 + 46 处锚点的完整表面统一
  推迟 0.10 独立立项。
- **B5 降级保留**：`whitelist_storage_key` 占位参数不动，docstring 补日落决策点
  （0.10 前 wildcard 不立项则连参数移除）。

### 质量护栏（轴 D）

- **三盲区补盲（红灯先行 + 定点变异抽查捕获力）**：scheduler 76%→98%（巡检循环
  全分支/清理循环/事件溢出）、delivery 74%→94%（发送异常与 context 兜底分支）、
  storage 72%→95%（配置对象多态/容错分支）；总覆盖率 91.31%→94.47%（569 tests）。
- **mypy 显式覆盖全源码**：files 清单补齐 decision/delivery/generation/
  session_coordinator/state_saver/whitelist 六模块（原仅经 main.py 传递检查），全绿。
- **门槛上调（禁止下调规约）**：fail_under 70→89；coverage_gates 按实测-5% 重标定
  （scheduler 66→93、delivery 60→89、generation 79→80，新增 storage 90，
  移除已并入的 events）。
- **P2 宿主兼容层入门禁**：runtime_adapter.py 降级宿主分支补盲 86%→100%
  （validate 告警分支/路径回退/工具过滤 fail-closed 边界），coverage_gates
  新增 runtime_adapter.py 95 门槛（宿主升级风险最高的模块，CI 已有独立门禁作业）。
- mutation_check 完整矩阵补跑：38/38 全击杀（含 delivered-no-advance 锚点同步
  0.8.8 三分支重整；首次运行残留识别机制成功检出 outbound.py 变异残留）；
  hatch build + check_wheel 实跑通过（42 文件/1859 KB，无开发物泄漏，版本一致；
  Windows 下需 PYTHONUTF8=1 绕过 hatch 子进程 GBK stdout 解码崩溃）。

## [0.8.8] - 2026-08-07

### 发布质量（交叉复审 + 实测闭环）

- **版本五源守卫**：pyproject.toml 纳入版本一致性测试（tomllib/tomli 兼容 3.10 矩阵）；0.8.7 发布时 pyproject 漏在守卫外，wheel 文件名与 dist-info 版本停留在 0.8.3（实测实锤）。
- **wheel 内容收敛**：开发物（tests/.scratch/scripts/docs/.github/开发配置）不再打进生产 wheel（实测 102 文件 2.8MB → 44 文件 1.9MB）；新增 `scripts/check_wheel.py` 内容断言 + CI 版本一致性断言（wheel 文件名与 dist-info 必须与 metadata 一致）。
- **配置 schema 键守卫**：`_conf_schema.json` 与 CONFIG_SCHEMA_KEYS 硬性对齐（别名提为 `_SCHEMA_ALIAS_KEYS` 显式常量），漂移即红。

### 工程

- **maybe_await 单源化**：runtime_adapter / image.recorder_bridge 两处私有副本删除，统一到 `utils.maybe_await`（inspect.isawaitable 语义）；修复 hasattr 判定漏 await generator-based coroutine 的行为差异，新增锚定测试。
- **跨类私有访问消除**：scheduler `_delayed_check` → `delayed_check`；`sanitize_prompt_variable` 函数内 import 上移至模块顶部。
- **会话回收单点化**：`_prune_session` 统一回收代次/裁决/sessions 条目，WhitelistManager 不再自行 pop；非白名单 force-check 的内存残留（交叉复审 N2）修复。
- **落盘语义澄清**：delivery 注释与 SaveStorageCallback 契约更新为合并写语义，新增真实 DebouncedStateSaver 集成测试锁定"置脏→flush"契约。
- **vision prompt 变更门禁**：VISION_PROMPT_VERSION 与模板指纹锚定，改模板必须同步 bump 版本（缓存键语义）。
- **文档基线刷新**：BEHAVIOR_CONTRACT 与 coverage_gates 实测基线更新（0.8.8 / 543 tests / 91.35%）。

### 复审加固（第三轮交叉复审：行为/契约 + 可维护/轻量）

- **定时落盘自取消竞态修复（S2）**：DebouncedStateSaver 定时路径（_flush_later 到点自调 flush）原先会 cancel 自身任务，CancelledError 在 do_save 的锁等待点投递且不被 except Exception 捕获，_pending 已置 False → 脏状态静默丢失；修复为"非自身任务才 cancel" + 取消兜底恢复脏标记，红灯测试实测捕获（原行为注入即红）。
- **死代码清理**：utils.session_label、host_stubs._empty_coro、ImageCache.clear/contains/size、main._message_trigger_delay 委托壳（零引用）删除；host_stubs._noop_async 内不可达 close 缩进 bug 修复（_FakeResetCoro 补齐 close 契约）。
- **镜像收敛**：response 文本提取三处镜像 → utils.response_text 单源；delivery 双 save 块 → 单点保存；image/parser 三并发方法 → _run_concurrent 模板；CI 内联 wheel 版本断言并入 check_wheel.py（找 wheel 逻辑单源）；命令别名双源新增一致性守卫测试；wheel REQUIRED_FILES 与 pyproject 打包覆盖新增静态断言。
- **测试基建收敛**：host_stubs 新增 load_package()，11 个测试文件的私有加载模板与 4 份私有装桩实现删除，统一完整版装桩（observability 解除对 test_vision 的耦合）；覆盖口径修正（scripts/ 加入 omit，CI 工具脚本不进生产指标）。

### 复审加固（第四轮：质量优化方案审核——合并/收敛而非重写）

- **方案审核结论**（2026-08-07）：外部优化方案中"合并 session 三模块 / 合并 pipeline / 779KB→350KB"等主张与实测不符（三者职责异构、351 行≠250 行、798KB 含 tests/scripts 且 wheel 已 exclude），未实施；方案实质是回滚到 0.8.6 前的拆分前状态。
- **main.py 死壳清零**：8 个零生产调用的委托壳删除（`_record_proactive_state`/`_main_agent_build_config`/`_recent_reply_request_reason`/`_ask_decision_model`/`_build_decision_prompt`/`_remaining_silence_sec`/`_delayed_check`/`_last_event_cleanup` property），连同 `MainAgentBuildConfig` 别名与 `REPLY_REQUEST_WINDOW_SEC` 死导入；测试改直连子模块（main.py 1355→1285 行）。
- **重复测试清除**：main 级与模块级重复的 10 个门面测试删除（local_gate 5 组 ≡ decision_maker、send_reply 2 组与 record_proactive_state 3 组 ≡ delivery_runner，断言逐条比对等价后删除）。
- **红灯测试主题化合并**：7 个 `test_red_light_round3~8/security`（轮次命名、难以发现）合并为 2 个主题文件——`test_security.py`（图片/净化安全 + 健壮性边界）、`test_regressions.py`（r1-r20 编号回归 + 命令/生命周期 + 日志级别契约），测试内容 100% 保留（72 项）。
- **单源守卫重建**：合并过程中发现 0.8.8 未提交工作树中 2 个单源守卫测试（response_text / 命令别名表）因 git checkout 回退丢失，按 CHANGELOG 记录与代码现状重建，并实测变异捕获（镜像注入即红）；`test_refresh_admin_ids` 的 NTFS mtime 粒度抖动加固（utime +1s 强制刷新）。

### 复审加固（第五轮：方案 v2 审核——测试组织收敛，源码合并否决）

- **方案 v2 审核裁定**（2026-08-07）：删除委托壳 A 中 6/13 已在前轮完成，剩余 7 个为文档化测试接缝（main.py 注释明示"测试替换实例方法后仍命中"，30+ 处测试 mock 依赖），删除收益 ~50 行但改动面 ~40 处，否决；SharedTaskState B 实测账目为负（dataclass + 7 兼容 property 约 +49 行 vs 删除约 -25 行），"省 55 行"无推导路径，否决；SessionGate+Coordinator 合并 C 触发 test_host_contract 的 HOST_CONTRACT 清单、子模块（generation/delivery/scheduler）接口变宽、109 处测试 + 30 处 main 引用改写，否决。三主张的源码行数数据仍全部过期（1235→1285、126/80→147/101、3 个测试→4 个）。
- **阶段化测试文件去阶段化**：`test_phase5_fixes.py`（4 个 webapi 配置边界/白名单回收/管理员热读测试）合并入 `test_security.py`（35 测试）并删除；`test_phase0_fixes.py` 更名为 `test_agent_pipeline_regressions.py`（12 个 Agent 管线回归，docstring 去"阶段 0"表述）。测试数量不变（533），文件 -1。


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
