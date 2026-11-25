"""compat_check 必须可被 import，且自有的临时目录生命周期必须闭合。

脚本本体在模块级做进程级副作用（sys.path 注入 / chdir 临时目录 / 注册假包），
而 tests/test_runtime_adapter.py 为取 EXPECTED_HANDLER_NAMES 而 import 它，
副作用会改掉 pytest 进程的 cwd、并在宿主真包已装时用假包顶掉 sys.modules 里的
同名条目。副作用因此收敛进 _bootstrap()，只由 __main__ 入口调用。

自有临时目录（``_bootstrap`` 的 chdir 目标）同样由入口拥有：异常路径上
**先恢复 cwd 再 rmtree**（Windows 上顺序反了会 PermissionError）。检查失败
（抛错或非零退出码）与 _bootstrap 自身抛错的路径均需覆盖；成功路径的清理
由 ``test_main_preserves_preexisting_*`` 两条用例顺带覆盖，不单独设例。
"""

from __future__ import annotations

import importlib
import json
import os
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
print(len(scripts.compat_check.EXPECTED_HANDLER_NAMES))
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


# --- 自有临时目录生命周期 ------------------------------------------------------


def _no_plugin_package(monkeypatch: pytest.MonkeyPatch) -> None:
    """让 ``_bootstrap`` 走假包注册分支（无论是否装了真包）。"""
    monkeypatch.setitem(sys.modules, PKG_NAME, None)


@pytest.fixture(autouse=True)
def _host_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """把宿主前置检查钉成"已安装"。

    本文件测的是 bootstrap 与清理生命周期，与真实 ``astrbot`` 在不在无关；而
    ``main()`` 的前置检查缺宿主时直接返回 ``EXIT_HOST_MISSING``，根本不进
    bootstrap。不钉这一下，本文件在未装宿主的机器上单独跑就会红在退出码上，
    而不是红在被测的生命周期断言上。
    """
    compat = importlib.import_module("scripts.compat_check")
    monkeypatch.setattr(compat, "_host_importable", lambda: True)


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


def test_main_reports_missing_host_before_bootstrapping(
    monkeypatch: pytest.MonkeyPatch,
    compat: ModuleType,
    capsys: pytest.CaptureFixture,
) -> None:
    """缺宿主：一行可行动提示 + 专用退出码，且不建临时目录、不切 cwd。

    没有前置检查时，缺宿主以 ``runtime_adapter`` 的 ``ModuleNotFoundError``
    traceback 结束，调用方看不出该去装 astrbot；退出码也与契约缺口的 1 混在
    一起，分不清"环境没装"和"上游漂移"。
    """
    created = _instrumented_tempfile(monkeypatch, compat)
    monkeypatch.setattr(compat, "_host_importable", lambda: False)
    before_cwd = Path.cwd()
    before_path = list(sys.path)

    assert compat.main() == compat.EXIT_HOST_MISSING
    assert compat.EXIT_HOST_MISSING != 1, "缺宿主必须与契约缺口用不同退出码"

    assert not created, "缺宿主时不该建临时目录"
    assert Path.cwd() == before_cwd, f"cwd 未复原：{Path.cwd()} != {before_cwd}"
    assert sys.path == before_path, "失败路径后 sys.path 未复原"
    assert "pip install astrbot" in capsys.readouterr().err


def _stub_host_probes(
    monkeypatch: pytest.MonkeyPatch,
    compat: ModuleType,
    *,
    absent: tuple[str, ...] = (),
    names: dict[str, set[str]] | None = None,
    unclassified: tuple[str, ...] = (),
    failed: tuple[str, ...] = (),
    denylist: frozenset[str] = frozenset(),
) -> None:
    """把两条宿主探针钉成真实形状：``DANGEROUS_TOOL_MODULES`` 每一条都在，可指定缺的。

    不用手挑两三条模块的桩：真实宿主是清单里十几条缺一条，而「全缺才报」的实现
    恰好只在这种数量下暴露——桩太窄时同一份桩换成任何实现都能过，用例失去判别力。
    """
    enumerated: dict[str, set[str] | None] = {
        mod: (None if mod in absent else set()) for mod in compat.DANGEROUS_TOOL_MODULES
    }
    enumerated.update(names or {})
    monkeypatch.setattr(compat, "_enumerate_tool_names", lambda: enumerated)
    known = (
        set(compat.DANGEROUS_TOOL_MODULES)
        | set(compat.SAFE_TOOL_MODULES)
        | set(compat.NON_TOOL_MODULES)
    )
    found = sorted(known | set(unclassified))
    monkeypatch.setattr(compat, "_walk_tool_package", lambda: (found, list(failed)))
    # _denylist_gaps 从插件包取 denylist：宿主探针被替身后仍须 import 成功。
    fake_models = ModuleType("astrbot_plugin_self_initiated_reply.models")
    fake_models.HOST_DANGEROUS_TOOL_IDS = denylist
    monkeypatch.setitem(sys.modules, "astrbot_plugin_self_initiated_reply.models", fake_models)


