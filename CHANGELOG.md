# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的格式。
小版本（0.8.1~0.8.8）迭代细节省略，详见 git 历史。

## [0.9.5] - 2026-08-10

### 修复

- **在 AstrBot 4.27.2 上无法安装。** 安装时报「加载插件…时出现问题，原因：name
  'CommandReply' is not defined」，插件整体拒绝加载。指令处理器的返回类型注解用了一个
  只在类型检查期存在的名字，而新版宿主在加载期会真去解析这些注解。改为运行时可解析，
  4.23.3 与 4.27.2 均可安装。
- **不再破坏同一条消息上其他插件的发送钩子。** 主动回复期间本插件会临时接管事件的发送
  方法（用于统计工具直发），收尾时无条件还原——如果这期间另一个插件也包装了同一个方法，
  还原会把它的包装删掉或覆盖掉，那个插件在这条消息之后静默失效且无任何日志。现在只还原
  确认是自己装上的那一个。

## [0.9.4] - 2026-08-10

以「外部输入不可信」为主线的一批修复：状态文件、群聊内容与宿主对象形态都不再被当作
善意输入。无配置迁移。

### 行为变更

- **`/selfreply off` 与 `/on` 现在跨重启保持。** 此前它们只改内存运行态，宿主一重启就
  回落到配置里的开关：你打完 `/off` 以为插件不会再主动说话，重启后它继续发言，而没有
  任何提示告诉你要再打一次。现在开关直接落盘，回显也写明「重启后保持」。

### 安全

- **群聊内容不能再撑开提示词信封。** `<recent_chat>` 信封原样拼接消息文本，用户发一条
  含 `</recent_chat>` 的消息即可提前闭合信封，其后文字与插件自己的尾部指令同级
  （实测模型确实读到了信封外的攻击者指令）。改为在拼装处把信封标签换成全角尖括号，
  容忍空白与大小写变形；聊天记录里的代码片段、`<_<`、`List<int>` 不受影响。
- **状态文件时间戳两侧钳位。** `data/` 下的状态文件是可手工编辑的外部输入，此前只挡
  NaN/inf，负值与远未来原样穿透。远未来值让静默剩余变成数十年、该会话永久锁死且巡检
  每轮白跑；负值配合另一个字段可绕过「这条消息之后已回复过」的判据。改为钳到
  `[0, 当前时间 + 300s]`（覆盖 NTP 与容器时钟正常漂移）。

### 修复

- **配置回滚不再让会话静默死亡。** 回滚会把运行标记恢复成快照态，而支撑它的检查任务
  可能已结束、不会再释放闸门，裸等待将永久挂起。改为 30s 超时 + 20 轮上限，失同步
  降级为一次延迟或一次丢弃（丢弃记 WARNING），不再无声停止。
- 钩子抛异常（而非正常早退）时，生成流程仍会回收未结算的 reset 协程，不再只依赖各早退点
  自己关闭。
- 工具边界把「读不到宿主 `func_tool`」与「宿主声明本次无工具」分开处理：前者改为中止
  主动运行（fail closed），不再被当作「没有工具」放行。该路径由加载期断言保证**当前
  不可达**，属纵深防御；它的前提现在有测试守着，断言一旦被移走即恢复可达。
- 调试面板的状态接口补上异常兜底：它此前是唯一没有兜底的面板接口，异常会按宿主的方式
  呈现。**今天并无已知可达的异常**，补的是「内部细节不流向调用方」这层保证的缺口。
- 静默不足文案不再出现「-300s / 45s」这类负数。

### 分发

- 版本号收敛为单一事实源（`models.PLUGIN_VERSION`），wheel 文件名与 dist-info 由它派生。
- `coverage.json` / `*.egg-info/` / 嵌套 `__pycache__/` 不再进 wheel（此前 exclude 带根锚
  或只匹配后缀，实测有 220KB 覆盖率产物被打进发布包）。
- 开发依赖补齐版本上界，ruff 三处声明的同版要求改由门禁逐处核验。

## [0.9.3] - 2026-08-08

分发瘦身、结构收敛与质量留痕。无配置迁移、无命令契约改动，**但有三处真实行为变更**
（均为修复静默失效，见下方「行为变更」节）——不再宣称"功能行为零变更"。

### 行为变更

- **本地图片放行判据收紧（安全修复）。** 原实现采信提取层推断的
  `trusted_local_path`，宿主 aiocqhttp 走通用分支装配 `Image`（`file` 为对端可控的
  OneBot 原始值，且 `Image` 是 pydantic 组件而非 Mapping，恰好满足旧判据），
  可被伪造成任意绝对路径并被判为 host-trusted，构成任意文件读取。
  改为只认路径落在允许根内（`<data>` 与插件 image_cache），在 `_file_to_data_url`
  一处收口。**已知影响**：Telegram 配自建 Bot API 服务器时 `file_path` 在 `<data>`
  外，该场景图片被拒 → 降级为纯文本主动回复（默认 HTTPS 配置不受影响）。
  锁定版宿主 18 个平台适配器已逐个核过入站图片落盘位置，合法生产者全在 `<data>`
  子树内，无误拒回归；该核查已固化为测试（含宿主源码漂移预警）。
