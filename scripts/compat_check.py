"""真实宿主兼容性检查：插件绑定的私有 AstrBot API 符号存在性 + 契约断言 + 加载路径。

在装有真实 ``astrbot`` 包的环境中运行（CI 兼容矩阵 job 用）：
- 锁定版（默认）：契约缺口（符号缺失/签名缺参/危险工具未覆盖/处理器注解不可解析）即 exit 1
- 最新版（--warn-latest）：同一组检查降级为漂移预警，只告警不阻塞

三类检查的性质不同：前两类只问「宿主有没有这个符号」，第三类
（``_handler_signature_gaps``）**真的走一遍宿主加载期的动作**。0.9.5 之前只有前两类，
结果插件在 4.27.2 上装不上而本脚本仍报 OK——见该函数的 docstring。

存在性清单与契约断言单源：符号清单来自 runtime_adapter.host_contract()，
参数契约来自 AstrBotRuntimeAdapter.validate()——增删符号只需改适配层一处。
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# astrbot 包 import 会在 cwd 生成运行时 data/ 目录：切到临时目录防污染工作区
os.chdir(tempfile.mkdtemp(prefix="astrbot-compat-"))

# 包导入兼容：优先用已安装的包（CI 的 pip install -e 后运行）；本地直接跑
# 脚本而未安装时，以包名把仓库根注册进 sys.modules（与 tests 加载模式同源）。
# Windows 中文路径下 editable 安装的 .pth 会被 pip 以错误编码写入导致 import
# 失败（CI ubuntu UTF-8 无此问题），此回退保证本地也能验证。
try:
    import astrbot_plugin_self_initiated_reply  # noqa: F401
except ModuleNotFoundError:
    import types

    _pkg = types.ModuleType("astrbot_plugin_self_initiated_reply")
    _pkg.__path__ = [str(ROOT)]
    sys.modules["astrbot_plugin_self_initiated_reply"] = _pkg

# 宿主危险内置工具模块：这些模块内所有 FunctionTool 子类的 name 必须全部被
# models.HOST_DANGEROUS_TOOL_IDS 覆盖——宿主新增/改名危险工具时缺失即报错，
# 防止 denylist（"最终防线"）静默失效。与 models.py 的清单同步维护。
DANGEROUS_TOOL_MODULES = [
    "astrbot.core.tools.cron_tools",
    "astrbot.core.tools.knowledge_base_tools",
    "astrbot.core.tools.computer_tools.fs",
    "astrbot.core.tools.computer_tools.shell",
    "astrbot.core.tools.computer_tools.python",
    "astrbot.core.tools.computer_tools.shipyard_neo.browser",
]


# 本检查实际能扫到的处理器数量：on_message + 9 个子指令 = 10（实测，不是推算）。
#
# 为什么不是 11：指令组本身（selfreply）在类属性上已被装饰器换成
# RegisteringCommandable（宿主 star_handler.py:251，只有 group/command/
# custom_filter/parent_group 四个属性），原函数从类属性取不到，故扫不到它。
#
# 这不构成盲区，两条实测理由：
# 1. 宿主只对 CommandFilter 做注解解析（command.py:66 init_handler_md）。
#    CommandGroupFilter 与 EventMessageTypeFilter 都没有这个方法，也没有
#    eval_str 命中——即宿主本身就不解析指令组与 on_message 的注解。本检查扫
#    10 个是宿主那 9 个的**超集**，严于宿主而非松于宿主。
# 2. 10 个子指令与指令组共用同一个 CommandReply 别名，别名一旦不可解析，
#    9 个子指令会同时报错。
#
# 加指令时同步改这里与 tests/test_host_contract.py 的同名断言。
EXPECTED_HANDLER_COUNT = 10


def _handler_signature_gaps() -> list[str]:
    """走一遍宿主注册处理器时真正做的那一步注解解析（0.9.5 补）。

    符号存在性检查**走不到加载路径**，这正是它当初没能拦住 0.9.5 那个 P0 的原因：
    插件在 4.27.2 上装不上（``name 'CommandReply' is not defined``），而当时
    ``host compat OK``。根因已在真机确证：宿主
    ``core/star/filter/command.py::CommandFilter.init_handler_md`` 在 4.23.3 是
    ``inspect.signature(handler)``，4.27.2 起变成 ``inspect.signature(handler,
    eval_str=True)``——一个参数之差，让 ``from __future__ import annotations``
    产出的字符串注解在加载期真的被 eval，于是 TYPE_CHECKING-only 的名字 NameError。

    这里照抄那一步（``eval_str=True``），因此任何「注解里出现运行时不存在的名字」
    都会在此暴露，而不必等到装机。不去 import 宿主的 CommandFilter 来跑：本函数
    要在锁定版与最新版两种宿主上都成立，直接用 inspect 才不受宿主内部重构影响。

    **不允许静默空转**：处理器是按名字前缀筛的，改名或重构后前缀不再命中时，
    循环会一个都扫不到而本函数照旧返回空列表——那是假绿，正是本函数要消灭的
    失败模式的翻版。故先断言扫到的数量等于 ``EXPECTED_HANDLER_COUNT``
    （见该常量上方对「为什么是 10 而不是 11」的实测说明）。
    """
    import inspect

    from astrbot_plugin_self_initiated_reply.main import SelfInitiatedReplyPlugin

    gaps: list[str] = []
    scanned = 0
    for name in sorted(dir(SelfInitiatedReplyPlugin)):
        if name != "on_message" and not name.startswith("selfreply"):
            continue
        target = getattr(SelfInitiatedReplyPlugin, name)
        func = getattr(target, "handler", target)
        if not callable(func):
            continue
        scanned += 1
        try:
            inspect.signature(func, eval_str=True)
        except Exception as exc:
            gaps.append(f"处理器 {name} 的注解在加载期无法解析：{type(exc).__name__}: {exc}")
    if scanned != EXPECTED_HANDLER_COUNT:
        gaps.append(
            f"加载路径检查只扫到 {scanned} 个处理器，预期 {EXPECTED_HANDLER_COUNT} 个："
            f"筛选条件与实际处理器命名已脱节，本检查处于空转状态（假绿）"
        )
    return gaps


def _enumerate_tool_names() -> dict[str, set[str]]:
    """从宿主危险工具模块枚举所有 FunctionTool 子类的 name 类属性。"""
    import importlib
    import inspect

    from astrbot.core.agent.tool import FunctionTool

    found: dict[str, set[str]] = {}
    for mod_name in DANGEROUS_TOOL_MODULES:
        mod = importlib.import_module(mod_name)
        names = {
            str(getattr(obj, "name", "")).strip()
            for _, obj in inspect.getmembers(mod, inspect.isclass)
            if issubclass(obj, FunctionTool) and obj is not FunctionTool
        }
        found[mod_name] = {name for name in names if name}
    return found


def _denylist_gaps() -> dict[str, list[str]]:
    """宿主危险工具全集与 HOST_DANGEROUS_TOOL_IDS 的缺口（空 = 全覆盖）。"""
    from astrbot_plugin_self_initiated_reply.models import HOST_DANGEROUS_TOOL_IDS

    return {
        mod: sorted(names - HOST_DANGEROUS_TOOL_IDS)
        for mod, names in _enumerate_tool_names().items()
        if names - HOST_DANGEROUS_TOOL_IDS
    }


def run_contract_checks(*, warn: bool) -> int:
    """符号存在性 + 契约断言 + denylist 覆盖；返回进程退出码。"""
    import importlib

    # 包化导入（与 CI 的 pip install -e 后运行一致）：插件内部模块使用相对导入，
    # 顶层 import 会断（0.8.8 B1 起 runtime_adapter 引入 .utils 相对导入后实测发现）。
    from astrbot_plugin_self_initiated_reply import runtime_adapter

    failures: list[str] = []
    for mod_name, attrs in runtime_adapter.AstrBotRuntimeAdapter.host_contract():
        mod = importlib.import_module(mod_name)
        for attr in attrs:
            if not hasattr(mod, attr):
                failures.append(f"{mod_name}.{attr} 缺失——宿主私有 API 漂移")

    event_type_members = runtime_adapter.EVENT_TYPE_MEMBERS
    star_handler = importlib.import_module("astrbot.core.star.star_handler")
    for member in event_type_members:
        if not hasattr(star_handler.EventType, member):
            failures.append(f"EventType.{member} 缺失——宿主私有 API 漂移")

    adapter = runtime_adapter.AstrBotRuntimeAdapter.from_host()
    problems = adapter.validate(soft=True)
    gaps = _denylist_gaps()
    for module_name, missing in gaps.items():
        failures.append(f"denylist 未覆盖 {module_name}: {', '.join(missing)}")
    failures.extend(_handler_signature_gaps())

    all_problems = failures + problems
    if not all_problems:
        print("host compat OK")
        return 0
    for problem in all_problems:
        level = "warn" if warn else "error"
        print(f"[{level}] {problem}", file=sys.stderr)
    return 0 if warn else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--warn-latest",
        action="store_true",
        help="最新版漂移预警：契约缺口只告警不阻塞（锁定版硬门禁为默认）",
    )
    args = parser.parse_args()
    return run_contract_checks(warn=args.warn_latest)


if __name__ == "__main__":
    raise SystemExit(main())
