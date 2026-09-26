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

- 逐条：锚点在文件内必须**恰好出现一次**（否则 exit 2，绝不静默跳过）→ 基线自检
  → 注入 → 跑目标测试 → **测试必须失败**（失败=捕获=通过）→ ``finally`` 恢复并逐字节校验文件。
- 目标测试意外全绿 → ``MISSED``，exit 1。
- 目标测试挂死（超过单条超时）→ 记为 ``TIMEOUT``，exit 1：不把「不知道」当通过。
- 基线不干净 / 报告缺失或不可解析 / setup·teardown·钩子清理失败 / runner 崩溃
  → ``ERROR``，exit 1：不把「不知道」当捕获。
- ``--list`` 只做锚点自检与清单打印，不跑测试（锚点腐烂可在此提前发现）。

可信度判据（为何不再只看退出码，均为实测）：

- 注入前每个唯一 ``(runner, targets)`` 先跑一次**未变异基线**并缓存；基线必须
  ``rc=0``、报告含 >=1 个真实目标用例、``failures=0``、``errors=0``，否则该变异
  直接 ERROR 且不得注入。没有这道闸，「目标测试本来就挂」会被任何变异记成 CAUGHT。
- ``node --test`` 把文件加载失败（语法错误 / import 抛错）报成
  ``<failure type="testCodeFailure">`` 且 ``rc=1``；before/after hook 失败则报成
  ``<failure type="hookFailed">``。只按 ``rc==1`` 判捕获，等于把「目标根本跑不起来」
  或钩子失败当捕获。
- 空 ``.mjs`` 被 node 记成 1 个 pass（实测），所以基线必须核查**真实用例数**。
- ``pytest`` 的 teardown/setup 失败在 JUnit 里是 ``errors=1, failures=0``（实测），
  旧判据只看 ``rc!=0``，会把它算捕获。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]

# 单条变异的测试超时：目标测试子集都是秒级（实测最长 ~6s），120s 足够区分
# 「注入导致挂死」与「机器慢」。超时按未通过处理，不静默放行。
PER_MUTATION_TIMEOUT_SEC = 120.0

# node --test junit reporter 用 failure/@type 区分真实用例断言与钩子失败
# （实测 24.15.0：hook 失败为 hookFailed，message 是自由文本）。
NODE_CODE_FAILURE = "testCodeFailure"
JS_SUFFIXES = (".mjs", ".cjs", ".js")

