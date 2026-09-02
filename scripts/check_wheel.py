"""Wheel 内容断言：开发物不得泄漏，运行时必需文件必须存在。

由 CI build 作业在 `hatch build` 之后强制。
依据（2026-08-07 实测）：整目录打包会把 tests/.scratch/scripts/docs/.github
与开发配置全部打进生产 wheel（原 0.8.3 wheel 102 文件 / 2.8MB），
pyproject 的 exclude 修复后必须由本脚本断言闭环，防止配置漂移复发。

用法：先 `hatch build`（或 pip wheel .），再从仓库根执行
`python scripts/check_wheel.py`。
"""

from __future__ import annotations

import argparse
import sys
import tomllib
import zipfile
from fnmatch import fnmatch
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import InvalidVersion, Version

try:
    from scripts.release_artifacts import ArtifactError, expected_project_name, resolve_artifact
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from release_artifacts import ArtifactError, expected_project_name, resolve_artifact

ROOT = Path(__file__).resolve().parents[1]

# 禁止出现的开发物前缀（相对 wheel 内路径）
# assets/ 是 README 图源（4 张 JPG / 463KB），运行时零引用；0.9.2 实测它占 wheel 的 69%，
# 移出后 687,531 → 213,049 B。与 0.8.3 的 exclude 漂移同理，必须由断言锁死防复发。
FORBIDDEN_PREFIXES = (
    "tests/",
    ".scratch/",
    "scripts/",
    "docs/",
    ".github/",
    ".gitignore",
    "assets/",
    "node_modules/",
    "output/",
    "package.json",
    "package-lock.json",
    "playwright.config.mjs",
    "uv.lock",
)
# 必须存在的运行时文件（相对 wheel 内路径）
# README.md / CHANGELOG.md 自 0.9.5 起是必需项：exclude 的 `*.md` 曾把它们一起排掉，
# 装完插件的目录里没有任何面向用户的说明。它们靠 artifacts 收回（artifacts 优先于
# exclude），而 artifacts 漏一条不会让构建失败——只会静默少文件，故必须在此断言。
REQUIRED_FILES = (
    "metadata.yaml",
    "_conf_schema.json",
    "main.py",
    "logo.png",
    "pages/",
    "README.md",
    "CHANGELOG.md",
    "pages/主动回复设置/index.html",
    "pages/主动回复设置/style.css",
    "pages/主动回复设置/app.js",
    "pages/主动回复设置/frontend-core.mjs",
    "pages/主动回复设置/config-form.mjs",
    "pages/主动回复设置/config-io.mjs",
    "pages/主动回复设置/providers.mjs",
    "pages/主动回复设置/theme.mjs",
    "pages/主动回复设置/chrome.mjs",
)
# 开发配置类：wheel 内不应出现
FORBIDDEN_SUFFIXES = (".pre-commit-config.yaml", ".pyc")
# 测试/工具运行产物。上面按固定前缀与后缀匹配的名单认不出这类文件：
# pytest-cov 并行数据名为 .coverage.<host>.pid<N>.<rand>，既不在已知前缀下，
# 也不以已知后缀结尾。实测它被打进 wheel 而守卫仍报"无泄漏"。
FORBIDDEN_GLOBS = (
    ".coverage",
    ".coverage.*",
    # 不带点前缀的覆盖率产物：`coverage json` 写 coverage.json、
    # CI 的 `pytest --cov-report=xml` 写 coverage.xml，两者都落在仓库根，既不在
    # 已知前缀下、也不匹配 .coverage*。实测遗留的 coverage.json（220KB）
    # 被打进 wheel 而守卫仍报"无泄漏"——与上方 .coverage.* 是同一类漏报的第二次
    # 复发，故这里用通配收口而非再补一个具名条目。
    "coverage.*",
    "*/__pycache__/*",
    "__pycache__/*",
    ".pytest_cache/*",
    ".ruff_cache/*",
    ".mypy_cache/*",
    "*.egg-info/*",
)
# wheel 体积回归上限。基线 213KB（0.9.3 assets 排除后实测），
# +30% 缓冲覆盖 logo/文档微增；超阈值必是新增开发物或大文件泄漏。
MAX_WHEEL_BYTES = 280_000


def _find_wheel(explicit: str | Path | None = None) -> Path:
    try:
        return resolve_artifact(
            ROOT,
            pattern="*.whl",
            kind="wheel",
            explicit=explicit,
        ).path
    except ArtifactError as exc:
        print(f"FAIL: {exc}")
        raise SystemExit(1) from exc


