# 结构决策

行为不变量见 `BEHAVIOR_CONTRACT.md`。本页只记结构取舍。

## 发布产物

发布主路径是 AstrBot 插件市场（git 仓库）。手工部署包（VPS 直投 zip）用
`git archive --format=zip -o <name>.zip HEAD` 导出：排除规则单点声明在仓库根
`.gitattributes` 的 `export-ignore`，未跟踪/被 .gitignore 排除的文件天然不进包。

不采用「hatch 构建 wheel → `check_wheel`/`check_sdist` 内容断言 → 从 wheel
派生部署 zip」的理由：那套三层互锁（pyproject exclude 列表 ↔ 检查脚本禁运名单 ↔
pathspec 交叉核验）曾漂移过两次，而分发主路径不产生 wheel，其全部维护成本只服务
于次要路径；而 `git archive` + `export-ignore` 把同一保证变成单点声明
——“缓存泄漏进包”这类问题在结构上不再存在。pyproject 的 wheel/sdist 配置保留
（本地构建仍干净），但不再有发布链依赖它。

## 双面板

`CONFIG_SPECS.surfaces` 区分官方 Dashboard（`host`）与自定义设置页（`panel`）。常用键上自定义页；巡检、勿扰、回复长度、日上限、`log_reply_content` 等只在 Dashboard。两套面板读写同一份配置。前端可写键必须等于 panel 面，由 `test_fe_writable_keys_match_panel_surfaces` 锁定。

GET `/config` 是 panel 视图：只回 panel 键加 `runtime_enabled` / `decision_prompt_default` / `config_revision`。POST `/config` 接受全部 schema 键，测试与旧客户端可改巡检、勿扰、日上限；无 `base_revision` 的调用仍可串行写入（前端始终携带 revision）。官方 Dashboard 走宿主配置文件，自定义页走本 API。

## 装配

协作对象接线在 `main._assemble_components`。循环依赖与宿主热替换用调用期查找。

`webapi` 用 `TYPE_CHECKING` 导入 `SelfInitiatedReplyPlugin`，避免与 `main` 运行时成环。注解只给编辑器跳转：函数都经 `partial` 再注册，宿主看不到 `__annotations__`。`ignore_missing_imports` 下 mypy 把 `Star` 子类看成 `Any`，这些注解不产生类型检查力。

## 设置页 chrome

浅/深/跟随系统与压暗/粗体均经 `GET/POST ui/theme` 写入 `ui_prefs.json`（页面在 Dashboard iframe 内，localStorage 不可靠）。保存必须带齐三字段，禁止只写主题抹掉压暗/粗体。落盘与状态文件共用 `storage` 原子写。服务端 prefs 覆盖 localStorage，但 `GET ui/theme` 返回前用户已点过主题、压暗或粗体则那次点击优先，迟到的 GET 不得抹掉；本地缓存也只能经守卫后的 `applyTheme` 落盘（`restoreTheme` 不自行写缓存）。

## 默认值

运行默认以 `CONFIG_SPECS` 为准。README 建议区间不是默认值；文档写建议时同时写默认。

## 设置页配置写入与请求协调

设置页的保存请求属于版本化写入：POST `/config` 总是带当前 GET 返回的
`base_revision`。Bridge 与 fetch 只是传输路径，不改变 CAS 语义；Bridge 没有取消能力时，
超时只表示客户端无法确认结果，不能当作服务端未写入。服务端在 `_config_lock` 内拒绝
过期 revision，旧客户端的无版本调用保留兼容但明确标记为未版本化。

迟到的 GET 不能覆盖已经编辑的表单；初始加载失败或超时也不能解除表单的 inert 状态。
保存超时、异常或 `STALE_WRITE` 后，页面必须先刷新取得新 revision 才能再次提交完整配置。

## 私聊主动回复开关

`enabled_private_sessions` 放自定义页与 Dashboard（`surfaces=_PANEL`），默认开。
关了只挡自动路径（入口 / 非 force 门卫 / 巡检），不挡 `/selfreply check`。
不新增独立私聊模式，也不做第二套白名单。

## 新消息放弃旧回复开关

`abandon_stale_on_new_message` 放自定义页与 Dashboard，默认关。关了只挡入口推进代次；
白名单移除、`/selfreply check` 和插件停止仍走 `invalidate`。不按消息类型拆第二套代次策略，
也不为表情包/单独符号单独加过滤。关开关时，在途检查的静默按检查开始时的活动时间算，
途中新消息只刷新下一轮静默，不得拦下本轮已通过的发送。

