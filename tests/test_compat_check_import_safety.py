"""compat_check 必须可被 import，且自有的临时目录生命周期必须闭合。

脚本本体在模块级做进程级副作用（sys.path 注入 / chdir 临时目录 / 注册假包），
而 tests/test_runtime_adapter.py 为取 EXPECTED_HANDLER_COUNT 而 import 它，
副作用会改掉 pytest 进程的 cwd、并在宿主真包已装时用假包顶掉 sys.modules 里的
同名条目。副作用因此收敛进 _bootstrap()，只由 __main__ 入口调用。

自有临时目录（``_bootstrap`` 的 chdir 目标）同样由入口拥有：异常路径上
**先恢复 cwd 再 rmtree**（Windows 上顺序反了会 PermissionError）。检查失败
（抛错或非零退出码）与 _bootstrap 自身抛错的路径在此实测；成功路径的清理
由 ``test_main_preserves_preexisting_*`` 两条用例顺带覆盖，不单独设例。
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]

PKG_NAME = "astrbot_plugin_self_initiated_reply"


def test_importing_compat_check_keeps_process_state_intact() -> None:
    """import 后 cwd 不变、sys.modules 不出现假包、sys.path 未被改写。"""
    probe = """
import sys
from pathlib import Path

before_cwd = Path.cwd()
before_path = list(sys.path)
before_module = sys.modules.get("astrbot_plugin_self_initiated_reply")

import scripts.compat_check  # noqa: F401

