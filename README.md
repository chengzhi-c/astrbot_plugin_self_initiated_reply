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
  <img alt="版本" src="https://img.shields.io/badge/版本-1.3.0-4a5580">
  <img alt="AstrBot 插件" src="https://img.shields.io/badge/AstrBot-插件-7b86ab">
</p>

<p align="center">
  <strong><a href="#目录">目录</a></strong> · <a href="#english">English</a>
</p>

---

<a name="目录"></a>
<details markdown="block">
<summary>📑 目录（点击展开）</summary>

- [简介](#简介)
- [工作方式](#工作方式)
- [工具边界](#工具边界)
- [图片识别（可选）](#图片识别可选)
- [设置页面](#设置页面)
- [关于白名单](#关于白名单)
- [指令](#指令)
- [建议配置](#建议配置)
- [故障排查](#故障排查)
- [安全提示](#安全提示)
- [小结](#小结)
- [English](#english)

</details>

---

<a name="简介"></a>

## 📖 简介

> **业镜（名字取自《阴阳师：百闻牌》中阎魔的卡牌「业镜」）** 让 AstrBot 不再只被动应答，而是能在白名单会话里"主动接话"。

核心思路，四点说明：

- 收到消息后，插件会等对话静默下来，再用**判断模型**决定要不要接一句。
- 主动回复仍由 AstrBot 主 Agent 管线生成，但**默认收紧工具边界**（可手动放开）。
- 直接 `@Bot`、唤醒词与指令仍走正常回复链，两条路径互不干扰。
- 所有配置与运行状态跟随 AstrBot 数据根目录；跨平台群聊按完整 **UMO**（统一消息对象）保存，裸群号继续兼容。

> 💡 **快速上手：** 先打开[设置页面](#设置页面)，再套用[建议配置](#建议配置)，即可跑通第一个主动回复。

---

<a name="工作方式"></a>

## 🚀 工作方式

插件以"三步流水线"运作：

1. **收集** —— 监听白名单会话的普通消息；明确要接话、发图、找图的内容优先放行，其余进入判断模型。
2. **判断** —— 判断模型只回答一个问题——"接不接"，并输出严格 JSON：

   ```json
   {"should_reply": true, "reason": "一句简短理由"}
   ```

3. **回复** —— 正文由 AstrBot 主 Agent / tool loop 生成，走完整工具链但边界受限。

---

<a name="工具边界"></a>

## 🔒 工具边界

主动回复与 `@Bot` 正常回复是两条**独立路径**。

默认情况下，主动路径会关闭：

- 第三方插件工具，
- Shell / Python 执行，
- 文件读写，
- 浏览器，
- cron，
- 技能管理，
- 记忆写入，
- 以及跨会话发送。

当可用工具集无法验证时，插件会**直接拒绝运行（fail closed）**。设置页提供「允许继承插件工具」选项来放开边界——但宿主级危险能力（cron、浏览器 / 电脑使用、文件提取）在任何情况下都保持关闭。

---

<a name="图片识别可选"></a>

## 🖼️ 图片识别（可选）

默认关闭。开启后，判断或生成阶段会带上最近几张图的文字描述。

- **两个开关相互独立。** **判断模型识图**在判断是否接话时看图；**主模型识图**在生成正文时看图。判断阶段触发频繁，只开主模型识图更省 token。两者可分别指定 Provider；同一 Provider 下相同图片只描述一次，结果按 Provider 存入运行期内存缓存（约 50 张上限，插件重载 / 重启或超限逐出后重新描述），避免不同 Provider 串用描述。
- **可跳过表情包识图。** QQ OneBot `subType=1` 的表情包直接排除，不调 Vision。
- **快照安全可控。** 图片快照进插件自己的 `image_cache`，远程图后台冻结，不依赖会过期的 QQ CDN 链接；按魔数校验真实格式，防 SSRF 与伪造路径。固定地址传输每跳重新解析公网 IP，且不读取环境代理。
- **图片缓存有界。** Vision 描述缓存同时受条目数和 UTF-8 字节预算限制；磁盘不可用时的内存 data URL 受全局与单会话原始载荷字节预算约束，超限会告警并跳过图片。
- **仅作不可信上下文。** 描述只作为不可信上下文参与判断，不写入历史与状态，不能触发工具；缓存按有效期与容量上限自动清理，设置页提供「立即清理」按钮。

---

<a name="设置页面"></a>

## ⚙️ 设置页面

插件详情页内的**自定义设置页**是常用面：日常开关、白名单、判断模型与识图。  
官方 Dashboard 的 `_conf_schema.json` 是全量面：巡检、勿扰、回复长度、每日上限等运维键只在那边。两套面板读同一份配置，不是两套配置。

自定义页覆盖：

- 运行开关、私聊主动回复开关与白名单概况；
- 上下文 / 延迟 / 静默 / 冷却参数；
- 判断模型（Provider、温度、超时、提示词编辑与实时预览）；
- 白名单 UMO 批量编辑；
- 图片缓存清理。

其余配置（巡检、勿扰时段、回复长度、每日上限等）仍在官方 Dashboard 的插件配置页。

界面自带浅深双主题 —— 浅色「慈爱之惠」，深色「审判之司」，右上角切换，默认跟随系统：

<p align="center">
  <img src="https://raw.githubusercontent.com/chengzhi-c/astrbot_plugin_self_initiated_reply/main/assets/慈爱之惠.jpg" alt="浅色主题 · 慈爱之惠" width="49%">
  <img src="https://raw.githubusercontent.com/chengzhi-c/astrbot_plugin_self_initiated_reply/main/assets/审判之司.jpg" alt="深色主题 · 审判之司" width="49%">
</p>

**判断模型调参**是最常用入口：

| 字段 | 说明 |
| --- | --- |
| 启用判断模型 | 关闭后普通消息不再主动接话，明确请求仍可放行 |
| 判断模型 Provider | 默认当前会话模型，可下拉选择或手动填写 |
| 判断温度 | 建议 `0.0 – 0.3`，越低越稳 |
| 判断超时秒数 | 建议 `20` 秒 |
| 判断提示词 | 支持 `{session}` `{trigger}` `{bot_aliases}` `{latest_message}` `{recent_messages}` `{last_message_age_sec}` `{last_reply_age_sec}` 变量，右侧实时预览；留空保存恢复默认 |

改完点「保存」，页面会回读服务端配置。重载插件或重启 AstrBot 后仍想看到最新页面资源，请硬刷新浏览器。

---

<a name="关于白名单"></a>

## 🎯 关于白名单

白名单有**两层**：

1. AstrBot **全局白名单**先拦截；
2. 过了之后，才轮到**插件白名单**决定"是否主动回复"。

`/selfreply add` 加入当前会话（群聊是整个群，私聊是该好友 UMO）。页面「生效范围」支持批量编辑：群聊可填完整 UMO 或裸群号，私聊须填完整 UMO。

「允许私聊主动回复」默认开启，与官方 Dashboard 同一键。关闭后只对群聊主动开口；私聊仍可用 `/selfreply check` 测试，也仍可用 `/selfreply add` 加入白名单。

---

<a name="指令"></a>

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
| `/selfreply on` / `off` | 启用 / 暂停，重启后保持 |
| `/selfreply debug` | 当前会话、发送者、触发识别信息 |

也支持 `@Bot selfreply add` 这类写法。

> ⚠️ 注意：`/selfreply on` / `off` 自 0.9.4 起**直接落盘**，宿主重启后保持。它写的就是插件配置里的 `enabled`，所以官方 Dashboard 的插件配置页会同步显示这个开关状态。

</details>

<a name="建议配置"></a>

<details markdown="block">
<summary>✅ 建议配置（点击展开）</summary>

| 参数 | 建议 |
| --- | --- |
| 判断温度 | `0.0 – 0.3` |
| 判断超时 | `10 – 30` 秒 |
| 最少上下文消息数 | `3 – 8`（默认 `5`） |
| 消息后延迟 | `60 – 180` 秒（测试时可调低） |
| 最小静默秒数 | `120 – 300` 秒（默认 `45`） |
| 冷却秒数 | `300 – 900` 秒 |
| 每日每会话最大回复次数 | `5 – 12`（`0` = 不限） |
| 主动回复最长字符数 | `100 – 300`（`0` = 不限） |
| 图片识别 | 默认关闭；表情包多的群可开「跳过表情包识图」 |

- 「忽略发送者 ID」可屏蔽特定用户（含管理员），指令路径不受影响。
- 「日志记录主动回复内容」默认关闭以保护隐私；排查时在官方 Dashboard 插件配置页开启。

</details>

<a name="故障排查"></a>

<details markdown="block">
<summary>🛠️ 故障排查（没有主动回复时，点击展开）</summary>

按顺序查：

1. 会话已加入主动回复白名单？
2. 若是私聊：设置里「允许私聊主动回复」是否开启？
3. AstrBot 全局白名单没有拦截？
4. 看 `/selfreply status`。
5. 日志里的 `should_reply` 和 `reason`。
6. 判断模型不放行？调「判断提示词」与「判断温度」。

改完插件代码、页面或 metadata 后，重启 AstrBot 或重载插件生效。

</details>

---

<a name="安全提示"></a>

## 🔐 安全提示

> ⚠️ **安全警告：** 插件自带设置页与 Web API（如 `/api/plugin/selfreply/config`）**不提供独立鉴权**，访问控制完全依赖宿主 AstrBot Dashboard 的登录与权限体系。

- 请勿将 AstrBot 的 Web 端口暴露到不受信任的网络。
- `proactive_inherit_tools`、白名单等安全敏感配置的变更会记录在插件 **INFO** 日志中，便于事后追溯。

---

<a name="小结"></a>

## 📌 小结

业镜让 AstrBot 既能主动搭话又不越界：轻量的判断模型决定"何时开口"，主 Agent 决定"说些什么"，严格的工具边界确保每次主动行为默认安全。从[设置页面](#设置页面)与[建议配置](#建议配置)起步，再用[指令](#指令)与[故障排查](#故障排查)微调与诊断。别忘了把 Dashboard 置于可信访问之后——详见[安全提示](#安全提示)。

---

<a name="english"></a>

## 🌐 English summary

An AstrBot plugin that lets the bot **join conversations on its own** inside
whitelisted sessions.

- **How it works.** After a message arrives the plugin waits for the
  conversation to fall silent, then asks a cheap *judge model* one question —
  "should I reply?" — which must answer with strict JSON
  (`{"should_reply": bool, "reason": str}`). The reply body itself is produced
  by AstrBot's own main Agent pipeline.
- **Two independent paths.** Direct `@Bot` mentions, wake words and commands
  keep using the normal reply chain; the proactive path never interferes.
- **Tool boundary is fail-closed.** A proactive run defaults to an empty tool
  allowlist. If the tool set cannot be enumerated or cleaned, the run is
  aborted rather than executed with a partial boundary. Host-level dangerous
  capabilities (cron, browser/computer use, file extraction) stay disabled even
  when `proactive_inherit_tools` is on.
- **Optional vision.** Off by default. When enabled, recent images can be
  described and attached to the judge and/or main prompt. Descriptions are
  treated as untrusted context: they never enter history or trigger tools.
- **Two settings surfaces.** The plugin page is the daily face (switch,
  whitelist, judge, vision). AstrBot Dashboard `_conf_schema.json` is the full
  face (patrol, quiet hours, reply length, daily cap). Both read the same
  config.
- **Commands.** `/selfreply` plus `status`, `add`, `remove`, `list`,
  `check [content]`, `on`, `off`, `debug`.
- **Requirements.** AstrBot `>=4.23.3,<5`, Python `>=3.12`, and the declared
  runtime dependencies `httpx>=0.27,<0.29` plus `httpcore>=1,<1.1` for fixed-address
  HTTPS image downloads. Vision remains optional and disabled by default.

> ⚠️ **Security.** The bundled settings page and its Web API have **no
> independent authentication**; access control is inherited entirely from the
> AstrBot Dashboard. Do not expose the AstrBot web port to untrusted networks.

Full documentation is Chinese-only (the config schema and UI are Chinese); see
the sections above.
