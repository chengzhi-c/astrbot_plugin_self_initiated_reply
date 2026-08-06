# 12 — 落盘合并与内存基准

**What to build:** 主动回复记录从"每次尝试都落盘"改为脏标记 + 合并写（批量/间隔触发），但终止与重载路径仍保证最终落盘。同时产出每会话内存占用基准（会话状态、事件缓存、图片索引），把图片缓存容量与消息上限从拍脑袋常数变为可证明的公式。

**Blocked by:** 07 — 会话状态显式化

**Status:** resolved（2026-08-06）

- [x] 合并写路径红灯测试：连续多次记录只触发一次落盘；进程终止/插件重载必落盘
- [x] 每会话内存上限可证明（公式 + 实测数据进文档）
- [x] 崩溃恢复语义不变：损坏/半写状态文件仍走备份容错

## Resolution

### 合并写（state_saver.py，新增 ~110 行）

- `DebouncedStateSaver`：脏标记 + 合并写。`mark_dirty` 置脏并确保已调度
  一次延迟 flush（窗口内重复置脏复用同一调度）；`flush` 强制落盘
  （取消未到期延迟 flush），失败保持脏状态并自动重试；`cancel` 取消
  调度（终止路径由 flush 接管）
- main 装配：`self._state_saver = DebouncedStateSaver(do_save=self._save_storage)`；
  delivery 的 `save_storage` 回调改为 `_queue_state_save`（置脏排队）；
  **whitelist 双写保持逐次落盘**（同步回滚语义不能合并）；
  terminate 改为 `await flush()`（最终落盘）+ `cancel()`（清重试任务）
- 常数 `STATE_SAVE_DEBOUNCE_SEC = 2.0`（models.py，共享常量）

### 测试（先红后绿 + 变异实测）

- 红灯：`tests/test_state_saver.py` 6 项（多标记单任务/窗口自动落盘/
  flush 清零/失败保持脏并自动重试/重调度/取消后 flush 仍落盘）
- 变异 4 处实测：3 KILLED（flush 不清脏 / 失败不重试 / 不调度），
  1 存活 = **等价变异**（pending 幂等赋值 + 调度独立守卫，删除去重检查
  无行为差异）→ 剔除不补测；固化 3 点，mutation_check 35 → 38
- 崩溃恢复回归：corrupt/version-mismatch 既有变异点 + storage 测试保持

### 内存基准（docs/MEMORY_BUDGET.md + tests/test_memory_budget.py）

- 公式：`B(session) = F + R×M + E + I×V×G`
  （会话固定 ≈0.95KB + 历史 R×0.33KB + 事件引用 + 图片索引 20×V×0.21KB）
  默认 ≈13.6KB/会话，最坏 ≈55KB/会话（N=1000 → ≈55MB）
- 图片本体（data URL 冻结）张数受 `I×V ≤ 100` 约束，大小无硬性字节上限
  （依赖图片本身），如实标注为唯一风险项；磁盘侧 MAX_IMAGE_CACHE_BYTES
  256MB 独立上限
- 实测数据（CPython 3.14/x64）随文档留档；常数关系由
  test_memory_budget.py 锁定（改常数必须同步文档）
