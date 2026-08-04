"""变异检测制度化：把三方审查历史实测过的击杀点固化为一键回归。

每个变异点 = (名称, 目标文件, 原始串, 变异串, 应击杀的测试 -k 表达式)。
- 锚定串漂移（不存在）直接报错，强制人工更新变异定义——防止变异静默失效变成假绿灯。
- 恢复用 copy2 唯一命名备份 + 逐字节校验，禁止 git checkout。
- 挂 nightly，不进 PR 门禁。

用法：python scripts/mutation_check.py   （退出码：0 全击杀，1 有存活）
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

# (名称, 目标文件, 原始串, 变异串, 应击杀的测试 -k 表达式)
MUTANTS = [
    (
        "generation-always-current",
        "session_gate.py",
        "return generation is None or self._session_generation.get(umo, 0) == generation",
        "return True",
        "aba",
    ),
    (
        "per-session-counter",
        "session_gate.py",
        "generation = next(self._generation_counter)",
        "generation = self._session_generation.get(umo, 0) + 1",
        "aba",
    ),
    (
        "unmark-no-wake",
        "session_gate.py",
        (
            "        release = self._session_release.get(umo)\n"
            "        if release is not None:\n"
            "            release.set()"
        ),
        "        release = None",
        "waits_for_running_session_release",
    ),
    (
        "prune-no-wake",
        "session_gate.py",
        (
            "        release = self._session_release.pop(umo, None)\n"
            "        if release is not None:\n"
            "            release.set()"
        ),
        "        release = None",
        "prune_wakes",
    ),
    (
        "restore-drops-running",
        "session_gate.py",
        '        self._running_sessions = snap["running"]\n',
        "        pass\n",
        "restore_recovers_running",
    ),
    (
        "whitelist-gate-removed",
        "main.py",
        "        if not force and not session_whitelisted(umo, self.settings.whitelist):",
        "        if False:",
        "r12_non_force",
    ),
    (
        "record-unconfirmed-writes-history",
        "models.py",
        "        if not confirmed:\n            return",
        "        if False:\n            return",
        "record_proactive_attempt",
    ),
    (
        "cancel-force-no-running-cancel",
        "main.py",
        "        if force and running_task and not running_task.done()"
        " and running_task is not task:",
        "        if False and running_task and not running_task.done()"
        " and running_task is not task:",
        "force_cancel_kills_running_check",
    ),
    (
        "discard-background-no-op",
        "main.py",
        "        self._background_tasks.discard(task)",
        "        pass",
        "converges_all_tables",
    ),
    (
        "running-check-no-pop",
        "main.py",
        (
            "                if running_task is not None and self._running_check_tasks.get(umo)"
            " is running_task:\n"
            "                    self._running_check_tasks.pop(umo, None)"
        ),
        "                pass",
        "running_check_residue",
    ),
    (
        "call-compat-retry-on-body-typeerror",
        "adapters.py",
        "        return await maybe_await(func(**call_kwargs))",
        (
            "        try:\n"
            "            return await maybe_await(func(**call_kwargs))\n"
            "        except TypeError:\n"
            "            minimal = AstrBotBridge._supported_kwargs(func, minimal_kwargs, aliases)\n"
            "            return await maybe_await(func(**minimal))"
        ),
        "call_compat",
    ),
]


def main() -> int:
    survivors: list[str] = []
    for name, rel, old, new, k in MUTANTS:
        target = ROOT / rel
        src = target.read_text(encoding="utf-8")
        assert old in src, f"{name}: 锚定串不存在（代码已漂移，需更新变异定义）"
        backup = Path(tempfile.mkdtemp()) / f"{name}.bak"
        shutil.copy2(target, backup)
        try:
            target.write_text(src.replace(old, new, 1), encoding="utf-8")
            r = subprocess.run(
                [PY, "-m", "pytest", "tests/", "-q", "-o", "addopts=", "-k", k, "-x"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=180,
            )
            if r.returncode == 0:
                survivors.append(name)
                print(f"SURVIVED: {name}")
            else:
                print(f"KILLED:   {name}")
        finally:
            shutil.copy2(backup, target)
            assert target.read_bytes() == backup.read_bytes(), f"{name} 恢复校验失败"
    print(f"\n{len(MUTANTS) - len(survivors)}/{len(MUTANTS)} killed")
    if survivors:
        print(f"SURVIVED: {', '.join(survivors)}")
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main())
