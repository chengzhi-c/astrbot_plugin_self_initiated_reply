"""gates.py 的 pytest 临时根契约：本轮专属、显式 basetemp、必清理。

为什么值得单独立文件：``scripts/gates.py`` 是本地一键门禁，pytest 那一步此前
沿用 shell 继承的 ``TEMP``/``TMP``。宿主 AstrBot 也是常驻进程，同一临时根下
两个 Python 进程各自建 ``pytest-of-*`` 时，Windows 上会撞 "目录名已存在"；
pytest 随后只会退化成 warning 继续跑，一键门禁看起来"过了"，其实是从别人
的临时目录里捡数据。因此这里的判据全部落在**本进程可观测的事实**上：

1. env 里 ``TEMP``/``TMP`` 指向同一专属根，``--basetemp`` 是其下唯一子目录
   且在子进程启动前就已 ``mkdir``（不是靠 pytest 的 rootdir 清理 warning）；
2. 外部遗留的 ``PYTEST_ADDOPTS`` / ``PYTEST_DEBUG_TEMPROOT`` 不得继承，
   它们会让显式 basetemp 与“保留临时根便于排查”的行为互相打架；
3. 整轮临时根在成功、pytest 失败、预清理抛 ``OSError``（fail closed，直接
   不启动）时都不得泄漏；清理失败也不得把原异常掩盖掉。
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from .host_stubs import ROOT, arg_value


def _gate():
    return importlib.import_module("scripts.gates")


class _Runner:
    """subprocess.run 桩：记录每次调用，按调用序号吐预置退出码。"""

    def __init__(self, returncodes: list[int] | None = None):
        self.returncodes = list(returncodes or [0])
        self.calls: list[SimpleNamespace] = []

    def __call__(self, argv, **kwargs):
        rc = self.returncodes.pop(0) if self.returncodes else 0
        basetemp = arg_value(argv, "--basetemp")
        # 子进程启动这一刻 basetemp 必须已存在：finally 里的清理发生在调用返回
        # 之后，所以此刻取到的是「门禁是否先 mkdir」的铁证。
        self.calls.append(
            SimpleNamespace(
                argv=list(argv),
                cwd=kwargs.get("cwd"),
                env=dict(kwargs.get("env") or {}),
                basetemp_existed=Path(basetemp).is_dir(),
            )
        )
        return SimpleNamespace(returncode=rc, stdout="", stderr="")


def _patch(monkeypatch, gate, runner: _Runner, scratch: Path) -> None:
    monkeypatch.setattr(gate, "subprocess", SimpleNamespace(run=runner))
    monkeypatch.setattr(gate, "_scratch_root", lambda: scratch)
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    monkeypatch.delenv("PYTEST_DEBUG_TEMPROOT", raising=False)


def _pytest_call(runner: _Runner) -> SimpleNamespace:
    """唯一一次 pytest 调用（本文件只测这一个子进程）。"""
    assert len(runner.calls) == 1, f"应只启动一个子进程，实际 {len(runner.calls)}"
    return runner.calls[0]


def test_pytest_invocation_uses_dedicated_basetemp(monkeypatch, tmp_path) -> None:
    """pytest 显式绑定本轮专属、唯一且已 mkdir 的 basetemp，落在系统临时根下。"""
    gate = _gate()
    scratch = tmp_path / "scratch-root"
    runner = _Runner([0])
    _patch(monkeypatch, gate, runner, scratch)

    gate._run_pytest()

    call = _pytest_call(runner)
    basetemp = Path(arg_value(call.argv, "--basetemp"))
    assert call.basetemp_existed, "basetemp 必须由门禁先 mkdir，而不是交给 pytest"
    assert basetemp.is_relative_to(scratch), "basetemp 必须落在本轮专属临时根下"
    assert basetemp != scratch, "basetemp 不得就是临时根本身（清理会连同其它产物一起走）"
    assert not basetemp.is_relative_to(ROOT), "临时根不得落在仓库内"
    assert "astrbot_plugin" not in str(basetemp), "临时根不得按仓库名硬编码"
    assert call.argv[:3] == [sys.executable, "-m", "pytest"], "其余 pytest 参数保持不变"


def test_pytest_env_points_temp_and_tmp_to_same_root(monkeypatch, tmp_path) -> None:
    """TEMP 与 TMP 同源，且外部遗留的两种 pytest 变量不得继承。"""
    gate = _gate()
    scratch = tmp_path / "scratch-root"
    runner = _Runner([0])
    _patch(monkeypatch, gate, runner, scratch)
    monkeypatch.setenv("TEMP", "E:\\foreign-temp")
    monkeypatch.setenv("TMP", "E:\\foreign-temp")
    monkeypatch.setenv("PYTEST_ADDOPTS", "-p no:randomly")
    monkeypatch.setenv("PYTEST_DEBUG_TEMPROOT", "1")

    gate._run_pytest()

    call = _pytest_call(runner)
    assert call.env["TEMP"] == call.env["TMP"], "TEMP/TMP 必须指向同一专属根"
    assert Path(call.env["TEMP"]).is_absolute()
    assert Path(call.env["TEMP"]).is_relative_to(scratch), "TEMP/TMP 不得指向外部临时根"
    assert "PYTEST_ADDOPTS" not in call.env
    assert "PYTEST_DEBUG_TEMPROOT" not in call.env
    assert call.cwd == ROOT, "子进程 cwd 仍是仓库根"


def test_scratch_root_is_cleaned_after_success(monkeypatch, tmp_path) -> None:
    """成功结束时整轮临时根必须清掉，不留 pytest 的 rootdir 残骸。"""
    gate = _gate()
    scratch = tmp_path / "scratch-root"
    runner = _Runner([0])
    _patch(monkeypatch, gate, runner, scratch)

    gate._run_pytest()

    assert not scratch.exists(), f"成功路径未清理专属临时根：{scratch}"


def test_scratch_root_is_cleaned_after_pytest_failure(monkeypatch, tmp_path) -> None:
    """pytest 失败（SystemExit）同样清理：失败现场由 stderr 保留，不靠临时根。"""
    gate = _gate()
    scratch = tmp_path / "scratch-root"
    runner = _Runner([1])
    _patch(monkeypatch, gate, runner, scratch)

    with pytest.raises(SystemExit) as excinfo:
        gate._run_pytest()

    assert excinfo.value.code == 1
    assert not scratch.exists(), f"失败路径未清理专属临时根：{scratch}"


def test_pre_clean_failure_blocks_startup(monkeypatch, tmp_path) -> None:
    """basetemp 预清理抛 PermissionError → fail closed，且不启动 pytest。

    Windows 上删不掉 basetemp 通常是残留句柄（查看器、索引器、杀软扫描）。
    此时若照常启动，pytest 只会 warning 后继续用别人留下的数据，等于把
    「不知道」当通过，必须先清干净或明确失败。
    """
    gate = _gate()
    scratch = tmp_path / "scratch-root"
    runner = _Runner([0])
    _patch(monkeypatch, gate, runner, scratch)
    occupied = scratch / "basetemp" / "pinned"
    occupied.mkdir(parents=True)
    occupied.joinpath("keep.txt").write_text("pinned", encoding="utf-8")
    monkeypatch.setattr(
        gate,
        "shutil",
        SimpleNamespace(rmtree=lambda *_a, **_k: _raise_permission_error(), which=None),
    )

    with pytest.raises(PermissionError):
        gate._run_pytest()

    assert not runner.calls, "预清理失败时不得启动 pytest 子进程"


def test_pre_clean_failure_reports_real_reason(monkeypatch, tmp_path) -> None:
    """预清理失败必须是真实的 PermissionError，不得被吞成无害的 FileExistsError。

    ``shutil.rmtree(..., ignore_errors=True)`` 会把删不掉的原因吞掉，让后面的
    ``mkdir`` 抛 ``FileExistsError``，读起来像"目录已存在"这种无害事，实际是
    本轮临时数据不干净。判据必须是**异常类型**，不能只看"抛了没有"。这里让
    ``os.unlink`` 报 PermissionError（等价于 Windows 上目录被占用）。
    """
    gate = _gate()
    scratch = tmp_path / "scratch-root"
    runner = _Runner([0])
    _patch(monkeypatch, gate, runner, scratch)
    (scratch / "basetemp").mkdir(parents=True)
    (scratch / "basetemp" / "pinned.txt").write_text("pinned", encoding="utf-8")

    def occupied_unlink(path, *args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(gate.os, "unlink", occupied_unlink)

    with pytest.raises(PermissionError):
        gate._run_pytest()

    assert not runner.calls, "预清理失败时不得启动 pytest 子进程"


def test_cleanup_failure_does_not_mask_the_original_error(monkeypatch, tmp_path) -> None:
    """pytest 失败后清理又失败：原退出码仍要冒出去，清理失败另走一处可见输出。"""
    gate = _gate()
    scratch = tmp_path / "scratch-root"
    runner = _Runner([1])
    _patch(monkeypatch, gate, runner, scratch)
    monkeypatch.setattr(
        gate,
        "shutil",
        SimpleNamespace(rmtree=_permission_erroring_rmtree, which=None),
    )

    with pytest.raises(SystemExit) as excinfo:
        gate._run_pytest()

    assert excinfo.value.code == 1, "清理失败不得改写 pytest 的失败退出码"
    assert "pytest" not in str(excinfo.value), "原异常不得被清理失败掩盖"


def test_cleanup_removes_the_basetemp_it_declared(monkeypatch, tmp_path) -> None:
    """basetemp 一经 mkdir 就在整轮结束时清掉，不留给下一次或别的进程。"""
    gate = _gate()
    scratch = tmp_path / "scratch-root"
    runner = _Runner([0])
    _patch(monkeypatch, gate, runner, scratch)

    gate._run_pytest()

    assert runner.calls, "前置条件：pytest 子进程确实启动过"
    assert runner.calls[0].basetemp_existed, "前置条件：门禁先 mkdir 了 basetemp"
    assert not scratch.exists(), "临时根与 basetemp 必须一起清掉"


def _raise_permission_error():
    raise PermissionError(13, "Permission denied")


def _permission_erroring_rmtree(*_args, **_kwargs):
    raise PermissionError(13, "Permission denied")


# --------------------------------------------------------------------------
# 真子进程：桩测不出「环境变量真的被改写」与「pytest 真的用了专属根」
# --------------------------------------------------------------------------


def test_real_pytest_child_gets_clean_env(monkeypatch, tmp_path):
    """真 pytest 子进程：写脏的 TEMP/TMP 与遗留 PYTEST_* 下，basetemp 仍是显式那个。"""
    gate = _gate()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    scratch = tmp_path / "scratch-root"
    probe = _write_probe(tmp_path / "probe-src")
    observed: dict[str, Path] = {}

    real_run = subprocess.run
    seen: list[SimpleNamespace] = []

    def spy(argv, **kwargs):
        observed["during"] = Path(arg_value(argv, "--basetemp"))
        result = real_run(argv, **kwargs)
        seen.append(SimpleNamespace(argv=list(argv), kwargs=dict(kwargs)))
        # 真子进程刚返回：finally 里的清理还没发生，此刻 basetemp 一定在盘上。
        numbered = [child for child in observed["during"].iterdir() if child.name != "current"]
        observed["after"] = numbered[0] if numbered else scratch
        return result

    monkeypatch.setattr(gate, "subprocess", SimpleNamespace(run=spy))
    monkeypatch.setattr(gate, "_scratch_root", lambda: scratch)
    # 只跑最小探针目录：本条验的是 env/basetemp 事实，不是全量用例。
    monkeypatch.setattr(gate, "PYTEST_ARGS", ["-q", str(probe)])
    monkeypatch.setenv("TEMP", str(foreign))
    monkeypatch.setenv("TMP", str(foreign))
    monkeypatch.setenv("PYTEST_ADDOPTS", "-p no:cacheprovider")
    monkeypatch.setenv("PYTEST_DEBUG_TEMPROOT", "1")

    gate._run_pytest()

    assert len(seen) == 1, "只应启动一个 pytest 子进程"
    env = seen[0].kwargs["env"]
    assert env["TEMP"] == env["TMP"], "TEMP/TMP 必须指向同一专属根"
    assert Path(env["TEMP"]).is_relative_to(scratch), "TEMP/TMP 不得沿用外部临时根"
    assert "PYTEST_ADDOPTS" not in env and "PYTEST_DEBUG_TEMPROOT" not in env
    assert seen[0].kwargs.get("cwd") == ROOT
    # 子进程侧的事实：给 tmp_path 建的 numbered 目录必须落在显式 basetemp 下，
    # 外部写脏的 TEMP/TMP 与 PYTEST_ADDOPTS 都没有把它掰走。
    assert observed["after"].is_relative_to(observed["during"]), (
        f"pytest 未把本轮 rootdir 建在专属 basetemp 下：{observed}"
    )
    assert not scratch.exists(), "真 pytest 跑完也必须清理专属临时根"


def _write_probe(root: Path) -> Path:
    """落一个最小 tests 目录：conftest 请求一次 tmp_path，逼 pytest 建 basetemp。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "test_probe.py").write_text(
        "def test_probe(tmp_path):\n    assert tmp_path.is_dir()\n", encoding="utf-8"
    )
    return root


def test_cleanup_keeps_other_scratch_contents(monkeypatch, tmp_path):
    """整轮临时根最终整体清理；中间只删本轮 basetemp，别的内容不被提前动。"""
    gate = _gate()
    scratch = tmp_path / "scratch-root"
    keep = None

    def fake_run(argv, **kwargs):
        nonlocal keep
        Path(arg_value(argv, "--basetemp")).mkdir(parents=True, exist_ok=True)
        keep = scratch / "keep.txt"
        keep.parent.mkdir(parents=True, exist_ok=True)
        keep.write_text("keep", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(gate, "subprocess", SimpleNamespace(run=fake_run))
    monkeypatch.setattr(gate, "_scratch_root", lambda: scratch)

    gate._run_pytest()

    assert keep is not None, "setup 的 keep 文件未建立，用例空转"
    assert not scratch.exists(), "整轮临时根最终必须整体清理"
