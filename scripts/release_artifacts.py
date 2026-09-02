"""Shared discovery and filename validation for release artifacts."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from packaging.utils import (
    InvalidSdistFilename,
    InvalidWheelFilename,
    canonicalize_name,
    parse_sdist_filename,
    parse_wheel_filename,
)
from packaging.version import Version


class ArtifactError(ValueError):
    """Raised when release artifact discovery is ambiguous or invalid."""


@dataclass(frozen=True)
class ArtifactTarget:
    """A uniquely selected and filename-validated release artifact."""

    path: Path
    name: str
    version: Version


def expected_project_name(root: Path) -> str:
    """Return the canonical project name from pyproject.toml."""
    try:
        with (root / "pyproject.toml").open("rb") as handle:
            name = tomllib.load(handle)["project"]["name"]
    except (OSError, KeyError, TypeError) as exc:
        raise ArtifactError("pyproject.toml 缺少 project.name") from exc
    if not isinstance(name, str) or not name.strip():
        raise ArtifactError("pyproject.toml 缺少 project.name")
    return canonicalize_name(name)


def validate_archive_member(name: str) -> str:
    """Return a relative archive path and reject traversal or absolute paths."""
    normalized = name.replace("\\", "/")
    if (
        not normalized
        or "\x00" in normalized
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or ".." in PurePosixPath(normalized).parts
    ):
        raise ArtifactError(f"unsafe archive member path: {name!r}")
    return normalized.removeprefix("./")


def _parse_artifact(path: Path, kind: str) -> tuple[str, Version]:
    try:
        if kind == "wheel":
            name, version, _, _ = parse_wheel_filename(path.name)
        elif kind == "sdist":
            name, version = parse_sdist_filename(path.name)
        else:
            raise ArtifactError(f"unsupported artifact kind: {kind}")
    except (InvalidSdistFilename, InvalidWheelFilename, ValueError) as exc:
        raise ArtifactError(f"invalid {kind} filename: {path.name}") from exc
    return canonicalize_name(name), version


def resolve_artifact(
    root: Path,
    *,
    pattern: str,
    kind: str,
    explicit: str | Path | None = None,
) -> ArtifactTarget:
    """Resolve exactly one artifact and validate its PEP 440 filename version."""
    if explicit is not None:
        path = Path(explicit)
        if not path.is_absolute():
            path = root / path
        candidates = [path]
    else:
        candidates = sorted((root / "dist").glob(pattern))

    if not candidates:
        raise ArtifactError(f"no {kind} artifact found; build it before release verification")
    if len(candidates) != 1:
        names = ", ".join(path.name for path in candidates)
        raise ArtifactError(
            f"multiple {kind} artifacts found ({names}); pass an explicit artifact path"
        )

    path = candidates[0]
    if not path.is_file():
        raise ArtifactError(f"{kind} artifact is not a file: {path}")
    name, version = _parse_artifact(path, kind)
    return ArtifactTarget(path=path, name=name, version=version)
