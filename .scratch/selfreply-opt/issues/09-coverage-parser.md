# 09 — 图片解析链路覆盖补盲（63% → 80%）

**What to build:** 图片解析链路的未覆盖分支补盲：错误降级（坏格式、超时、Provider 异常）、缓存逐出（年龄/容量/清理互斥）、快照与索引时序、直发与描述缓存复用。与结构拆分工作并行，不等待 07。

**Blocked by:** 01 — 基线冻结与行为契约

**Status:** resolved

## Resolution (2026-08-06)

- 新增 `tests/test_vision_parser_gaps.py`（63 用例，只补分支不改源）：DNS 守卫、
  传输层拒绝、prepare/快照/批处理、parse 降级（空描述/拒识/超时/异常/截断/
  chain 回退）、清理守卫（data URL 保护、symlink 跳过、unlink/stat 失败降级、
  配额保护）、源解析（recorder/相对路径/远程）、内容寻址写入、远程下载全分支
  （fake httpx 客户端，不触网络）。
- 实测：image/parser.py 63.4% → 100%（349 tests 全绿，整体 79.80%）。
- 模块门槛上调 58 → 80（`scripts/coverage_gates.py`），PASS 实测。

- [ ] 图片解析链路覆盖 ≥80%，模块级门槛生效
- [ ] 缓存逐出与"保护窗口内源不被清"的红灯测试补齐
- [ ] 旧测试全绿
