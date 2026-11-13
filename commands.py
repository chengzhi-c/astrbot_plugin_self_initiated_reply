"""指令文本解析、回显拼装，以及写动作分派。

解析/帮助/状态/列表/调试为纯函数。``dispatch_command_action`` 承载 add/remove/
check/on/off 等有副作用分支，经 plugin 回调访问状态（测试可替换实例方法）。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from astrbot.api.event import AstrMessageEvent

from .models import (
    MAX_WHITELIST_SIZE,
    CheckTrigger,
    SessionState,
    Settings,
    fmt_ts,
    now_ts,
    today_key,
)
from .plugin_state import append_recent_user_message, apply_enabled_topology, read_session_state
from .utils import (
    clean_chat_text,
    collapse_whitespace,
    event_group_id,
    event_self_id,
    event_sender_id,
    event_text,
    event_umo,
    is_at_or_wake_command_event,
    is_explicit_direct_call,
    raw_umo,
    session_whitelisted,
    strip_leading_mentions,
)

# 手动检查前等待上一轮检查释放运行标记的预算。取消是异步投递的，正常情况
# 下一个事件循环轮次即释放；预算只防「旧任务卡在不可取消的步骤里」，超时后
# 照旧走既有「已有判断任务在运行」文案，不无限挂住指令回执。
# 等待骨架与调度路径共用（SessionGate.await_release），故总预算按单步轮询
# 折算成轮数：改单步或总预算都由这一句推出，不存在第二处手写份数。
_MANUAL_CHECK_RELEASE_WAIT_SEC = 2.0
_MANUAL_CHECK_RELEASE_POLL_SEC = 0.05
_MANUAL_CHECK_RELEASE_ROUNDS = max(
    1, round(_MANUAL_CHECK_RELEASE_WAIT_SEC / _MANUAL_CHECK_RELEASE_POLL_SEC)
)

if TYPE_CHECKING:
    from .main import SelfInitiatedReplyPlugin

COMMAND_ALIASES: dict[str, set[str]] = {
    "help": {"help", "h"},
    "status": {"status", "stat"},
    "add": {"add"},
    "remove": {"remove", "rm", "del", "delete"},
    "list": {"list", "ls", "whitelist"},
    "check": {"check", "test"},
    "on": {"on", "enable", "start"},
    "off": {"off", "disable", "pause", "stop"},
    "debug": {"debug", "diag", "diagnose"},
}


def parse_command_text(text: str) -> tuple[str, str] | None:
    raw = strip_leading_mentions(str(text or "")).strip()
    if not raw:
        return None
    if raw.startswith("/"):
        raw = raw[1:].lstrip()
    lowered = raw.lower()
    if lowered == "selfreply":
        return "help", ""
    # 分隔符取任意空白：只认 ASCII 空格时 `/selfreply\tadd` 会被判成"不是指令"，
    # 而宿主装饰器路径的 tokenizer 仍会匹配它，两条出口给出相反答案。
    if not re.match(r"selfreply\s", lowered):
        return None
    body = raw[len("selfreply") :].strip()
    parts = body.split(maxsplit=1)
    token = parts[0].strip().lower()
    rest = parts[1].strip() if len(parts) > 1 else ""
    for action, names in COMMAND_ALIASES.items():
        if token in names:
            return action, rest
    return None


def strip_command_prefix(text: str) -> str:
    """取指令正文（``/selfreply check 你好`` → ``你好``）；非指令文本原样返回。

    装饰器路径 ``main.selfreply_check`` 恒传 ``arg=""``，用户附带的测试内容
    只能由本函数从事件原文取出；删掉它会让装饰器路径的 check 静默丢失内容。
    """
    parsed = parse_command_text(text)
    return parsed[1] if parsed else text


def _alias_help_line() -> str:
    """别名说明从调度表派生：给动作加别名而忘了同步文案会静默失效，
    装饰器侧的 ``alias=`` 与本表的等价由 test_command_aliases_single_source 钉住。"""
    groups = [
        f"{action}/{'/'.join(sorted(names - {action}))}"
        for action, names in COMMAND_ALIASES.items()
        if names - {action}
    ]
    return "可用英文别名：" + "、".join(groups) + "。"


def help_text() -> str:
    return "\n".join(
        [
            "主动回复指令组：/selfreply",
            "/selfreply status: 查看运行状态、判断模型、白名单、冷却和今日次数（管理员）",
            "/selfreply add: 将当前会话加入主动回复白名单（管理员）",
            "/selfreply remove: 将当前会话移出主动回复白名单（管理员）",
            "/selfreply list: 查看当前主动回复白名单（管理员）",
            "/selfreply check [content]: 手动测试一次主动回复；可附带测试内容（管理员）",
            "/selfreply on: 启用主动回复，重启后保持（管理员）",
            "/selfreply off: 暂停主动回复，重启后保持（管理员）",
            "/selfreply debug: 查看当前会话、发送者与识别信息（管理员）",
            _alias_help_line(),
            "也支持 @Bot selfreply <动作>（无需斜杠）。",
        ]
    )


def list_text(settings: Settings) -> str:
    if not settings.whitelist:
        return "主动回复白名单为空。"
    return "主动回复白名单：\n" + "\n".join(f"- {item}" for item in sorted(settings.whitelist))


# 最近裁决一行的原因截断长度。reason 的长度不因单点上界而可依赖（模型异常
# 路径是脱敏原文拼接，且会引用用户原文），单行回显必须自带上限。
_RECENT_DECISION_REASON_MAX = 60


def recent_decision_line(decision: dict[str, Any] | None) -> str:
    """把本会话最近一次裁决渲染成一行（与 ``GET /status`` 的 ``last_decisions`` 同源）。"""
    if not decision:
        return "最近裁决: 暂无记录"
    verdict = "接话" if decision.get("should_reply") else "不接话"
    # reason 可能是多行，先折成单行再截断，保证回显行数稳定。
    reason = collapse_whitespace(decision.get("reason") or "未说明")
    if len(reason) > _RECENT_DECISION_REASON_MAX:
        reason = reason[: _RECENT_DECISION_REASON_MAX - 1] + "…"
    return f"最近裁决: {fmt_ts(decision.get('at'))} {verdict} · {reason}"


def status_text(
    settings: Settings,
    event: AstrMessageEvent,
    state: SessionState,
    runtime_enabled: bool,
    lifecycle: str,
    last_decision: dict[str, Any] | None,
    effective_message_delay_sec: int,
) -> str:
    """渲染 /selfreply status 文本。

    ``lifecycle`` 与 ``last_decision`` 必填且无默认值：降级是单向门，
    ``runtime_enabled`` 读持久配置仍为 True，只有 lifecycle 能说明插件实际
    已拒绝一切新工作；给默认值会让新调用点静默回落 "RUNNING" 谎报正常。
    """
    umo = event_umo(event)
    # STOPPING 与 DEGRADED 分开措辞：前者是正常关停，后者才需要重启恢复。
    if lifecycle == "DEGRADED":
        running_line = f"状态: 已降级（需重启插件恢复），持久开关: {runtime_enabled}"
    elif lifecycle == "STOPPING":
        running_line = f"状态: 正在关闭，持久开关: {runtime_enabled}"
    else:
        running_line = f"运行中: {runtime_enabled}"
    return "\n".join(
        [
            "主动回复状态",
            running_line,
            f"当前会话: {umo or '-'}",
            f"当前会话在白名单: {'是' if session_whitelisted(umo, settings.whitelist) else '否'}",
            f"私聊主动回复: {'启用' if settings.enabled_private_sessions else '关闭'}",
            f"新消息放弃旧回复: {'启用' if settings.abandon_stale_on_new_message else '关闭'}",
            f"白名单数量: {len(settings.whitelist)}",
            f"判断模型: {'启用' if settings.decision_model_enabled else '关闭'}，"
            f"Provider: {settings.judge_provider_id or '当前会话模型'}",
            f"判断提示词: {'自定义' if settings.decision_prompt_custom else '默认'}",
            f"最少上下文消息数: {settings.decision_history_min_messages} 条",
            # 延迟显示**生效值**而不是配置值：调度器实际用的是
            # max(message_delay_sec, min_silence_sec)，两者不一致时打配置值
            # 会让用户以为调低了延迟却毫无变化（默认 60/45 恰好相等，所以这个
            # 偏差在出厂配置下看不见）。生效值由 scheduler 单一表达式给出。
            f"消息后触发: {settings.enabled_message_trigger}，"
            f"延迟 {effective_message_delay_sec}s"
            f"（配置 {settings.message_delay_sec}s，最小静默 "
            f"{settings.min_silence_sec}s）",
            f"后台巡检: {settings.enabled_patrol_trigger}",
            "直接 @Bot: 始终交给 AstrBot 主回复链，主动回复不抢答",
            f"忽略发送者: {', '.join(sorted(settings.ignored_sender_ids)) or '-'}",
            f"冷却: {settings.cooldown_sec}s，今日上限: "
            f"{settings.max_daily_replies_per_session or '不限'}",
            # daily_key 不是今天时计数属于昨天：read_session_state 刻意不刷新
            # 日期（只读路径不改状态），直接显示会让运营者把昨天的用量当成
            # 今天的——而配额闸门看到的是刷新后的 0，两者恰好相反。
            f"今日已回复: {state.daily_count if state.daily_key == today_key() else 0}",
            f"上次主动回复: {fmt_ts(state.last_proactive_at)}",
            recent_decision_line(last_decision),
            "回复生成: AstrBot 正常 LLM 管线模式",
            "表情包/LivingMemory: 由 AstrBot 主回复链中的插件自动处理",
        ]
    )


async def _await_previous_check_release(plugin: SelfInitiatedReplyPlugin, umo: str) -> None:
    """有界等待该会话上一轮检查让出运行标记。

    ``/selfreply check`` 先 ``invalidate(force_cancel=True)``，但取消是异步
    投递的，旧任务要到下一个 await 点才退出并 ``unmark_running``；立即进
    pipeline 会撞上「已有判断任务在运行」。等待骨架与调度路径同源
    （``SessionGate.await_release``），预算耗尽仍被占用时照旧返回，
    由 pipeline 给出既有文案。
    """
    await plugin._gate.await_release(
        umo,
        trigger=str(CheckTrigger.MANUAL),
        timeout_sec=_MANUAL_CHECK_RELEASE_POLL_SEC,
        max_rounds=_MANUAL_CHECK_RELEASE_ROUNDS,
    )


def _lifecycle_reject_text(plugin: SelfInitiatedReplyPlugin, action: str) -> str:
    """生命周期拒绝的统一文案（DEGRADED 是"已启用但降级"，需重启恢复，
    与未启用分开说）。"""
    if plugin.lifecycle_state == "DEGRADED":
        return f"插件已降级，无法{action}（需重启插件恢复）。"
    return f"插件未启用或正在关闭，无法{action}。"


def debug_text(event: AstrMessageEvent, ignored_sender: bool) -> str:
    text = event_text(event)
    return "\n".join(
        [
            "主动回复调试信息",
            f"原始 UMO: {raw_umo(event) or '-'}",
            f"归一化 UMO: {event_umo(event) or '-'}",
            f"group_id: {event_group_id(event) or '-'}",
            f"sender_id: {event_sender_id(event) or '-'}",
            f"self_id: {event_self_id(event) or '-'}",
            f"message_str: {text or '-'}",
            f"is_at_or_wake_command: {is_at_or_wake_command_event(event)}",
            f"ignored_sender: {ignored_sender}",
            f"explicit_direct_call: {is_explicit_direct_call(event, text)}",
        ]
    )


async def dispatch_command_action(
    plugin: SelfInitiatedReplyPlugin,
    event: AstrMessageEvent,
    action: str,
    arg: str = "",
) -> str:
    """指令动作 → 回显文本。check 在 finally 回收缓存；未知 action 回落 help。"""
    umo = event_umo(event)
    if action == "help":
        return help_text()
    if action == "status":
        # 只读组装：不得经 state_for 隐式创建并滞留非白名单会话的状态。
        state = read_session_state(plugin, umo) if umo else SessionState()
        return status_text(
            plugin.settings,
            event,
            state,
            plugin.runtime_enabled,
            plugin.lifecycle_state,
            plugin._last_decisions.get(umo) if umo else None,
            # 生效延迟由调度器的单一表达式给出，不在此处重算 max(...)：
            # 两处各写一遍就会出现"面板显示与实际等待不一致"。
            plugin._scheduler.message_trigger_delay(),
        )
    if action == "list":
        return list_text(plugin.settings)
    if not umo:
        return "无法识别当前会话。"
    if action == "add":
        if not plugin._can_start_tasks():
            return _lifecycle_reject_text(plugin, "修改白名单")
        try:
            added = await plugin._add_whitelist_session(umo)
        except RuntimeError:
            # 锁内复查才失败：等 _config_lock 期间 terminate/DEGRADED 可能已发生。
            # 与 on/off 同一文案，否则管理员收到的是"写盘失败、内存已回滚"的
            # 谎报——实际什么都没写，也没有回滚。
            return _lifecycle_reject_text(plugin, "修改白名单")
        if added:
            return f"已将当前会话加入主动回复白名单：{umo}"
        if session_whitelisted(umo, plugin.settings.whitelist):
            return f"当前会话已在主动回复白名单中：{umo}"
        # 容量已满：add 拒绝而非静默挤掉一个正常会话，必须把原因说清，
        # 否则运营者只看到「未加入」而无从判断该做什么。
        return (
            f"未能加入主动回复白名单：{umo}"
            f"（已满 {MAX_WHITELIST_SIZE} 条，请先 /selfreply remove 移出不再使用的会话）"
        )
    if action == "remove":
        if not plugin._can_start_tasks():
            return _lifecycle_reject_text(plugin, "修改白名单")
        try:
            removed = await plugin._remove_whitelist_session(umo)
        except RuntimeError:
            return _lifecycle_reject_text(plugin, "修改白名单")
        return f"已移出主动回复白名单：{umo}" if removed else f"当前会话本不在主动回复白名单：{umo}"
    if action == "check":
        if not plugin._can_start_tasks():
            return _lifecycle_reject_text(plugin, "手动检查")
        # 必须 force_cancel：在途检查若不取消，它只在自己的代次闸门处退出，
        # 而下面 2s 的释放等待等不到它，本次手动检查会撞「已有判断任务在运行」。
        generation = plugin._coordinator.invalidate(umo, force_cancel=True)
        # 取消是异步投递的，旧任务要到下一个 await 点才 unmark_running，直接进
        # pipeline 会撞上「已有判断任务在运行」净效果是旧检查被静默掐掉、新的
        # 也没执行，用户必须重发一次。故有界等待其释放。
        await _await_previous_check_release(plugin, umo)
        # 有界等待只在实际有在途检查时才消耗时间，而这期间到达的新用户消息
        # 已经记下自己的事件并排好延迟检查。无条件覆写会把那条消息的事件换成
        # 指令事件，finally 再当"自己的"清掉——它的延迟检查随后被 guard 以
        # 「没有可用的最近消息事件」丢弃，巡检也因事件缓存为空跳过该会话。
        # 已有更新的事件时保留它，手动检查照常跑（判定用的是 recent 文本）。
        if not plugin._last_events.get(umo):
            plugin._coordinator.record_event(umo, event, now_ts())
        text = clean_chat_text(arg or strip_command_prefix(event_text(event)))
        if text:
            append_recent_user_message(
                plugin,
                event,
                umo=umo,
                clean_text=text,
            )
        try:
            result = await plugin._pipeline.check_session(
                umo,
                trigger=CheckTrigger.MANUAL,
                force=True,
                expected_generation=generation,
            )
        finally:
            if plugin._last_events.get(umo) is event:
                active_at = plugin._last_event_at.get(umo)
                if active_at is not None:
                    plugin._coordinator.clear_event(umo, expected_active_at=active_at)
            if not session_whitelisted(umo, plugin.settings.whitelist):
                plugin._prune_session(umo)
        return f"主动回复检查结果：{result}"
    if action == "on":
        # 降级态下「已启用」是谎报：该实例仍拒绝一切新任务（含 force check），
        # 巡逻也不会重启，恢复只能靠重载插件。文案与 check 分支同源。
        if not plugin._can_start_tasks():
            return _lifecycle_reject_text(plugin, "启用")
        async with plugin._config_lock:
            # 锁内必须复查：等锁期间 terminate/DEGRADED 都可能已经发生（它们不经
            # 这把锁或先改状态再抢锁），而本分支随后要写的是整份配置——用本实例
            # 的陈旧快照覆盖新实例。与 webapi._apply_config_updates 同一道判据。
            # 拒绝要走回显而不是上抛：本函数契约是「文本进、文本出」，装饰器
            # 路径的 async generator 没人接异常，管理员在聊天里什么都收不到。
            try:
                plugin._reject_if_not_running("启用")
            except RuntimeError:
                return _lifecycle_reject_text(plugin, "启用")
            await plugin._persist_enabled(True)
            await apply_enabled_topology(plugin, enabled=True)
        return "主动回复插件已启用（重启后保持）。"
    if action == "off":
        # 与 on 对等的生命周期守卫：DEGRADED 是永久降级、_stopping 期间宿主正要
        # 卸载，两者都不该再接受一次整份配置写盘（那会用本实例的快照覆盖新实例）。
        if not plugin._can_start_tasks():
            return _lifecycle_reject_text(plugin, "暂停")
        async with plugin._config_lock:
            try:
                plugin._reject_if_not_running("暂停")
            except RuntimeError:
                return _lifecycle_reject_text(plugin, "暂停")
            await plugin._persist_enabled(False)
        # 拓扑收敛在锁外：stop_patrol 最长等 PATROL_STOP_TIMEOUT_SEC（9s），持锁
        # 会把这段时间里每一次 Dashboard 配置写一起阻塞（terminate 对同一段等待
        # 给出了同样的结论）。
        await apply_enabled_topology(plugin, enabled=False)
        return "主动回复插件已暂停（重启后保持）。"
    if action == "debug":
        return debug_text(
            event,
            ignored_sender=event_sender_id(event) in plugin.settings.ignored_sender_ids,
        )
    return help_text()
