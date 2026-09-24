"""变异门禁自身的 fail-closed 判据：目标缺失不得被当成「已捕获」。

``node --test`` 对「目标文件不存在」与「测试失败」都返回 1（实测），仅按退出码
判定会把「目标测试被删掉/改名」记成 CAUGHT——正是门禁要防的假绿灯。
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

from .host_stubs import ROOT

MISSING_MJS = "tests/does_not_exist_mutation_probe.mjs"


def _gate():
    return importlib.import_module("scripts.mutation_gate")


def _mutation(target: str, runner: str = "node"):
    gate = _gate()
    return gate.Mutation(
        key="probe_target_liveness",
        rel="models.py",
        anchor="__probe_anchor__",
        replacement="__probe_replacement__",
        contract="§0",
        note="探针：目标文件缺失/存在时的判据",
        targets=(target,),
        runner=runner,
    )


def _fake_runner(calls: list[list[str]], *, returncode: int = 0):
    def run(argv, **_kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=returncode, stdout="", stderr="")

    return run


def test_missing_node_target_is_fail_closed(monkeypatch) -> None:
    """目标缺失必须落 ERROR 且不启动测试进程（前置检查失败即返回）。"""
    gate = _gate()
    assert not (ROOT / MISSING_MJS).exists(), "探针目标不该存在"
    calls: list[list[str]] = []
    monkeypatch.setattr(gate, "subprocess", SimpleNamespace(run=_fake_runner(calls)))

    status, _elapsed, detail = gate._run_targets(_mutation(MISSING_MJS))

    assert status.startswith("ERROR"), f"目标缺失被判为 {status}（假绿）"
    assert MISSING_MJS in detail
    assert not calls, "目标缺失时不该启动测试进程"


def test_present_target_still_reaches_the_runner(monkeypatch) -> None:
    """守卫不得过宽：目标存在时必须真的跑，退出码照旧映射为状态。"""
    gate = _gate()
    calls: list[list[str]] = []
    monkeypatch.setattr(gate, "subprocess", SimpleNamespace(run=_fake_runner(calls)))

    status, _elapsed, _detail = gate._run_targets(_mutation("tests/test_outbound.py"))

    assert len(calls) == 1, "目标存在时未启动测试进程"
    assert calls[0][:2] == ["node", "--test"]
    assert status == "MISSED"  # 假的 0 退出码 → 未被捕获