# 基线缓存：``(runner, targets)`` -> ``None``（干净）或失败原因。
# 只在本进程内有效（不进盘、不跨次运行），每次 ``main()`` 前清空。
_BASELINES: dict[tuple[str, tuple[str, ...]], str | None] = {}


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
    Mutation(
        key="runtime_require_stops_raising",
        rel="runtime_adapter.py",
        anchor="    if value is None:\n"
        '        raise RuntimeError(f"当前 AstrBot 缺少主动回复所需的 {name}")\n',
        replacement="    if value is None:\n        return None\n",
        contract="§10",
        note="宿主符号缺失不再 fail closed：None 漏进宿主调用，在更深处以难诊断的形态崩溃",
        targets=("tests/test_runtime_adapter_blindspots.py",),
    ),
    Mutation(
        key="source_contract_ambiguous_lookup_silent",
        rel="tests/source_contract.py",
        anchor="    raise AssertionError(\n"
        '        f"{rel} 中 {qualname!r} 有多处同名定义，请写限定名：'
        '{sorted(tail_matches)}"\n'
        "    )\n",
        replacement="    return scopes[tail_matches[0]]\n",
        contract="元设施",
        note="同名歧义不再 raise：单源守卫可能断言到另一处同名定义上，收敛点唯一变恒真",
        targets=("tests/test_source_contract_self_guard.py",),
    ),
    Mutation(
        key="name_references_name_only",
        rel="tests/test_single_source_anchors.py",
        anchor="        elif isinstance(node, ast.Attribute) and node.attr == target:\n"
        '            hits.append(f"line {node.lineno}: {ast.unparse(node)[:60]}")\n'
        "        elif isinstance(node, ast.alias) and node.name == target:\n"
        '            hits.append(f"line {node.lineno}: import {node.name}")\n',
        replacement='        elif node.__class__.__name__ == "_NeverMatchAttribute":\n'
        '            hits.append(f"line {node.lineno}: unreachable")\n'
        '        elif node.__class__.__name__ == "_NeverMatchAlias":\n'
        '            hits.append(f"line {node.lineno}: unreachable")\n',
        contract="元设施",
        note="_name_references 退化为只匹配 ast.Name：属性/getattr/import 别名三种绕过写法全部失效",
        targets=("tests/test_source_contract_self_guard.py",),
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


def _command(mutation: Mutation, *, tmp_root: Path, report: Path, phase: str) -> list[str]:
    """构造该次运行的命令（runner 无关），pytest 额外绑定新建的 basetemp。"""
    if mutation.runner == "node":
        return [
            "node",
            "--test",
            "--test-reporter=junit",
            f"--test-reporter-destination={report}",
            *mutation.targets,
        ]
    basetemp = tmp_root / f"basetemp-{phase}"
    basetemp.mkdir(parents=True, exist_ok=True)
    return [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-x",
        "-p",
        "no:cacheprovider",
        "--no-header",
        f"--basetemp={basetemp}",
        f"--junitxml={report}",
        *mutation.targets,
    ]


def _run_env(tmp_root: Path) -> dict[str, str]:
    """该次运行的 env：TEMP/TMP 指向同一专属根；不借 PYTEST_ADDOPTS。"""
    root = str(tmp_root)
    env = dict(os.environ, TEMP=root, TMP=root, PYTHONDONTWRITEBYTECODE="1")
    # 外部遗留的这两项会让显式 --basetemp 与超时判据失效，必须剔除。
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTEST_DEBUG_TEMPROOT", None)
    return env


def _scratch_root() -> Path:
    """本次 main() 的专属临时根：报告 / basetemp / TEMP+TMP 全部落在其下。

    不硬编码盘符或路径（``tempfile.mkdtemp``），也不落在仓库内；每次运行新建，
    在 ``finally`` 里清理。
    """
    return Path(tempfile.mkdtemp(prefix="mutation-gate-"))


def _cleanup_scratch(root: Path) -> str | None:
    """清理自有临时根；失败返回原因（调用方据此落 ERROR，不静默）。"""
    try:
        shutil.rmtree(root)
    except OSError as exc:
        return f"清理临时根失败: {exc}"
    return None


def _is_file_level(case: ET.Element) -> bool:
    """node 文件级失败：``name`` 即被测文件路径，说明真实用例一个都没跑起来。"""
    return (case.get("name") or "").endswith(JS_SUFFIXES)


def _node_real_code_failure(case: ET.Element) -> str | None:
    """真实用例的 ``testCodeFailure`` 消息；文件级 / 钩子失败返回 ``None``。"""
    if _is_file_level(case):
        return None
    failure = case.find("failure")
    if failure is None:
        return None
    if (failure.get("type") or "") != NODE_CODE_FAILURE:
        return None
    return failure.get("message") or ""


def _report_summary(root: ET.Element, *, runner: str) -> dict:
    """把 JUnit 根压成纯数据摘要（表驱动判定用，无副作用）。"""
    cases = list(root.iter("testcase"))
    suites = list(root.iter("testsuite"))
    counts = {
        key: sum(int(suite.get(key) or 0) for suite in suites)
        for key in ("tests", "failures", "errors")
    }
    return {
        "runner": runner,
        "reported": counts,
        "pytest_failures": sum(1 for case in cases if case.find("failure") is not None),
        "pytest_errors": sum(1 for case in cases if case.find("error") is not None),
        "node_code_failures": [
            msg for case in cases if (msg := _node_real_code_failure(case)) is not None
        ],
        "node_real_cases": sum(1 for case in cases if not _is_file_level(case)),
    }


def _baseline_ok(summary: dict, *, rc: int) -> bool:
    """基线判据：干净，且报告里真有目标用例在跑。"""
    if rc != 0:
        return False
    reported = summary["reported"]
    if reported["failures"] or reported["errors"]:
        return False
    if summary["runner"] == "node":
        # 空 .mjs 被 node 记成 1 pass（实测），所以「有 tests」不等于「有用例」。
        return summary["node_real_cases"] > 0
    return reported["tests"] > 0


def _baseline_reason(summary: dict, rc: int) -> str:
    reported = summary["reported"]
    if rc != 0:
        return f"基线未变异即失败（rc={rc}）"
    if reported["failures"]:
        return "基线报告 failures>0"
    if reported["errors"]:
        return "基线报告 errors>0"
    return "基线报告没有真实目标用例"


def _is_pytest_caught(root: ET.Element) -> bool:
    """pytest：failures>0 且 errors==0 才算捕获（teardown/setup 失败是 error）。"""
    summary = _report_summary(root, runner="pytest")
    reported = summary["reported"]
    if reported["errors"]:
        return False
    if reported["failures"] != summary["pytest_failures"]:
        return False
    return reported["failures"] > 0 and summary["pytest_failures"] > 0


def _is_node_caught(root: ET.Element) -> bool:
    """node：至少一个真实用例的 ``testCodeFailure``（文件级 / 钩子失败都不算）。"""
    return bool(_report_summary(root, runner="node")["node_code_failures"])


def _read_report(path: Path) -> ET.Element | None:
    """读并解析 JUnit 报告；缺失或不可解析一律 ``None``（调用方落 ERROR）。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        return None


def _node_invocation(mutation: Mutation, scratch: Path, phase: str) -> tuple[list[str], Path]:
    """node 的命令与报告路径；报告落在专属 scratch 根下。"""
    report = scratch / f"junit-node-{phase}.xml"
    argv = [
        "node",
        "--test",
        "--test-reporter=junit",
        f"--test-reporter-destination={report}",
        *mutation.targets,
    ]
    return argv, report


def _pytest_invocation(
    mutation: Mutation, scratch: Path, phase: str
) -> tuple[list[str], Path, Path]:
    """pytest 的命令、报告路径与专属临时根；basetemp 由 ``_command`` 负责 mkdir。"""
    report = scratch / f"junit-{phase}.xml"
    tmp_root = scratch / f"run-{phase}"
    argv = _command(mutation, tmp_root=tmp_root, report=report, phase=phase)
    return argv, report, tmp_root


def _invocation(mutation: Mutation, scratch: Path, phase: str) -> tuple[list[str], Path, Path]:
    if mutation.runner == "node":
        argv, report = _node_invocation(mutation, scratch, phase)
        return argv, report, scratch
    return _pytest_invocation(mutation, scratch, phase)


def _execute(argv: list[str], tmp_root: Path) -> tuple[int | None, float, str]:
    """跑一次测试进程；返回 ``(rc, 秒数, 末行输出)``，超时 rc 为 ``None``。"""
    started = time.monotonic()
    try:
        done = subprocess.run(
            argv,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PER_MUTATION_TIMEOUT_SEC,
            env=_run_env(tmp_root),
        )
    except subprocess.TimeoutExpired:
        return None, time.monotonic() - started, f">{PER_MUTATION_TIMEOUT_SEC:.0f}s 未退出"
    output = (done.stdout or "") + (done.stderr or "")
    last_line = next((line for line in reversed(output.splitlines()) if line.strip()), "")
    return done.returncode, time.monotonic() - started, last_line.strip()[:100]


def _node_reject_reason(root: ET.Element, summary: dict) -> str:
    """node 未捕获的具体原因（hook / 文件级 / 其他），供人读。"""
    hooks = [
        failure.get("message") or "hook failure"
        for case in root.iter("testcase")
        if (failure := case.find("failure")) is not None and failure.get("type") == "hookFailed"
    ]
    if hooks:
        return f"钩子失败而非目标用例失败（{hooks[0]}）"
    if summary["reported"]["errors"]:
        return "运行器报错（文件级失败）"
    return "无真实 testCodeFailure"


def _finalize(
    mutation: Mutation, rc: int | None, elapsed: float, detail: str, report: Path
) -> tuple[str, float, str]:
    """把一次运行的结果按**报告实体**判定为状态（退出码只用于区分 MISSED）。"""
    if rc is None:
        return "TIMEOUT", elapsed, detail
    root = _read_report(report)
    if root is None:
        return "ERROR(no-report)", elapsed, "报告缺失或不可解析"
    summary = _report_summary(root, runner=mutation.runner)
    if rc == 0:
        return "MISSED", elapsed, detail
    if mutation.runner == "node":
        if _is_node_caught(root):
            return "CAUGHT", elapsed, detail
        return f"ERROR({rc})", elapsed, _node_reject_reason(root, summary)
    if _is_pytest_caught(root):
        return "CAUGHT", elapsed, detail
    reported = summary["reported"]
    if reported["errors"]:
        return f"ERROR({rc})", elapsed, "报告含 error（setup/teardown/清理失败）"
    return f"ERROR({rc})", elapsed, "报告无失败用例"


def _run_baseline(mutation: Mutation, scratch: Path) -> str | None:
    """注入前在**未变异**源码上跑一次基线；通过返回 ``None``，否则返回失败原因。

    失败的变异不得注入：目标测试本来就挂时，任何变异都会被记成 CAUGHT（假绿）。
    """
    argv, report, tmp_root = _invocation(mutation, scratch, "baseline")
    rc, _elapsed, _detail = _execute(argv, tmp_root)
    problem: str | None = None
    if rc is None:
        problem = f"基线超时（>{PER_MUTATION_TIMEOUT_SEC:.0f}s）"
    else:
        root = _read_report(report)
        if root is None:
            problem = "基线报告缺失或不可解析"
        else:
            summary = _report_summary(root, runner=mutation.runner)
            if not _baseline_ok(summary, rc=rc):
                problem = _baseline_reason(summary, rc)
    _BASELINES[(mutation.runner, mutation.targets)] = problem
    return problem


def _ensure_baseline(mutation: Mutation, scratch: Path) -> str | None:
    """带缓存的基线：同一 ``(runner, targets)`` 在本次 main() 内只跑一次。"""
    key = (mutation.runner, mutation.targets)
    if key in _BASELINES:
        return _BASELINES[key]
    return _run_baseline(mutation, scratch)


def _run_targets(mutation: Mutation) -> tuple[str, float, str]:
    """跑目标测试；返回 ``(状态, 秒数, 末行输出)``。

    自己负责建基线（``_ensure_baseline`` 带缓存），因此测试可以单独调用它。
    ``main()`` 里基线排在改写源文件之前，这里再取一次只是命中缓存。
    """
    # 目标文件缺失时绝不能算捕获：``node --test`` 对「找不到文件」返回 1，与
    # 「测试失败」同码，仅按退出码判定会把「目标测试被删掉/改名」记成 CAUGHT
    # （正是本门禁要防的假绿灯）。pytest 对同一情况返回 4（落 ERROR，fail closed），
    # 但没必要继续依赖各运行器的退出码语义——前置检查让两者一致 fail closed。
    missing = [target for target in mutation.targets if not (ROOT / target).exists()]
    if missing:
        return "ERROR(no-target)", 0.0, f"目标不存在: {', '.join(missing)}"

    scratch = _scratch_root()
    status = "ERROR(internal)"
    elapsed = 0.0
    final_detail = ""
    try:
        baseline = _ensure_baseline(mutation, scratch)
        if baseline is not None:
            status, final_detail = "ERROR(baseline)", baseline
        else:
            argv, report, tmp_root = _invocation(mutation, scratch, "run")
            rc, elapsed, detail = _execute(argv, tmp_root)
            status, _elapsed, final_detail = _finalize(mutation, rc, elapsed, detail, report)
    finally:
        # 清理失败同样是不确定状态：不能静默降级成 CAUGHT/MISSED。
        problem = _cleanup_scratch(scratch)
        if problem:
            print(f"WARN: {problem}", file=sys.stderr)
            status, final_detail = "ERROR(cleanup)", problem
    return status, elapsed, final_detail


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

    _BASELINES.clear()
    failures: list[str] = []
    print(f"==> 变异门禁：{len(selected)} 条（每条必须让目标测试失败）")
    for mutation in selected:
        # 基线必须在注入前的干净源码上跑（缓存按 (runner, targets) 去重），
        # 所以基线排在改写文件之前。
        scratch = _scratch_root()
        try:
            baseline = _ensure_baseline(mutation, scratch)
        finally:
            cleanup_problem = _cleanup_scratch(scratch)
        if cleanup_problem is not None:
            label = "ERROR(cleanup)"
            print(f"  [FAIL] {label:<7} {0.0:5.1f}s {mutation.key} ({mutation.contract})")
            print(f"         {cleanup_problem}")
            failures.append(f"{mutation.key}={label}")
            continue
        if baseline is not None:
            label = "ERROR(baseline)"
            print(f"  [FAIL] {label:<7} {0.0:5.1f}s {mutation.key} ({mutation.contract})")
            print(f"         {baseline}")
            failures.append(f"{mutation.key}={label}")
            continue

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
