# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的格式。

## [Unreleased]

### 修复

- **越权取消**：`/selfreply` 写指令（add/remove/check/on/off）的会话取消移入管理员权限校验之后；无权限用户不再能取消在途主动回复（只读指令不打断进行中的检查）。
- **webapi 静默吞字段**：`_parse_config_updates` 补全 13 个 schema 键处理分支（recent_message_limit/reply_length_mode/allow_multiline_reply/max_reply_chars/log_reply_content/bot_aliases/ignored_sender_ids/check_interval_sec/max_daily_replies_per_session/quiet_hours/enabled_message_trigger/enabled_patrol_trigger/generation_timeout_sec），`decision_history_min_messages` 支持规范键；schema 之外的键 fail loud 拒绝（400 列出未知键）。
- **denylist 死条目**：`HOST_DANGEROUS_TOOL_IDS` 删除 6 个实测不存在的 ID（create/delete/list_future_task、astrbot_create_file、astrbot_read_file、astrbot_read_file_tool），补真实名 `future_task`（4.23.3 实测单工具 multiCommand）、`astrbot_file_read_tool`、`astrbot_shell_session`（4.27.1）。
- **私有 API 加载即崩缺口**：main.py 四个宿主私有 import 统一守卫，缺失时给出可诊断的加载失败提示。
- **覆盖率口径**：`--cov=.` 排除 tests/（历史假绿灯根因），生产口径实测 68.19%，门槛 76 → 65（保持"实测-5%"缓冲）。
- **parse 路径文件 IO 阻塞事件循环**：`_resolve_image_url` 三处 `_file_to_data_url` 同步调用改 `asyncio.to_thread`，与 snapshot 路径对齐（分歧裁决 #2）。

### 工程化

- **红绿灯测试第七轮**（`tests/test_red_light_round7.py`，R13-R17）：MP1-3 越权取消、MP1-4 webapi 新键与 fail loud、MP1-8 bridge 缓存复用；五个测试在修复前基线实测全红，捕获能力非推演（R14 断言曾因 asyncio `cancelling` 态假绿，改用任务表引用断言修正）。
- **mutation_check 四道守卫**（均实测红→绿）：① 目标文件工作区洁净检查（脏则拒绝）；② 锁文件并发互斥（O_EXCL + 超龄回收）；③ 变异残留识别（特征串比对 HEAD，给出恢复命令）；④ 变异前锚点测试基线预检（基线本红拒绝执行）。
- **compat_check 扩展**：CHECKS 补 4 个私有 API 模块 + EventType 三枚举成员；新增宿主危险工具全集覆盖断言（枚举宿主 FunctionTool 子类 name ⊆ denylist，宿主改名/新增即 CI 报错）。
- **flaky 测试修复**：三处固定 sleep 时序断言改事件驱动 `until()` 条件等待（test_phase0_fixes / test_resource_leaks）。
- **测试排序依赖**：`test_phase0_fixes._load_main()` 显式安装 astrbot stubs，可独立运行。
- **bridge 缓存击溃**：`get_recorder_bridge` 首次构造后复用缓存实例（唯一调用点 context 生命周期内稳定）。
- **mypy**：files 补 webapi.py、session_gate.py（18 文件零问题），新增 CI job。
- **CI 矩阵**：测试矩阵加 Python 3.13/3.14；lint 钉 ruff==0.16.1 与 pre-commit 对齐。
- **webapi 审计**：enabled/proactive_inherit_tools/whitelist 变更记 INFO 审计日志；README 声明鉴权依赖宿主 Dashboard。

### 批次3（质量增强）

- **低覆盖模块补盲**：adapters.py 37%→97%（`tests/test_adapters.py` 38 用例：兼容调用降级/探测/宿主差异全分支）、recorder_bridge.py 38%→95%（`tests/test_recorder_bridge.py` 25 用例：探测回退链/路径解析/MIME 魔数校验）；生产口径覆盖率 68.19%→75.43%，门槛按"实测-5%"校准 65→70。
- **mutation 扩面 11→19**（P2-23）：SSRF 三变异（scheme 白名单旁路/非标准端口旁路/私有 IP 放行，test_vision 补 ftp:// 用例）、webapi 拒绝路径三变异（bool 接受/非法字符放行/未知键旁路）、storage 恢复两变异（损坏不备份/版本不符不备份）；实测 19/19 击杀。
- **SessionGate 只读视图**（P2-24）：generation_view/locks_view 用 MappingProxyType 实时映射、running_sessions_view 用 frozenset；main.py 三个转发 property 改返回只读视图（全仓调用点均只读，行为零变更）；误写运行时抛错，杜绝绕过语义；两个测试搭场景写点改用公开入口 lock_for。

#### 批次3 复审修复（2026-08-04 晚，全方面复审）

- **补盲测试抓出 2 个假断言并修复**：
  - `test_read_history_filters_and_maps`：`history[-limit:]` 截断把无效数据（system/非 dict/空文本）全切出窗口，过滤分支 continue 从未执行——测试名说"过滤"实际只测截断+映射。重排数据 + limit=6 全量遍历（过滤分支真实执行）+ limit=5 验证截断，adapters.py 97%→99%。
  - `test_image_to_data_url_os_error`：目录路径被 is_file() 先行拒绝，IsADirectoryError 永不发生——名为 os_error 实为 is_file 分支。改用 monkeypatch 真实触发 read_bytes OSError，recorder_bridge.py 95%→99%（仅剩 110 行 size 检查双保险死分支）。
  - 补 `test_ensure_api_false_when_get_api_not_callable`（探测链 get_api 不可调用分支）与 `test_resolve_relative_path_resolver_not_callable`（resolver 不可调用分支）。
- **SessionGate 只读视图守护测试**：`test_gate_views_are_read_only_and_live`——三视图写抛错（TypeError/AttributeError）、读实时（advance/mark_running 后视图反映最新状态）、locks_view 引用同一锁对象；session_gate.py 覆盖率 98%→100%。
- 复审后状态：284 passed、覆盖率 75.73%（adapters 99%/recorder_bridge 99%/session_gate 100%）。

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
