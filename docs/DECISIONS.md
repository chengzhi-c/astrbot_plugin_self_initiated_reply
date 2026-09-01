# 结构决策

行为不变量见 `BEHAVIOR_CONTRACT.md`。本页只记结构取舍。

## 双面板

`CONFIG_SPECS.surfaces` 区分官方 Dashboard（`host`）与自定义设置页（`panel`）。常用键上自定义页；巡检、勿扰、回复长度、日上限、`log_reply_content` 等只在 Dashboard。两套面板读写同一份配置。前端可写键必须等于 panel 面，由 `test_fe_writable_keys_match_panel_surfaces` 锁定。

## 装配模块

协作对象接线留在 `assembly.py`。循环依赖与宿主热替换用调用期查找。容器持有者守卫同时扫描 `main.py` 与 `assembly.py`。

## 设置页 chrome

浅/深/跟随系统主题经 `GET/POST ui/theme` 写入 `ui_prefs.json`（页面在 Dashboard iframe 内，localStorage 不可靠）。压暗与粗体是页面能力，浏览器测试覆盖。主题落盘与状态文件共用 `storage` 原子写。

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


每次主动检查由 pipeline 创建一个带 UUID4 hex `ledger_id` 的 `AttemptLedger`。生成、投递和唯一 record task 传递同一 ledger 引用；`ledger_id` 只用于日志、任务关联和一次性记账诊断，不进入持久配置或 `config_revision`，也不依赖进程级计数器。

## 轻量化边界

按生产调用和契约测试复查后，删除发布脚本中无调用的 `_find_sdist` 辅助函数，以及
`runtime_adapter` 的路径 accessor 方法：路径函数的出口是 `_HOST_CONTRACT` 探测出的
capabilities 字段本身（main.py import 期绑定为模块级名字，同时是测试替换点）；原方法
吞异常返回 None，与 `resolve_paths`「路径解析失败让异常传播、加载期即崩」的方向相反——
静默回退会让状态写到错误路径后无声丢失。方法级的 None 回退语义由
`test_resolve_paths_branches` 在真实消费方 `plugin_state.resolve_paths` 上覆盖。
`image.is_image_payload` 是包级公开导出；`save_storage_sync` 负责启动失败告警，不能视作
纯转发 wrapper。后续若要删除这些边界，必须先迁移对应宿主/测试契约，而不是以删行数为目标。


发布脚本不再按文件名字典序猜测目标 wheel。`release_artifacts.py` 使用 `packaging` 解析 wheel/sdist 文件名中的 PEP 440 版本；默认发现多个候选或坏文件名直接失败，只有显式路径能消除歧义。`check_wheel.py`、`check_sdist.py` 与部署 zip 共享同一解析器。`gates.py` 的普通本地模式在缺 wheel/sdist 时只报告 `NOT RELEASE-VERIFIED`，`--release` 则非零退出；CI build 独立检查 wheel、sdist 和 deploy zip。

远程图片使用 `httpx` + `httpcore` 的固定地址传输：DNS 只在每个请求入口解析一次，
TCP 连接使用已验证 IP，原 hostname 继续承担 Host/SNI；环境代理关闭，重定向由 HTTPX
逐跳重新进入传输层。图片描述 LRU 同时受条目数和字节预算约束，磁盘不可用时的 data URL
索引受全局/会话原始载荷预算约束。事件清理只删除事件引用，图片索引由独立保护窗口回收；
失效和终止才清理两者。运行时依赖和使用到的公开 API 由 `runtime_dependency_gates.py`
与宿主兼容检查锁定。


覆盖率门槛（`fail_under` 与 `scripts/coverage_gates.py`）只上调。不为减行数删除行为测试或下调门槛。
