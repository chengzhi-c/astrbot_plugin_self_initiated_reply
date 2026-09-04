"""Runtime dependency allowlist and installed-version gate."""

from __future__ import annotations

import importlib.metadata
import tomllib
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parent.parent
EXPECTED_RUNTIME_DEPENDENCIES = frozenset({"httpx>=0.27,<0.29", "httpcore>=1,<1.1"})


def declared_runtime_dependencies() -> frozenset[str]:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        data = tomllib.load(handle)
    return frozenset(data.get("project", {}).get("dependencies", []))


def installed_runtime_dependency_gaps() -> list[str]:
    gaps: list[str] = []
    for raw in EXPECTED_RUNTIME_DEPENDENCIES:
        requirement = Requirement(raw)
        try:
            version = importlib.metadata.version(requirement.name)
        except importlib.metadata.PackageNotFoundError:
            gaps.append(f"{requirement.name} 未安装（需要 {requirement.specifier}）")
            continue
        if version not in requirement.specifier:
            gaps.append(f"{requirement.name} 版本 {version} 不满足 {requirement.specifier}")
    return gaps


def runtime_api_gaps() -> list[str]:
    """Check the public APIs used by the fixed-address transport."""
    import inspect

    try:
        import httpcore
        import httpx
    except ImportError as exc:
        return [f"运行时图片依赖导入失败: {exc}"]

    gaps: list[str] = []
    for module, names in (
        (httpx, ("AsyncBaseTransport", "AsyncByteStream", "AsyncClient")),
        (httpcore, ("AsyncConnectionPool", "AsyncNetworkBackend", "AnyIOBackend")),
    ):
        for name in names:
            if not hasattr(module, name):
                gaps.append(f"{module.__name__}.{name} 缺失")
    params = inspect.signature(httpcore.AsyncNetworkBackend.connect_tcp).parameters
    if not {"host", "port"}.issubset(params):
        gaps.append("httpcore.AsyncNetworkBackend.connect_tcp 不再使用 host/port 参数")
    required_signatures = (
        (
            "httpcore.AsyncConnectionPool",
            inspect.signature(httpcore.AsyncConnectionPool),
            {"network_backend"},
        ),
        (
            "httpcore.Request",
            inspect.signature(httpcore.Request),
            {"method", "url", "headers", "content", "extensions"},
        ),
        (
            "httpcore.URL",
            inspect.signature(httpcore.URL),
            {"scheme", "host", "port", "target"},
        ),
        (
            "httpx.AsyncClient",
            inspect.signature(httpx.AsyncClient),
            {"timeout", "follow_redirects", "max_redirects", "trust_env", "transport"},
        ),
    )
    for label, signature, required in required_signatures:
        if not required.issubset(signature.parameters):
            missing = sorted(required - set(signature.parameters))
            gaps.append(f"{label} 缺少固定图片传输所需参数: {missing}")
    return gaps


def main() -> int:
    declared = declared_runtime_dependencies()
    if declared != EXPECTED_RUNTIME_DEPENDENCIES:
        print(
            "FAIL: runtime dependency allowlist drift: "
            f"declared={sorted(declared)!r} expected={sorted(EXPECTED_RUNTIME_DEPENDENCIES)!r}"
        )
        return 1
    print("PASS: runtime dependency allowlist is exact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