def _normalize_requirement(raw: str) -> str:
    requirement = Requirement(raw)
    return f"{requirement.name}{requirement.specifier}"


def _expected_runtime_dependencies() -> set[str]:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    return {_normalize_requirement(raw) for raw in project.get("dependencies", [])}


def _expected_version() -> str:
    """metadata.yaml 的发布版本（wheel 文件名与 dist-info 的基准）。"""
    import re

    meta = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
    match = re.search(r"^version: (.+)$", meta, re.M)
    if match is None:
        print("FAIL: metadata.yaml 缺少 version 字段")
        sys.exit(1)
    return match.group(1).strip()


def _normalize(name: str) -> str:
    """wheel 内路径规范化。

    hatchling 生成的路径带 "./" 前缀。必须用 removeprefix 而非 lstrip("./")：
    后者按字符集剥离，会把 ".github/..." 削成 "github/..."、".gitignore" 削成
    "gitignore"，使三个以点开头的 FORBIDDEN_PREFIXES 永远匹配不到（0.9.3 修复）。
    """
    return name.replace("\\", "/").removeprefix("./")


def _has_required_file(names: list[str], required: str) -> bool:
    if required.endswith("/"):
        return any(name.startswith(required) for name in names)
    return required in names


def main(wheel_path: str | Path | None = None) -> int:
    wheel = _find_wheel(wheel_path)
    target = resolve_artifact(
        ROOT,
        pattern="*.whl",
        kind="wheel",
        explicit=wheel,
    )
    expected = _expected_version()
    expected_name = expected_project_name(ROOT)
    print(f"检查 wheel: {wheel.name} ({wheel.stat().st_size / 1024:.0f} KB)")
    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
        dist_meta = [n for n in names if n.endswith(".dist-info/METADATA")]
        dist_text = zf.read(dist_meta[0]).decode("utf-8") if dist_meta else ""

    failures: list[str] = []
    for name in names:
        normalized = _normalize(name)
        if (
            normalized.startswith(FORBIDDEN_PREFIXES)
            or normalized.endswith(FORBIDDEN_SUFFIXES)
            or any(fnmatch(normalized, pattern) for pattern in FORBIDDEN_GLOBS)
        ):
            failures.append(f"开发物泄漏: {normalized}")

    normalized_names = [_normalize(name) for name in names]
    for required in REQUIRED_FILES:
        if not _has_required_file(normalized_names, required):
            failures.append(f"缺少必需文件: {required}")

    # 版本一致性（0.8.3 发布缺陷回归守卫：wheel 文件名与 dist-info 曾停在 0.8.3）
    try:
        expected_version = Version(expected)
    except InvalidVersion:
        print(f"FAIL: invalid metadata version: {expected}")
        return 1
    if target.version != expected_version:
        failures.append(f"wheel 文件名版本 {target.version} 与 metadata {expected_version} 不一致")

    if target.name != expected_name:
        failures.append(f"wheel 项目名 {target.name} 与 pyproject {expected_name} 不一致")

    if not dist_meta:
        failures.append("wheel 内缺少 .dist-info/METADATA")
    elif not any(line == f"Version: {expected_version}" for line in dist_text.splitlines()):
        failures.append(f"dist-info 版本与 metadata {expected_version} 不一致")

    # 运行时依赖必须进入生产 METADATA；只出现在 dev extra 会导致安装后
    # Vision 固定地址传输缺包，且此前的零依赖门禁无法发现这种漂移。
    expected_runtime = _expected_runtime_dependencies()
    actual_runtime = {
        line.removeprefix("Requires-Dist: ").split(";", 1)[0].strip()
        for line in dist_text.splitlines()
        if line.startswith("Requires-Dist: ") and "; extra ==" not in line
    }
    if actual_runtime != expected_runtime:
        failures.append(
            f"wheel runtime METADATA 依赖漂移: actual={sorted(actual_runtime)!r} "
            f"expected={sorted(expected_runtime)!r}"
        )

    # logo/文档微增；超阈值必是新增开发物或大文件泄漏，早于发布拦住。
    size = wheel.stat().st_size
    if size > MAX_WHEEL_BYTES:
        failures.append(f"wheel 体积回归: {size} B > {MAX_WHEEL_BYTES} B 上限")

    if failures:
        print("FAIL:")
        for line in failures:
            print(f"  - {line}")
        return 1
    print(
        f"OK: {len(names)} 个文件，{size / 1024:.0f} KB，"
        f"无开发物泄漏，必需文件齐全，版本 {expected} 一致"
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, help="explicit wheel path")
    sys.exit(main(parser.parse_args().wheel))