- **配置回滚不再让协作对象写孤儿容器（B1）。** `webapi._restore_plugin_state` 原先对
  `_last_events` / `_whitelist_runtime_umos` 用属性重绑定恢复，而 scheduler /
  coordinator / whitelist / gate 在装配时捕获的是容器对象本身；回滚后 main 从新 dict
  读、协作对象继续写旧 dict，该会话主动回复静默停止直到重启（不抛异常、无日志）。
  改为 `clear()` + 原地写入，5 个容器全部保持身份；`_last_event_at` /
  `_recent_image_events` / `sessions` 三个此前漏恢复的容器一并补上。
- **白名单双写回滚会还原被摘下的会话状态（B2）。** `commit_change` 持久化失败时原先
  只回滚白名单集合，被 prune 掉的会话状态（配额、冷却、观察窗口）不再放回，
  等价于"操作已失败但配额被清零"。改为回滚时 `_sessions.update(pruned)`；
  回滚本身再失败记 `logger.error` 并上抛。

### 分发

- `assets/` 4 张 banner 不再进 wheel（README 引用的是 GitHub 绝对链接，本地副本对分发无用）：
  **687,531 → 223,340 B，-67.5%**。资产仍留在 git 仓库。
- `scripts/check_wheel.py` 的 `FORBIDDEN_PREFIXES` 增加 `assets/` 断言，防 0.8.3 那类
  exclude 配置漂移复发（已做负向验证：注入 assets 后 exit=1 并报出泄漏路径）。

### 安全与可观测

- WebAPI 配置审计键 3 → 9：新增 3 个 Provider（`judge_provider_id` /
  `vision_provider_id` / `vision_judge_provider_id`）、2 个 Vision 开关与
  `ignored_sender_ids`。Provider 被改指向攻击者端点等于持续外发群聊上下文与图片，
  此前无审计痕迹；`ignored_sender_ids` 能静默屏蔽含管理员的特定用户。纯增日志。
- 图片下载失败日志的凭证脱敏：图床直链常把 Signature / rkey 放在 query，
  原实现会把整段 URL 写进日志。

### 内部结构

- `generation.py` 抽出模块级纯函数 `build_proactive_prompt()`，`generate()`
  247 → 232 行；提示词文案首次获得独立测试（此前零守护）。
- 新增 `image/context.py`：图片上下文的拼装与净化抽为纯函数 `format_image_context()`，
  含不可信声明常量。**图片方法组的整组外迁经实测否决**——`webapi.py` 的失效协议里
  标量重绑不会经共享引用传导，搬进服务对象会造成「改 Vision 超时不生效」的静默回归。
- `_event_extra` 迁入 `utils.py::event_extra`，与既有宿主字段探测函数归位。
- 13 处 `except Exception: pass` 补内联注释，说明各自为什么可以静默。
  其中 `utils.py` 的 `is_admin()` 兜底方向是 **fail-safe（判为非管理员）**，
  注释显式标注以防后人「优化」成 `return True`。
- 移除 `DebouncedStateSaver`（投递侧改为逐次原子落盘）与 `SessionPhase` FSM
  （五个阶段只写不读，纯记账）。

### 质量门禁

- 新增 `scripts/docstring_gates.py`（第 5 道 CI 门禁）：>50 行或 CC≥12 的函数必须有
  docstring。终态命中 **28** 个（5 个仅行数触发、5 个仅复杂度触发、18 个两者皆触发），
  缺失 → **0**。纯 stdlib，同时接入 pre-commit；`tests/` `scripts/` `.scratch/` 前缀排除。
  CC 阈值初版取 16，复审实测发现那是**死规则**——全仓 ≤50 行的函数 CC 最高只有 14，
  永远够不到 16，命中全部由行数触发。阈值从「当前实测最大值」反推只能保证当下
  全绿，代价是规则不生效；改按「想抓什么」定为 12 后独立命中 5 个短高复杂度函数。
- `scripts/coverage_gates.py` 新增 `utils.py`(86) 与 `image/extractor.py`(82) 两个模块门槛
  ——此前它们是薄弱模块中唯一无门禁者。
- 新增 `docs/COVERAGE_BLIND_SPOTS.md`：未覆盖行按「宿主异常兜底 / 防御性早退 /
  降级日志 / 宿主对象形态变体」四类逐行留痕，**只补第四类**。用 mock 硬凑宿主异常路径
  会锁死宿主 API 实现细节，宿主升级时先红的是测试。
- 由此翻出 6 处真实逻辑盲区并补测（`event_umo` 群组改写、`dedupe_message_records`
  保留最新、reason 截断、`content_to_text` 四形态、组件类型枚举形态、URL/file scheme 互换）。
  这些分支写错不抛异常，只静默取到空值。`utils.py` 86% → **91.3%**。
- `image/parser.py::cleanup_source_cache` 的目录回收改为显式空目录守卫，不再依赖
  「非空目录 rmdir 抛 ENOTEMPTY」这一 OS 错误语义（某些沙箱的文件系统钩子会让它成功
  并连带删除目录内文件）。副作用是该文件覆盖率 100% → 99.5%，属正确性换来的。
