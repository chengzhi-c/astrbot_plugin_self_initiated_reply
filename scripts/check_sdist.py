"""Source distribution content and version assertions."""

from __future__ import annotations

import argparse
import fnmatch
import re
import sys
import tarfile
from pathlib import Path

from packaging.version import InvalidVersion, Version

try:
    from scripts.release_artifacts import (
        ArtifactError,
        expected_project_name,
        expected_version,
        normalize_member,
        resolve_artifact,
        validate_archive_member,
    )
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from release_artifacts import (
        ArtifactError,
        expected_project_name,
        expected_version,
        normalize_member,
        resolve_artifact,
        validate_archive_member,
    )

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FILES = (
    "pyproject.toml",
    "PKG-INFO",
    "README.md",
    "CHANGELOG.md",
    "metadata.yaml",
    "_conf_schema.json",
    "main.py",
    "pages/",
)
FORBIDDEN_GLOBS = (
    ".coverage",
    ".coverage.*",
    "coverage.*",
    "output/**",
    "dist/**",
    ".pytest_cache/**",
    ".ruff_cache/**",
    ".mypy_cache/**",
    "**/__pycache__/**",
    "__pycache__/**",
    "*.pyc",
    "*.egg-info/**",
    ".venv/**",
    "venv/**",
    ".tox/**",
    ".git/**",
)
MACHINE_PATH_PATTERNS = (
    re.compile(rb"(?<![A-Za-z])[A-Za-z]:[\\/][^\x00-\x20<>]+"),
    re.compile(rb"(?<![A-Za-z0-9])/(?:home|Users|root|tmp)/[^\x00-\x20<>]+"),
)
# 只对文本类文件做机器路径扫描。二进制（图片等）里出现「盘符 + 路径分隔符」这种形态
# 纯属巧合，那是假阳；sdist 里真正会携带本机路径的是源码、配置与文档。
# 本注释不写盘符字面量：本文件本身在 sdist 内，写示例会让 _machine_path_in 扫中自己，
# 门禁恒红（实测：干净 HEAD 上 hatchling 构建 sdist 后 check_sdist 必失败）。
TEXT_SCAN_SUFFIXES = (
    ".py",
    ".pyi",
    ".md",
    ".txt",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".cfg",
    ".ini",
    ".html",
    ".css",
    ".mjs",
    ".js",
    ".ts",
    ".sh",
    ".in",
)


def _is_text_member(relative: str) -> bool:
    """是否值得做机器路径扫描（见 ``TEXT_SCAN_SUFFIXES`` 注释）。"""
    suffix = Path(relative).suffix.lower()
    return suffix in TEXT_SCAN_SUFFIXES or not suffix


def _machine_path_in(data: bytes) -> bytes | None:
    for pattern in MACHINE_PATH_PATTERNS:
        match = pattern.search(data)
        if match:
            return match.group(0)
    return None


def _expected_version() -> str:
    return expected_version(ROOT)


def _relative_name(name: str, root_name: str) -> str | None:
    normalized = normalize_member(name)
    prefix = f"{root_name}/"
    if normalized == root_name:
        return ""
    if not normalized.startswith(prefix):
        return None
    return normalized[len(prefix) :]


def _is_forbidden(name: str) -> bool:
    """禁运清单命中，或路径本身不安全（绝对路径、盘符、``..`` 穿越、NUL）。

    不安全判据用 ``release_artifacts.validate_archive_member``：那边是权威字面量
    集（比本文件原先的 ``^[A-Za-z]:/`` 更严，盘符后不跟斜杠也拒），且三个发布
    脚本共用同一份。
    """
    try:
        normalized = validate_archive_member(name)
    except ArtifactError:
        return True
    return any(fnmatch.fnmatch(normalized, pattern) for pattern in FORBIDDEN_GLOBS)


def main(sdist_path: str | Path | None = None) -> int:
    try:
        target = resolve_artifact(
            ROOT,
            pattern="*.tar.gz",
            kind="sdist",
            explicit=sdist_path,
        )
        expected = _expected_version()
        expected_name = expected_project_name(ROOT)
    except (ArtifactError, OSError, ValueError) as exc:
        print(f"FAIL: {exc}")
        return 1

    try:
        expected_version = Version(expected)
    except InvalidVersion:
        print(f"FAIL: invalid metadata version: {expected}")
        return 1

    root_name = target.path.name.removesuffix(".tar.gz")
    failures: list[str] = []
    relative_names: list[str] = []
    try:
        with tarfile.open(target.path, "r:gz") as archive:
            for member in archive.getmembers():
                relative = _relative_name(member.name, root_name)
                if relative is None:
                    failures.append(f"invalid top-level path: {member.name}")
                    continue
                if relative:
                    if member.isfile() and _is_text_member(relative):
                        payload = archive.extractfile(member)
                        if payload is not None:
                            machine_path = _machine_path_in(payload.read())
                            if machine_path is not None:
                                failures.append(
                                    "machine-specific path in content: "
                                    f"{relative} ({machine_path!r})"
                                )
                    if member.issym() or member.islnk():
                        link_name = _relative_name(member.linkname, root_name)
                        if link_name is None or _is_forbidden(link_name or ""):
                            failures.append(
                                f"unsafe archive link: {member.name} -> {member.linkname}"
                            )
                    elif member.isdev():
                        failures.append(f"unsupported special file: {member.name}")
                    if _is_forbidden(relative):
                        failures.append(f"forbidden build artifact: {relative}")
                    relative_names.append(relative)
    except (OSError, tarfile.TarError) as exc:
        print(f"FAIL: cannot read sdist: {exc}")
        return 1

    for required in REQUIRED_FILES:
        if required.endswith("/"):
            present = any(name.startswith(required) for name in relative_names)
        else:
            present = required in relative_names
        if not present:
            failures.append(f"missing required file: {required}")

    if target.version != expected_version:
        failures.append(
            "sdist filename version "
            f"{target.version} differs from metadata version {expected_version}"
        )
    if target.name != expected_name:
        failures.append(f"sdist 项目名 {target.name} 与 pyproject {expected_name} 不一致")

    if failures:
        print("FAIL:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print(f"OK: {len(relative_names)} files, sdist version {target.version}, no build artifacts")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sdist", type=Path, help="explicit source distribution path")
    sys.exit(main(parser.parse_args().sdist))
