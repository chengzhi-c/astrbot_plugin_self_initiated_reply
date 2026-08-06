# 08 — 主插件覆盖补盲（60% → 80%）

**What to build:** 把主插件当前未覆盖的关键路径补上红灯测试：巡检循环、判断提示词构建与变量注入、免打扰时段解析、指令分支（含权限拒绝/回滚/取消语义）、清理逻辑、生成超时与取消出口。测试按 07 之后的新模块组织。

**Blocked by:** 07 — 会话状态显式化

**Status:** resolved

## Resolution (2026-08-06)

- 新增 `tests/test_main_coverage.py`（40 用例，+8 第二批补盲后共 40）：
  - on_message 门卫：命令已处理/禁用/非白名单/忽略名单（含 direct call 推进
    last_active）/空文本/纯图占位/图片后台捕获/快照失败降级/提取为空
  - `_prepare_images_for_session` 全出口：陈旧代次/超时/异常/全部失败/vision 关闭
  - 图片 parser 缓存（同 provider 共享、超时变化重建、vision 关闭 None）与
    描述上下文（含空描述/无 parser/无图片）
  - check 守卫全分支、判断早退（str 透传/代次拒绝/dict 不回复）、直发文本
    重复抑制（真实直发收集路径）
  - 命令文本全分支（help/list/remove/check/on/off/debug/未知/无 umo）、
    check 非白名单回收、stopping 拒绝、全部子命令处理器、send 兜底
  - 管理员热读（缓存命中/重载/坏文件）、路径解析分支、事件 extra 容错、
    后台任务取消、track 屏障 close 失败兜底
- **测试基建教训（实测暴露）**：本机真实安装 astrbot 4.23.3，若先 import
  完整插件树（`_load_main` 式）会真实加载 `astrbot.core.astr_main_agent`，
  使 host_stubs 的 `not hasattr` 补全失效 → 后续 pipeline 测试的
  capabilities.build_main_agent 变真实宿主函数（调 context.get_using_provider）
  → 测试间污染。修复：补盲测试一律不导入真实链，只经 with_plugin 的
  host_stubs 包访问（early-return 测试改从 `main.__package__` 取常量）。
- 实测：main.py 69% → 96%（目标 ≥80%），全量 492 passed（+40），整体
  90.49%，门槛 main 55 → 80 加入 `scripts/coverage_gates.py`，ruff/mypy 全清。
- 剩余缺失 23 行：构造期异常（mkdir/startup cleanup）、写盘失败、
  shield 分支、不可达防御（events 判定先拦截）、装饰器替换的组入口——刻意不追。
