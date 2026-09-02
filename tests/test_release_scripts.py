"""Release artifact gate contracts."""

from __future__ import annotations

import importlib.util
import io
import sys
import tarfile
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]

FRONTEND_FILES = [
    "pages/主动回复设置/index.html",
    "pages/主动回复设置/style.css",
    "pages/主动回复设置/app.js",
    "pages/主动回复设置/frontend-core.mjs",
    "pages/主动回复设置/config-form.mjs",
    "pages/主动回复设置/config-io.mjs",
    "pages/主动回复设置/providers.mjs",
    "pages/主动回复设置/theme.mjs",
    "pages/主动回复设置/chrome.mjs",
]


def _load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"selfreply_test_{name}", ROOT / "scripts" / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_wheel(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("pkg-1.0.dist-info/METADATA", "Version: 1.0\n")


def _write_complete_wheel(path: Path, *, metadata_version: str) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name in [
            "metadata.yaml",
            "_conf_schema.json",
            "main.py",
            "logo.png",
            "pages/index.html",
        ]:
            archive.writestr(name, "")
        archive.writestr(
            "pkg-1.3.0.dist-info/METADATA",
            f"Metadata-Version: 2.3\nVersion: {metadata_version}\n",
        )


def _write_project_metadata(root: Path, *, version: str = "1.3.0") -> None:
    (root / "pyproject.toml").write_text(
        '[project]\nname = "astrbot_plugin_self_initiated_reply"\ndependencies = []\n',
        encoding="utf-8",
    )
    (root / "metadata.yaml").write_text(
        f"name: astrbot_plugin_self_initiated_reply\nversion: {version}\n",
        encoding="utf-8",
    )


def _write_release_wheel(path: Path, *, name: str, frontend: list[str] | None = None) -> None:
    files = [
        "metadata.yaml",
        "_conf_schema.json",
        "main.py",
        "logo.png",
        "README.md",
        "CHANGELOG.md",
        *(frontend if frontend is not None else FRONTEND_FILES),
    ]
    with zipfile.ZipFile(path, "w") as archive:
        for file_name in files:
            archive.writestr(file_name, "")
        archive.writestr(
            f"{name}-1.3.0.dist-info/METADATA",
            f"Metadata-Version: 2.3\nName: {name}\nVersion: 1.3.0\n",
        )


def _write_sdist(path: Path, names: list[str], contents: dict[str, bytes] | None = None) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name in names:
            data = (contents or {}).get(name, b"")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


def test_wheel_discovery_rejects_multiple_candidates(tmp_path: Path, monkeypatch, capsys) -> None:
    check_wheel = _load_script("check_wheel")
    dist = tmp_path / "dist"
    dist.mkdir()
    _write_wheel(dist / "astrbot_plugin_self_initiated_reply-1.9.0-py3-none-any.whl")
    _write_wheel(dist / "astrbot_plugin_self_initiated_reply-1.10.0-py3-none-any.whl")
    monkeypatch.setattr(check_wheel, "ROOT", tmp_path)

    with pytest.raises(SystemExit):
        check_wheel._find_wheel()

    assert "multiple" in capsys.readouterr().out.lower()


def test_wheel_discovery_accepts_explicit_path_and_rejects_bad_filename(
    tmp_path: Path, monkeypatch
) -> None:
    check_wheel = _load_script("check_wheel")
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel = dist / "astrbot_plugin_self_initiated_reply-1.10.0-py3-none-any.whl"
    _write_wheel(wheel)
    monkeypatch.setattr(check_wheel, "ROOT", tmp_path)

    assert check_wheel._find_wheel(wheel) == wheel
    with pytest.raises(SystemExit):
        check_wheel._find_wheel(dist / "not-a-wheel.whl")


def test_wheel_required_files_use_exact_file_matching() -> None:
    check_wheel = _load_script("check_wheel")

    assert check_wheel._has_required_file(["main.py.bak"], "main.py") is False
    assert check_wheel._has_required_file(["pages/index.html"], "pages/") is True


def test_wheel_checker_rejects_version_substring_match(tmp_path: Path, monkeypatch, capsys) -> None:
    check_wheel = _load_script("check_wheel")
    (tmp_path / "metadata.yaml").write_text("version: 1.3.0\n", encoding="utf-8")
    _write_project_metadata(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel = dist / "astrbot_plugin_self_initiated_reply-1.3.0.post1-py3-none-any.whl"
    _write_complete_wheel(wheel, metadata_version="1.3.0.post1")
    monkeypatch.setattr(check_wheel, "ROOT", tmp_path)

    assert check_wheel.main() == 1
    assert "版本" in capsys.readouterr().out


def test_wheel_checker_rejects_wrong_distribution_name(tmp_path: Path, monkeypatch, capsys) -> None:
    check_wheel = _load_script("check_wheel")
    _write_project_metadata(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel = dist / "evil_pkg-1.3.0-py3-none-any.whl"
    _write_release_wheel(wheel, name="evil_pkg")
    monkeypatch.setattr(check_wheel, "ROOT", tmp_path)

    assert check_wheel.main() == 1
    assert "项目名" in capsys.readouterr().out


def test_sdist_checker_rejects_wrong_distribution_name(tmp_path: Path, monkeypatch, capsys) -> None:
    check_sdist = _load_script("check_sdist")
    _write_project_metadata(tmp_path)
    archive = tmp_path / "dist" / "evil_pkg-1.3.0.tar.gz"
    archive.parent.mkdir()
    root = "evil_pkg-1.3.0"
    _write_sdist(
        archive,
        [
            f"{root}/pyproject.toml",
            f"{root}/PKG-INFO",
            f"{root}/README.md",
            f"{root}/CHANGELOG.md",
            f"{root}/metadata.yaml",
            f"{root}/_conf_schema.json",
            f"{root}/main.py",
            f"{root}/pages/index.html",
        ],
    )
    monkeypatch.setattr(check_sdist, "ROOT", tmp_path)

    assert check_sdist.main(archive) == 1
    assert "项目名" in capsys.readouterr().out


def test_sdist_rejects_cache_and_temporary_output(tmp_path: Path, capsys) -> None:
    check_sdist = _load_script("check_sdist")
    _write_project_metadata(tmp_path)
    archive = tmp_path / "dist" / "astrbot_plugin_self_initiated_reply-1.3.0.tar.gz"
    archive.parent.mkdir()
    root = "astrbot_plugin_self_initiated_reply-1.3.0"
    _write_sdist(
        archive,
        [
            f"{root}/pyproject.toml",
            f"{root}/main.py",
            f"{root}/metadata.yaml",
            f"{root}/_conf_schema.json",
            f"{root}/pages/index.html",
            f"{root}/.coverage.host.123",
            f"{root}/output/preview.png",
        ],
    )
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(check_sdist, "ROOT", tmp_path)
    try:
        assert check_sdist.main(archive) == 1
    finally:
        monkeypatch.undo()

    assert "forbidden" in capsys.readouterr().out.lower()


def test_sdist_rejects_machine_path_content(tmp_path: Path, capsys) -> None:
    check_sdist = _load_script("check_sdist")
    _write_project_metadata(tmp_path)
    archive = tmp_path / "dist" / "astrbot_plugin_self_initiated_reply-1.3.0.tar.gz"
    archive.parent.mkdir()
    root = "astrbot_plugin_self_initiated_reply-1.3.0"
    main_name = f"{root}/main.py"
    _write_sdist(
        archive,
        [
            f"{root}/pyproject.toml",
            f"{root}/PKG-INFO",
            f"{root}/README.md",
            f"{root}/CHANGELOG.md",
            f"{root}/metadata.yaml",
            f"{root}/_conf_schema.json",
            main_name,
            f"{root}/pages/index.html",
        ],
        {main_name: b"CACHE = r'C:\\Users\\example\\temp'\n"},
    )
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(check_sdist, "ROOT", tmp_path)
    try:
        assert check_sdist.main(archive) == 1
    finally:
        monkeypatch.undo()

    assert "machine-specific path" in capsys.readouterr().out


def test_sdist_rejects_parent_traversal_member() -> None:
    check_sdist = _load_script("check_sdist")

    assert check_sdist._is_forbidden("../outside.txt") is True


def test_archive_member_validation_rejects_parent_traversal() -> None:
    release_artifacts = _load_script("release_artifacts")

    with pytest.raises(ValueError):
        release_artifacts.validate_archive_member(r"..\outside.txt")


def test_deploy_zip_rejects_missing_frontend_entry(tmp_path: Path, monkeypatch, capsys) -> None:
    make_release_zip = _load_script("make_release_zip")
    _write_project_metadata(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel = dist / "astrbot_plugin_self_initiated_reply-1.3.0-py3-none-any.whl"
    _write_release_wheel(
        wheel,
        name="astrbot_plugin_self_initiated_reply",
        frontend=FRONTEND_FILES[:-1],
    )
    monkeypatch.setattr(make_release_zip, "ROOT", tmp_path)

    assert make_release_zip.main() == 1
    assert "必需文件" in capsys.readouterr().out


def test_deploy_zip_rejects_unsafe_member(tmp_path: Path, monkeypatch, capsys) -> None:
    make_release_zip = _load_script("make_release_zip")
    _write_project_metadata(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel = dist / "astrbot_plugin_self_initiated_reply-1.3.0-py3-none-any.whl"
    _write_release_wheel(
        wheel,
        name="astrbot_plugin_self_initiated_reply",
        frontend=FRONTEND_FILES + [r"..\outside.txt"],
    )
    monkeypatch.setattr(make_release_zip, "ROOT", tmp_path)

    assert make_release_zip.main() == 1
    assert "unsafe archive member" in capsys.readouterr().out


def test_gates_reports_not_release_verified_without_artifacts(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    gates = _load_script("gates")
    page = tmp_path / "pages" / "主动回复设置"
    page.mkdir(parents=True)
    (page / "app.js").write_text("const ok = true;\n", encoding="utf-8")
    monkeypatch.setattr(gates, "ROOT", tmp_path)
    monkeypatch.setattr(gates, "PAGE", page)
    monkeypatch.setattr(gates, "ruff_targets", lambda: ["main.py"])
    monkeypatch.setattr(gates, "_run", lambda _label, _argv: None)

    assert gates.main() == 0
    output = capsys.readouterr().out
    assert "NOT RELEASE-VERIFIED" in output
    assert "OK: all gates passed" not in output


def test_deploy_zip_rejects_multiple_wheels(tmp_path: Path, monkeypatch, capsys) -> None:
    make_release_zip = _load_script("make_release_zip")
    dist = tmp_path / "dist"
    dist.mkdir()
    _write_wheel(dist / "astrbot_plugin_self_initiated_reply-1.3.0-py3-none-any.whl")
    _write_wheel(dist / "astrbot_plugin_self_initiated_reply-1.4.0-py3-none-any.whl")
    monkeypatch.setattr(make_release_zip, "ROOT", tmp_path)

    assert make_release_zip.main() == 1
    assert "multiple" in capsys.readouterr().out.lower()


def test_gates_release_mode_rejects_missing_artifacts(tmp_path: Path, monkeypatch, capsys) -> None:
    gates = _load_script("gates")
    page = tmp_path / "pages" / "主动回复设置"
    page.mkdir(parents=True)
    (page / "app.js").write_text("const ok = true;\n", encoding="utf-8")
    monkeypatch.setattr(gates, "ROOT", tmp_path)
    monkeypatch.setattr(gates, "PAGE", page)
    monkeypatch.setattr(gates, "ruff_targets", lambda: ["main.py"])
    monkeypatch.setattr(gates, "_run", lambda _label, _argv: None)

    assert gates.main(require_release=True) == 1
    assert "NOT RELEASE-VERIFIED" in capsys.readouterr().out
