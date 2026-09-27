# 结构决策

行为不变量见 `BEHAVIOR_CONTRACT.md`。本页只记结构取舍。

## 发布产物

发布主路径是 AstrBot 插件市场（git 仓库）。手工部署包（VPS 直投 zip）用
`git archive --format=zip -o <name>.zip HEAD` 导出：排除规则单点声明在仓库根
`.gitattributes` 的 `export-ignore`，未跟踪/被 .gitignore 排除的文件天然不进包。

不采用「hatch 构建 wheel → `check_wheel`/`check_sdist` 内容断言 → 从 wheel
派生部署 zip」的理由：那套三层互锁（pyproject exclude 列表 ↔ 检查脚本禁运名单 ↔
pathspec 交叉核验）要求三份名单同步，任一处漏改就静默失守，而分发主路径不产生
wheel，全部维护成本只服务于次要路径；`git archive` + `export-ignore` 把同一保证变成
单点声明，“缓存泄漏进包”这类问题在结构上不再存在。pyproject 的 wheel/sdist 配置保留
（本地构建仍干净），但不再有发布链依赖它。

## 双面板

`CONFIG_SPECS.surfaces` 区分官方 Dashboard（`host`）与自定义设置页（`panel`）。常用键上自定义页；巡检、勿扰、回复长度、日上限、`log_reply_content` 等只在 Dashboard。两套面板读写同一份配置。前端可写键必须等于 panel 面，由 `test_fe_writable_keys_match_panel_surfaces` 锁定。

GET `/config` 是 panel 视图：只回 panel 键加 `runtime_enabled` / `decision_prompt_default` / `config_revision`。POST `/config` 接受全部 schema 键，测试与旧客户端可改巡检、勿扰、日上限；无 `base_revision` 的调用仍可串行写入（前端始终携带 revision）。官方 Dashboard 走宿主配置文件，自定义页走本 API。

## 装配

协作对象接线在 `main._assemble_components`。循环依赖与宿主热替换用调用期查找。

`webapi` 用 `TYPE_CHECKING` 导入 `SelfInitiatedReplyPlugin`，避免与 `main` 运行时成环。注解只给编辑器跳转：函数都经 `partial` 再注册，宿主看不到 `__annotations__`。`ignore_missing_imports` 下 mypy 把 `Star` 子类看成 `Any`，这些注解不产生类型检查力。

## 设置页 chrome

浅/深/跟随系统与压暗/粗体均经 `GET/POST ui/theme` 写入 `ui_prefs.json`（页面在 Dashboard iframe 内，localStorage 不可靠）。POST 只提交**用户真的动过**的键，后端对未提交的键保持原值：GET 返回前的渲染态是本地默认（主题恒为 `auto`、压暗/粗体恒为关），把它一并提交会把服务端已存的选项静默改掉。守卫是 `theme.mjs keeps the dim/bold submission behind the touched guard` 与浏览器用例对请求体的反向断言。落盘与状态文件共用 `storage` 原子写。服务端 prefs 覆盖 localStorage，但 `GET ui/theme` 返回前用户已点过主题、压暗或粗体则那次点击优先，迟到的 GET 不得抹掉；本地缓存也只能经守卫后的 `applyTheme` 落盘（`restoreTheme` 不自行写缓存）。

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

生命周期由插件 owner 持有 `RUNNING`、`STOPPING`、`DEGRADED` 三态。停止等待使用硬时间边界；仍吞取消的生成、patrol 或最终状态保存 task 进入 quarantine 并触发 `DEGRADED`，后续 spawn、巡检、force check、工具直发和最终回复全部拒绝。隔离即进入 `DEGRADED` 且不可逆，不提供在线恢复命令；任务结束后仅从注册表移除，恢复仍依赖插件重载或宿主重启。

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

## 图片链路与宿主兼容层

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
一个调用方，扇入低意味着拆分收益也低。属"高 churn、零行为收益"的纯文件搬迁。

替代做法是文件顶部补齐与其余模块同款的结构说明（拥有 / 不拥有 + 分区目录）：
让读者拿到定位索引，不复用文件边界。`webapi.py` 同理，一并补齐。将来若测试改为
只依赖公开接口，可重新评估拆分。

## 门禁与守卫的准入证据

新增门禁前先证明「现有 ruff/pytest 抓不到目标缺陷」。以下是仍在生效的守卫
各自的准入证据。