## 非协作任务隔离

生命周期由插件 owner 持有 `RUNNING`、`STOPPING`、`DEGRADED` 三态。停止等待使用硬时间边界；仍吞取消的生成、patrol 或最终状态保存 task 进入 quarantine 并触发 `DEGRADED`，后续 spawn、巡检、force check、工具直发和最终回复全部拒绝。`MAX_QUARANTINED_TASKS` 是代码容量上限，不提供在线恢复命令；任务结束后仅从注册表移除，恢复仍依赖插件重载或宿主重启。

## 轻量化冻结

新抽象必须已有第二个实现或第二个调用方。新门禁必须证明现有 ruff/pytest 抓不到。
不为覆盖率补行，不为文件变少合并领域模块。

## 兼容层签名探测

宿主公开层（`adapters`）的签名探测只有一个出口：`_signature_or_none` /
`_keyword_names`。kwargs 过滤、绑定预检与候选构造都从它取参数名集，
改兼容规则只动这里；「预检绝不调用、函数体内 TypeError 不重试」的
双副作用约定锚定在 `models.first_bindable_args`。

## SUPPRESSED 文案按成因分流

`send_reply` 的 SUPPRESSED 有两类成因：代次已变（`STALE_REPLY_MESSAGE`）
与插件停止（「插件正在停止，放弃回复。」）。两类都不计失败、不重试；
文案分流只为排障方向准确，不改变记账语义。

## 阈值分工

安全上限（防 OOM/费用爆炸/性能降级）在 `models.py` 顶部常量；行为调参（静默等待余量、
清理周期、冻结预算、裁决 token）在各模块本地常量并附一行取值理由。不把后者搬进
`models.py`，避免依赖图叶子继续膨胀。


`recorder_bridge` 按平台消息 ID 查本地图片时，多图记录里 URL 未命中必须拒绝
盲取首图（首图属于另一张图，错配会让 Vision 描述错图）；单图消息宽容取用
唯一组件是安全的。处理器数量上限 `EXPECTED_HANDLER_COUNT` 以
`scripts/compat_check.py` 为单一事实源，`tests/test_runtime_adapter.py` 经
import 引用。

远程图片使用 `httpx` + `httpcore` 的固定地址传输：DNS 只在每个请求入口解析一次，
TCP 连接使用已验证 IP，原 hostname 继续承担 Host/SNI；环境代理关闭，重定向由 HTTPX
逐跳重新进入传输层。下载全过程（DNS、连接、流式读取）受单图超时预算约束：httpx 的
timeout 只覆盖单次操作，慢速滴流与无响应 DNS 不得无限拖住解析，超限按下载失败降级。
图片描述 LRU 同时受条目数和字节预算约束，磁盘不可用时的 data URL
索引受全局/会话原始载荷预算约束。事件清理只删除事件引用，图片索引由独立保护窗口回收；
失效和终止才清理两者。运行时依赖由 `pyproject.toml` 与宿主兼容检查锁定。

覆盖率门槛以 `pyproject.toml` 的 `fail_under` 为准。

## models.py 不拆分

`models.py` 是最大的生产文件，承载五类职责：常量与工具函数、数据类与枚举、
`AttemptLedger` 账本状态机、`ConfigSpec`/`Settings` 与 coerce/normalize、
提示词模板。

不拆的理由是扇入成本远大于文件长度的收益：`models` 被绝大多数生产模块以 import
语句直接引用，`Settings`、`SessionState` 与 `config_revision` 是全仓共享的叶子类型。
把配置子系统搬到新模块要同时改这些 import 与 `pyproject.toml` 的 mypy 显式文件清单，
属高 churn、零行为收益的重排；
而"读一个文件要切换几次心智模型"的代价，靠下面的结构契约即可抵消。

取而代之的守卫是结构契约而非文件边界：配置键的单源由 `ConfigSpec` 表 +
`test_config_schema` 断言，前端可写键由 `test_config_source_of_truth` 与 panel
面比对。新增职责时按同一方式加断言，不靠拆文件降低阅读成本。

## image/parser.py 不拆分

`image/parser.py` 是第二大生产文件，同样并置三类关注点：SSRF 安全的固定地址
传输、内容寻址缓存与清理、识图解析与描述缓存。

