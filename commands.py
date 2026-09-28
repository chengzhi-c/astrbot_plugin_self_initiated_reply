"""指令文本解析、回显拼装，以及写动作分派。

解析/帮助/状态/列表/调试为纯函数。``dispatch_command_action`` 承载 add/remove/
check/on/off 等有副作用分支，经 plugin 回调访问状态（测试可替换实例方法）。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from astrbot.api.event import AstrMessageEvent

from .models import CheckTrigger, SessionState, Settings, fmt_ts, now_ts
from .plugin_state import append_recent_user_message, read_session_state
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
_MANUAL_CHECK_RELEASE_WAIT_SEC = 2.0
_MANUAL_CHECK_RELEASE_POLL_SEC = 0.05

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
    if not lowered.startswith("selfreply "):
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

    ``check`` 取测试内容的唯一路径：装饰器路径 ``main.selfreply_check`` 恒定传
    ``arg=""``（它不解析参数），此时用户附带的 ``/selfreply check 你好`` 只能由
    本函数从事件原文取出。删掉它会让装饰器路径的 check 静默丢失测试内容，
    而内联路径的 ``arg`` 已非空、不会暴露这个缺口。
    """
    parsed = parse_command_text(text)
    return parsed[1] if parsed else text


def _alias_help_line() -> str:
    """别名说明从调度表派生。

    手写这份清单时，给某个动作加别名而忘了同步文案是静默的：用户查帮助看不到，
    而宿主那边其实已经注册上了。装饰器侧的 ``alias=`` 仍各自写字面量（不共用同一
    个 set 对象，理由见 ``test_command_aliases_single_source``），它与本表由那条
    守卫钉等价，本行则直接从表里长出来。
    """
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


# 最近裁决一行的原因截断长度。reason 的长度不因单点上界而可依赖：模型 JSON 路径
# 按 utils.DECISION_REASON_MAX_CHARS 截过，模型异常路径是「判断模型异常：」拼脱敏
# 原文（redact_exc_text 只钳 URL 片段，不限总长），且都会引用用户原文。
# 单行回显必须自带上限，否则一整段会塞进指令状态。
_RECENT_DECISION_REASON_MAX = 60


def recent_decision_line(decision: dict[str, Any] | None) -> str:
    """把本会话最近一次裁决渲染成一行（与 ``GET /status`` 的 ``last_decisions`` 同源）。"""
    if not decision:
        return "最近裁决: 暂无记录"
    verdict = "接话" if decision.get("should_reply") else "不接话"
    # reason 可能是多行（模型 JSON 自由文本），先折成单行再截断，保证回显行数稳定。
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
) -> str:
    """渲染 /selfreply status 文本。

    ``lifecycle`` 与 ``last_decision`` 必填且无默认值：降级是单向门，
    ``runtime_enabled`` 读持久配置仍为 True，只有 lifecycle 能说明插件实际已
    拒绝一切新工作。给默认值会让新增调用点静默回落 "RUNNING" 而谎报正常
    故由签名强制每个调用方显式表态。
    ``last_decision`` 同理：它是本会话最近一次裁决（``_last_decisions``，与
    ``GET /status`` 同源），拿不到就得显式传 None，而不是让新调用点默默不显示。
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
            f"消息后触发: {settings.enabled_message_trigger}，延迟 {settings.message_delay_sec}s，"
            f"最小静默 {settings.min_silence_sec}s",
            f"后台巡检: {settings.enabled_patrol_trigger}",
            "直接 @Bot: 始终交给 AstrBot 主回复链，主动回复不抢答",
            f"忽略发送者: {', '.join(sorted(settings.ignored_sender_ids)) or '-'}",
            f"冷却: {settings.cooldown_sec}s，今日上限: "
            f"{settings.max_daily_replies_per_session or '不限'}",
            f"今日已回复: {state.daily_count}",
            f"上次主动回复: {fmt_ts(state.last_proactive_at)}",
            recent_decision_line(last_decision),
            "回复生成: AstrBot 正常 LLM 管线模式",
            "表情包/LivingMemory: 由 AstrBot 主回复链中的插件自动处理",
        ]
    )


async def _await_previous_check_release(plugin: SelfInitiatedReplyPlugin, umo: str) -> None:
    """有界等待该会话上一轮检查让出运行标记（最多等 ``_MANUAL_CHECK_RELEASE_WAIT_SEC``）。

    背景：``/selfreply check`` 会先 ``invalidate(force_cancel=True)`` 取消在途
    检查，但取消是异步投递的，旧任务要到下一个 await 点才真正退出并
    ``unmark_running``。立即进 pipeline 会撞上 ``is_running`` 检查而返回
    「已有判断任务在运行」：旧检查被静默掐掉、本次也没执行，用户得重发。
    预算耗尽仍被占用时照旧返回，由 pipeline 给出既有文案（不无限挂住指令回执）。
    """
    deadline = _MANUAL_CHECK_RELEASE_WAIT_SEC
    waited = 0.0
    step = _MANUAL_CHECK_RELEASE_POLL_SEC
    while waited < deadline and plugin._gate.is_running(umo):
        event = plugin._gate.release_event(umo)
        try:
            await asyncio.wait_for(event.wait(), timeout=step)
        except TimeoutError:
            waited += step
            continue
        break


def _lifecycle_reject_text(plugin: SelfInitiatedReplyPlugin, action: str) -> str:
    """生命周期拒绝的统一文案（DEGRADED 与未启用分开说）。

    DEGRADED 时插件是"已启用但降级"：统一说"未启用"会误导运营去改配置而不是
    重启插件。多个写指令共用本函数，避免同一成因在不同指令上措辞漂移。
    """
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
        )
    if action == "list":
        return list_text(plugin.settings)
    if not umo:
        return "无法识别当前会话。"
    if action == "add":
        if not plugin._can_start_tasks():
            return _lifecycle_reject_text(plugin, "修改白名单")
        added = await plugin._add_whitelist_session(umo)
        return (
            f"已将当前会话加入主动回复白名单：{umo}"
            if added
            else f"当前会话已在主动回复白名单中：{umo}"
        )
    if action == "remove":
        if not plugin._can_start_tasks():
            return _lifecycle_reject_text(plugin, "修改白名单")
        removed = await plugin._remove_whitelist_session(umo)
        return f"已移出主动回复白名单：{umo}" if removed else f"当前会话本不在主动回复白名单：{umo}"
    if action == "check":
        if not plugin._can_start_tasks():
            return _lifecycle_reject_text(plugin, "手动检查")
        generation = plugin._coordinator.invalidate(umo)
        # 旧检查被 force-cancel 后不会立即让出运行标记（取消是异步投递的），
        # 直接进 pipeline 会撞上「已有判断任务在运行」净效果是旧检查被静默
        # 掐掉、新的也没执行，用户必须重发一次。故有界等待其释放。
        await _await_previous_check_release(plugin, umo)
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
            await plugin._persist_enabled(True)
            plugin._scheduler.ensure_patrol()
            plugin._scheduler.ensure_image_cleanup()
        return "主动回复插件已启用（重启后保持）。"
    if action == "off":
        async with plugin._config_lock:
            await plugin._persist_enabled(False)
            plugin._cancel_delay_tasks()
            await plugin._scheduler.stop_patrol()
        return "主动回复插件已暂停（重启后保持）。"
    if action == "debug":
        return debug_text(
            event,
            ignored_sender=event_sender_id(event) in plugin.settings.ignored_sender_ids,
        )
    return help_text()