**设置页字面量 id 契约**（`tests/frontend_contract.test.mjs`）：`app.js` 的 `$("id")` 与
`chrome.mjs` 的 `getElementById("id")` 拼错、或页面删掉对应元素，都不抛异常：调用点
普遍有 `if (el)` 守卫，用户只是静默少一块功能。把 `whitelistSummary` 拼成
`whitelistSummaryTYPO` 时，其余全部用例（含浏览器用例）仍然全绿。
只做单向 JS ⊆ HTML：反向的孤儿 id 是无害死标记，且会在 `<svg><use href="#…">` 与
`aria-*` 锚点上误报，豁免名单本身会腐烂。

**`RUF100`**（`pyproject.toml`）：为未启用规则写的 `noqa` 让人以为某处已被忽略，实际
没有；存量里确实出现过此类指令（`tests/test_adapters.py` 曾挂 `N802`，而 `__signature__`
是 dunder，该规则对该文件本就是 All checks passed）。
注意别用 `ruff check --select RUF100` 去复核存量：`--select` 会整体**替换**配置里的
选择集，`F401` 随之不在启用之列，在用的 `# noqa: F401` 会被连带报成「未启用」，那是
命令副作用，不是存量问题。只用配置本身跑。

**日志断言一律经 `capture_logs(模块.logger)`，禁用 `caplog.at_level(..., logger="astrbot")`**
（`tests/test_observability.py`、`tests/test_runtime_adapter.py`）：生产代码都
`from astrbot.api import logger`，而测试里桩 logger 的 name 是 `host_stubs.py` 自己起的
`selfreply-main-test`，传 `"astrbot"` 时级别提升落在一个不相关的 logger 上。现状能过
纯属巧合（桩 logger `propagate=True`，caplog 的 handler 挂在 root），一旦宿主侧改成
`propagate=False`，所有日志断言恒空且无人会发现。配套地，承重日志在被测文档里承诺了
级别时，断言必须钉 `record.levelno`：否则「降到 INFO 被噪音淹没」「升到 ERROR 触发无关
告警通道」两个方向都不报（`test_leak_warning_task_threshold` 即按级别断言，两个方向的
降级都判红）。

**`POST /ui/theme` 关停门有行为断言**（`tests/test_webapi.py::test_api_post_ui_theme_paths`）：
teardown 之后落盘的偏好会在下次启动被读回，用户看到「已被丢弃」却仍然生效的旧设置。
该门在生产里是**两层**（锁外预检 + 锁内复查），只删一层另一层兜住，行为断言
锁住两层并存，与本仓库其余端点（config / image-cache）的口径一致。

## 核实后刻意不改的项

以下都是「看起来能删/能收，核实后判定不该动」的项：

- **`PipelineReply.direct_send_count` / `direct_texts`（`models.py`）**：是对
  `AttemptLedger` 的视图式读取，多处测试以 `result` 直接读它
  （`test_generation_runner` / `test_main_runtime`）。删它要把这些断言改写成
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
- **双层防护中的冗余层**：例如图片端口白名单与传输层地址校验重叠：去掉任一层
  都有另一层兜住，行为等价；这是刻意的纵深，不是重复实现。
- **`compat_check._runtime_api_gaps` 用签名枚举，不改成行为冒烟**：冒烟形态（真跑一次
  `_fetch_image_data_url` 断言返回 None）净代码更长，且要 import `ImageParser` → 需要
  真实 `astrbot`；签名枚举只依赖 httpx/httpcore，`_bootstrap()` 的假包路径（本地未装
  宿主时）也能跑。它守的「本仓库**直接使用**的第三方 API 形态」与依赖上界不同层：
  上界只挡大版本，挡不住小版本的签名变化。
- **扩充 ruff 规则集只到 `RUF100` 为止**：逐条核实后否决的判据（命中数是版本相关的，
  不在此复述）：`S110`+`SIM105` 的差集是**多 except 子句**（既有取消/超时处理又有日志，
  `contextlib.suppress` 表达不了），采纳要为一条零事故记录的门禁动几十处并加一批
  `noqa`，破坏运行时模块零 `noqa` 这个更有价值的现状。`ASYNC` 只认固定列表的阻塞调用，
  本仓真实出现过的阻塞缺陷（`rglob` 遍历、`write_json_atomic`）它都抓不到，命中则是
  httpcore `connect_tcp(timeout=)` 的必需签名与测试轮询。`BLE` / `TRY` / `PL` / `EM` /
  `SLF` / `RUF` 全量撞上刻意写法与中文标点的 ambiguous-unicode 误报；`PTH` 会改行为
  （`image/extractor` 刻意用 `os.path.isabs or ntpath.isabs` 兼容异风格路径，
  `Path.is_absolute()` 不等价）；`TID` / `N` / `A` 分别撞上包名带连字符（宿主约定）
  与宿主 API 名 `filter`。