不拆的理由与 `models.py` 同款，且多一条测试耦合：传输层私有名
（`_FixedAddressTransport` / `_FixedAddressBackend` / `_resolve_global_address` /
`_global_addresses`）被 `tests/test_vision.py` 直接引用，并按**本模块
对象** monkeypatch；拆出后这些 patch 目标要逐处改指新模块，等于把"传输层守卫"
与"解析层守卫"人为分开。而生产侧只有 `ImageParser._download_image_data_url`
一个调用方——扇入低意味着拆分收益也低。属"高 churn、零行为收益"的纯文件搬迁。

替代做法是文件顶部补齐与其余模块同款的结构说明（拥有 / 不拥有 + 分区目录）：
让读者拿到定位索引，不复用文件边界。`webapi.py` 同理，一并补齐。将来若测试改为
只依赖公开接口，可重新评估拆分。

## 门禁与守卫的准入证据

新增门禁前先证明「现有 ruff/pytest 抓不到目标缺陷」。以下是仍在生效的守卫
各自的准入证据。

**设置页字面量 id 契约**（`tests/frontend_contract.test.mjs`）：`app.js` 的 `$("id")` 与
`chrome.mjs` 的 `getElementById("id")` 拼错、或页面删掉对应元素，都不抛异常——调用点
普遍有 `if (el)` 守卫，用户只是静默少一块功能。把 `whitelistSummary` 拼成
`whitelistSummaryTYPO` 时，其余全部用例（含浏览器用例）仍然全绿。
只做单向 JS ⊆ HTML：反向的孤儿 id 是无害死标记，且会在 `<svg><use href="#…">` 与
`aria-*` 锚点上误报，豁免名单本身会腐烂。

**`RUF100`**（`pyproject.toml`）：为未启用规则写的 `noqa` 让人以为某处已被忽略，实际
没有。实测存量里确有此类指令（`tests/test_adapters.py` 的 `N802`：`__signature__`
是 dunder，`--select N802` 对该文件也是 All checks passed）。
注意别用 `ruff check --select RUF100` 去复核存量：`--select` 会整体**替换**配置里的
选择集，`F401` 随之不在启用之列，两条 `# noqa: F401` 会被连带报成「未启用」——那是
命令副作用，不是存量问题。只用配置本身跑。

**日志断言一律经 `capture_logs(模块.logger)`，禁用 `caplog.at_level(..., logger="astrbot")`**
（`tests/test_observability.py`、`tests/test_runtime_adapter.py`）：生产代码都
`from astrbot.api import logger`，而测试里桩 logger 的 name 是 `host_stubs.py` 自己起的
`selfreply-main-test`——传 `"astrbot"` 时级别提升落在一个不相关的 logger 上。现状能过
纯属巧合（桩 logger `propagate=True`，caplog 的 handler 挂在 root），一旦宿主侧改成
`propagate=False`，所有日志断言恒空且无人会发现。配套地，承重日志在被测文档里承诺了
级别时，断言必须钉 `record.levelno`：否则「降到 INFO 被噪音淹没」「升到 ERROR 触发无关
告警通道」两个方向都不报。实测：把泄漏告警 `logger.warning` 改成 `info` 或 `error`，
两条方向都能被 `test_leak_warning_task_threshold` 捕获。

**`POST /ui/theme` 关停门有行为断言**（`tests/test_webapi.py::test_api_post_ui_theme_paths`）：
teardown 之后落盘的偏好会在下次启动被读回，用户看到「已被丢弃」却仍然生效的旧设置。
该门在生产里是**两层**（锁外预检 + 锁内复查），只删一层另一层兜住，行为断言
锁住两层并存，与本仓库其余端点（config / image-cache）的口径一致。

## 核实后刻意不改的项

以下都是「看起来能删/能收，实测后判定不该动」的项，不必重新测一遍：

- **`PipelineReply.direct_send_count` / `direct_texts`（`models.py`）**：是对
  `AttemptLedger` 的视图式读取，**有 18 处测试读者**（`test_generation_runner` /
  `test_main_runtime` 里的 `result` 就是 `PipelineReply`）。删它要改写 18 处断言为
  `.ledger.*`，生产侧零收益，只是把测试更深地绑到内存结构。
