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
路径；磁盘不可用时才保留 data URL。内存回退按**原始载荷字节数**计数，并同时受：

- `MAX_SESSION_IMAGE_MEMORY_BYTES = 16 MiB`：单会话图片索引预算；
- `MAX_IMAGE_MEMORY_BYTES = 64 MiB`：所有会话图片索引共享预算；
- `MAX_IMAGE_BYTES = 10 MiB`：单张图片输入上限。

超出预算的图片不会进入会话索引，并记录 WARNING；淘汰按最旧图片事件进行，
不会静默无限增长。Vision 描述缓存另受 `MAX_IMAGE_DESCRIPTION_CACHE_BYTES = 512 KiB`
和 50 条条目上限约束。磁盘冻结缓存仍受 `MAX_IMAGE_CACHE_BYTES = 256 MiB`
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

- `MAX_CACHED_IMAGE_EVENTS × vision_max_images` = 每会话图片索引张数上限
- `MAX_SESSION_IMAGE_MEMORY_BYTES` = 单会话 data URL 原始载荷字节上限
- `MAX_IMAGE_MEMORY_BYTES` = 全局 data URL 原始载荷字节上限
- `MAX_RECENT_MESSAGE_LIMIT` = 每会话历史消息条数上限（recent deque maxlen）
- `MAX_IMAGE_BYTES` = 单张图片输入上限（`image/recorder_bridge.py`）

字节预算行为：

- 会话 / 全局 data URL：`tests/test_session_coordinator.py`
- Vision 描述 LRU：`tests/test_image_cache.py`
- 单张输入上限：`tests/test_vision_parser_gaps.py`

改常数时同步本页；不要为 KB 估算补公式测试。
