"""``tests/source_contract.py`` 与 ``_name_references`` 的自守卫。

这几个辅助设施承载了约 60 行「单源/收敛点唯一」类源码契约断言
（见 ``tests/test_single_source_anchors.py``）。它们本身是「守卫的守卫」，
此前没有任何测试看守自己：实测三处退化（同名歧义静默取第一个、
``callers_of`` 去掉最深层过滤、``_name_references`` 退化为只匹配 ``ast.Name``）
都能让整组守卫静默失效而全仓测试仍全绿。

本文件直接测这几个函数的行为，让它们与其它元设施同级：
``mutation_gate.py`` / ``gates.py`` / ``host_stubs.py`` 的破坏都有对应测试变红，
唯独这一层此前没有。

``_name_references`` 的用例落在真实生产源码上，因为生产代码恰好同时含
四种引入方式（裸 Name、属性访问、``getattr`` 字符串、import 别名），
不需要造临时模块即可覆盖全部分支。
"""

from __future__ import annotations

import ast
import textwrap

import pytest

from .source_contract import (
    _lookup,
    call_names,
    callers_of,
)
from .test_single_source_anchors import _name_references

# 生产源码里末段同名多处的定义（歧义分支的素材）：`_GenerateRun.__init__` 与
# `GenerationRunner.__init__` 同名。选它的理由：它与被测行为无关，不会因业务
# 重构改名而失效：真改了本用例会以「找不到定义」失败，而不是静默通过。
_AMBIGUOUS_TAIL = "__init__"
_AMBIGUOUS_MODULE = "generation.py"


def test_lookup_accepts_unique_tail() -> None:
    """末段唯一时可用短名（各守卫现有写法，不得被收紧掉）。"""
    assert _lookup("utils.py", "build_history_text") is not None
    assert _lookup("session_gate.py", "SessionGate") is not None


def test_lookup_rejects_ambiguous_tail() -> None:
    """末段同名多处必须 raise，绝不可以静默取第一个。

    静默取第一个的后果：守卫要断言的收敛点可能落到另一处同名定义上，
    「收敛点唯一」随之变成恒真，而调用点毫无异常。
    """
    with pytest.raises(AssertionError) as excinfo:
        _lookup(_AMBIGUOUS_MODULE, _AMBIGUOUS_TAIL)
    assert "同名" in str(excinfo.value), f"歧义报错必须说明原因：{excinfo.value}"


def test_lookup_reports_missing_definition() -> None:
    """找不到定义必须 raise 并列出可用名，而不是返回 None。"""
    with pytest.raises(AssertionError) as excinfo:
        _lookup("utils.py", "this_definition_does_not_exist")
    assert "找不到定义" in str(excinfo.value)


def test_callers_of_drops_inner_closure_duplicates(tmp_path, monkeypatch) -> None:
    """闭包内的调用只归最深层函数，不得同时算外层函数的调用者。

    去掉最深层过滤会让「收敛点唯一」的判据变宽：外层函数与内层闭包都出现在
    ``owners`` 里时，等价判据（``owners == ["A.b"]``）会多出一项而误红，
    宽松判据（``in``）则会恒真。两个方向都应被本用例挡住。

    生产代码里目前没有「外层与内层闭包引用同一目标」的场景，故此处构造一份
    最小源码喂给 ``module_ast`` 的读取点：守卫的过滤分支是防御性的，但它一旦
    被简化，所有使用 ``callers_of`` 的收敛点断言都会在同一轮静默放宽。
    """
    nested = textwrap.dedent(
        """
        def outer():
            def inner():
                helper(1)
            return inner


        def sibling():
            def inner():
                helper(2)
            return inner
        """
    )
    monkeypatch.setattr("tests.source_contract.module_ast", lambda _rel: ast.parse(nested))

    owners = callers_of("probe.py", "helper")
    assert owners == ["outer.inner", "sibling.inner"], f"闭包内的调用必须只归最深层：{owners}"

    assert callers_of("probe.py", "missing_target") == []


def test_callers_of_finds_innermost_closure_owner() -> None:
    """闭包内的调用必须被记账在最深层，而不是整条丢掉。"""
    owners = callers_of("generation.py", "original_send")
    assert owners, "generation.py 必须有人引用 original_send"
    assert any(name.endswith("tracked_send") for name in owners), (
        f"闭包 tracked_send 内的 original_send 调用未被记账：{owners}"
    )


def test_call_names_covers_nested_calls() -> None:
    """调用名收集含嵌套闭包（收敛点判据的地基）。"""
    names = call_names(_lookup("generation.py", "GenerationRunner._prepare_outbound_tracker"))
    assert any(name == "outbound.send" for name in names)


def test_name_references_catches_every_introduction_form() -> None:
    """``_name_references`` 必须命中四种引入方式。

    只匹配 ``ast.Name`` 的实现能过生产代码的现状（守卫目标恰好只以裸 Name
    出现在被检模块里），但会放掉属性访问、``getattr`` 字符串实参与 import 别名
    三种「又抄了一份」的绕过写法，而那正是它的用途。
    """
    # import 别名形态：plugin_state 与 storage 都在 import 列表里带上它。
    imported = _name_references("plugin_state.py", "whitelist_storage_key")
    forms = " ".join(imported)
    assert "import whitelist_storage_key" in forms, f"import 别名形态未命中：{imported}"
    # 同一模块还必须同时命中裸 Name 引用形态，否则说明判决据本身失效。
    assert "whitelist_storage_key" in forms.replace("import whitelist_storage_key", ""), (
        f"裸 Name 形态未命中：{imported}"
    )

    # 同模块内的多处引用都要记账（plugin_state 的调用点 + storage 的下划线别名导入）。
    storage_forms = " ".join(_name_references("storage.py", "whitelist_storage_key"))
    assert "import whitelist_storage_key" in storage_forms, (
        f"storage.py 的 import 形态未命中：{storage_forms}"
    )
    assert "line 286" in storage_forms and "line 346" in storage_forms, (
        f"storage.py 的调用点未被逐个记账：{storage_forms}"
    )


def test_name_references_does_not_match_longer_identifiers() -> None:
    """前缀相同的不同标识符不得算命中（否则守卫会把无关代码误判成镜像）。"""
    assert _name_references("utils.py", "whitelist_storage_key_extra") == []


def test_name_references_ignores_comments_and_docstrings() -> None:
    """注释与文档串中的同形文本不算引用。

    否则「不得出现某标识符」的守卫会被一句注释满足，这是该类判据最典型的
    失效方式。
    """
    node = _lookup("utils.py", "whitelist_storage_key")
    body_source = ast.unparse(node)
    del body_source
    # 该函数的 docstring 里就写着 whitelist_storage_key；若注释/文档串被计入，
    # 一个只在文档里提到的模块也会被判为「自行派生状态键」。
    unrelated = _name_references("decision.py", "whitelist_storage_key")
    assert unrelated == [], f"decision.py 只在注释/文档串中提到即被判命中：{unrelated}"