- **`MessageRecord.sender_id`**：不是会话级中转字段，而是消息记录的固有字段，
  且是去重语义用例的证据标记；删除要重写多个测试文件而不改变任何行为。
  真正纯中转的 `SessionState.last_active_sender_id` 已删（见 `storage._load_session_record`
  不再读该键；旧文件里的多余键由 `raw.get` 忽略，不需要 `STATE_VERSION` 迁移）。
- **`SessionState.last_proactive_text`**：`BEHAVIOR_CONTRACT.md` §1 具名（DELIVERED 时
  写入），删它要改契约，超出「只做收益为正的收敛」边界。
- **`AttemptState` / `SuppressCode` 的只写成员**：它们是分类域（枚举成员即语义标签），
  删成员要另造一套等价表达，负收益。
- **理由型注释的体量**：`docs` 另计，生产代码里的注释主体是**理由型**注释（为何不
  那样写、哪条边界是刻意的）；它们是该仓库可评审性的来源，收敛它只会让下一个读者
  重新推导一遍。变更史、评审轮次与外部条目号不属此类，不该保留。
- **双层防护中的冗余层**：例如图片端口白名单与传输层地址校验重叠——去掉任一层
  都有另一层兜住，行为等价；这是刻意的纵深，不是重复实现。
- **`compat_check._runtime_api_gaps` 不改成行为冒烟**：曾试过把 52 行签名枚举换成
  「对 `http://127.0.0.1/x.png` 跑一次 `_fetch_image_data_url` 断言返回 None」。
  撤回理由两条：一是**净代码零缩减**（`with` + async probe + 标签字典反而更长），
  没换来任何维护面收益；二是冒烟要 import `ImageParser` → 需要真实 `astrbot`，
  而签名枚举只依赖 httpx/httpcore，`_bootstrap()` 的假包路径（本地未装宿主时）也能跑。
  签名枚举守的「本仓库**直接使用**的第三方 API 形态」确实与依赖上界不同层：上界只挡
  大版本，挡不住小版本的签名变化。
- **扩充 ruff 规则集**：逐条实测后只加了 `RUF100`。`S110`+`SIM105`（吞异常）里
  `S110` 默认只报裸 `except: pass` 与 `except Exception: pass`（16 条），开
  `check-typed-exception` 后涨到 39 条，而 `SIM105` 只有 18 条——差集全是**多 except
  子句**（既有取消/超时处理又有日志，`contextlib.suppress` 表达不了），要为一条零事故
  记录的门禁动 18–39 处并加一批 `noqa`，破坏运行时模块零 `noqa` 这个更有价值的现状。
  `ASYNC` 全仓 9 条全是误报（4 条是 httpcore `connect_tcp(timeout=)` 的必需签名、5 条
  是测试轮询），且它只认固定列表的阻塞调用——本仓两次真实的阻塞缺陷（`rglob` 遍历、
  `write_json_atomic`）它都抓不到。`BLE`(88) / `TRY`(73) / `PL`(65) / `EM`(54) /
  `SLF`(114) / `RUF`全量(2563) 是刻意写法与中文标点的 ambiguous-unicode 误报；
  `PTH` 会改行为（`image/extractor` 刻意用 `os.path.isabs or ntpath.isabs` 兼容异风格
  路径，`Path.is_absolute()` 不等价）；`TID` / `N` / `A` 分别撞上包名带连字符（宿主约定）
  与宿主 API 名 `filter`。
- **前端不引 ESLint / `tsc --checkJs` / CSS lint**：为 4.8k 行零构建前端新增
  devDependency 与配置的维护成本高于它能抓到的缺陷类；其中真会静默失效的一类
  （字面量 id 注册表）已由上面的 id 契约以约 20 行断言覆盖。