- **前端不引 ESLint / `tsc --checkJs` / CSS lint**：为零构建前端新增 devDependency 与
  配置的维护成本高于它能抓到的缺陷类；其中真会静默失效的一类（字面量 id 注册表）
  已由上面的 id 契约以少量断言覆盖。
- **不追覆盖率**：门槛以 `pyproject.toml` 的 `fail_under` 为准。未覆盖行集中在四类，
  补它们只能靠构造宿主异常注入，属“为覆盖率补行”，不做：

  1. **防御分支**：异常兜底、`not x` 早退、`return ""/None/False` 降级（`delivery` 的
     quote/mention 组件构造失败、`generation` 的任务结果回收、`storage` 的原子写失败）。
     价值在于**存在**而非被执行：对应“宿主/磁盘/平台出错时不要崩”。
  2. **宿主能力分支**：宿主配置对象签名差异、`save_config` 缺失、`get_messages` /
     `message_obj` / `raw_message` 形态差异、`set_extra` 老宿主未实现（`storage` 的
     `_config_to_dict`/`_persist_config_obj` 兜底、`adapters` 的签名探测回退、
     `image/extractor` 的组件字段读取差异）。CI 只跑三条宿主腿，其余版本的差异分支
     不被驱动。
  3. **二次回滚失败**：`whitelist.commit_change` 回滚再失败、`plugin_state.persist_enabled`
     的二次回滚、`storage` 的状态文件备份失败。触发条件是磁盘在回滚窗口内连续两次失败，
     属可接受降级 + 告警路径。
  4. **注册面不可驱动**：`main.py` 的指令组函数。类属性被宿主装饰器换成
     `RegisteringCommandable`，真实宿主与 `host_stubs` 都取不回原函数，其行为不可被任何
     测试驱动；两条路径的等价由别名契约 + 装饰器委托契约钉住（见下文“不改双指令路径架构”）。

  要收紧门槛应先改第 4 类的分母口径，而不是直接抬数字：`omit` 只按文件路径匹配，而指令组
  函数必须在 `Star` 子类内（宿主 `selfreply.command` 装饰器依赖类属性），移不出去。
- **测试去重的判据是「被更强断言覆盖」，不是「行数差不多」**：收敛掉的重复用例
  各有一条覆盖它的用例，且覆盖方的断言集是它的超集（例：`test_config_schema.py` 的
  「规格表键 == schema 键且顺序一致」蕴含另两条只做集合比较的用例；`CONTAINER_HOLDERS`
  表驱动用例逐一枚举全部持有者绑定，强于原先只抽查部分的那条）。
  剩下的不重复靠这条纪律保持：**新用例若与既有用例断言同一事实，必须说明覆盖方为何
  不是超集**，说不出来就不加。
- **不重构 `style.css`**：文件内零 id 选择器；重复规则体绝大多数是同一声明出现在
  互不相关的选择器上下文（焦点环、`:focus-visible` 系列、hover 态、动作行 flex 等），
  合并要么引入跨组件分组选择器、要么加一层自定义属性间接，收益为零而视觉回归风险
  不可控。两份深色令牌块是刻意一致，已有用例钉住。
- **不改双指令路径架构**：删内联路径会丢 `_is_command_entry` 的裸词保护与
  `COMMAND_HANDLED_KEY` 去重；删装饰器路径会让宿主失去指令组注册与权限声明。两条路径
  的等价由别名契约 + 装饰器委托契约共同钉住，成本远低于重构。

## `webapi.py` 不拆

与 `models.py` / `image/parser.py` 同款理由：它并置五类关注点（路由注册、
配置读视图、严格校验、应用与回滚、审计 + UI 偏好 + 运维 status），扇入面只有
`main.py` 的 `register_web_apis` 与按函数本体调用处理器的测试；
拆文件要同步改这两处引用面，属高 churn、零行为收益的纯搬迁。文件顶部已补齐与其余模块
同款的「拥有 / 不拥有 + 分区目录」结构说明，阅读定位靠它而不是文件边界。