def test_denylist_gaps_reports_single_missing_dangerous_module(
    monkeypatch: pytest.MonkeyPatch,
    compat: ModuleType,
) -> None:
    """真实形状的模块清单里缺一条（改名/搬迁）：报那一条，不许整片静默。

    「全缺才报」的判据在这里天然成立不了（缺 1 ≠ 缺全部），未守卫时 gaps 为
    空列表，危险模块改名后 denylist 少一片覆盖面而无人知道。
    """
    moved = "astrbot.core.tools.computer_tools.shell"
    _stub_host_probes(monkeypatch, compat, absent=(moved,))

    assert compat._denylist_gaps() == [f"{moved} 缺失，宿主私有 API 漂移"]


def test_denylist_gaps_reports_whole_computer_tools_subtree_missing(
    monkeypatch: pytest.MonkeyPatch,
    compat: ModuleType,
) -> None:
    """整片 ``computer_tools`` 都取不到：逐条报，白名单里的 cua 除外。

    ``computer_tools/__init__`` 顶层 import 各危险模块，改名一个子模块会让整片
    目录 import 失败；此时所有子模块都是"缺失"，而 cua 是旧宿主没有的版本差，
    只有它被白名单容忍。
    """
    subtree = tuple(
        mod
        for mod in compat.DANGEROUS_TOOL_MODULES
        if mod.startswith("astrbot.core.tools.computer_tools.")
    )
    _stub_host_probes(monkeypatch, compat, absent=subtree)

    expected = [
        f"{mod} 缺失，宿主私有 API 漂移"
        for mod in subtree
        if mod not in compat.HOST_VERSION_OPTIONAL_TOOL_MODULES
    ]
    assert len(expected) > 1, "整片缺失应报出多条，单条会让用例回到「挑一两条」的老桩"
    assert compat._denylist_gaps() == expected


def test_denylist_gaps_tolerates_optional_module_absent_on_older_host(
    monkeypatch: pytest.MonkeyPatch,
    compat: ModuleType,
) -> None:
    """旧宿主没有较新版本才引入的危险模块：不算漂移。

    旧宿主没有 ``computer_tools.cua`` 与 ``neo_skills``，把它们报成缺失会让兼容
    门禁误报。白名单只登记这类版本差，且必须真的在危险清单里——写错名字的条目
    永远不会被查到，等于既没容忍也没报错。
    """
    assert set(compat.HOST_VERSION_OPTIONAL_TOOL_MODULES) <= set(compat.DANGEROUS_TOOL_MODULES)
    # 白名单是版本差的显式台账，不接受把改名后的模块塞进来糊掉缺失报告。
    assert compat.HOST_VERSION_OPTIONAL_TOOL_MODULES == {
        "astrbot.core.tools.computer_tools.cua",
        "astrbot.core.tools.computer_tools.shipyard_neo.neo_skills",
    }
    _stub_host_probes(monkeypatch, compat, absent=tuple(compat.HOST_VERSION_OPTIONAL_TOOL_MODULES))

    assert compat._denylist_gaps() == []


def test_denylist_gaps_still_checks_tool_names_of_optional_module(
    monkeypatch: pytest.MonkeyPatch,
    compat: ModuleType,
) -> None:
    """白名单只容忍"模块缺失"，模块存在时它的工具名仍须被 denylist 覆盖。

    容忍分支若写成模块级 continue（不区分 None 与工具名缺口），较新宿主上 cua
    新增的工具就会静默漏出最终防线。
    """
    cua = "astrbot.core.tools.computer_tools.cua"
    _stub_host_probes(monkeypatch, compat, names={cua: {"astrbot_cua_screenshot"}})

    assert compat._denylist_gaps() == [f"denylist 未覆盖 {cua}: astrbot_cua_screenshot"]


