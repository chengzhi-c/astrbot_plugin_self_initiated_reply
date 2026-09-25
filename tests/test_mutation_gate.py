"""变异门禁自身的可信度：基线把关 / JUnit 判定 / 专属临时根。

判据从「只看退出码」升级为「看报告实体」。原实现的漏洞（实测过的）：
- ``node --test`` 对「文件加载失败」「after hook 失败」都返回 1，旧门禁把 rc==1
  一律记 CAUGHT——目标测试被语法错误/import 崩掉也算「捕获」；
- 没有注入前基线：目标测试本来就挂时，任何变异都会被记成 CAUGHT；
- 没要求 ``failures=0, errors=0``：teardown error 的 JUnit 形态是
  ``errors=1, failures=0``（实测），旧门禁只看 rc!=0 也算捕获。

这里钉住的行为：
1) 每个唯一 ``(runner, targets)`` 注入前先跑一次未变异基线并缓存；基线必须
   rc=0、报告含 >=1 个真实目标用例、failures=0、errors=0，否则该变异 ERROR 且
   不得注入。
2) pytest 只有 failures>0 且 errors==0 才 CAUGHT；teardown/setup/sessionfinish
   清理失败、runner 崩溃、无报告都 ERROR。node 必须至少一个**非文件级、非 hook**
   的 ``testCodeFailure``；空 mjs 被 node 记 1 pass，基线必须 ERROR；
   after hook failure 不得 CAUGHT。
3) 每次 pytest 运行用新建且已 mkdir 的独立 ``--basetemp``，TEMP/TMP 指向同一
   专属根；node 报告也放专属根。自有报告/临时根在 ``finally`` 清理。
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from .host_stubs import ROOT, arg_value

TARGET_PY = "tests/test_outbound.py"
TARGET_MJS = "tests/frontend_contract.test.mjs"


def _gate():
    return importlib.import_module("scripts.mutation_gate")


def _mutation(target: str, *, runner: str = "node", key: str = "probe"):
    gate = _gate()
    return gate.Mutation(
        key=key,
        rel="models.py",
        anchor="__probe_anchor__",
        replacement="__probe_replacement__",
        contract="§0",
        note="探针",
        targets=(target,),
        runner=runner,
    )


# --------------------------------------------------------------------------
# JUnit 判定：纯函数 + 表驱动
# --------------------------------------------------------------------------


def _case(
    name: str = "t",
    *,
    kind_failure: str = "",
    kind_error: str = "",
    message: str = "boom",
) -> str:
    if kind_failure:
        body = (
            f'<testcase name="{name}"><failure type="{kind_failure}" '
            f'message="{message}">body</failure></testcase>'
        )
    elif kind_error:
        body = f'<testcase name="{name}"><error message="{message}">body</error></testcase>'
    else:
        body = f'<testcase name="{name}"/>'
    return body


def _suite(*cases: str, tests: int, failures: int, errors: int) -> str:
    joined = "".join(cases)
    return (
        '<?xml version="1.0" encoding="utf-8"?><testsuites>'
        f'<testsuite name="pytest" tests="{tests}" failures="{failures}" errors="{errors}">'
        f"{joined}</testsuite></testsuites>"
    )


def _parse(text: str) -> object:
    import xml.etree.ElementTree as ET

    return ET.fromstring(text)


def test_pytest_caught_requires_real_failure_and_no_errors() -> None:
    """pytest：failures>0 且 errors==0 才算捕获，其余（含统计造假）一律不捕获。"""
    gate = _gate()

    real = _suite(_case(kind_failure="testCodeFailure"), tests=1, failures=1, errors=0)
    assert gate._is_pytest_caught(_parse(real)), "真实 assertion 失败必须算捕获"

    teardown = _suite(_case(kind_error="failed on teardown"), tests=1, failures=0, errors=1)
    assert not gate._is_pytest_caught(_parse(teardown)), "teardown error 不得算捕获"

    setup = _suite(_case(kind_error="failed on setup"), tests=1, failures=0, errors=1)
    assert not gate._is_pytest_caught(_parse(setup)), "setup error 不得算捕获"

    clean = _suite(_case(), tests=1, failures=0, errors=0)
    assert not gate._is_pytest_caught(_parse(clean)), "全绿不是捕获（是 MISSED）"

    empty = _suite(tests=0, failures=0, errors=0)
    assert not gate._is_pytest_caught(_parse(empty)), "没收集到用例不得算捕获"

    mixed = _suite(_case(kind_failure="testCodeFailure"), tests=2, failures=1, errors=1)
    assert not gate._is_pytest_caught(_parse(mixed)), "夹带任何 error 都不得算捕获"


def test_node_caught_requires_real_case_failure() -> None:
    """node：真实用例的 testCodeFailure 才算捕获；文件级 / hook 失败一律不算。"""
    gate = _gate()

    real = _suite(
        _case(name="renders revision", kind_failure="testCodeFailure"),
        tests=1,
        failures=1,
        errors=0,
    )
    assert gate._is_node_caught(_parse(real)), "真实 assertion 必须算捕获"

    before_hook = _suite(
        _case(
            name="ok",
            kind_failure="hookFailed",
            message="failed running before hook",
        ),
        tests=1,
        failures=1,
        errors=0,
    )
    assert not gate._is_node_caught(_parse(before_hook)), "before hook 失败不得算捕获"

    after_hook = _suite(
        _case(
            name="ok",
            kind_failure="hookFailed",
            message="after boom",
        ),
        tests=1,
        failures=1,
        errors=0,
    )
    assert not gate._is_node_caught(_parse(after_hook)), "after hook 失败不得算捕获"

    file_level = _suite(
        _case(name=str((ROOT / "x.mjs").as_posix()), kind_failure="testCodeFailure"),
        tests=1,
        failures=1,
        errors=0,
    )
    assert not gate._is_node_caught(_parse(file_level)), (
        "文件级失败不得算捕获（目标根本加载不起来）"
    )

    other_type = _suite(
        '<testcase name="t"><failure type="testTimeout" message="slow">body</failure></testcase>',
        tests=1,
        failures=1,
        errors=0,
    )
    assert not gate._is_node_caught(_parse(other_type)), "非 testCodeFailure 不得算捕获"

    clean = _suite(_case(), tests=1, failures=0, errors=0)
    assert not gate._is_node_caught(_parse(clean)), "全绿不是捕获"


def test_baseline_requires_real_target_cases() -> None:
    """基线：空 mjs 被 node 记 1 pass，必须识别为「没有真实目标用例」。"""
    gate = _gate()
    assert (ROOT / TARGET_MJS).exists()

    empty = _suite(_case(name="empty_probe.mjs"), tests=1, failures=0, errors=0)
    summary = gate._report_summary(_parse(empty), runner="node")
    assert not gate._baseline_ok(summary, rc=0), "空 mjs 的 1 pass 不是真实目标用例"

    real = _suite(_case(name="some node test"), tests=1, failures=0, errors=0)
    summary = gate._report_summary(_parse(real), runner="node")
    assert gate._baseline_ok(summary, rc=0), "干净的真实用例必须能过基线"


@pytest.mark.parametrize(
    ("rc", "tests", "failures", "errors"),
    [
        (1, 1, 0, 0),  # 目标本来就挂
        (0, 1, 1, 0),  # 有失败
        (0, 0, 0, 0),  # 一个用例都没有
        (2, 0, 0, 0),  # runner 用法错误
    ],
)
def test_baseline_rejects_anything_unhealthy(
    rc: int, tests: int, failures: int, errors: int
) -> None:
    gate = _gate()
    xml = _suite(
        *[_case(name=f"t{i}") for i in range(tests)],
        tests=tests,
        failures=failures,
        errors=errors,
    )
    summary = gate._report_summary(_parse(xml), runner="pytest")
    assert not gate._baseline_ok(summary, rc=rc)


# --------------------------------------------------------------------------
# 编排：基线缓存 / 专属临时根 / finally 清理
# --------------------------------------------------------------------------


def _phase_of(argv: list[str]) -> str:
    """这次运行是基线还是注入后：从报告文件名读。"""
    report = _report_of(argv)
    return "baseline" if report is not None and "baseline" in report.name else "run"


def _report_of(argv: list[str]) -> Path | None:
    """从命令行里取 JUnit 报告路径（pytest 用 ``--junitxml``，node 用 reporter-destination）。"""
    for index, arg in enumerate(argv):
        if arg in ("--junitxml", "--test-reporter-destination") and index + 1 < len(argv):
            return Path(argv[index + 1])
        if arg.startswith("--junitxml=") or arg.startswith("--test-reporter-destination="):
            return Path(arg.split("=", 1)[1])
    return None


class _Runner:
    """subprocess.run 桩：按该次运行的报告路径吐预置 XML，并记录每次调用。"""

    def __init__(self, reports: dict[str, list[str]], returncodes: list[int]) -> None:
        self.reports = reports
        self.returncodes = returncodes
        self.calls: list[SimpleNamespace] = []
        self.basetemps_existed: list[bool] = []

    def __call__(self, argv, **kwargs):
        report = _report_of(argv)
        rc = self.returncodes.pop(0) if self.returncodes else 1
        if report is not None:
            report.parent.mkdir(parents=True, exist_ok=True)
            bodies = self.reports.get(report.name)
            if bodies:
                report.write_text(bodies.pop(0), encoding="utf-8")
            else:
                # 没有预置报告的阶段，写一份空壳，逼实现落 ERROR(no-report)
                report.write_text('<testsuites name="pytest tests"/>', encoding="utf-8")
        if any(arg.startswith("--basetemp") for arg in argv):
            # 进程启动这一刻记录 basetemp 是否已存在：finally 的清理发生在返回之后，
            # 所以这里取到的是「门禁是否先 mkdir」的实测事实。
            self.basetemps_existed.append(Path(arg_value(argv, "--basetemp")).is_dir())
        self.calls.append(
            SimpleNamespace(
                argv=list(argv),
                cwd=kwargs.get("cwd"),
                env=dict(kwargs.get("env") or {}),
                timeout=kwargs.get("timeout"),
            )
        )
        return SimpleNamespace(returncode=rc, stdout="", stderr="")

    @property
    def argv_lists(self) -> list[list[str]]:
        return [call.argv for call in self.calls]


def _patch_gate(monkeypatch, gate, runner: _Runner, scratch: Path) -> None:
    monkeypatch.setattr(gate, "subprocess", SimpleNamespace(run=runner))
    monkeypatch.setattr(gate, "_scratch_root", lambda: scratch)
    monkeypatch.setattr(gate, "_BASELINES", {})


CLEAN_PY = _suite(_case(name="t"), tests=1, failures=0, errors=0)
FAILING_PY = _suite(_case(kind_failure="testCodeFailure"), tests=1, failures=1, errors=0)
CLEAN_MJS = _suite(_case(name="node test"), tests=1, failures=0, errors=0)
FAILING_MJS = _suite(
    _case(name="node test", kind_failure="testCodeFailure"), tests=1, failures=1, errors=0
)


def test_baseline_failure_blocks_injection(monkeypatch, tmp_path) -> None:
    """基线不干净 → ERROR，且不得启动变异后的目标运行。"""
    gate = _gate()
    mutation = _mutation(TARGET_PY, runner="pytest", key="baseline_probe")
    runner = _Runner({"junit-baseline.xml": [CLEAN_PY]}, returncodes=[1, 0])
    _patch_gate(monkeypatch, gate, runner, tmp_path / "scratch")

    status, _elapsed, detail = gate._run_targets(mutation)

    assert status.startswith("ERROR"), f"基线失败必须 ERROR，实际 {status}"
    assert "基线" in detail
    assert len(runner.calls) == 1, "基线未通过时不得跑变异后的目标测试"


def test_real_assertion_capture_is_caught(monkeypatch, tmp_path) -> None:
    """基线干净 + 注入后真实 assertion 失败 → CAUGHT（报告驱动，非退出码）。"""
    gate = _gate()
    mutation = _mutation(TARGET_PY, runner="pytest", key="capture_probe")
    runner = _Runner(
        {"junit-baseline.xml": [CLEAN_PY], "junit-run.xml": [FAILING_PY]},
        returncodes=[0, 1],
    )
    _patch_gate(monkeypatch, gate, runner, tmp_path / "scratch")

    status, _elapsed, _detail = gate._run_targets(mutation)

    assert status == "CAUGHT", f"真实 assertion 必须判捕获，实际 {status}"
    assert len(runner.calls) == 2, "必须先基线再变异"
    basetemps = [arg_value(argv, "--basetemp") for argv in runner.argv_lists]
    assert len(set(basetemps)) == 2, "每次运行都必须是独立 basetemp"
    for call, argv in zip(runner.calls, runner.argv_lists, strict=True):
        assert call.env["TEMP"] == call.env["TMP"], "TEMP/TMP 必须指向同一专属根"
        assert Path(call.env["TEMP"]).is_absolute()
        assert "PYTEST_ADDOPTS" not in call.env
        assert "PYTEST_DEBUG_TEMPROOT" not in call.env
        base = Path(arg_value(argv, "--basetemp"))
        assert base.is_relative_to(Path(call.env["TEMP"])), "basetemp 必须落在该次 TEMP 根下"
        assert f"basetemp-{_phase_of(argv)}" in str(base), "基线/运行的 basetemp 必须互不共用"
    assert all(runner.basetemps_existed), "每次运行启动时 basetemp 必须已由门禁 mkdir"
    assert not any(
        Path(arg_value(argv, "--basetemp")).is_relative_to(ROOT) for argv in runner.argv_lists
    )


def test_cleanup_failure_is_not_caught(monkeypatch, tmp_path) -> None:
    """断言失败已发生，但自有临时根清理失败：仍须 ERROR，不能保留 CAUGHT。"""
    gate = _gate()
    runner = _Runner(
        {"junit-baseline.xml": [CLEAN_PY], "junit-run.xml": [FAILING_PY]},
        returncodes=[0, 1],
    )
    _patch_gate(monkeypatch, gate, runner, tmp_path / "scratch")
    monkeypatch.setattr(gate, "_cleanup_scratch", lambda _root: "清理临时根失败: probe")

    status, _elapsed, detail = gate._run_targets(_mutation(TARGET_PY, runner="pytest"))

    assert status == "ERROR(cleanup)", f"清理失败必须 ERROR，实际 {status}"
    assert "清理" in detail


def test_baseline_is_cached_per_runner_targets(monkeypatch, tmp_path) -> None:
    """同一次 main() 内，同一 (runner, targets) 的基线只跑一次。"""
    gate = _gate()
    scratch = tmp_path / "scratch"
    first = _mutation(TARGET_PY, runner="pytest", key="cache_a")
    second = _mutation(TARGET_PY, runner="pytest", key="cache_b")
    runner = _Runner(
        {"junit-baseline.xml": [CLEAN_PY], "junit-run.xml": [FAILING_PY, FAILING_PY]},
        returncodes=[0, 1, 1],
    )
    _patch_gate(monkeypatch, gate, runner, scratch)

    assert gate._run_targets(first)[0] == "CAUGHT"
    assert gate._run_targets(second)[0] == "CAUGHT"
    # 第一次：基线 + 变异；第二次：只跑变异（基线命中缓存）
    assert len(runner.calls) == 3, "基线缓存未生效"

    third = _mutation(TARGET_MJS, runner="node", key="cache_node")
    runner.reports["junit-node-baseline.xml"] = [CLEAN_MJS]
    runner.reports["junit-node-run.xml"] = [FAILING_MJS]
    runner.returncodes.extend([0, 1])
    assert gate._run_targets(third)[0] == "CAUGHT", "不同 runner/targets 必须各自建基线"
    assert len(runner.calls) == 5, "node 必须先建自己的基线再跑变异"
    assert runner.calls[-1].argv[:2] == ["node", "--test"]


def test_node_after_hook_failure_is_not_caught(monkeypatch, tmp_path) -> None:
    """注入后只剩 after hook 失败 → ERROR，不得记成捕获。"""
    gate = _gate()
    mutation = _mutation(TARGET_MJS, runner="node", key="hook_probe")
    hook = _suite(
        _case(name="ok", kind_failure="hookFailed", message="after boom"),
        tests=1,
        failures=1,
        errors=0,
    )
    runner = _Runner(
        {"junit-node-baseline.xml": [CLEAN_MJS], "junit-node-run.xml": [hook]},
        returncodes=[0, 1],
    )
    _patch_gate(monkeypatch, gate, runner, tmp_path / "scratch")

    status, _elapsed, detail = gate._run_targets(mutation)

    assert status.startswith("ERROR"), f"after hook 失败必须 ERROR，实际 {status}"
    assert "钩子" in detail or "hook" in detail.lower(), f"详情没说清是钩子：{detail}"


def test_empty_node_target_fails_baseline(monkeypatch, tmp_path) -> None:
    """空 mjs 被 node 记 1 pass：基线必须 ERROR，不得注入。"""
    gate = _gate()
    empty = _suite(_case(name="probe_empty.mjs"), tests=1, failures=0, errors=0)
    runner = _Runner({"junit-node-baseline.xml": [empty]}, returncodes=[0, 1])
    _patch_gate(monkeypatch, gate, runner, tmp_path / "scratch")

    status, _elapsed, detail = gate._run_targets(
        _mutation(TARGET_MJS, runner="node", key="empty_probe")
    )

    assert status.startswith("ERROR"), f"空 mjs 基线必须 ERROR，实际 {status}"
    assert len(runner.calls) == 1, "基线未通过时不得跑变异"


def test_missing_report_is_error(monkeypatch, tmp_path) -> None:
    """runner 崩掉、报告缺失/不可解析 → ERROR，不按退出码猜。"""
    gate = _gate()
    runner = _Runner({"junit-baseline.xml": [CLEAN_PY]}, returncodes=[0, 1])
    _patch_gate(monkeypatch, gate, runner, tmp_path / "scratch")
    monkeypatch.setattr(gate, "_read_report", lambda _path: None)
    monkeypatch.setattr(gate, "_ensure_baseline", lambda *_a, **_k: None)

    status, _elapsed, detail = gate._run_targets(
        _mutation(TARGET_PY, runner="pytest", key="report")
    )

    assert status.startswith("ERROR"), f"无报告必须 ERROR，实际 {status}"
    assert "报告" in detail or "report" in detail.lower()


def test_timeout_without_report_is_still_timeout(tmp_path) -> None:
    """目标进程超时且尚未写报告：TIMEOUT 优先于 no-report。"""
    gate = _gate()
    mutation = _mutation(TARGET_PY, runner="pytest", key="timeout_probe")

    status, _elapsed, detail = gate._finalize(
        mutation,
        rc=None,
        elapsed=1.0,
        detail="目标测试超时",
        report=tmp_path / "missing.xml",
    )

    assert status == "TIMEOUT", f"超时必须优先判 TIMEOUT，实际 {status}"
    assert "超时" in detail


def test_unparsable_report_is_error(monkeypatch, tmp_path) -> None:
    gate = _gate()
    mutation = _mutation(TARGET_PY, runner="pytest", key="unparsable")
    runner = _Runner({"junit-baseline.xml": [CLEAN_PY]}, returncodes=[0, 1])
    _patch_gate(monkeypatch, gate, runner, tmp_path / "scratch")
    monkeypatch.setattr(gate, "_ensure_baseline", lambda *_a, **_k: None)
    original = gate.ET.fromstring if hasattr(gate, "ET") else None
    assert original is not None

    def boom(_text):
        raise gate.ET.ParseError("not xml")

    monkeypatch.setattr(gate.ET, "fromstring", boom)

    status, _elapsed, detail = gate._run_targets(mutation)

    assert status.startswith("ERROR"), f"不可解析报告必须 ERROR，实际 {status}"
    assert "报告" in detail or "解析" in detail


def test_scratch_root_is_dedicated_and_cleaned(monkeypatch, tmp_path) -> None:
    """专属根：不在仓库内、不硬编码盘符；运行结束时 finally 清掉自有报告与临时根。"""
    gate = _gate()
    root = gate._scratch_root()
    assert root.is_absolute()
    assert not root.is_relative_to(ROOT), "临时根不得落在仓库内"
    assert "astrbot_plugin" not in str(root)

    mutation = _mutation(TARGET_PY, runner="pytest", key="cleanup_probe")
    runner = _Runner(
        {"junit-baseline.xml": [CLEAN_PY], "junit-run.xml": [FAILING_PY]}, returncodes=[0, 1]
    )
    _patch_gate(monkeypatch, gate, runner, tmp_path / "unused")
    monkeypatch.setattr(gate, "_scratch_root", lambda: root)

    try:
        status, _elapsed, _detail = gate._run_targets(mutation)
        assert status == "CAUGHT"
    finally:
        gate._cleanup_scratch(root)
    assert not root.exists(), "finally 必须清掉自有报告与临时根"
    for key in ("junit-baseline.xml", "junit-run.xml"):
        assert runner.reports[key] == [], f"{key} 未被消费（报告没落到专属根里）"


def test_missing_node_target_is_fail_closed(monkeypatch) -> None:
    """目标缺失必须落 ERROR 且不启动测试进程（前置检查失败即返回）。"""
    gate = _gate()
    missing = "tests/does_not_exist_mutation_probe.mjs"
    assert not (ROOT / missing).exists(), "探针目标不该存在"
    monkeypatch.setattr(gate, "_ensure_baseline", lambda *_a, **_k: None)
    calls: list[list[str]] = []

    def run(argv, **_kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(gate, "subprocess", SimpleNamespace(run=run))

    status, _elapsed, detail = gate._run_targets(_mutation(missing))

    assert status.startswith("ERROR"), f"目标缺失被判为 {status}（假绿）"
    assert missing in detail
    assert not calls, "目标缺失时不该启动测试进程"


def test_present_target_still_reaches_the_runner(monkeypatch, tmp_path) -> None:
    """守卫不得过宽：目标存在时必须真的跑，且退出码不再单独决定状态。"""
    gate = _gate()
    runner = _Runner(
        {"junit-baseline.xml": [CLEAN_PY, FAILING_PY], "junit-run.xml": [FAILING_PY]},
        returncodes=[0, 1],
    )
    _patch_gate(monkeypatch, gate, runner, tmp_path / "scratch")

    status, _elapsed, _detail = gate._run_targets(
        _mutation(TARGET_PY, runner="pytest", key="runner_probe")
    )

    assert status == "CAUGHT"
    assert len(runner.calls) == 2, "目标存在时必须真的跑（基线 + 变异）"
    for argv in runner.argv_lists:
        assert arg_value(argv, "--junitxml").endswith(".xml")
        assert "--basetemp" in " ".join(argv), "pytest 必须用显式 basetemp"
        assert "-p" in argv and "no:cacheprovider" in argv, "缓存 provider 仍须关闭"


def test_command_exposes_dedicated_paths_and_env(tmp_path) -> None:
    """不得借 PYTEST_ADDOPTS / PYTEST_DEBUG_TEMPROOT 控制 pytest；TEMP 专属且唯一。"""
    gate = _gate()
    mutation = _mutation(TARGET_PY, runner="pytest", key="argv_probe")
    tmp_root = tmp_path / "run1"
    argv = gate._command(mutation, tmp_root=tmp_root, report=tmp_path / "r.xml", phase="run")
    env = gate._run_env(tmp_root)

    basetemp = Path(arg_value(argv, "--basetemp"))
    assert Path(arg_value(argv, "--junitxml")).is_absolute()
    assert basetemp.is_absolute() and basetemp.is_dir(), "basetemp 必须先 mkdir"
    assert basetemp.is_relative_to(tmp_root), "basetemp 必须落在本次专属根下"
    assert "--rootdir" not in argv and "--cwd" not in argv
    assert "PYTEST_ADDOPTS" not in env
    assert "PYTEST_DEBUG_TEMPROOT" not in env
    assert env["TEMP"] == env["TMP"] == str(tmp_root)


def test_node_report_lives_in_scratch_root(tmp_path) -> None:
    """node 的 junit 报告也必须落在该次专属根里。"""
    gate = _gate()
    mutation = _mutation(TARGET_MJS, runner="node", key="node_report")
    scratch = tmp_path / "scratch"
    baseline_argv, baseline_report = gate._node_invocation(mutation, scratch, "baseline")
    argv, report = gate._node_invocation(mutation, scratch, "run")

    assert report.is_relative_to(scratch)
    assert f"--test-reporter-destination={report}" in argv
    assert report != baseline_report, "基线与运行的报告路径必须分开，否则缓存会互相覆盖"
    assert baseline_report.is_relative_to(scratch)
    assert f"--test-reporter-destination={baseline_report}" in baseline_argv
