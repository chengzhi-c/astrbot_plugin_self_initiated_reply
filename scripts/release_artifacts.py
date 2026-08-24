"""Shared discovery and filename validation for release artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from packaging.utils import (
    InvalidSdistFilename,
    InvalidWheelFilename,
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
    version: Version


def _parse_version(path: Path, kind: str) -> Version:
    try:
        if kind == "wheel":
            _, version, _, _ = parse_wheel_filename(path.name)
        elif kind == "sdist":
            _, version = parse_sdist_filename(path.name)
        else:
            raise ArtifactError(f"unsupported artifact kind: {kind}")
    except (InvalidSdistFilename, InvalidWheelFilename, ValueError) as exc:
        raise ArtifactError(f"invalid {kind} filename: {path.name}") from exc
    return version


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
    return ArtifactTarget(path=path, version=_parse_version(path, kind))
