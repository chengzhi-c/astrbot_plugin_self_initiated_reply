# 04 — 拆分生成管线（主 Agent）

**What to build:** 把正文生成迁出为独立模块：工具边界安装/恢复、最终工具策略强制（fail-closed 与危险工具拒绝）、构建配置组装、生成运行（超时、优雅停止、孤儿收敛）、工具直发追踪。运行期间的全部行为契约保持原样。

**Blocked by:** 01 — 基线冻结与行为契约

**Status:** resolved

## Resolution (2026-08-06)

- 新增 `generation.py`（GenerationRunner，473 行）：生成管线全部迁入
  （提示词/上下文组装、工具边界安装与恢复、最终策略强制 keep/drop 两模式、
  构建配置组装、超时优雅停止与孤儿收敛、直发追踪）。
- 运行时查找模式：`runtime=lambda: _AGENT_RUNTIME`（测试替换 main 全局生效）、
  `enforce_policy` 经 self 回调（round4/5 替换实例方法仍命中）、
  `call_hook`/`grace_stop_sec` 同样经 lambda（round5 替换 main 常量生效）。
- main.py 2017 → 1512 行（-25%）；`_build_context_text` 迁出无壳；
  其余方法保留委托壳（test_main_runtime 直连调用面）。
- round3 源码护栏更新指向 generation.py（锚定 generate/enforce_final_tool_policy/
  install_agent_tool_boundary，断言内容不变，捕获力保持）。
- 新增 `tests/test_generation_runner.py`（15 用例）：假运行时适配器独立单测
  （验收项 1）、直发预算/代次闸门抑制（验收项 2）、超时/取消/失败三类出口
  直发计数与文本不丢（验收项 3）、fail-closed keep/drop 两模式。
- 变异自检（实测）：超时出口 direct_send_count 置 0 变异 → 验收测试真实变红。
- 实测：406 tests 全绿（+15），整体 83.16%，generation.py 覆盖 84%，
  模块门槛 79 已加入 `scripts/coverage_gates.py`，ruff/mypy 全清。
