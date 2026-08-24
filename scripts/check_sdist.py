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
    from scripts.release_artifacts import ArtifactError, resolve_artifact
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from release_artifacts import ArtifactError, resolve_artifact

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


def _expected_version() -> str:
    match = re.search(
        r"^version:\s*(.+)$",
        (ROOT / "metadata.yaml").read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if match is None:
        raise ValueError("metadata.yaml is missing version")
    return match.group(1).strip()


def _relative_name(name: str, root_name: str) -> str | None:
    normalized = name.replace("\\", "/").removeprefix("./")
    prefix = f"{root_name}/"
    if normalized == root_name:
        return ""
    if not normalized.startswith(prefix):
        return None
    return normalized[len(prefix) :]


def _is_forbidden(name: str) -> bool:
    normalized = name.replace("\\", "/").removeprefix("./")
    return (
        any(fnmatch.fnmatch(normalized, pattern) for pattern in FORBIDDEN_GLOBS)
        or normalized.startswith("/")
        or bool(re.match(r"^[A-Za-z]:/", normalized))
        or ".." in Path(normalized).parts
    )


def main(sdist_path: str | Path | None = None) -> int:
    try:
        target = resolve_artifact(
            ROOT,
            pattern="*.tar.gz",
            kind="sdist",
            explicit=sdist_path,
        )
        expected = _expected_version()
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