## 前端契约的已知无守卫面

契约覆盖面见两个前端测试文件的用例清单（条数会随用例增删腐烂，不在此复述）。
以下几类**刻意**不守，改动前请自行评估后果，不要误以为有网兜住：

- **类选择器**（`.topbar` / `.sidenav-list` / `.sidenav-fade-*` / `.mtab` /
  `.sidenav-link[data-target]`）：`styles do not target element ids` 只断言 CSS 不用 id，
  不守 JS 用类锚定 DOM。类名重命名会让吸顶、导航偏移、渐隐提示静默失效。
- **`body.is-ready`**：样式表零消费，唯一读者是浏览器用例的 `toHaveClass(/is-ready/)`，
  是「模块已启动」的测试锚而非视觉状态。
- **三档超时**（8s 内联 boot fail / 12s `BOOT_TIMEOUT_MS` / 15s `FETCH_TIMEOUT_MS`）：
  分散三处且语义不同（前者是脚本加载失败，后两者是配置加载 deadline 与单次 API 上限），
  不收敛。
- **源码文本断言**（`assert.match(源码)` / `.includes`）：那批是**防删除锚**，不是行为
  契约，改写法即红，与真实行为无关。要守行为请补浏览器/契约行为用例（参照
  `save validation guards on whitelist before the numeric scan` 由源码顺序改为行为断言）。

## Provider 手动态：容器类由控件自己挂，不靠 per-spec 回调

三个 Provider 字段（judge / vision / visionJudge）的「手动输入 ↔ 使用列表」切换
形态同源：`pages/主动回复设置/providers.mjs` 的 `setManual` 在读入 `refs.field`
时自行 `classList.toggle("manual", …)`，`app.js` 的 `PROVIDER_CONTROLS` 每一项都必须
传 `field`。

**不要退回「让 judge 的 `onModeChange` 加类」那种写法。** 三个字段共用一条路径才有保证：
`manual` 类只挂在 judge 的 spec 回调上时，vision 两个字段切手动后容器类恒为空，基类
`.provider-control` 的两列定义继续生效，按钮被拉成整行宽。

为什么 CSS 不用改（这是一个容易误判的点）：`.provider-field.manual .provider-control`
的特异性本来就压过基类 `.provider-control`，与源码先后位置无关；缺的只是那个类没挂上。
给 vision 补 `:not(.manual)` 或再写一条 `.provider-field.manual` 覆盖规则都是多余的。

守卫：`tests/frontend_browser.test.mjs` 的
`vision provider fields lay out on one row like the judge field` 在手动态量取
容器类名、列数与按钮宽度（阈值不锁像素），并反向断言切回列表态恢复两列。

## 吸顶态不得改变 topbar 的占位高度

`.topbar` 是页面首个 `position: sticky` 元素，粘附态只能用 `border-color` /
`background` 表达。**不得**在 `.is-stuck` 里改 `padding`、`margin`、`height` 等任何
影响占位高度的属性。

原因：粘附阈值在 `chrome.mjs` 是 `window.scrollY > 8`。用 `padding` 一类占位属性表达
"变矮"时，占位高度随之变化；浏览器滚动锚定为保持视觉锚点会补偿 `scrollY`，而
`scrollY` 又决定 `is-stuck` 是否保留，高度差一旦超过阈值就自激，表现为页面接近最顶部时
持续抖动。

- 守卫：`tests/frontend_browser.test.mjs`
  `topbar must not change its height when the stuck class toggles`
  （把占位属性改回收缩态即红）
- 视觉收缩需求请改用不占布局高度的手段，并先确认不会缩放文字。

## `--topbar-h` 由运行时写回，不违反上面的防抖动约束

`--topbar-h` 的三个静态值（`:110` / `@1024` / `@720`）与顶栏在各断点的真实高度都不相等，
侧栏让位与锚点偏移会差一截，故由 `chrome.mjs` 的 `syncTopbarHeight` 用 `ResizeObserver`
观测 `.topbar` 实测高度写回，静态值降为无 JS 时的兜底。

