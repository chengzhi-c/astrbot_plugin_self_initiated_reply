# 10 — 变异测试扩面（19 → 30+）

**What to build:** 把变异点从 19 扩到 30 以上，覆盖 07 之后明确的模块级不变量：代次复核、观察窗口推进、直发预算、fail-closed 工具策略、UNKNOWN 消耗语义、白名单回滚。每个新变异点必须被现有测试击杀。

**Blocked by:** 07 — 会话状态显式化

**Status:** resolved（2026-08-06）

- [x] 变异点 ≥30，全部击杀，无存活变异
- [x] 扩面脚本纳入 nightly CI（已有调度扩展）
- [x] 击杀结果随工件留档，新增未击杀变异必须回退或补测

## Resolution

`scripts/mutation_check.py` 变异点 19 → **32**（+13），全部击杀无存活：

| 主题 | 变异点 | 目标 | 击杀测试 |
| --- | --- | --- | --- |
| 代次复核 | delivery-stale-before-hooks | delivery.send_reply 钩子前复核 | test_send_reply_stale_before_hooks_skips_hooks（新增） |
| 观察窗口推进 | unconfirmed-no-advance | record_proactive_state unconfirmed 分支 | test_record_unconfirmed_sets_state_fields |
| 观察窗口推进 | delivered-no-advance | record_proactive_state confirmed 分支 | test_deliver_delivered_advances_observation_and_history |
| 直发预算 | tool-direct-budget-bypass | outbound 预算闸门 | test_tool_direct_send_budget_is_consumed_before_adapter_call |
| 直发预算 | tool-direct-count-lost | generation tracked_send 计数 | test_generate_tracks_direct_sends_within_budget |
| fail-closed | keep-policy-filter-skipped | enforce keep 过滤 | test_enforce_policy_keep_mode_filters |
| fail-closed | drop-denylist-skipped | enforce drop 过滤 | test_enforce_policy_drop_mode_removes_dangerous |
| UNKNOWN 消耗 | unknown-no-record | deliver_reply UNKNOWN 分支 | test_deliver_unknown_consumes_state_without_retry |
| UNKNOWN 消耗 | unconfirmed-writes-history | record confirmed 强转 | test_record_unconfirmed_sets_state_fields |
| 白名单回滚 | whitelist-rollback-skipped | commit_change 内存回滚 | test_add_rolls_back_in_memory_on_persist_failure |
| 级联失效 | coordinator-cancel-skipped | invalidate 延迟任务取消 | test_invalidate_cascades_all_resources |
| 级联失效 | coordinator-clear-keeps-images | clear 图片清理 | test_invalidate_cascades_all_resources |
| 级联失效 | coordinator-clear-keeps-phases | clear 阶段清理 | test_invalidate_resets_explicit_phase |

**过程发现（先红后补）**：`delivery-stale-before-hooks` 首轮变异**存活**——旧击杀测试
`test_send_reply_stale_before_hooks_suppressed` 只断言结果 SUPPRESSED，被后续复核点
兜底掩盖（钩子前的复核被删除后，钩子仍会被调用，只是结果不变）。补测试
`test_send_reply_stale_before_hooks_skips_hooks`（代次失效时装饰钩子不得被调用）
后击杀，M20 击杀表达式同步指向新测试。全部 13/13 实测 KILLED。

**留档**：变异验证采用与 mutation_check.py 相同的 copy2 备份 + 逐字节恢复流程，
13 点 KILLED 输出随会话记录；nightly CI 调度（cron 0 18 * * * + mutation job）已存在，
新增点直接纳入。
