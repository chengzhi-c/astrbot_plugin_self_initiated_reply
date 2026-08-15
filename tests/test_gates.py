"""Local quality-gate input selection contracts."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]


def _load_gates() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "selfreply_test_gates", ROOT / "scripts" / "gates.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ruff_targets_use_only_git_tracked_python_sources(monkeypatch) -> None:
    gates = _load_gates()
    tracked = [
        ROOT / "main.py",
        ROOT / "tests" / "test_gates.py",
        ROOT / "docs" / "OPTIMIZATION_PLAN.md",
        ROOT / "pages" / "主动回复设置" / "app.js",
    ]
    monkeypatch.setattr(gates, "_tracked_files", lambda: tracked)

    assert gates.ruff_targets() == ["main.py", "tests/test_gates.py"]


def test_source_tree_fallback_ignores_local_documents_and_scratch(
    tmp_path: Path, monkeypatch
) -> None:
    gates = _load_gates()
    (tmp_path / "tests").mkdir()
    (tmp_path / ".scratch").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "tests" / "test_main.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / ".scratch" / "probe.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "docs" / "notes.md").write_text("notes\n", encoding="utf-8")
    monkeypatch.setattr(gates, "ROOT", tmp_path)
    monkeypatch.setattr(gates, "_tracked_files", lambda: None)

    assert gates.ruff_targets() == ["main.py", "tests/test_main.py"]
