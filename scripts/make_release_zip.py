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

用法（动过打包配置或版本号时，先删 dist/ 再构建，否则 check_wheel 会读到
字典序最后的旧 wheel 而假绿）：

    python -m hatch build
    python scripts/check_wheel.py
    python scripts/make_release_zip.py
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR_NAME = "astrbot_plugin_self_initiated_reply"
# 缺任何一条即拒绝出包：宿主靠 main.py 找入口、靠 metadata.yaml 认插件，
# 缺了不会报错只会"装上却不工作"。
REQUIRED = ("main.py", "metadata.yaml", "_conf_schema.json", "__init__.py")
# 与 check_wheel.py 的 FORBIDDEN_PREFIXES 同源的开发物前缀。这里再查一遍不是
# 冗余：wheel 与 zip 之间还有本脚本这一层转写，转写逻辑写错时 check_wheel 已经
# 跑完了。
DEV_PREFIXES = ("tests/", "scripts/", "docs/", ".github/", ".scratch/", "assets/")


def main() -> int:
    wheels = sorted((ROOT / "dist").glob("*.whl"))
    if not wheels:
        print("FAIL: dist/ 下没有 wheel，请先执行 python -m hatch build")
        return 1
    # 与 check_wheel.py 同口径取字典序最后一个，保证两个脚本看的是同一个 wheel。
    wheel = wheels[-1]

    out_path = ROOT / "dist" / f"{PLUGIN_DIR_NAME}-deploy.zip"
    copied: list[str] = []
    skipped = 0

    with zipfile.ZipFile(wheel) as src, zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as dst:
        for entry in src.infolist():
            name = entry.filename.replace("\\", "/")
            if name.endswith("/"):
                continue
            if ".dist-info/" in name:
                skipped += 1
                continue
            rel = name.removeprefix("./")
            dst.writestr(f"{PLUGIN_DIR_NAME}/{rel}", src.read(entry))
            copied.append(rel)

    failures = [f"缺少必需文件: {item}" for item in REQUIRED if item not in copied]
    failures += [f"开发物泄漏: {name}" for name in copied if name.startswith(DEV_PREFIXES)]
    if failures:
        print("FAIL:")
        for line in failures:
            print(f"  - {line}")
        return 1

    size_kb = out_path.stat().st_size // 1024
    print(f"源 wheel: {wheel.name}")
    print(f"输出:     {out_path.name}（{size_kb} KB）")
    print(f"文件:     {len(copied)} 个（跳过 {skipped} 个 dist-info 条目）")
    print(f"根目录:   {PLUGIN_DIR_NAME}/")
    print("OK: 必需文件齐全，无开发物泄漏")
    return 0


if __name__ == "__main__":
    sys.exit(main())
