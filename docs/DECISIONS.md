# 结构决策

行为不变量见 `BEHAVIOR_CONTRACT.md`。本页记录结构选择及其运行边界。

## 发布产物

AstrBot 插件市场从 Git 仓库安装。手工部署包从选定提交导出：

```sh
git archive --format=zip --prefix=<name>/ -o <name>.zip <commit>
```

归档内容以提交为准，只有提交中的文件参与导出。排除规则由仓库根 `.gitattributes` 的
`export-ignore` 统一声明。zip 保留一层插件目录，以兼容 AstrBot 上传安装时的解包方式。
`pyproject.toml` 的 wheel 配置用于本地构建和开发安装，部署包直接使用 Git 归档。

## 配置单源与双面板

`CONFIG_SPECS` 定义默认值、类型、边界与面板归属。`surfaces` 区分官方 Dashboard
（`host`）与自定义设置页（`panel`），两套面板读写同一份持久配置。

GET `/config` 返回 panel 键、`runtime_enabled`、`decision_prompt_default` 和
`config_revision`；POST `/config` 接受全部 schema 键。前端提交 `base_revision`，
服务端在 `_config_lock` 内比较 revision 并拒绝过期写入。旧客户端省略 revision 时保留
串行写入兼容。保存结果未知或配置冲突后，页面刷新取得新 revision 才能再次保存。

迟到的配置读取响应必须保留用户已编辑的表单。首次读取完成前，表单保持 `inert`。

## 组件装配与共享状态

`main._assemble_components` 负责协作对象接线；宿主热替换与循环依赖通过调用期查找处理。
`webapi` 只在 `TYPE_CHECKING` 下引用插件类型。

协作对象持有同一份 `Settings` 与状态容器引用。配置应用和回滚使用原地更新，保留容器和
存活 `SessionState` 的对象身份。在途检查持有会话状态，替换对象会让后续记账写入孤儿状态。

代次随实时失效推进。运行标记归在途任务所有。回滚恢复锁表，并保留仍被任务持有的锁。
运行集与 release 事件按 owner 存活状态协调。

## 任务生命周期

插件 owner 持有 `RUNNING`、`STOPPING`、`DEGRADED` 三种状态。停止等待有时间边界，
仍未收敛的任务进入隔离并触发 `DEGRADED`。此状态拒绝新检查和发送，恢复需要插件重载
或宿主重启；隔离任务退出后只移除登记。

持久化回滚必须完成后才释放配置锁，防止迟到回滚覆盖后续成功写入。会话记账独立于外层
检查的取消收尾，运行租约覆盖整个记账窗口。

## 消息与发送

`enabled_private_sessions` 复用现有白名单和检查门卫，控制私聊自动触发；手动检查使用
`force=True`。`abandon_stale_on_new_message` 控制新消息是否推进代次；白名单移除、手动
检查和停止始终使旧任务失效。关闭该开关时，在途发送使用检查开始时的活动时间判定静默。

生成返回文本和 `AttemptLedger`，直发结果、发送次数与记账状态由账本统一持有。
抑制原因使用 `SuppressCode` 分类：代次变化、插件停止、账本封闭分别呈现对应原因，
三者均不重试。工具直发预算由独立的发送闸门执行。

内联指令负责裸词入口保护与事件去重，装饰器指令负责宿主注册和权限声明，两条入口共享
动作实现。`_NamedAt` 继承宿主 `At`，保留出站序列化时需要的昵称字段。

## 图片与宿主兼容

公开适配层直接使用宿主 Context 与 Provider API。当前模型 ID 查询在不同宿主版本中
分别为同步和异步，由 `maybe_await` 统一等待。私有 Agent API 由运行时适配层核对。

`recorder_bridge` 按消息 ID 查图时，多图记录必须匹配 URL；单图记录可以取唯一组件。
本地图片读取统一校验允许根目录。

远程图片通过 `httpx`、`httpcore` 固定地址传输：每次请求解析并校验 IP，连接使用该 IP，
原 hostname 用于 Host 与 TLS SNI；关闭环境代理，重定向逐跳重新校验。DNS、连接和读取
共享单图下载预算。请求使用 `Accept-Encoding: identity`，拒绝压缩响应，避免解压发生在
体积计数之前。

普通事件回收与图片保护窗口分离。data URL 索引按原始载荷计入全局及会话预算，图片描述
LRU 按条目数和字节数限制，磁盘冻结缓存按年龄及容量回收。安全上限集中在 `models.py`，
模块内的时序参数保留在使用处。

识图上下文在判断或生成调用的预算之外构建，每张图片受 `vision_timeout_sec` 约束。
因此一次检查的总耗时还包括识图时间；超时图片降级为文本上下文。

## 设置页偏好与布局

主题、压暗和粗体通过 `/ui/theme` 保存至 `ui_prefs.json`，与状态文件共用原子写入。
POST 只提交用户操作过的键；迟到的 GET 保留已发生的用户选择。

Provider 控件共用 `setManual`，由控件在 `refs.field` 上切换 `manual` 类，保证判断与
识图字段使用相同的列表、手动输入布局。

顶栏粘附态只改变背景与边框颜色，保持占位高度，避免滚动锚定与粘附阈值形成抖动回路。
`ResizeObserver` 将实际顶栏高度写入 `--topbar-h`，该变量供侧栏和滚动锚点消费。

## 白名单命令回滚边界

`whitelist.commit_change` 恢复白名单、会话状态及运行期 UMO 映射。移除动作已取消的检查
和清理掉的事件保持失效，下一条消息会重建调度状态；白名单层保持对调度器的单向依赖。
持久化失败会向管理员返回错误并记录日志。