- **不追覆盖率**：未覆盖行按四类逐行归类，96% 落在前两类，补它们只能靠构造宿主
  异常注入，属“为覆盖率补行”。归类如下（`pytest --cov=. --cov-report=term-missing`
  实测 270 行未覆盖，占比以该次运行为准）：

  1. **防御分支（约 49%）**：异常兜底、`not x` 早退、`return ""/None/False` 降级。
     例：`delivery.py` 的 quote/mention 组件构造失败静默降级（L146-148、L209-211）、
     `generation.py` 的任务结果回收（L58-60）、`storage.py` 的原子写失败路径
     （L148-149、L160-161）。这些分支的价值在于**存在**而非被执行——它们对应的
     是“宿主/磁盘/平台出错时不要崩”，触发条件是外部故障，不是代码路径。
  2. **宿主能力分支（约 47%）**：宿主配置对象签名差异、`save_config` 缺失、
     `get_messages`/`message_obj`/`raw_message` 形态差异、`set_extra` 老宿主未实现。
     例：`storage.py` 的 `_config_to_dict`/`_persist_config_obj` 兜底（L41、L80-81）、
     `adapters.py` 的签名探测回退、`image/extractor.py` 的组件字段读取差异
     （L33-38、L116）。触发条件是**换一个宿主版本**，本仓库只测 4.23.3 / 4.27.2 /
     latest 三条腿，其余版本的差异分支不被驱动。
  3. **二次回滚失败（约 3%）**：`whitelist.commit_change` 回滚再失败、
     `plugin_state.persist_enabled` 的二次回滚、`storage` 的状态文件备份失败。
     触发条件是“磁盘在回滚窗口内连续两次失败”，属可接受降级 + 告警路径。
  4. **注册面不可驱动（约 1%）**：`main.py` 的 10 个指令组函数
     （`selfreply`/`_help`/`_status`/`_list`/`_add`/`_remove`/`_check`/`_on`/`_off`/
     `_debug`）。类属性被宿主装饰器换成 `RegisteringCommandable`，真实宿主与
     `host_stubs` 都取不回原函数，其行为不可被任何测试驱动。两条路径的等价由别名
     契约 + 装饰器委托契约钉住（见下文“不改双指令路径架构”）。

  按第 4 类剔除后的**可驱动口径覆盖率为 93.21%**（3843 条可驱动语句、261 行未覆盖），
  与 `fail_under = 89` 之间有 4.21 点缓冲。缓冲偏大意味着门槛对「新增未覆盖代码」
  不敏感：一次改动覆盖不到十行也不会把总覆盖率拉下 0.2 点。要收紧应先改这里的分母
  口径（例如把第 4 类从 `[tool.coverage.run] omit` 表达出去），而不是直接抬数字——
  `omit` 只按文件路径匹配，而指令组函数必须在 `Star` 子类内（宿主
  `selfreply.command` 装饰器依赖类属性），移不出去。

  **同步义务**：改动上述任一类未覆盖行时，必须同步本归类（行号与占比以最近一次
  `pytest --cov=. --cov-report=term-missing` 实测为准）。
- **测试去重的判据是「被更强断言覆盖」，不是「行数差不多」**：收敛掉的重复用例
  各有一条覆盖它的用例，且覆盖方的断言集是它的超集（例：`test_config_schema.py` 的
  「规格表键 == schema 键且顺序一致」蕴含另两条只做集合比较的用例；`CONTAINER_HOLDERS`
  表驱动用例逐一枚举 11 个持有者绑定，强于原先抽查 4 个的那条）。
  剩下的不重复靠这条纪律保持：**新用例若与既有用例断言同一事实，必须说明覆盖方为何
  不是超集**，说不出来就不加。
- **不重构 `style.css`**：文件内零 id 选择器；重复规则体**绝大多数**是单声明出现在
  不同选择器上下文（实测 13 组里 6 组是多声明，去掉刻意一致的两份深色令牌块后仍余 5 组：
  `h2`/`.master-copy b`、`.sidenav-link:focus-visible`/`.mtab:focus-visible`、
  `.theme-toggle:hover`/`.top-actions .action-refresh:hover`、三处焦点环、两处动作行
  flex）。那 5 组是同一视觉模式在互不相关的语义上下文里的重复，合并要么引入跨组件分组
  选择器、要么加一层自定义属性间接，收益为零而视觉回归风险不可控。
  另有两份刻意保持一致的深色令牌块（已有用例钉住）。
- **不改双指令路径架构**：删内联路径会丢 `_is_command_entry` 的裸词保护与
  `COMMAND_HANDLED_KEY` 去重；删装饰器路径会让宿主失去指令组注册与权限声明。两条路径
  的等价由别名契约 + 装饰器委托契约共同钉住，成本远低于重构。

## `webapi.py` 不拆

