<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/chengzhi-c/astrbot_plugin_self_initiated_reply/main/assets/banner-dark.jpg">
    <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/chengzhi-c/astrbot_plugin_self_initiated_reply/main/assets/banner-light.jpg">
    <img alt="业镜 · 主动回复" src="https://raw.githubusercontent.com/chengzhi-c/astrbot_plugin_self_initiated_reply/main/assets/banner-light.jpg" width="100%">
  </picture>
</p>

<h1 align="center">业镜 · 主动回复</h1>

<p align="center">
  让 AstrBot 在白名单会话里学会"自然接话"的插件。<br>
  <img alt="版本" src="https://img.shields.io/badge/版本-0.8.1-4a5580">
  <img alt="AstrBot 插件" src="https://img.shields.io/badge/AstrBot-插件-7b86ab">
</p>

<p align="center">
  <a href="README.en.md">English</a>
</p>

接收到一条消息后，插件会等一段静默时间，用"判断模型"决定要不要主动接一句；走 AstrBot 主 Agent 管线，并默认把工具边界收紧（可放开）。直接 `@Bot`、唤醒词和指令仍交给正常回复链，两边互不干扰。配置与状态跟随 AstrBot 数据根目录，跨平台群聊按完整 UMO 保存，裸群号仍兼容。

## 🚀 工作方式

1. **收集** —— 监听白名单会话的普通消息；明确要接话、发图、找图的内容优先放行，其余进入判断模型。
2. **判断** —— 判断模型只回答"接不接"，输出严格 JSON：
   ```json
   {"should_reply": true, "reason": "一句简短理由"}
   ```
3. **回复** —— 正文由 AstrBot 主 Agent / tool loop 生成，走完整工具链但边界受限。

## 🔒 工具边界

主动回复与 `@Bot` 正常回复是两条独立路径。主动路径默认关闭第三方插件工具、Shell/Python、文件读写、浏览器、cron、技能管理、记忆写入和跨会话发送；工具集无法验证时直接拒绝运行（fail closed）。设置页可开「允许继承插件工具」放开边界，但宿主级危险能力（cron、浏览器/电脑使用、文件提取）在任何情况下都保持关闭。

## 🖼️ 图片识别（可选）

默认关闭。开启后，判断或生成阶段会带上最近几张图的文字描述：

- 两个开关独立：**判断模型识图**（判断是否接话时看图）与**主模型识图**（生成正文时看图）。判断阶段触发频繁，只开主模型识图更省 token；两者可分别指定 Provider，同一 Provider 下相同图片只描述一次，结果按 Provider 存入运行期内存缓存（约 50 张上限，插件重载/重启或超限逐出后重新描述），避免不同 Provider 串用描述。
- 可**跳过表情包识图**：QQ OneBot `subType=1` 的表情包直接排除，不调 Vision。
- 图片快照进插件自己的 `image_cache`，远程图后台冻结，不依赖会过期的 QQ CDN 链接；按魔数校验真实格式，防 SSRF 与伪造路径。
- 描述只作为不可信上下文参与判断，不写入历史与状态，不能触发工具；缓存按有效期与容量上限自动清理，设置页有"立即清理"按钮。

## ⚙️ 设置页面

插件详情页内的设置页，覆盖：运行开关与白名单概况、上下文/延迟/静默/冷却参数、判断模型（Provider、温度、超时、提示词编辑与实时预览）、白名单 UMO 批量编辑、图片缓存清理。

界面自带浅深双主题 —— 浅色「慈爱之惠」，深色「审判之司」，右上角切换，默认跟随系统：

<p align="center">
  <img src="https://raw.githubusercontent.com/chengzhi-c/astrbot_plugin_self_initiated_reply/main/assets/%E6%85%88%E7%88%B1%E4%B9%8B%E6%83%A0.png" alt="浅色主题 · 慈爱之惠" width="49%">
  <img src="https://raw.githubusercontent.com/chengzhi-c/astrbot_plugin_self_initiated_reply/main/assets/%E5%AE%A1%E5%88%A4%E4%B9%8B%E5%8F%B8.png" alt="深色主题 · 审判之司" width="49%">
</p>

**判断模型调参**是最常用入口：

| 字段 | 说明 |
| --- | --- |
| 启用提示词判断模型 | 关闭后普通消息不再主动接话，明确请求仍可放行 |
| 判断模型 Provider | 默认当前会话模型，可下拉选择或手动填写 |
| 判断温度 | 建议 `0.0 - 0.3`，越低越稳 |
| 判断超时秒数 | 建议 `20` 秒 |
| 判断提示词 | 支持 `{session}` `{trigger}` `{bot_aliases}` `{latest_message}` `{recent_messages}` `{last_message_age_sec}` `{last_reply_age_sec}` 变量，右侧实时预览；留空保存恢复默认 |

改完点"保存"，页面会回读服务端配置。重载插件或重启后仍想看到最新页面资源，请硬刷新浏览器。

## 🎯 关于白名单

白名单有两层：AstrBot 全局白名单先拦截，过了才轮到插件白名单决定"是否主动回复"。`/selfreply add` 加入的是整个群会话。页面"高级：白名单会话 ID"支持批量编辑，可填完整 UMO 或直接填群号。

<details markdown="block">
<summary>📋 指令（7 条，点击展开）</summary>

只注册英文命令入口，避免日常聊天误触：

| 指令 | 说明 |
| --- | --- |
| `/selfreply` | 查看帮助 |
| `/selfreply status` | 运行状态、判断模型、白名单 |
| `/selfreply add` / `remove` | 将当前会话加入 / 移出白名单 |
| `/selfreply list` | 查看白名单 |
| `/selfreply check [content]` | 手动测试一次主动回复 |
| `/selfreply on` / `off` | 临时启用 / 暂停 |
| `/selfreply debug` | 当前会话、发送者、触发识别信息 |

也支持 `@Bot selfreply add` 这类写法。注意 `/selfreply on/off` 是临时运行态：官方 Dashboard 保存插件配置会触发热重载并重置临时状态，插件自带设置页保存不会。

</details>

<details markdown="block">
<summary>✅ 建议配置（点击展开）</summary>

| 参数 | 建议 |
| --- | --- |
| 判断温度 | `0.0 - 0.3` |
| 判断超时 | `10 - 30` 秒 |
| 最少上下文消息数 | `3 - 8`（默认 `5`） |
| 消息后延迟 | `60 - 180` 秒（测试时可调低） |
| 最小静默秒数 | `120 - 300` 秒 |
| 冷却秒数 | `300 - 900` 秒 |
| 每日每会话最大回复次数 | `5 - 12`（`0` 不限） |
| 主动回复最长字符数 | `100 - 300`（`0` 不限） |
| 图片识别 | 默认关闭；表情包多的群可开"跳过表情包识图" |

「忽略发送者 ID」可屏蔽特定用户（含管理员），指令路径不受影响。

「日志记录主动回复内容」默认关闭以保护隐私；排查时可在设置页开启。

</details>

<details markdown="block">
<summary>🛠️ 排查建议（没有主动回复时，点击展开）</summary>

按顺序查：

1. 会话已加入主动回复白名单？
2. AstrBot 全局白名单没有拦截？
3. 看 `/selfreply status`。
4. 日志里的 `should_reply` 和 `reason`。
5. 判断模型不放行？调"判断提示词"和"判断温度"。

改完插件代码、页面或 metadata 后，重启 AstrBot 或重载插件生效。

</details>