- 625 tests / 覆盖率 **95.83%**（fail_under 89）；ruff + ruff-format + docstring + mypy +
  coverage(12/12) + wheel 六道门禁全绿。新增测试全部经手工变异验证（改坏源码确认打红）。
- 删除 `scripts/mutation_check.py`（549 行自研工具，只做固定锚点串替换，不生成变异体、
  不算变异分数，对单插件仓不成比例）。方法论改由「新增测试必须做变异验证」的流程约定承担，
  历史锚点表可经 `git show 27d8864:scripts/mutation_check.py` 取回。

### 文档

- README 合并 `README.en.md` 为锚点小节（双文件长期不同步，删除孤立文件）。

## [0.9.2] - 2026-08-08

三要素极致收敛（可维护/最轻量/高质量）；功能行为零回归，兼容债一次性偿还。

### 行为变更

- WebAPI 配置接口只接受正式键：6 个兼容别名（cooldown_seconds / idle_trigger_seconds / min_context_messages / proactive_threshold / vision_enabled / whitelist）移除；旧前端发别名会得到显式「未知配置键」错误（此前被隐式接受）。
- GET config / unified/overview 响应只返回正式键（decision_history_min_messages / whitelist_sessions 等）。
- check result 判断日志升为 INFO 并登记观测白名单（用户指定可见性）。

### 兼容迁移

- 只含别名键的存量配置文件不丢值：Settings.from_config 回退读取别名（whitelist→whitelist_sessions、cooldown_seconds→cooldown_sec、idle_trigger_seconds→message_delay_sec、min_context_messages/proactive_threshold→decision_history_min_messages），一次 load+save 后别名自然消失。
- 随包前端页面全面切换正式键（读写两侧 + DEFAULT_CONFIG 单表同源）。

### 内部结构

- `whitelist_storage_key` 占位参数日落（0.9.0 B5 决策点到期，wildcard 未立项）：签名收敛为单参，9 处调用点同步。
- 管理员列表双窗缓存：事件路径 30s 窗口内跳过 cmd_config.json 的 stat（mtime 缓存保留，最大延迟 = 窗口长）。
- 宽捕获定向收窄：ui 主题纯 I/O 点位收窄为可枚举异常；storage 已是双层模式保持现状。
- ImageCache 并发语义注释；utils.py 补模块职责 docstring。
- main.py / webapi.py 拆分经实测评估否决（main.py 55/67 方法 ≤25 行无状态块可抽；webapi 拆分必产生双向引用），结论入档方案文档。

### 质量门禁

- 582 tests / 覆盖率 95.22%（fail_under 89）；mutation 38/38 全击杀；mypy 全源码零错；wheel 内容断言实跑通过。

## [0.9.1] - 2026-08-07

仅表现层改动，后端逻辑、配置 schema 与命令契约零变更。

### 新增

- 顶栏「压暗亮度」「粗体字」两个显示偏好开关，状态持久化并暴露 `aria-pressed`。

### 缺陷修复

- 压暗只覆盖半个页面：固定定位的保存条、Tab 栏、Toast 未纳入滤镜。
- 亮色主题下按钮出现贴顶白线。
- 显示偏好按钮的开关态与悬停态同样式，状态不可辨。
- 纯图标按钮与保存按钮标签光学偏心。
- 提示词编辑区中西文混排抖动：等宽栈无中文字形。
- 折叠标题图标与文字零间距。

### 界面调整

- 字号阶梯按职能重排：区块标题 20px、长文编辑区 16px、表单控件 14px、侧栏导航 16px。
- 标题内图标改用 `1em`，与字身同高。
- 控件高度提至 44px，与移动端触控档位统一。
- 保存按钮补图标；三个图标按钮统一为正方形。

## [0.9.0] - 2026-08-07

### 缺陷修复

- 配置热更新分裂：webapi 整体替换 `plugin.settings`，五组件持有旧引用读过期配置。
- delayed_check 早退路径 UnboundLocalError。

### 内部结构

- 模块合并：unified_manager → webapi、events → utils。
- 删死代码：2 个零锚点委托壳、main.py 死字段、image 模块 19 处日志前缀硬编码。
- delivery 工具直发记录提取为 `_record_direct_sends`。

### 质量护栏

- 三盲区补盲，覆盖率 91.31% → 94.47%（569 tests）；门槛 fail_under 70 → 89。
- mypy 显式覆盖全源码；mutation 38/38 全击杀；wheel 内容断言实跑通过。

## [0.8.0] - 2026-08-04

### 行为变更

- 隐私默认收紧：`log_reply_content` 默认改为 false。
- 只读指令（status/list/help/debug/check）不再使缓存失效；高频日志降 DEBUG。

### 修复与工程化

- 运行中检查 force cancel 时 run_agent 不再孤儿；context 兜底发送不再误记 UNKNOWN。
- 拆出 webapi.py（main.py 2815 → 2317 行）；引入 ruff/mypy/pre-commit/CI；测试 144 → 162。
