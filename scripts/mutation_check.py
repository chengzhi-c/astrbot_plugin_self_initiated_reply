"""变异检测制度化：把三方审查历史实测过的击杀点固化为一键回归。

每个变异点 = (名称, 目标文件, 原始串, 变异串, 应击杀的测试 -k 表达式)。
- 锚定串漂移（不存在）直接报错，强制人工更新变异定义——防止变异静默失效变成假绿灯。
- 恢复用 copy2 唯一命名备份 + 逐字节校验，禁止 git checkout。
- 入口守卫：目标文件工作区必须干净（拒绝把用户未提交改动与变异残留混淆），
  并以锁文件互斥并发运行；疑似上次强杀残留的变异会被识别并提示恢复方式。
- 挂 nightly，不进 PR 门禁。

用法：python scripts/mutation_check.py
退出码：0 全击杀，1 有存活，2 前置守卫拒绝
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
STALE_LOCK_SEC = 3600  # 运行时长上限：超龄锁文件视为上次强杀残留

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
        "scheduler.py",
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
        "scheduler.py",
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
    # ---- 批次3-2：P2-23 mutation 扩面（webapi 拒绝路径 / parser SSRF / storage 恢复） ----
    (
        "ssrf-scheme-bypass",
        "image/parser.py",
        '        if parsed.scheme not in {"http", "https"} or not parsed.hostname:',
        "        if not parsed.hostname:",
        "non_http_schemes",
    ),
    (
        "ssrf-port-bypass",
        "image/parser.py",
        "        if parsed.port is not None and parsed.port not in {80, 443}:",
        "        if False:",
        "non_standard_ports",
    ),
    (
        "ssrf-private-ip-allowed",
        "image/parser.py",
        "        return await asyncio.to_thread(_host_all_global, parsed.hostname)",
        "        return True",
        "rejects_private",
    ),
    (
        "webapi-bool-accepted",
        "webapi.py",
        '    if isinstance(value, bool):\n        raise ValueError(f"{field} 必须是整数")',
        '    if False:\n        raise ValueError(f"{field} 必须是整数")',
        "rejects_invalid",
    ),
    (
        "webapi-illegal-chars-allowed",
        "webapi.py",
        '        if re.search(r"[\\x00-\\x1f\\"\'\\\\]", text):',
        "        if False:",
        "illegal_whitelist",
    ),
    (
        "webapi-unknown-key-accepted",
        "webapi.py",
        "    if unknown:",
        "    if False:",
        "r16_unknown",
    ),
    (
        "storage-corrupt-not-backed-up",
        "storage.py",
        (
            '            logger.error("[%s] failed to load state '
            '(backing up): %s", PLUGIN_ID, exc)\n'
            "            _backup_state_file(path)"
        ),
        (
            '            logger.error("[%s] failed to load state '
            '(backing up): %s", PLUGIN_ID, exc)\n'
            "            pass"
        ),
        "corrupt_state",
    ),
    (
        "storage-version-not-backed-up",
        "storage.py",
        "                if file_version is not None and file_version != STATE_VERSION:",
        "                if False:",
        "version_mismatch",
    ),
    # ---- 批次4（ticket 10 扩面：19 → 32）：代次复核 / 观察窗口推进 /
    # 直发预算 / fail-closed 工具策略 / UNKNOWN 消耗语义 / 白名单回滚 /
    # 会话级联失效 ----
    (
        "delivery-stale-before-hooks",
        "delivery.py",
        (
            "        if not self._gate.is_current(umo, expected_generation):\n"
            '            logger.info("[%s] suppress stale reply before hooks session=%s",'
            " PLUGIN_ID, umo)"
        ),
        (
            "        if False:\n"
            '            logger.info("[%s] suppress stale reply before hooks session=%s",'
            " PLUGIN_ID, umo)"
        ),
        "test_send_reply_stale_before_hooks_skips_hooks",
    ),
    (
        "unconfirmed-no-advance",
        "delivery.py",
        (
            "            if self._gate.is_current(umo, expected_generation):\n"
            "                state.last_proactive_observed_at = ("
        ),
        ("            if False:\n                state.last_proactive_observed_at = ("),
        "test_record_unconfirmed_sets_state_fields",
    ),
    (
        "delivered-no-advance",
        "delivery.py",
        (
            "        if self._gate.is_current(umo, expected_generation):\n"
            "            state.last_proactive_observed_at = ("
        ),
        ("        if False:\n            state.last_proactive_observed_at = ("),
        "test_deliver_delivered_advances_observation_and_history",
    ),
    (
        "tool-direct-budget-bypass",
        "outbound.py",
        "            if self._direct_send_count >= self._max_direct_sends:",
        "            if False:",
        "test_tool_direct_send_budget_is_consumed_before_adapter_call",
    ),
    (
        "tool-direct-count-lost",
        "generation.py",
        (
            "            direct_send_count = outbound.direct_send_count\n"
            "            direct_send_texts[:] = outbound.direct_texts"
        ),
        (
            "            direct_send_count = 0\n"
            "            direct_send_texts[:] = outbound.direct_texts"
        ),
        "test_generate_tracks_direct_sends_within_budget",
    ),
    (
        "keep-policy-filter-skipped",
        "generation.py",
        (
            "        if self._runtime().filter_final_tools(req, keep=PROACTIVE_ALLOWED_TOOL_IDS):\n"
            "            return True"
        ),
        "        return True",
        "test_enforce_policy_keep_mode_filters",
    ),
    (
        "drop-denylist-skipped",
        "generation.py",
        (
            "            if self._runtime().filter_final_tools(req,"
            " drop=HOST_DANGEROUS_TOOL_IDS):\n"
            "                return True"
        ),
        "            return True",
        "test_enforce_policy_drop_mode_removes_dangerous",
    ),
    (
        "unknown-no-record",
        "delivery.py",
        "                if sent.status is SendStatus.UNKNOWN:",
        "                if False:",
        "test_deliver_unknown_consumes_state_without_retry",
    ),
    (
        "unconfirmed-writes-history",
        "delivery.py",
        "        state.record_proactive_attempt(confirmed=confirmed, text=text, at=at)",
        "        state.record_proactive_attempt(confirmed=True, text=text, at=at)",
        "test_record_unconfirmed_sets_state_fields",
    ),
    (
        "whitelist-rollback-skipped",
        "whitelist.py",
        "            self.replace(old_whitelist)",
        "            pass",
        "test_add_rolls_back_in_memory_on_persist_failure",
    ),
    (
        "coordinator-cancel-skipped",
        "session_coordinator.py",
        "        self._cancel_delay(umo, force_cancel)",
        "        pass",
        "test_invalidate_cascades_all_resources",
    ),
    (
        "coordinator-clear-keeps-images",
        "session_coordinator.py",
        ("        self._event_at.pop(umo, None)\n        self._images.pop(umo, None)"),
        ("        self._event_at.pop(umo, None)\n        pass"),
        "test_invalidate_cascades_all_resources",
    ),
    (
        "coordinator-clear-keeps-phases",
        "session_coordinator.py",
        ("        self._images.pop(umo, None)\n        self._phases.pop(umo, None)"),
        ("        self._images.pop(umo, None)\n        pass"),
        "test_invalidate_resets_explicit_phase",
    ),
    # ---- 批次5（ticket 11 事件化：静默等待去轮询）----
    (
        "silence-notify-dropped",
        "scheduler.py",
        (
            "        event = self._silence_events.pop(umo, None)\n"
            "        if event is not None:\n"
            "            event.set()"
        ),
        (
            "        event = self._silence_events.pop(umo, None)\n"
            "        if event is not None:\n"
            "            pass"
        ),
        "test_silence_interrupted_aborts_when_session_invalidated",
    ),
    (
        "silence-wake-no-generation-recheck",
        "scheduler.py",
        (
            "                if not self._should_run() or not self._gate.is_current(umo,"
            " generation):\n"
            "                    return\n"
            "                silence_left = self.remaining_silence_sec(state)"
        ),
        (
            "                if False:\n"
            "                    return\n"
            "                silence_left = self.remaining_silence_sec(state)"
        ),
        "test_silence_interrupted_aborts_when_session_invalidated",
    ),
    (
        "silence-wake-no-silence-recheck",
        "scheduler.py",
        (
            "                if not self._should_run() or not self._gate.is_current(umo,"
            " generation):\n"
            "                    return\n"
            "                silence_left = self.remaining_silence_sec(state)"
        ),
        (
            "                if not self._should_run() or not self._gate.is_current(umo,"
            " generation):\n"
            "                    return\n"
            "                silence_left = silence_left"
        ),
        "test_silence_interrupted_restarts_full_silence_cycle",
    ),
    # ---- 批次6（ticket 12 落盘合并：脏标记 + 合并写）----
    (
        "saver-flush-keeps-pending",
        "state_saver.py",
        (
            "        if not self._pending:\n"
            "            return True\n"
            "        self._pending = False\n"
            "        try:\n"
            "            await self._do_save()"
        ),
        (
            "        if not self._pending:\n"
            "            return True\n"
            "        try:\n"
            "            await self._do_save()"
        ),
        "test_flush_pending_zeroes_and_saves",
    ),
    (
        "saver-failure-no-retry",
        "state_saver.py",
        (
            "            self._pending = True\n"
            '            logger.warning("[%s] debounced state save failed: %s", PLUGIN_ID, exc)\n'
            "            self._ensure_retry()\n"
            "            return False"
        ),
        (
            "            self._pending = True\n"
            '            logger.warning("[%s] debounced state save failed: %s", PLUGIN_ID, exc)\n'
            "            return False"
        ),
        "test_flush_failure_keeps_dirty_and_retries",
    ),
    (
        "saver-mark-no-schedule",
        "state_saver.py",
        (
            "        self._pending = True\n"
            "        if self._task is None or self._task.done():\n"
            "            self._task = asyncio.create_task(self._flush_later())"
        ),
        "        self._pending = True",
        "test_auto_flush_after_debounce_window",
    ),
]


def _target_files() -> set[Path]:
    return {ROOT / rel for _, rel, _, _, _ in MUTANTS}


def _acquire_lock(lock_path: Path) -> Path:
    """O_EXCL 独占锁：并发第二次运行直接拒绝；超龄锁视为强杀残留并回收。"""
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        age = time.time() - lock_path.stat().st_mtime
        if age <= STALE_LOCK_SEC:
            print("mutation_check: 另一个实例正在运行（锁文件存在）。", file=sys.stderr)
            sys.exit(2)
        print(f"mutation_check: 移除过期锁文件（{age:.0f}s 前创建，视为强杀残留）")
        lock_path.unlink()
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(fd, f"pid={os.getpid()} started={int(time.time())}\n".encode())
    os.close(fd)
    return lock_path


def _assert_clean_worktree() -> None:
    """目标文件存在未提交改动时拒绝执行。

    变异脚本会临时改写目标文件再恢复；若工作区本就脏，强杀后无法区分
    变异残留与用户改动，copy2 恢复也可能盖上用户未提交的编辑。
    """
    r = subprocess.run(
        ["git", "diff", "--exit-code", "--", *[str(p) for p in sorted(_target_files())]],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if r.returncode == 0:
        return
    # 残留识别：变异串是故意构造的破坏性代码，正常 HEAD 中不应出现；
    # 若目标文件含变异串而锚定串已消失，判定为上次强杀残留。
    suspects = []
    for name, rel, old, new, _ in MUTANTS:
        head = subprocess.run(
            ["git", "show", f"HEAD:{rel}"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        ).stdout
        if new in head:
            continue  # 变异串在 HEAD 中存在则无特征，跳过识别
        src = (ROOT / rel).read_text(encoding="utf-8")
        if old not in src and new in src:
            suspects.append((name, rel))
    print("mutation_check: 前置守卫拒绝——以下目标文件存在未提交改动：", file=sys.stderr)
    for name, rel in suspects:
        hint = f"  [疑似变异残留] {name} → {rel}（可用 git checkout -- {rel} 恢复）"
        print(hint, file=sys.stderr)
    print("  请先 git status 确认改动归属（提交或 stash）后重试。", file=sys.stderr)
    sys.exit(2)


def _assert_anchor_tests_green() -> None:
    """变异前预检：所有锚点测试（各变异的 -k 表达式并集）基线必须全绿。

    基线本红时变异击杀/存活判定会失真（假击杀或假存活），直接拒绝执行。
    """
    seen: list[str] = []
    for _, _, _, _, k in MUTANTS:
        if k not in seen:
            seen.append(k)
    expression = " or ".join(f"({k})" for k in seen)
    r = subprocess.run(
        [PY, "-m", "pytest", "tests/", "-q", "-o", "addopts=", "-k", expression],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    if r.returncode != 0:
        msg = "mutation_check: 锚点测试基线未全绿，拒绝执行变异（先修复测试再跑）。"
        print(msg, file=sys.stderr)
        tail = (r.stdout or "")[-2000:] + (r.stderr or "")[-2000:]
        print(tail, file=sys.stderr)
        sys.exit(2)


def main() -> int:
    lock = _acquire_lock(ROOT / ".mutation_check.lock")
    try:
        _assert_clean_worktree()
        _assert_anchor_tests_green()
        return _run_mutants()
    finally:
        lock.unlink(missing_ok=True)


def _run_mutants() -> int:
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
                encoding="utf-8",
                errors="replace",
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