与 `models.py` / `image/parser.py` 同款理由：它并置五类关注点（路由注册与处理器绑定、
配置读视图、严格校验、应用与回滚、审计 + UI 偏好 + 运维 status），扇入面只有
`main.py` 的 `bind_api_handlers` / `register_web_apis` 与 `tests/test_webapi.py`；
拆文件要同步改这两处引用面，属高 churn、零行为收益的纯搬迁。文件顶部已补齐与其余模块
同款的「拥有 / 不拥有 + 分区目录」结构说明，阅读定位靠它而不是文件边界。

## 前端契约的已知无守卫面

前端 4.8k 行、9 个文件、27 个配置键，契约覆盖面见两个测试文件的用例清单（不在此复述条数——数字会随用例增删而腐烂）。以下几类**刻意**不守，
改动前请自行评估后果，不要误以为有网兜住：

- **类选择器**（`.topbar` / `.sidenav-list` / `.sidenav-fade-*` / `.mtab` /
  `.sidenav-link[data-target]`）：`styles do not target element ids` 只断言 CSS 不用 id，
  不守 JS 用类锚定 DOM。类名重命名会让吸顶、导航偏移、渐隐提示静默失效。
- **`body.is-ready`**：样式表零消费，唯一读者是浏览器用例的 `toHaveClass(/is-ready/)`，
  是「模块已启动」的测试锚而非视觉状态。
- **三档超时**（8s 内联 boot fail / 12s `BOOT_TIMEOUT_MS` / 15s `FETCH_TIMEOUT_MS`）：
  分散三处且语义不同（前者是脚本加载失败，后两者是配置加载 deadline 与单次 API 上限），
  不收敛。
- **源码文本断言**（`assert.match(源码)` / `.includes`）：那批是**防删除锚**，不是行为
  契约——改写法即红，与真实行为无关。要守行为请补浏览器/契约行为用例（参照
  `save validation guards on whitelist before the numeric scan` 由源码顺序改为行为断言）。

## Provider 手动态：容器类由控件自己挂，不靠 per-spec 回调

三个 Provider 字段（judge / vision / visionJudge）的「手动输入 ↔ 使用列表」切换
形态同源：`pages/主动回复设置/providers.mjs` 的 `setManual` 在读入 `refs.field`
时自行 `classList.toggle("manual", …)`，`app.js` 的 `PROVIDER_CONTROLS` 每一项都必须
传 `field`。

**不要退回「让 judge 的 `onModeChange` 加类」那种写法。** 那是本缺陷的成因：
`manual` 类只在 judge 的 spec 上有回调，vision 两个字段切手动后容器类恒为空，
于是基类 `.provider-control` 的两列定义继续生效，按钮被拉成
整行宽。实测 1440×1000 下按钮宽 373px，而 judge 同态 78px。

为什么 CSS 不用改（这是一个容易误判的点）：`.provider-field.manual .provider-control`
含 **3 个类**（特异性 0,3,0），基类 `.provider-control` 只有 **1 个**
（0,1,0）。前者本来就压过后者，与源码先后位置无关；此前失败的唯一原因是那个类
从未被挂到 vision 容器上。给 vision 补 `:not(.manual)` 或再写一条
`.provider-field.manual` 覆盖规则都是多余的。

守卫：`tests/frontend_browser.test.mjs` 的
`vision provider fields lay out on one row like the judge field` 在手动态量取
容器类名、列数与按钮宽度（阈值 100px 不锁像素），并反向断言切回列表态恢复两列。

## 吸顶态不得改变 topbar 的占位高度

`.topbar` 是页面首个 `position: sticky` 元素，粘附态只能用 `border-color` /
`background` 表达。**不得**在 `.is-stuck` 里改 `padding`、`margin`、`height` 等任何
影响占位高度的属性。

原因：粘附阈值在 `chrome.mjs` 是 `window.scrollY > 8`。曾用 `padding: 20px → 12px`
表达"变矮"，实测占位高度随之变化约 16px；浏览器滚动锚定为保持视觉锚点会补偿
`scrollY`，而 `scrollY` 又决定 `is-stuck` 是否保留——高度差一旦超过阈值就自激，
表现为页面接近最顶部时疯狂抖动（实测 1.2s 内 class 翻转 129 次）。

- 守卫：`tests/frontend_browser.test.mjs`
  `topbar must not change its height when the stuck class toggles`
  （还原 padding 收缩即红，已用自变异确认）
- 视觉收缩需求请改用不占布局高度的手段，并先确认不会缩放文字。

## `--topbar-h` 由运行时写回，不违反上面的防抖动约束

