<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/chengzhi-c/astrbot_plugin_self_initiated_reply/main/assets/banner-dark.jpg">
    <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/chengzhi-c/astrbot_plugin_self_initiated_reply/main/assets/banner-light.jpg">
    <img alt="业镜 · 主动回复" src="https://raw.githubusercontent.com/chengzhi-c/astrbot_plugin_self_initiated_reply/main/assets/banner-light.jpg" width="100%">
  </picture>
</p>

<h1 align="center">业镜 · 主动回复</h1>

<p align="center">
  让 AstrBot 在白名单会话中自然接话的插件。<br>
  <img alt="版本" src="https://img.shields.io/badge/版本-1.3.3-4a5580">
  <img alt="AstrBot 插件" src="https://img.shields.io/badge/AstrBot-插件-7b86ab">
</p>

<p align="center">
  <a href="#-简介">简介</a> · <a href="#-工作方式">工作方式</a> · <a href="#-工具边界">工具边界</a> · <a href="#-设置页面">设置页面</a> · <a href="#-指令">指令</a> · <a href="#-english">English</a>
</p>

---

## 📖 简介

> **业镜**（名字取自《阴阳师：百闻牌》中阎魔卡牌「业镜」）为 AstrBot 提供白名单会话内的主动接话能力。

- 收到消息后等待对话静默，再由判断模型评估是否接话。
- 主动回复由 AstrBot 主 Agent 管线生成，默认收紧工具边界。
- `@Bot`、唤醒词与指令保持原有回复逻辑，两套流程互相独立。
- 配置与运行状态保存在 AstrBot 数据目录，跨平台群聊使用完整 UMO 保存，兼容纯群号格式。

---

## 🚀 工作方式

1. **收集**：监听白名单会话中的普通消息，等待对话进入静默期。
2. **判断**：调用判断模型评估当前氛围，返回接话判定与原因：
   ```json
   {"should_reply": true, "reason": "群友提到了感兴趣的话题"}
   ```
3. **回复**：判断通过后，由 AstrBot 主 Agent 生成回复内容并发往会话。

---

## 🔒 工具边界

主动回复与常规回复采用独立执行路径。

为了保障主动发言的安全性，主动链路默认关闭以下工具权限：
- 第三方插件工具
- Shell / Python 代码执行
- 本地文件读写
- 浏览器与系统操作

如果希望主动回复时调用外部插件（如查天气、查百科），可以在设置页开启「允许继承插件工具」。

---

## 🖼️ 图片识别（可选）

默认关闭。开启后，可感知对话中近期发送的图片：

- **独立开关**：支持分别控制判断模型与主模型的识图功能。判断阶段建议保持关闭以节省 Token，正文生成阶段按需开启。
- **表情过滤**：自动跳过普通表情包，将识图额度留给真实聊天图片。
- **本地缓存**：相同图片在有效期内复用解析结果，减少重复请求。

---

## ⚙️ 设置页面

在 AstrBot 插件详情中即可打开可视化设置页。界面支持浅色「慈爱之惠」与深色「审判之司」双主题，可在右上角切换：

<p align="center">
  <img src="https://raw.githubusercontent.com/chengzhi-c/astrbot_plugin_self_initiated_reply/main/assets/慈爱之惠.jpg" alt="浅色主题 · 慈爱之惠" width="49%">
  <img src="https://raw.githubusercontent.com/chengzhi-c/astrbot_plugin_self_initiated_reply/main/assets/审判之司.jpg" alt="深色主题 · 审判之司" width="49%">
</p>

### 常用配置

| 字段 | 建议值 | 说明 |
| --- | --- | --- |
| 启用判断模型 | 开启 | 主动接话的核心开关 |
| 判断模型 Provider | 留空或轻量模型 | 默认跟随会话模型，也可配置 `gpt-4o-mini` 等模型 |
| 判断温度 | `0.0 – 0.3` | 控制判断输出的确定性，数值越低越稳定 |
| 判断提示词 | 默认模板 | 支持 `{latest_message}`、`{recent_messages}`、`{session}`、`{bot_aliases}` 等变量，右侧面板支持实时预览 |
| 最小静默秒数 | `45 – 120` 秒 | 收到消息后等待环境安静的秒数 |
| 冷却秒数 | `300 – 900` 秒 | 同一会话两次主动回复的最小时间间隔 |
| 新消息到达放弃旧回复 | 视场景开启 | 正在生成回复时若出现新发言，自动作废当前生成 |

---

## 🎯 关于白名单

会话需加入白名单后才会触发主动回复：

- 在目标群发送 `/selfreply add`，即可将当前会话加入白名单。
- 设置页「生效范围」支持批量编辑，群聊可填写完整 UMO 或纯群号，私聊需填写完整 UMO。
- 「允许私聊主动回复」控制私聊场景的触发，关闭时仅在群聊中主动发言。

---

## 📋 指令

| 指令 | 说明 |
| --- | --- |
| `/selfreply` | 查看指令帮助 |
| `/selfreply status` | 查看当前会话状态、配置与白名单概况 |
| `/selfreply add` / `remove` | 将当前会话加入 / 移出白名单 |
| `/selfreply list` | 查看已启用的白名单列表 |
| `/selfreply check [内容]` | 手动触发一次主动回复检测流程 |
| `/selfreply on` / `off` | 启用 / 暂停主动回复（配置持久化保存，重启依然生效） |
| `/selfreply debug` | 查看当前会话 UMO、发送者 ID 等诊断信息 |

---

## 🛠️ 故障排查

若遇到未按预期主动回复的情况，可依次检查：

1. 发送 `/selfreply status`，确认当前会话已在白名单内且插件处于开启状态；
2. 若在私聊测试，确认已开启「允许私聊主动回复」；
3. 确认距上一条发言的时间已达到设定的静默秒数，且不在冷却期内；
4. 查看后台日志中的 `should_reply` 及判断原因，根据情况调整提示词或调低温度；
5. 使用 `/selfreply check 测试` 手动执行一次完整调用，检查模型响应情况。

---

## 🔐 安全提示

插件设置页与相关接口依赖 AstrBot 控制台本身的权限验证，请将 AstrBot 管理端口部署在受信任的网络环境中。

---

## 🌐 English

An AstrBot plugin that enables your bot to naturally join conversations in whitelisted sessions.

- **Silence Detection**: Waits for active chat to pause before evaluating, preventing message interruption.
- **Two-Stage Architecture**: A lightweight judge model evaluates whether to respond, and the main AstrBot agent generates the reply.
- **Restricted Sandbox**: High-risk system execution and shell tools are disabled during proactive replies.
- **Optional Vision**: Capable of reading recent images with automatic caching and meme filtering.
- **Web Settings Page**: Built-in Light and Dark themes for managing whitelist, cooldowns, and judge prompts.

Commands: `/selfreply` · `status` · `add` · `remove` · `list` · `check [text]` · `on` · `off` · `debug`

---

## 📄 开源许可

本项目采用 [MIT License](LICENSE) 开源。
