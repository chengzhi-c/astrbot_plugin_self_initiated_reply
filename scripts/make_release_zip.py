"""从 wheel 派生 VPS 部署 zip（0.9.5 自 .scratch 提升为仓库脚本）。

为什么从 wheel 派生而不是从源码树重新遍历：wheel 是唯一内容已被
scripts/check_wheel.py 断言过的产物（无开发物泄漏、必需文件齐全、版本一致）。
从源码树再实现一遍 exclude 匹配，等于多一套没人核验的排除逻辑，而
pyproject 的 exclude 历史上已经漂移过两次（0.8.3 打进 tests/、0.9.2 打进
assets/）。这里只读 wheel 并丢掉 pip 专用的 dist-info/ 元数据——目录直投式
安装用不到它。

为什么必须在仓库里而不是留在 .scratch/：.scratch 被 gitignore，脚本留在那里
等于发布流程只存在于某台开发机上，clone 出来复现不了 zip。

输出布局：<plugin_name>/...，解压到 AstrBot 的 data/plugins/ 下即为插件目录。

用法（发布目录必须只含一个目标 wheel；多个候选时显式传路径）：

    Remove-Item -Recurse -Force dist -ErrorAction SilentlyContinue
    python -m hatchling build
    python scripts/check_wheel.py
    python scripts/check_sdist.py
    python scripts/make_release_zip.py
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

try:
    from scripts.release_artifacts import (
        REQUIRED_PAGE_FILES,
        ArtifactError,
        expected_project_name,
        resolve_artifact,
        validate_archive_member,
    )
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from release_artifacts import (
        REQUIRED_PAGE_FILES,
        ArtifactError,
        expected_project_name,
        resolve_artifact,
        validate_archive_member,
    )

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR_NAME = "astrbot_plugin_self_initiated_reply"
# 缺任何一条即拒绝出包：宿主靠 main.py 找入口、靠 metadata.yaml 认插件，
# 缺了不会报错只会"装上却不工作"。页面文件清单与 check_wheel 共用
# release_artifacts.REQUIRED_PAGE_FILES。
REQUIRED = (
    "main.py",
    "metadata.yaml",
    "_conf_schema.json",
    "__init__.py",
    *REQUIRED_PAGE_FILES,
)
# 与 check_wheel.py 的 FORBIDDEN_PREFIXES 同源的开发物前缀。这里再查一遍不是
# 冗余：wheel 与 zip 之间还有本脚本这一层转写，转写逻辑写错时 check_wheel 已经
# 跑完了。
DEV_PREFIXES = ("tests/", "scripts/", "docs/", ".github/", ".scratch/", "assets/")


def main(wheel_path: str | Path | None = None) -> int:
    try:
        wheel = resolve_artifact(
            ROOT,
            pattern="*.whl",
            kind="wheel",
            explicit=wheel_path,
        )
        expected_name = expected_project_name(ROOT)
    except (ArtifactError, OSError) as exc:
        print(f"FAIL: {exc}")
        return 1
    source_wheel = wheel.path

    version = str(wheel.version)
    out_path = ROOT / "dist" / f"{PLUGIN_DIR_NAME}-{version}-deploy.zip"
    copied: list[str] = []
    payloads: dict[str, bytes] = {}
    skipped = 0
    failures: list[str] = []

    if wheel.name != expected_name:
        failures.append(f"wheel 项目名 {wheel.name} 与 pyproject {expected_name} 不一致")

    try:
        src = zipfile.ZipFile(source_wheel)
    except (OSError, zipfile.BadZipFile) as exc:
        print(f"FAIL: cannot read wheel: {exc}")
        return 1
    with src:
        for entry in src.infolist():
            name = entry.filename.replace("\\", "/")
            try:
                name = validate_archive_member(name)
            except ArtifactError as exc:
                failures.append(str(exc))
                continue
            if name.endswith("/"):
                continue
            if name.split("/", 1)[0].endswith(".dist-info"):
                skipped += 1
                continue
            rel = name.removeprefix("./")
            copied.append(rel)
            payloads[rel] = src.read(entry)

    failures.extend(f"缺少必需文件: {item}" for item in REQUIRED if item not in copied)
    failures += [f"开发物泄漏: {name}" for name in copied if name.startswith(DEV_PREFIXES)]
    if failures:
        out_path.unlink(missing_ok=True)
        print("FAIL:")
        for line in failures:
            print(f"  - {line}")
        return 1

    try:
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as dst:
            for rel in copied:
                dst.writestr(f"{PLUGIN_DIR_NAME}/{rel}", payloads[rel])
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        out_path.unlink(missing_ok=True)
        print(f"FAIL: cannot write deploy zip: {exc}")
        return 1

    size_kb = out_path.stat().st_size // 1024
    print(f"源 wheel: {source_wheel.name}")
    print(f"输出:     {out_path.name}（{size_kb} KB）")
    print(f"文件:     {len(copied)} 个（跳过 {skipped} 个 dist-info 条目）")
    print(f"根目录:   {PLUGIN_DIR_NAME}/")
    print("OK: 必需文件齐全，无开发物泄漏")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, help="explicit wheel path")
    sys.exit(main(parser.parse_args().wheel))