def test_denylist_gaps_covers_430_subagent_tools(
    monkeypatch: pytest.MonkeyPatch,
    compat: ModuleType,
) -> None:
    new_root = "astrbot.core.astr_agent_runtime.tools"
    names = {
        "spawn_agent",
        "list_agents",
        "get_agent_result",
        "wait_agent",
        "send_message",
        "cancel_agent",
    }
    monkeypatch.setattr(compat, "_active_tool_package", lambda: new_root)
    monkeypatch.setattr(
        compat,
        "_enumerate_tool_names",
        lambda: {f"{new_root}.subagent": names},
    )
    fake_models = ModuleType("astrbot_plugin_self_initiated_reply.models")
    fake_models.HOST_DANGEROUS_TOOL_IDS = names
    monkeypatch.setitem(sys.modules, "astrbot_plugin_self_initiated_reply.models", fake_models)

    assert compat._denylist_gaps() == []


def test_denylist_gaps_reports_unclassified_new_module(
    monkeypatch: pytest.MonkeyPatch,
    compat: ModuleType,
) -> None:
    """宿主新增未归类模块：报漂移，逼着每次升级对新增模块做一次显式归类。

    cua / neo_skills 就是这么漏掉的：枚举只看清单，不看包里多出了什么。
    """
    new = "astrbot.core.tools.computer_tools.brand_new"
    _stub_host_probes(monkeypatch, compat, unclassified=(new,))

    assert compat._denylist_gaps() == [
        f"{new} 未归类：新增到 DANGEROUS_TOOL_MODULES（并补 HOST_DANGEROUS_TOOL_IDS）"
        f"或 SAFE_TOOL_MODULES"
    ]


def test_tool_package_gaps_accepts_astrbot_430_runtime_tools(
    monkeypatch: pytest.MonkeyPatch,
    compat: ModuleType,
) -> None:
    """4.30 的危险工具包迁移后仍由兼容检查覆盖。"""
    new_root = "astrbot.core.astr_agent_runtime.tools"
    modules = [
        f"{new_root}.cron_tools",
        f"{new_root}.knowledge_base_tools",
        f"{new_root}.computer_tools.shell",
    ]
    monkeypatch.setattr(compat, "_active_tool_package", lambda: new_root)
    monkeypatch.setattr(compat, "_walk_tool_package", lambda: (modules, []))
    monkeypatch.setattr(
        compat,
        "_enumerate_tool_names",
        lambda: {module: set() for module in compat.DANGEROUS_TOOL_MODULES},
    )

    assert compat._tool_package_gaps() == []


def test_denylist_gaps_reports_tool_package_import_failure(
    monkeypatch: pytest.MonkeyPatch,
    compat: ModuleType,
) -> None:
    """子包遍历时 import 失败：把它报成漂移，而不是当成"包里没有这些模块"。"""
    broken = "astrbot.core.tools.computer_tools"
    _stub_host_probes(monkeypatch, compat, failed=(broken,))

    assert compat._denylist_gaps() == [f"{broken} 导入失败，其子树未纳入检查（宿主私有 API 漂移）"]


def test_tool_package_walk_reports_subpackage_import_failure(tmp_path: Path) -> None:
    """真实 ``pkgutil.walk_packages``：子包 import 抛错时整棵子树静默跳过。

    用假宿主复刻真实形状——``computer_tools/__init__`` 顶层 import 各危险模块，
    改名一个子模块就让整片目录 import 失败。不传 ``onerror`` 时遍历器既不报错也
    不产出该子树的任何模块名，"未归类"检查对该目录同时失效。
    """
    host = tmp_path / "host"
    files = {
        "__init__.py": "",
        "core/__init__.py": "",
        "core/tools/__init__.py": "",
        "core/tools/cron_tools.py": "",
        "core/tools/computer_tools/__init__.py": "raise ImportError('cua 已搬走')\n",
        "core/tools/computer_tools/cua.py": "",
    }
    for relative, body in files.items():
        target = host / "astrbot" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")

    probe = """
import json

import scripts.compat_check as compat

found, failed = compat._walk_tool_package()
print(json.dumps({"found": sorted(found), "failed": sorted(failed)}))
"""
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", probe],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONPATH": str(host)},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["failed"] == ["astrbot.core.tools.computer_tools"]
    assert payload["found"] == ["astrbot.core.tools.cron_tools"], "子树被跳过后不应凭空产出模块名"