assert Path.cwd() == before_cwd, f"cwd changed to {Path.cwd()}"
assert sys.path == before_path, "sys.path mutated at import time"
after_module = sys.modules.get("astrbot_plugin_self_initiated_reply")
assert after_module is before_module, "fake package injected into sys.modules at import time"
print(scripts.compat_check.EXPECTED_HANDLER_COUNT)
"""
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", probe],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip().splitlines()[-1].isdigit(), proc.stdout


def test_importing_compat_check_does_not_chdir_even_once_loaded() -> None:
    """同一进程内二次 import（已缓存）同样不留副作用。"""
    before = Path.cwd()
    module = importlib.import_module("scripts.compat_check")
    assert Path.cwd() == before
    assert isinstance(module.EXPECTED_HANDLER_COUNT, int)


# --- 自有临时目录生命周期 ------------------------------------------------------


def _no_plugin_package(monkeypatch: pytest.MonkeyPatch) -> None:
    """让 ``_bootstrap`` 走假包注册分支（无论本机是否装了真包）。"""
    monkeypatch.setitem(sys.modules, PKG_NAME, None)


@pytest.fixture
def compat(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """加载 ``scripts.compat_check`` 本体（替身注入点就在它上面）。

    不用模块副本：本文件的替身只替换函数级绑定（``os.chdir`` / ``tempfile``），
    monkeypatch 在用例结束时逐项复原，不会串到别的测试文件。
    """
    return importlib.import_module("scripts.compat_check")


class _RecordingTempfile:
    """真建目录的 tempfile 替身：记录每次 ``mkdtemp`` 的产物路径。

    不伪造返回值，目录必须真实存在，``shutil.rmtree`` 才有可删的东西，
    Windows 上「cwd 已切走」也才有可验证的语义。
    """

    def __init__(self) -> None:
        self.created: list[Path] = []

    def mkdtemp(self, prefix: str) -> str:
        path = Path(tempfile.mkdtemp(prefix=prefix))
        self.created.append(path)
        return str(path)


def _instrumented_tempfile(monkeypatch: pytest.MonkeyPatch, compat: ModuleType) -> list[Path]:
    """替换模块内的 tempfile 绑定，返回创建记录（用例结束复原）。"""
    recording = _RecordingTempfile()
    monkeypatch.setattr(compat, "tempfile", recording)
    return recording.created


def test_bootstrap_failure_restores_cwd_and_removes_temp_dir(
    monkeypatch: pytest.MonkeyPatch,
    compat: ModuleType,
) -> None:
    """_bootstrap 准备步骤失败：cwd 复原、目录删掉、sys.path 不留副作用。

    以假包注册那步（最后一步）失败为例：目录已建、cwd 已切、sys.path 已注入，
    三项都必须回滚，否则异常会把 pytest 进程留在临时目录里，后续测试写文件
    全落到 /tmp，且真实宿主机上真包被假包顶掉。
    """
    _no_plugin_package(monkeypatch)
    created = _instrumented_tempfile(monkeypatch, compat)
    before_cwd = Path.cwd()
    before_path = list(sys.path)
    # 假包条目：bootstrap 收尾要删掉自己注册的那个（现场本来没有该条目）
    monkeypatch.delitem(sys.modules, PKG_NAME, raising=False)
    before_modules = dict(sys.modules)

    def _boom() -> None:
        raise RuntimeError("simulated bootstrap failure")

    monkeypatch.setattr(compat, "_register_plugin_package", _boom)
    with pytest.raises(RuntimeError, match="simulated bootstrap failure"):
        compat._bootstrap()

    assert len(created) == 1, f"预期自建一个临时目录，实际：{created}"
    workdir = created[0]
    assert not workdir.exists(), "bootstrap 失败后自有临时目录未删除"
    assert Path.cwd() == before_cwd, f"cwd 未复原：{Path.cwd()} != {before_cwd}"
    assert sys.path == before_path, "bootstrap 失败后 sys.path 未复原"
    assert dict(sys.modules) == before_modules, "bootstrap 改写了 sys.modules"


def test_bootstrap_creates_real_directory_and_chdirs(
    monkeypatch: pytest.MonkeyPatch,
    compat: ModuleType,
) -> None:
    """反向守卫：bootstrap 真的建了目录并切进去（否则上面的删除断言是空转）。

    替身不得伪造返回值，目录必须真实存在于磁盘上，Windows 上「cwd 在临时
    目录里」才有可验证的语义。
    """
    _no_plugin_package(monkeypatch)
    created = _instrumented_tempfile(monkeypatch, compat)
    before_cwd = Path.cwd()

    state = compat._bootstrap()
    workdir = state.workdir

    assert len(created) == 1, f"预期自建一个临时目录，实际：{created}"
    assert workdir == created[0]
    assert workdir.is_dir(), "bootstrap 必须在磁盘上真实建出目录"
    assert Path.cwd() == workdir, f"cwd 未切到自有临时目录：{Path.cwd()}"
    assert str(workdir).startswith(str(Path(tempfile.gettempdir())))
    # 收尾自己来：调用方才拥有生命周期
    compat._restore_process_state(before_cwd, state)
    compat._discard_workdir(workdir)
    assert not workdir.exists()
    assert Path.cwd() == before_cwd


def test_main_preserves_preexisting_plugin_package(
    monkeypatch: pytest.MonkeyPatch,
    compat: ModuleType,
) -> None:
    """真实插件包若调用前已在 sys.modules，入口不得把它当自有假包删除。"""
    created = _instrumented_tempfile(monkeypatch, compat)
    before_cwd = Path.cwd()
    before_path = list(sys.path)
    preexisting = ModuleType(PKG_NAME)
    monkeypatch.setitem(sys.modules, PKG_NAME, preexisting)
    monkeypatch.setattr(compat, "run_contract_checks", lambda: 0)

    assert compat.main() == 0
    assert not created[0].exists()
    assert Path.cwd() == before_cwd
    assert sys.path == before_path
    assert sys.modules[PKG_NAME] is preexisting, "入口删除了调用前已经存在的插件包"


def test_main_preserves_preexisting_repo_path(
    monkeypatch: pytest.MonkeyPatch,
    compat: ModuleType,
) -> None:
    """仓库根若调用前已在 sys.path，入口不得删掉调用方原有条目。"""
    created = _instrumented_tempfile(monkeypatch, compat)
    before_cwd = Path.cwd()
    root = str(ROOT)
    monkeypatch.setattr(sys, "path", [root, *(item for item in sys.path if item != root)])
    before_path = list(sys.path)
    monkeypatch.setitem(sys.modules, PKG_NAME, ModuleType(PKG_NAME))
    monkeypatch.setattr(compat, "run_contract_checks", lambda: 0)

    assert compat.main() == 0
    assert not created[0].exists()
    assert Path.cwd() == before_cwd
    assert sys.path == before_path, "入口删除了调用前已经存在的仓库路径"


@pytest.mark.parametrize(
    "stub_kind",
    ["raises", "exit_code_1"],
    ids=["contract-check-raises", "contract-check-exit-1"],
)
def test_main_cleans_up_when_checks_do_not_succeed(
    monkeypatch: pytest.MonkeyPatch,
    compat: ModuleType,
    stub_kind: str,
) -> None:
    """run_contract_checks 抛错或返回非零：清理照常，异常与退出码不被吞。

    两条失败路径触发的是同一段收尾代码，参数化钉住「不能只在成功路径收尾」。
    """
    _no_plugin_package(monkeypatch)
    created = _instrumented_tempfile(monkeypatch, compat)
    before_cwd = Path.cwd()
    before_path = list(sys.path)

    if stub_kind == "raises":

        def _boom() -> int:
            raise ValueError("simulated contract check failure")

        monkeypatch.setattr(compat, "run_contract_checks", _boom)
        with pytest.raises(ValueError, match="simulated contract check failure"):
            compat.main()
    else:
        monkeypatch.setattr(compat, "run_contract_checks", lambda: 1)
        assert compat.main() == 1

    assert len(created) == 1, f"预期自建一个临时目录，实际：{created}"
    workdir = created[0]
    assert not workdir.exists(), "检查失败路径未删除自有临时目录"
    assert Path.cwd() == before_cwd, f"cwd 未复原：{Path.cwd()} != {before_cwd}"
    assert sys.path == before_path, "失败路径后 sys.path 未复原"
