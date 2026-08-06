# 每会话内存基准（ticket 12）

本页把"拍脑袋常数"（缓存容量、消息上限）改写为可推导的公式，并给出
本机（CPython 3.14 / x64）实测数据。数值为上限估算：deque 容器随
`maxlen` 预分配，深度计算含嵌套对象，实测见下文表。

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

`prepared_source` 为事件级图片冻结（data URL），驻留内存，其大小依赖
图片本身而非常数。张数受公式约束：每会话 ≤ `I × V = 100` 张。QQ 图片
典型 <1-2 MB（base64 放大 ≈1.33×）：默认 V=2 时每会话 ≤40 张；最坏
V=5 时单会话可达 100-200 MB——**这是唯一无硬性字节上限的项**，其约束
来自张数公式与 `MAX_IMAGE_CACHE_BYTES(256 MB)` 磁盘冻结上限（disk 侧）。

## 实测数据（CPython 3.14 / x64，sys.getsizeof 深度求和）

| 对象 | 深度大小 |
| --- | --- |
| 空 SessionState（maxlen=100） | 0.95 KB |
| MessageRecord（20 字中文消息） | 0.33 KB |
| SessionState 满 100 条 | 14.8 KB |
| ImageInfo（含 prepared_source） | 0.21 KB |
| 图片索引满（20 事件 × 2 张） | 5.7 KB |

## 常数与公式的对应（锁定测试）

- `MAX_CACHED_IMAGE_EVENTS × MAX_VISION_IMAGES` = 每会话图片索引张数上限
- `MAX_RECENT_MESSAGE_LIMIT` = 每会话历史消息条数上限（recent deque maxlen）
- 上述关系由 `tests/test_memory_budget.py` 锁定，常数调整时必须同步文档
