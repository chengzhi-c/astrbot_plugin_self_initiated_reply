"""源码结构契约断言辅助：按 AST 定位，不按文本切片。

有些契约（收敛点唯一、日志级别、禁止某写法）只能在源码层面断言。用 AST 而非
``source.index("    def foo(")`` 定位，是因为切片必须指定终点，而终点只能写成
**相邻方法名**——邻居改名或中间插入新方法就会连带变红，与被断言的行为无关。

对被断言方法自身改名仍会变红，这是故意的：那是契约变更，应当有人复核。
"""

from __future__ import annotations

import ast
from functools import cache

from .host_stubs import ROOT

__all__ = [
    "call_names",
    "callers_of",
    "calls_in",
    "constructor_param_bindings",
    "defines",
    "logger_levels_for",
    "method_source",
    "module_ast",
    "source_of",
]


@cache
def module_ast(rel: str) -> ast.Module:
    """解析仓库内源文件为 AST（按相对路径缓存）。"""
    return ast.parse((ROOT / rel).read_text(encoding="utf-8"))


@cache
def source_of(rel: str) -> str:
    """读取仓库内源文件全文（按相对路径缓存）。"""
    return (ROOT / rel).read_text(encoding="utf-8")


def _walk_scopes(node: ast.AST, prefix: str = ""):
    """深度优先产出 (qualname, node)，覆盖模块级与类内的函数/类定义。"""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            qualname = f"{prefix}{child.name}"
            yield qualname, child
            yield from _walk_scopes(child, f"{qualname}.")


def _lookup(rel: str, qualname: str) -> ast.AST:
    """按限定名定位定义节点。

    ``qualname`` 可写全（``AstrBotBridge.llm_generate``）或只写末段
    （``llm_generate``）。末段写法在同名定义存在多处时抛错，强制写清楚。
    """
    tree = module_ast(rel)
    scopes = dict(_walk_scopes(tree))
    if qualname in scopes:
        return scopes[qualname]
    tail_matches = [name for name in scopes if name.rsplit(".", 1)[-1] == qualname]
    if len(tail_matches) == 1:
        return scopes[tail_matches[0]]
    if not tail_matches:
        raise AssertionError(f"{rel} 中找不到定义 {qualname!r}（可用：{sorted(scopes)[:12]}…）")
    raise AssertionError(
        f"{rel} 中 {qualname!r} 有多处同名定义，请写限定名：{sorted(tail_matches)}"
    )


def defines(rel: str, qualname: str) -> bool:
    """源文件中是否存在该定义（函数/方法/类）。"""
    try:
        _lookup(rel, qualname)
    except AssertionError:
        return False
    return True


def method_source(rel: str, qualname: str) -> str:
    """取某个定义的源码片段（边界由 AST 给出，不含相邻定义）。"""
    node = _lookup(rel, qualname)
    lines = source_of(rel).splitlines()
    start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
    return "\n".join(lines[start - 1 : node.end_lineno])


def call_names(node: ast.AST) -> list[str]:
    """产出节点内所有调用的被调表达式文本（如 ``self._gate.advance``）。"""
    return [ast.unparse(child.func) for child in ast.walk(node) if isinstance(child, ast.Call)]


def calls_in(rel: str, qualname: str) -> list[str]:
    """某个定义体内发生的全部调用名（含嵌套闭包）。"""
    return call_names(_lookup(rel, qualname))


def callers_of(rel: str, call_name: str) -> list[str]:
    """某个调用在哪些函数/方法里发生（限定名，去重排序）。

    用于表达「收敛点唯一」：``callers_of("delivery.py", "last_event.clear_result")``
    应当只返回 ``["DeliveryRunner._clear_result"]``。
    """
    owners: set[str] = set()
    for qualname, node in _walk_scopes(module_ast(rel)):
        if isinstance(node, ast.ClassDef):
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Call) and ast.unparse(child.func) == call_name:
                owners.add(qualname)
    # 内层函数同时命中时只保留最深的那个（闭包归属它自己，不归属外层）
    return sorted(
        name for name in owners if not any(other.startswith(f"{name}.") for other in owners)
    )


def constructor_param_bindings(rel: str, cls: str) -> dict[str, str]:
    """``__init__`` 里 ``self.<attr> = <param>`` 的 ``{形参名: 属性名}`` 映射。

    用途是证明「共享容器持有者表是完整的」：手工维护的持有者清单会随新增
    协作对象而过期（B1 就是漏了 6 个绑定），而这里从源码枚举，漏一个即可
    被交叉核对发现。只认裸形参赋值——``dict(param)`` 之类是拷贝、不是共享
    引用，本就不该进持有者表。
    """
    init = _lookup(rel, f"{cls}.__init__")
    args = init.args  # type: ignore[attr-defined]
    param_names = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    bindings: dict[str, str] = {}
    for stmt in ast.walk(init):
        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
            continue
        target, value = stmt.targets[0], stmt.value
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
            and isinstance(value, ast.Name)
            and value.id in param_names
        ):
            bindings[value.id] = target.attr
    return bindings


def logger_levels_for(rel: str, template: str) -> list[str]:
    """某条日志模板对应的全部 ``logger.<level>`` 级别。

    以 AST 调用节点为单位匹配首个字符串实参，不受换行/缩进/折行影响。
    """
    levels: list[str] = []
    for node in ast.walk(module_ast(rel)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        target = node.func
        if ast.unparse(target.value) != "logger" or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            if template in first.value:
                levels.append(f"logger.{target.attr}")
    return levels
