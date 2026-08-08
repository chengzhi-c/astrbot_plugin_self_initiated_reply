# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的格式。
小版本（0.8.1~0.8.8）迭代细节省略，详见 git 历史。

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