`--topbar-h` 的三个静态值（`:110` / `@1024` / `@720`）实测全与顶栏真实高度不符
（中屏 64 vs 83 → 滚动后侧栏被盖 19px；窄屏换行时 62 vs 115 → 锚点标题被遮 18px），
故改由 `chrome.mjs` 的 `syncTopbarHeight` 用 `ResizeObserver` 观测 `.topbar` 实测
高度写回，静态值降为无 JS 时的兜底。

**这不与上一节冲突**：那条约束针对的是改 `.topbar` 自身的**占位属性**；本机制只改
一个**不被顶栏消费**的变量（`--topbar-h` 的 4 个消费点全在 `.sidenav` 与
`scroll-margin-top`）。实测确认无自激：观测写回后 `writes: 1`、`distinctHeights: [83]`
（只写一次即收敛）；把该变量改成 200px / 20px 时 `.topbar` 高度恒为 83px（完全独立）。

整数值守卫（测得值与当前变量相同时跳过写回）**没有测试钉住**：实测同值
`setProperty` 不产生 style mutation、也不触发 `ResizeObserver` 回调，属行为等价写法；
该行由 `frontend_contract` 的源码文本断言（防删除锚）保护。

## 浏览器用例的「等首屏」预算与断言预算分开

`frontend_browser.test.mjs` 的 `openPage()`（及其 3 处同形态 `goto` 后等待）等
`#boot` 隐藏时显式传 `BOOT_WAIT_MS = 15s`，不用 `playwright.config.mjs` 的
`expect.timeout`（5s）。

原因：5s 是**断言**预算，不是**加载**预算。页面自身给首屏的是 12s 看门狗 +
每次抓取 15s 硬上限，即页面允许自己慢到 12s 才判定失败；测试用 5s 截断它，
在机器负载高时就会偶发超时。这类超时的特征是**失败点随负载漂移**（报在哪条
用例上不固定），与用例自身逻辑无关——排查时先看失败是否总落在同一用例，
再看它是否总落在首屏等待上。

这不放松任何断言：页面真加载失败时看门狗仍会隐藏 boot，各用例自己的
读值/toast/`errors` 断言照旧失败。
（`fetch-pending` 那条不受影响：它把 `FETCH_TIMEOUT_MS` 压到 30ms，走快速失败分支。）

---

# 每会话内存基准

本页把"拍脑袋常数"（缓存容量、消息上限）改写为可推导的公式，并给出
实测数据（CPython 3.14 / x64）。数值为上限估算：deque 容器随
`maxlen` 预分配，深度计算含嵌套对象，实测见下文表。

KB 数字是历史 `sys.getsizeof` 深度求和，没有公式测试钉住。
图片字节预算的行为由会话协调器与图片缓存测试锁定。

## 每会话内存组成

| 组件 | 容量公式 | 上限说明 |
| --- | --- | --- |
| 会话状态固定字段 | `F`（实测 ≈0.95 KB） | SessionState 8 字段 + deque 容器 |
| 历史消息 | `R × M` | `R = recent_message_limit`（配置 3..100），`M` = 单条 MessageRecord |
| 事件缓存 | `E`（≈0.1 KB） | `_last_events` 每会话 1 个宿主事件引用 + 时间戳 |
| 图片索引 | `I × V × G` | `I = MAX_CACHED_IMAGE_EVENTS(20)` 含图事件数，`V = vision_max_images`（配置 1..5），`G` = 单张 ImageInfo |

**单会话内存上限（不含图片本体）**

```
B(session) = F + R×M + E + I×V×G
默认配置（R=20, V=2）: ≈ 0.95 + 20×0.33 + 0.1 + 20×2×0.21 ≈ 13.6 KB
最坏配置（R=100, V=5）: ≈ 0.95 + 100×0.33 + 0.1 + 20×5×0.21 ≈ 55.1 KB
```

**全量内存**

```
B(total) = N × B(session) + 全局表（O(N)：delay/running/白名单运行时映射）
N = 活跃会话数（白名单上限 MAX_WHITELIST_SIZE = 1000）
最坏 N=1000、R=100、V=5：≈ 55 MB（不含图片本体）
```

## 图片本体（数据 URL 冻结）

正常路径把冻结图片写入内容寻址的磁盘缓存，`ImageInfo.prepared_source` 只保留
路径；磁盘不可用时才保留 data URL。内存回退按**原始载荷字节数**计数；热路径增量维护已入账字节，不再每次全量 `b64decode`。预算同时受：

