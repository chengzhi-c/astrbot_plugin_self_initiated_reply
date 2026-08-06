# 13 — 宿主符号收敛（expand-contract）

**What to build:** 把对宿主私有 API 的依赖收敛到两处：运行时适配层（能力探测与调用）与宿主桩（测试与兼容检查）。expand 阶段逐符号把直接 import 迁移进适配层并保留旧引用；contract 阶段删除直接引用。兼容检查从"加载冒烟"升级为契约断言（签名参数集合 + 关键行为），锁定版为硬门禁、最新版为漂移预警。

**Blocked by:** 07 — 会话状态显式化

**Status:** resolved（2026-08-06）

- [x] 全仓库 grep：宿主私有符号只出现在适配层与宿主桩两处
- [x] 契约断言覆盖全部私有入口（构建/运行/会话装载/事件结果），缺失参数即红
- [x] 兼容检查对锁定版硬门禁、对最新版不阻塞但告警
- [x] 旧测试全绿

## Resolution

### contract：直接 import 全量删除

- `delivery.py` 删除 `astrbot.core.message.message_event_result` /
  `astrbot.core.star.star_handler` 直接 import → 经注入的 runtime getter 取
  窄方法：`new_event_result()` / `result_llm_type` / `event_type.*` 成员
- `generation.py` 删除 `astrbot.core.provider.entities.ProviderRequest` /
  `star_handler.EventType` → `runtime().new_provider_request()` /
  `runtime().event_type.OnLLMRequestEvent`
- `main.py` 删除 `astrbot.core.pipeline.context.call_event_hook` /
  `astrbot.core.utils.astrbot_path` 直接 import → 模块级名字保留为适配层
  探测值绑定（测试替换 main.call_event_hook 等仍生效）；`_validate_agent_api`
  契约断言兜底（缺失即红，拒绝加载）
- 唯一收敛点：`runtime_adapter.py`（探测+调用+断言）、`tests/host_stubs.py`（桩）、
  `scripts/compat_check.py`（检查）；`test_private_host_symbols_confined`
  用 import 语句正则锁定全仓库无泄漏

### 契约断言（runtime_adapter.validate）

- 新增 capabilities：`event_result_cls` / `result_content_type` / `event_type` /
  `call_event_hook` / `provider_request_cls` / `config_path_fn` /
  `plugin_data_path_fn`（旧版 AstrBot 路径允许 None 回退）
- 断言覆盖：构建/运行/会话装载（原有）+ 事件结果可实例化且具备
  `message`/`set_result_content_type` 方法 + `ResultContentType.LLM_RESULT`
  成员 + `EventType` 三成员（OnLLMRequestEvent/OnDecoratingResultEvent/
  OnAfterMessageSentEvent）+ call_event_hook 可调用 + ProviderRequest 实例
  字段集（prompt/image_urls/audio_urls/func_tool/session_id/conversation/
  contexts）+ 路径函数 callable 或 None
- `validate(soft=False) -> list[str]`：硬模式首错 raise；软模式收集告警
  （最新版漂移预警）

### 兼容检查双模式（compat_check.py）

- 符号存在性清单单源：`AstrBotRuntimeAdapter.host_contract()`（原 CHECKS 迁移）
- 默认（锁定版）：全部缺口 exit 1；`--warn-latest`：只告警不阻塞
- 真实宿主实测：本机 AstrBot v4.23.3 → `host compat OK`（含 denylist 覆盖
  枚举），锁定/软两种模式均验证通过

### 顺带修复

- mutation_check.py：subprocess `text=True` 缺 encoding → Windows GBK 解码
  git 中文输出崩溃（3 处补 utf-8/errors=replace）
- 2 个变异点锚点漂移（force-cancel / running-check-pop 随 ticket 11 迁入
  scheduler.py）→ rel 迁移，kill 测试验证仍红；全量 38/38 KILLED
- runtime_adapter 顶层独立可导入（maybe_await 内联，compat_check 顶层
  import 不再依赖包上下文）

### 测试

- 新增 `tests/test_host_contract.py`（11 项）：收敛断言 + 契约红/绿 +
  软模式告警 + 窄方法 + host_contract 单源
- `test_runtime_adapter.py` / `test_delivery_runner.py` /
  `test_generation_runner.py` 适配新构造（runtime getter / FakeRuntime 补
  new_provider_request / event_type）
- `test_storage_and_umo.py` 锚点文本更新（装饰钩子调用形态）
- 516 passed、覆盖率门槛 PASS、ruff/mypy 全清