**这不与上一节冲突**：那条约束针对的是改 `.topbar` 自身的**占位属性**；本机制只改
一个**不被顶栏消费**的变量（`--topbar-h` 的消费点全在 `.sidenav` 与
`scroll-margin-top`），写回因此回不到上一条的自激回路。

整数值守卫（测得值与当前变量相同时跳过写回）**没有测试钉住**：同值 `setProperty` 不产生
style mutation、也不触发 `ResizeObserver` 回调，属行为等价写法；该行由
`frontend_contract` 的源码文本断言（防删除锚）保护。

## 浏览器用例的「等首屏」预算与断言预算分开

`frontend_browser.test.mjs` 的 `openPage()`（以及同形态直接 `goto` 后的等待）等
`#boot` 隐藏时显式传 `BOOT_WAIT_MS`，不用 `playwright.config.mjs` 的 `expect.timeout`。

原因：5s 是**断言**预算，不是**加载**预算。页面自身给首屏的是 12s 看门狗 +
每次抓取 15s 硬上限，即页面允许自己慢到 12s 才判定失败；测试用 5s 截断它，
在机器负载高时就会偶发超时。这类超时的特征是**失败点随负载漂移**（报在哪条
用例上不固定），与用例自身逻辑无关：排查时先看失败是否总落在同一用例，
再看它是否总落在首屏等待上。

这不放松任何断言：页面真加载失败时看门狗仍会隐藏 boot，各用例自己的
读值/toast/`errors` 断言照旧失败。
（`fetch-pending` 那条不受影响：它把 `FETCH_TIMEOUT_MS` 压到 30ms，走快速失败分支。）

## 内存与字节预算

上限全部落在具名常量里，单位口径按**代码同型表达式**读（避免 MiB/KiB 换算歧义）：
`MAX_RECENT_MESSAGE_LIMIT`（每会话历史条数）、`MAX_CACHED_IMAGE_EVENTS × vision_max_images`
（每会话图片索引张数）、`MAX_IMAGE_BYTES`（单张图片输入）、
`MAX_SESSION_IMAGE_MEMORY_BYTES` / `MAX_IMAGE_MEMORY_BYTES`（data URL 原始载荷，单会话与全局）、
`MAX_IMAGE_DESCRIPTION_CACHE_BYTES`（Vision 描述 LRU）、`MAX_IMAGE_CACHE_BYTES`（磁盘冻结缓存）。
会话内存随 `recent_message_limit` 与 `vision_max_images` 线性增长；图片本体只在磁盘不可用时
才以 data URL 留在内存，超预算的图片不进索引并记 WARNING。

字节预算的行为由三条测试钉住：会话与全局 data URL 见 `tests/test_session_coordinator.py`，
Vision 描述 LRU 见 `tests/test_image_cache.py`，单张输入上限见 `tests/test_vision.py`。
描述 LRU 只按**值**大小记账、不含 key：`ImageInfo.cache_key()` 对超长值做 sha256 摘要化，
key 长度因此有上界，预算不会从键上逃出。

KB 级估算（`sys.getsizeof` 深度求和）不写成文档也不补公式测试：它随 CPython 版本与
平台变，没有可维护的口径。

## 已知边界：命令路径回滚不覆盖调度器侧的三张表

`whitelist.commit_change` 的双写失败回滚只恢复**它自己拥有的状态**：白名单集合、
`sessions`（含 `pruned` 快照）、`_runtime_umos`。调度器侧由引用持有的三张表
（`main._last_events` / `main._delay_tasks` / `running_sessions` 相关集合）不在其中：
它们由 `scheduler` 通过构造期注入的引用直接操作，`whitelist.py` 拿不到，也不该
反向依赖。

后果：`/selfreply remove` 遇到「内存剪枝成功、磁盘双写失败」时，该会话在
`whitelist` 侧已复活，但调度器的在途检查/延迟任务可能已被取消 → 该会话**静默停摆
一次**（巡检不再访问它），下一条消息到达时自愈（消息路径会重建在途状态）。

**为什么不在 `whitelist.commit_change` 里补**：那要求把三张表的回滚钩子注入
白名单层，形成「存储层反向编排调度层」的耦合，而收益只是消掉一次**有自愈路径**的
静默期。webapi 路径（配置保存）不受影响：它走的是 `plugin_state` 侧的快照-回滚，
不经过 `_prune`。**这是刻意的取舍，不是遗漏**：若将来调度器改为事件驱动重建在途
状态，此处可一并消除。