- `MAX_SESSION_IMAGE_MEMORY_BYTES = 16 * 1024 * 1024`：单会话图片索引预算；
- `MAX_IMAGE_MEMORY_BYTES = 64 * 1024 * 1024`：所有会话图片索引共享预算；
- `MAX_IMAGE_BYTES = 10 * 1024 * 1024`：单张图片输入上限。

超出预算的图片不会进入会话索引，并记录 WARNING；淘汰按最旧图片事件进行，
不会静默无限增长。Vision 描述缓存另受 `MAX_IMAGE_DESCRIPTION_CACHE_BYTES = 512 * 1024`
和 50 条条目上限约束。磁盘冻结缓存仍受 `MAX_IMAGE_CACHE_BYTES = 256 * 1024 * 1024`
容量清理约束。

## 实测数据（CPython 3.14 / x64，sys.getsizeof 深度求和）

| 对象 | 深度大小 |
| --- | --- |
| 空 SessionState（maxlen=100） | 0.95 KB |
| MessageRecord（20 字中文消息） | 0.33 KB |
| SessionState 满 100 条 | 14.8 KB |
| ImageInfo（含 prepared_source） | 0.21 KB |
| 图片索引满（20 事件 × 2 张） | 5.7 KB |

## 常数与行为测试

各常数的单位按**代码同型表达式**书写（可 grep 比对，避免 MiB/KiB 换算歧义）：

- `MAX_CACHED_IMAGE_EVENTS × vision_max_images` = 每会话图片索引张数上限
- `MAX_SESSION_IMAGE_MEMORY_BYTES = 16 * 1024 * 1024` = 单会话 data URL 原始载荷字节上限
- `MAX_IMAGE_MEMORY_BYTES = 64 * 1024 * 1024` = 全局 data URL 原始载荷字节上限
- `MAX_IMAGE_DESCRIPTION_CACHE_BYTES = 512 * 1024` = Vision 描述内存缓存上限
- `MAX_IMAGE_CACHE_BYTES = 256 * 1024 * 1024` = 磁盘冻结缓存总容量上限
- `MAX_RECENT_MESSAGE_LIMIT = 100` = 每会话历史消息条数上限（recent deque maxlen）
- `MAX_IMAGE_BYTES = 10 * 1024 * 1024` = 单张图片输入上限

字节预算行为：

- 会话 / 全局 data URL：`tests/test_session_coordinator.py`
- Vision 描述 LRU：`tests/test_image_cache.py`
- 单张输入上限：`tests/test_vision.py`

描述 LRU 的字节预算只按**值**大小记账，不含 key：`ImageInfo.cache_key()` 对
超长值（磁盘缓存不可用时的 data URL 回退）做 sha256 摘要化，故 key 长度有上界、
不会逃出 `MAX_IMAGE_DESCRIPTION_CACHE_BYTES`。两条共同保证「键的开销不在预算外」。

改常数时同步本节；不要为 KB 估算补公式测试。

## 已知边界：命令路径回滚不覆盖调度器侧的三张表

`whitelist.commit_change` 的双写失败回滚只恢复**它自己拥有的状态**：白名单集合、
`sessions`（含 `pruned` 快照）、`_runtime_umos`。调度器侧由引用持有的三张表
（`main._last_events` / `main._delay_tasks` / `running_sessions` 相关集合）不在其中
——它们由 `scheduler` 通过构造期注入的引用直接操作，`whitelist.py` 拿不到，也不该
反向依赖。

后果：`/selfreply remove` 遇到「内存剪枝成功、磁盘双写失败」时，该会话在
`whitelist` 侧已复活，但调度器的在途检查/延迟任务可能已被取消 → 该会话**静默停摆
一次**（巡检不再访问它），下一条消息到达时自愈（消息路径会重建在途状态）。

**为什么不在 `whitelist.commit_change` 里补**：那要求把三张表的回滚钩子注入
白名单层，形成「存储层反向编排调度层」的耦合，而收益只是消掉一次**有自愈路径**的
静默期。webapi 路径（配置保存）不受影响：它走的是 `plugin_state` 侧的快照-回滚，
不经过 `_prune`。**这是刻意的取舍，不是遗漏**——若将来调度器改为事件驱动重建在途
状态，此处可一并消除。


