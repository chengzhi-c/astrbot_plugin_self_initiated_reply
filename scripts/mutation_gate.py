"""变异门禁：把「新测试必须实测能捕获目标缺陷」从手工纪律变成可执行断言。

动机：本仓库的承重不变量靠测试守住，而「测试本身是否真能捕获目标缺陷」此前只有一条
人工纪律（``docs/BEHAVIOR_CONTRACT.md`` 开头：先把被测逻辑改坏、确认该测试变红、再恢复）。
实测这条纪律会漏：17 条承重变异里有 6 条被现有门禁放行，其中 4 条是真实缺口
（闸门判定顺序、UNKNOWN 的代次门、重定向上限、前端 config_revision 格式校验）。
本脚本把该纪律自动化——每条变异都**必须**让指定测试失败。

准入判据（新增条目必须满足其一，并在 ``note`` 里写明理由）：

1. 破坏 ``docs/BEHAVIOR_CONTRACT.md`` 的某条具名不变量（``contract`` 写 §编号）；
2. 已发生过的 P0/P1 缺陷复现形态；
3. 关闭某条 fail-closed / 安全边界。

不得入表：纯性能调参、双层防护中的冗余层、行为等价的写法替换。实测反例两条，勿回收：
放宽图片端口白名单（传输层 ``_FixedAddressTransport`` 会二次拦截，行为等价）、
静默等待余量 ``+0.1s`` 改 ``0.001s``（只把一次等待拆成多次轮询，语义不变）。

语义与退出码：

- 逐条：锚点在文件内必须**恰好出现一次**（否则 exit 2，绝不静默跳过）→ 注入 →
  跑目标测试 → **测试必须失败**（失败=捕获=通过）→ ``finally`` 恢复并逐字节校验文件。
- 目标测试意外全绿 → ``MISSED``，exit 1。
- 目标测试挂死（超过单条超时）→ 记为 ``TIMEOUT``，exit 1：不把「不知道」当通过。
- ``--list`` 只做锚点自检与清单打印，不跑测试（锚点腐烂可在此提前发现）。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]

# 单条变异的测试超时：目标测试子集都是秒级（实测最长 ~6s），120s 足够区分
# 「注入导致挂死」与「机器慢」。超时按未通过处理，不静默放行。
PER_MUTATION_TIMEOUT_SEC = 120.0


class Mutation(NamedTuple):
    """一条变异：把 ``anchor`` 换成 ``replacement``，指定测试必须失败。"""

    key: str
    rel: str
    anchor: str
    replacement: str
    contract: str
    note: str
    targets: tuple[str, ...] = ()
    runner: str = "pytest"


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        key="container_identity_rollback",
        rel="webapi.py",
        anchor=(
            "    restore_container_inplace(plugin._whitelist_runtime_umos,"
            ' snapshot["whitelist_runtime_umos"])\n'
        ),
        replacement=(
            '    plugin._whitelist_runtime_umos = dict(snapshot["whitelist_runtime_umos"])\n'
        ),
        contract="§11 B1",
        note="回滚改属性重绑定：scheduler/whitelist continue 写孤儿容器，会话静默停止",
        targets=("tests/test_config_hot_reload.py",),
    ),
    Mutation(
        key="stale_recheck_after_decorating_hook",
        rel="delivery.py",
        anchor="            if not self._gate.is_current(umo, expected_generation):\n"
        "                self._clear_result(last_event)\n"
        "                logger.info(\n"
        '                    "[%s] suppress stale reply after decorating hook '
        'ledger_id=%s session=%s",\n',
        replacement="            if False:\n"
        "                self._clear_result(last_event)\n"
        "                logger.info(\n"
        '                    "[%s] suppress stale reply after decorating hook '
        'ledger_id=%s session=%s",\n',
        contract="§1 / §4",
        note="删掉装饰钩子之后的代次复核（唯一真实竞态窗口）",
        targets=("tests/test_delivery_blindspots.py",),
    ),
    Mutation(
        key="tool_boundary_fail_open",
        rel="runtime_adapter.py",
        anchor="        if tool_ids is None:\n",
        replacement="        if tool_ids is None and False:\n",
        contract="§3",
        note="工具集不可枚举时不再 fail closed（带病运行）",
        targets=("tests/test_runtime_adapter_blindspots.py",),
    ),
    Mutation(
        key="envelope_neutralization_removed",
        rel="generation.py",
        anchor="    safe_context = neutralize_envelope_tags(context_text)\n",
        replacement="    safe_context = context_text\n",
        contract="§7 提示词注入",
        note="用户内容里的 </recent_chat> 可提前闭合信封，与其后指令同层级",
        targets=("tests/test_generation_runner.py",),
    ),
    Mutation(
        key="local_image_allowlist_removed",
        rel="image/parser.py",
        anchor="            if not trusted and not any(\n"
        "                candidate == root or root in candidate.parents "
        "for root in self._allowed_local_roots\n"
        "            ):\n",
        replacement="            if False:\n",
        contract="§7.1",
        note="本地图片放行的唯一判据被去掉（宿主 OneBot 的 file 值对端可控）",
        targets=("tests/test_security.py",),
    ),
    Mutation(
        key="whitelist_rollback_state_restore",
        rel="whitelist.py",
        anchor="            self._sessions.update(pruned)\n",
        replacement="            pass\n",
        contract="§11 B2",
        note="双写失败回滚时不还原被 prune 的会话状态（配额与冷却被静默清零）",
        targets=("tests/test_whitelist_manager.py",),
    ),
    Mutation(
        key="config_unknown_keys_ignored",
        rel="webapi.py",
        anchor="    unknown = sorted(set(data) - CONFIG_SCHEMA_KEYS)\n"
        "    if unknown:\n"
        "        raise ValueError(f\"未知配置键: {', '.join(unknown)}\")\n",
        replacement="    unknown = sorted(set(data) - CONFIG_SCHEMA_KEYS)\n",
        contract="§7",
        note="未知配置键静默吞掉（面板上改得到、保存成功、值不生效）",
        targets=("tests/test_webapi_fixes.py",),
    ),
    Mutation(
        key="per_hop_dns_rebind",
        rel="image/parser.py",
        anchor="        self._address = None\n",
        replacement="",
        contract="§8",
        note="一次性地址不再消费即清空：重定向后的每跳不再重新解析并校验",
        targets=("tests/test_vision_parser_gaps.py",),
    ),
    Mutation(
        key="direct_send_budget_after_submit",
        rel="outbound.py",
        anchor="            self._direct_send_count += 1\n",
        replacement="            pass\n",
        contract="§2",
        note="直发预算不在调用适配器之前扣（提交后抛异常即可能重复发送）",
        targets=("tests/test_outbound.py",),
    ),
    Mutation(
        key="image_mime_trusts_header",
        rel="image/parser.py",
        anchor="                    content_type = sniff_image_mime(bytes(content))\n"
        "                    if not content_type:\n"
        "                        return None\n",
        replacement="                    content_type = response.headers.get("
        '"content-type", "image/jpeg").split(";")[0]\n'
        "                    if not content_type:\n"
        "                        return None\n",
        contract="§8",
        note="MIME 改信响应头而非载荷嗅探（非图片字节可被编码后外发）",
        targets=("tests/test_vision_parser_gaps.py",),
    ),
    Mutation(
        key="context_keep_head",
        rel="utils.py",
        anchor="    for line in reversed(lines):\n",
        replacement="    for line in lines:\n",
        contract="§14",
        note="上下文预算裁剪改保头（丢掉最新若干条，与提示词要求相反）",
        targets=("tests/test_decision_maker.py",),
    ),
    Mutation(
        key="gate_order_cooldown_before_silence",
        rel="decision.py",
        anchor="        silence_left = state.remaining_silence_sec(\n"
        "            self.settings.min_silence_sec, self._clock(), active_at=active_for_silence\n"
        "        )\n"
        "        if silence_left > 0:\n",
        replacement="        cooldown_left = self.settings.cooldown_sec - "
        "(self._clock() - state.last_proactive_at)\n"
        "        if cooldown_left >= 1:\n"
        '            return f"冷却中：还剩 {duration(cooldown_left)}。"\n'
        "        silence_left = state.remaining_silence_sec(\n"
        "            self.settings.min_silence_sec, self._clock(), active_at=active_for_silence\n"
        "        )\n"
        "        if silence_left > 0:\n",
        contract="§4",
        note="局部闸门判定顺序改把冷却提到静默之前（归因文案指向未参与判定的项）",
        targets=("tests/test_decision_maker.py",),
    ),
    Mutation(
        key="unknown_stale_generation_window",
        rel="delivery.py",
        anchor="        if not confirmed:\n"
        "            # UNKNOWN may have been delivered: advance the observed window so a\n"
        "            # later patrol does not regenerate a reply for the same event.\n"
        "            if self._gate.is_current(umo, expected_generation):\n"
        "                state.last_proactive_observed_at = (\n"
        "                    state.last_active_at if observed_active_at is None "
        "else observed_active_at\n"
        "                )\n",
        replacement="        if not confirmed:\n"
        "            # UNKNOWN may have been delivered: advance the observed window so a\n"
        "            # later patrol does not regenerate a reply for the same event.\n"
        "            state.last_proactive_observed_at = (\n"
        "                state.last_active_at if observed_active_at is None "
        "else observed_active_at\n"
        "            )\n",
        contract="§2",
        note="UNKNOWN 不再受代次门约束：旧事件的未确认提交会推进新会话的观察窗口",
        targets=("tests/test_delivery_runner.py",),
    ),
    Mutation(
        key="redirect_cap_relaxed",
        rel="image/parser.py",
        anchor="                max_redirects=3,\n",
        replacement="                max_redirects=20,\n",
        contract="§8",
        note="重定向跟随上限从 3 放宽到 20（每跳都重解析，放宽=放大 SSRF 跳数）",
        targets=("tests/test_vision_parser_gaps.py",),
    ),
    Mutation(
        key="status_recent_decision_line_removed",
        rel="commands.py",
        anchor="            recent_decision_line(last_decision),\n",
        replacement="",
        contract="§13",
        note="聊天窗口不再呈现最近裁决（README 排障指引变成空指向，与 GET /status 脱节）",
        targets=("tests/test_main_runtime.py",),
    ),
    Mutation(
        key="frontend_revision_format_relaxed",
        rel="pages/主动回复设置/frontend-core.mjs",
        anchor='    /^sha256:[0-9a-f]{64}$/.test(config.config_revision || "") &&\n',
        replacement='    typeof config.config_revision === "string" &&\n',
        contract="§7 面板 CAS",
        note="面板 config_revision 格式校验放宽：空串/短串也能过，保存时的乐观并发控制静默失效",
        targets=("tests/frontend_contract.test.mjs",),
        runner="node",
    ),
)


def _read(path: Path) -> str:
    """按文本读取（统一换行为 ``\\n``，锚点按 LF 书写）。"""
    return path.read_text(encoding="utf-8")


def _write_like(path: Path, text: str, original: bytes) -> None:
    """以原文件的换行风格写回，保证恢复后与原文件逐字节一致。

    ``Path.write_text`` 在 Windows 会把 ``\\n`` 全部展开成 CRLF；原文件若是 LF
    就会留下整文件行尾 diff（症状隐蔽，只有 git status 会说话）。这里显式继承原风格，
    并在调用方用字节哈希复核。
    """
    payload = text.replace("\n", "\r\n") if b"\r\n" in original else text
    path.write_bytes(payload.encode("utf-8"))


def _clear_pycache(rel: str) -> None:
    """删掉被变异模块的字节码缓存，排除「stale .pyc 命中」导致的假 MISSED。"""
    pycache = (ROOT / rel).parent / "__pycache__"
    if pycache.is_dir():
        shutil.rmtree(pycache, ignore_errors=True)


def _command(mutation: Mutation) -> list[str]:
    if mutation.runner == "node":
        return ["node", "--test", *mutation.targets]
    return [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-x",
        "-p",
        "no:cacheprovider",
        "--no-header",
        *mutation.targets,
    ]


def _run_targets(mutation: Mutation) -> tuple[str, float, str]:
    """跑目标测试；返回 ``(状态, 秒数, 末行输出)``。"""
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    started = time.monotonic()
    try:
        done = subprocess.run(
            _command(mutation),
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PER_MUTATION_TIMEOUT_SEC,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return "TIMEOUT", time.monotonic() - started, f">{PER_MUTATION_TIMEOUT_SEC:.0f}s 未退出"
    elapsed = time.monotonic() - started
    output = (done.stdout or "") + (done.stderr or "")
    last_line = next((line for line in reversed(output.splitlines()) if line.strip()), "")
    # 只有「测试真的失败」才算捕获。pytest 的 2/3/4/5 是中断/内部错误/用法错误/
    # 没收集到用例，node 的非 1 退出码是运行器自身问题——把它们当捕获，会让门禁
    # 在目标测试被删掉/改名时假绿（这正是本门禁要防的假绿灯）。
    if done.returncode == 0:
        status = "MISSED"
    elif done.returncode == 1:
        status = "CAUGHT"
    else:
        status = f"ERROR({done.returncode})"
    return status, elapsed, last_line.strip()[:100]


def _anchor_problems(selected: tuple[Mutation, ...]) -> list[str]:
    problems: list[str] = []
    for mutation in selected:
        source = _read(ROOT / mutation.rel)
        count = source.count(mutation.anchor)
        if count != 1:
            problems.append(
                f"{mutation.key}: 锚点在 {mutation.rel} 出现 {count} 次（必须恰好 1 次）"
            )
    return problems


def _print_table(selected: tuple[Mutation, ...]) -> None:
    print(f"{'key':<38}{'contract':<12}{'runner':<8}targets")
    for mutation in selected:
        targets = ", ".join(Path(t).name for t in mutation.targets)
        print(f"{mutation.key:<38}{mutation.contract:<12}{mutation.runner:<8}{targets}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="变异门禁：每条变异必须让目标测试失败")
    parser.add_argument("--list", action="store_true", help="只做锚点自检并打印清单")
    parser.add_argument("--only", action="append", default=[], help="只跑指定 key（可重复）")
    args = parser.parse_args(argv)

    known = {mutation.key for mutation in MUTATIONS}
    unknown = sorted(set(args.only) - known)
    if unknown:
        print(f"FAIL: 未知变异 key: {', '.join(unknown)}", file=sys.stderr)
        return 2
    selected = tuple(m for m in MUTATIONS if not args.only or m.key in set(args.only))

    problems = _anchor_problems(selected)
    if problems:
        print("FAIL: 锚点自检未通过（变异表与源码已脱节）", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 2
    if args.list:
        _print_table(selected)
        print(f"OK: {len(selected)} 条变异锚点自检通过")
        return 0

    failures: list[str] = []
    print(f"==> 变异门禁：{len(selected)} 条（每条必须让目标测试失败）")
    for mutation in selected:
        path = ROOT / mutation.rel
        original_bytes = path.read_bytes()
        original_text = _read(path)
        digest = hashlib.sha256(original_bytes).hexdigest()
        _write_like(
            path, original_text.replace(mutation.anchor, mutation.replacement), original_bytes
        )
        _clear_pycache(mutation.rel)
        try:
            status, elapsed, detail = _run_targets(mutation)
        finally:
            _write_like(path, original_text, original_bytes)
            _clear_pycache(mutation.rel)
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            print(f"FAIL: {mutation.rel} 恢复后与原文件不一致", file=sys.stderr)
            return 3
        mark = "ok  " if status == "CAUGHT" else "FAIL"
        print(f"  [{mark}] {status:<7} {elapsed:5.1f}s {mutation.key} ({mutation.contract})")
        if detail and status != "CAUGHT":
            print(f"         {detail}")
        if status != "CAUGHT":
            failures.append(f"{mutation.key}={status}")

    if failures:
        print(f"\nFAIL: 以下变异未被捕获，说明对应测试已失去捕获力：{', '.join(failures)}")
        return 1
    print(f"\nOK: {len(selected)} 条变异全部被目标测试捕获，且源码已逐字节恢复")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
