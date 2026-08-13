"""Web API、配置读写与 UI 偏好（自 main.py 拆分）。"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import math
from functools import partial
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # 只在类型检查期导入：运行时 main.py 先 import 本模块
    # （main.py:115），反向真导入会成环。`from __future__ import annotations` 已让
    # 注解全为字符串，故无需 quote、无加载期开销。
    #
    # 本模块保持 TYPE_CHECKING-only 是安全的，但**理由不是「宿主不解析注解」**：
    # 4.27.2 已证伪那条假设（见 main.py 的 CommandReply 注释——指令
    # 处理器就是因此在加载期 NameError）。这里安全的真实理由是本模块的函数从不以
    # 裸函数交给宿主：全部经 partial(...) 包装后再 register，partial 对象没有
    # __annotations__，宿主拿不到也不会去解析这些名字。
    # 哪天有函数改成裸函数直接注册，就必须同步把注解改成运行时可解析。
    #
    # **诚实说明其检查力**：本注解今天**不产生任何 mypy 检查力**。
    # `ignore_missing_imports = true` 让 astrbot.* 全解析为 Any，而
    # SelfInitiatedReplyPlugin 继承的 Star 就来自那里，于是整个子类坍缩成 Any——
    # 实测 `reveal_type(plugin)` 输出 `Any`，故意写错属性名（plugin.contextt）
    # 也照样 Success。同一结论 [tool.mypy] 的注释已记录过。
    #
    # 那为什么还写：它是本模块 17 个函数**唯一说明"这个 plugin 是什么"的地方**。
    # 此前读者看到裸 `plugin` 只能靠 grep 反查。编辑器的跳转与补全走的是
    # pyright/Pylance 对 main.py 的直接解析，不受上面那条 mypy 开关影响。
    # 且宿主哪天发布 py.typed（或本仓库补本地 stub），这 17 处会自动开始真检查。
    from .main import SelfInitiatedReplyPlugin

from astrbot.api import logger

try:
    from astrbot.api.web import request
except ImportError:  # pragma: no cover - compatibility with older AstrBot hosts
    from quart import request

from .models import (
    CONFIG_SPEC_BY_KEY,
    CONFIG_SPECS,
    DEFAULT_DECISION_PROMPT_TEMPLATE,
    MAX_STRING_LIST_ITEM_LEN,
    PLUGIN_ID,
    STRING_LIST_ILLEGAL_RE,
    CheckTrigger,
    ConfigSpec,
    Settings,
    normalize_config_updates,
    panel_config_specs,
)

# 配置 schema 全键（_conf_schema.json，与正式键一一对应）。此名单之外的键
# 一律 fail loud 拒绝，防止前端/未来代码提交新字段时被静默吞掉。
# 历史兼容别名（cooldown_seconds/idle_trigger_seconds/min_context_messages/
# proactive_threshold/vision_enabled/whitelist）已于 0.9.2 移除：随包前端已
# 切正式键，存量配置由 Settings.from_config 回退读取迁移，一致性守卫见
# tests/test_config_schema.py。
#
# 改为从 models.CONFIG_SPECS 派生，不再手抄 34 行。此前新增一个
# 键要同时改 schema / Settings 字段 / from_config / to_config_dict / 本名单 /
# _parse_config_updates 六处，漏一处即静默失效（漏本名单 → 面板提交被 400 拒）。
CONFIG_SCHEMA_KEYS = frozenset(spec.key for spec in CONFIG_SPECS)

# 枚举型字符串键 → 合法取值集合（schema options 的运行时镜像，同样派生自规格表）
_REPLY_LENGTH_MODES = set(CONFIG_SPEC_BY_KEY["reply_length_mode"].options)


def _config_value(config: Any, key: str, default: Any = "") -> Any:
    if isinstance(config, dict):
        return config.get(key, default)
    return getattr(config, key, default)


def _provider_config(provider: Any) -> Any:
    if isinstance(provider, dict):
        return provider.get("provider_config") or provider.get("config") or provider
    return getattr(provider, "provider_config", None) or getattr(provider, "config", None) or {}


def _provider_id(provider: Any, fallback_id: str = "") -> str:
    config = _provider_config(provider)
    return str(
        _config_value(config, "id")
        or _config_value(config, "provider_id")
        or getattr(provider, "id", "")
        or getattr(provider, "provider_id", "")
        or fallback_id
        or ""
    ).strip()


def _provider_label(provider: Any, provider_id: str) -> str:
    config = _provider_config(provider)
    label = str(
        _config_value(config, "display_name")
        or _config_value(config, "name")
        or _config_value(config, "model")
        or _config_value(config, "model_name")
        or getattr(provider, "display_name", "")
        or getattr(provider, "name", "")
        or provider_id
    ).strip()
    return f"{label} ({provider_id})" if label and label != provider_id else label or provider_id


def _provider_option(provider: Any, fallback_id: str = "") -> dict[str, str] | None:
    provider_id = _provider_id(provider, fallback_id)
    if not provider_id:
        return None
    return {"id": provider_id, "label": _provider_label(provider, provider_id)}


def _provider_items(source: Any) -> list[Any]:
    if isinstance(source, dict):
        return list(source.items())
    return list(source or [])


def _collect_provider_options(plugin: SelfInitiatedReplyPlugin) -> list[dict[str, str]]:
    providers: list[Any] = []
    get_all = getattr(plugin.context, "get_all_providers", None)
    if callable(get_all):
        try:
            providers = _provider_items(get_all())
        except Exception as exc:
            logger.debug("[%s] get_all_providers failed: %s", PLUGIN_ID, exc)

    if not providers:
        providers = _providers_from_manager(plugin)

    options: list[dict[str, str]] = []
    seen: set[str] = set()
    for provider in providers:
        fallback_id = ""
        if isinstance(provider, tuple) and len(provider) == 2:
            fallback_id = str(provider[0] or "")
            provider = provider[1]
        option = _provider_option(provider, fallback_id)
        if not option or option["id"] in seen:
            continue
        seen.add(option["id"])
        options.append(option)
    return sorted(options, key=lambda item: item["label"].lower())


def _providers_from_manager(plugin: SelfInitiatedReplyPlugin) -> list[Any]:
    provider_manager = getattr(plugin.context, "provider_manager", None)
    inst_map = getattr(provider_manager, "inst_map", None)
    if isinstance(inst_map, dict):
        return _provider_items(inst_map)
    return []


async def _api_get_config(plugin: SelfInitiatedReplyPlugin) -> dict[str, Any]:
    """返回当前配置。

    配置键从表里 ``panel`` 面派生，不再手抄。视图字段单独列出：
    ``enabled`` 是持久配置，``runtime_enabled`` 是当前运行态，两者不能合并。
    """
    try:
        payload: dict[str, Any] = {
            "ok": True,
            "runtime_enabled": plugin.runtime_enabled,
            "decision_prompt_default": DEFAULT_DECISION_PROMPT_TEMPLATE,
        }
        for spec in panel_config_specs():
            value = getattr(plugin.settings, spec.attr)
            payload[spec.key] = sorted(value) if spec.container == "set" else value
        return payload
    except Exception as exc:
        # 详情只进服务端日志：异常文本可能带绝对路径、内部键名或
        # 上游 provider 报错原文，回显给客户端等于把内部结构透给调用方。
        logger.warning("[%s] api get config failed: %s", PLUGIN_ID, exc)
        return {"ok": False, "error": "配置读取失败"}


async def _api_providers(plugin: SelfInitiatedReplyPlugin) -> dict[str, Any]:
    """返回当前可选聊天 Provider。"""
    try:
        return {"ok": True, "providers": _collect_provider_options(plugin)}
    except Exception as exc:
        # 同上：provider 枚举失败常带上游 SDK 的内部异常原文，不回显
        logger.warning("[%s] api providers failed: %s", PLUGIN_ID, exc)
        return {"ok": False, "providers": [], "error": "Provider 列表读取失败"}


async def _api_cleanup_image_cache(plugin: SelfInitiatedReplyPlugin) -> dict[str, Any]:
    """手动清理过期图片缓存；不删除有效窗口内仍受保护的源。"""
    if plugin._stopping:
        return {"ok": False, "error": "插件正在关闭"}
    try:
        removed = await plugin._scheduler.run_image_cleanup()
        return {
            "ok": True,
            "removed": removed,
            "max_age_sec": int(plugin.settings.vision_image_age_sec),
        }
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("[%s] manual image cache cleanup failed: %s", PLUGIN_ID, exc)
        return {"ok": False, "error": "图片缓存清理失败"}


def _strict_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} 必须是布尔值")
    return value


def _load_ui_theme(plugin: SelfInitiatedReplyPlugin) -> str:
    """从 ui_prefs.json 加载主题偏好；损坏或缺失回退 auto。"""
    try:
        raw = json.loads(plugin._ui_prefs_path.read_text(encoding="utf-8"))
        theme = str(raw.get("theme", "auto")).strip()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        # 文件缺失/编码损坏/JSON 损坏/顶层非对象（raw.get 缺失）一律回退
        return "auto"
    return theme if theme in {"auto", "light", "dark"} else "auto"


def _save_ui_theme(plugin: SelfInitiatedReplyPlugin, theme: str) -> bool:
    """原子写入主题偏好（tmp + replace）。"""
    try:
        tmp = plugin._ui_prefs_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"theme": theme}), encoding="utf-8")
        tmp.replace(plugin._ui_prefs_path)
        return True
    except (OSError, UnicodeEncodeError, TypeError, ValueError) as exc:
        # 与 _write_json_atomic 同型收窄：纯文件 I/O + JSON 序列化可枚举
        logger.warning("[%s] ui theme save failed: %s", PLUGIN_ID, exc)
        return False


async def _api_get_ui_theme(plugin: SelfInitiatedReplyPlugin) -> dict[str, Any]:
    """获取插件页面 UI 主题偏好。"""
    return {"ok": True, "theme": plugin._ui_theme}


async def _api_post_ui_theme(plugin: SelfInitiatedReplyPlugin) -> dict[str, Any]:
    """更新插件页面 UI 主题偏好（持久化到 ui_prefs.json）。"""
    try:
        data = await _request_json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        return {"ok": False, "error": "请求体必须是 JSON 对象"}
    theme = str(data.get("theme", "")).strip()
    if theme not in {"auto", "light", "dark"}:
        # 不回显 theme 原值：那是客户端可控输入，回显等于把请求体
        # 原文反射回响应。合法取值是固定枚举，直接告知即可，无需回放输入。
        return {"ok": False, "error": "无效主题，可选值：auto / light / dark"}
    if theme != plugin._ui_theme:
        if not _save_ui_theme(plugin, theme):
            return {"ok": False, "error": "主题写入失败"}
        plugin._ui_theme = theme
    return {"ok": True, "theme": plugin._ui_theme}


def _strict_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} 必须是整数")
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} 必须是整数") from exc


def _string_list(data: dict[str, Any], key: str) -> list[str]:
    """规范化字符串列表：strip、去空，条目长度/字符规则与白名单共用。"""
    raw = data[key]
    if not isinstance(raw, list):
        raise ValueError(f"{key} 必须是数组")
    items: list[str] = []
    for item in raw:
        text = str(item).strip()
        if not text:
            continue
        # 先查非法字符再查长度：过长文案会把 text[:20] 带进
        # logger.warning，若其中含 \n / 控制字符即可伪造日志行。收窄顺序后，
        # 能进日志的片段必然已通过控制字符过滤。
        if STRING_LIST_ILLEGAL_RE.search(text):
            raise ValueError(f"{key} 条目含非法字符")
        if len(text) > MAX_STRING_LIST_ITEM_LEN:
            raise ValueError(f"{key} 条目过长: {text[:20]}…")
        items.append(text)
    return items


def _strict_float(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} 必须是数字")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} 必须是数字") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{field} 必须是有限数字")
    return parsed


async def _request_json() -> Any:
    json_reader = getattr(request, "json", None)
    if callable(json_reader):
        try:
            signature = inspect.signature(json_reader)
        except (TypeError, ValueError):
            return await json_reader(default={})
        try:
            signature.bind(default={})
        except TypeError:
            try:
                signature.bind()
            except TypeError as exc:
                raise RuntimeError("当前 AstrBot Web API 不支持 JSON 请求读取") from exc
            return await json_reader()
        return await json_reader(default={})
    get_json = getattr(request, "get_json", None)
    if callable(get_json):
        return await get_json(silent=True)
    raise RuntimeError("当前 AstrBot Web API 不支持 JSON 请求读取")


async def _api_post_config(plugin: SelfInitiatedReplyPlugin) -> dict[str, Any]:
    async with plugin._config_lock:
        if plugin._stopping:
            return {"ok": False, "error": "插件正在关闭"}
        return await _api_post_config_locked(plugin)


async def _api_post_config_locked(plugin: SelfInitiatedReplyPlugin) -> dict[str, Any]:
    """更新配置。"""
    try:
        data = await _request_json()
        updates = _parse_config_updates(data)
        return await _apply_config_updates(plugin, updates)
    except ValueError as exc:
        # 校验失败的文案要回显：它由本模块自己构造，只含字段名与
        # 规则（"cooldown_sec 必须是整数"），不含内部路径/栈信息，且前端表单
        # 依赖它定位出错字段。
        logger.warning("[%s] api post config rejected: %s", PLUGIN_ID, exc)
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        # 内部异常一律通用文案：_apply_config_updates 会调 _save_storage，
        # OSError 的 str() 带绝对路径（磁盘布局泄露）。详情只进服务端日志。
        logger.warning("[%s] api post config failed: %s", PLUGIN_ID, exc)
        return {"ok": False, "error": "配置保存失败，请查看 AstrBot 日志"}


def _parse_config_updates(data: Any) -> dict[str, Any]:
    """从请求体提取合法配置变更并做严格类型校验；非法字段抛 ValueError。

    表驱动：此前 34 个键各写一段 ``if key in data``，119 行、
    圈复杂度 38（全仓最差）。真正的问题不是长度而是「新增键要记得同时改这里」，
    漏一处该键就被静默丢弃——面板上能改、保存返回成功、值不生效。

    与 ``Settings.from_config`` 的关键差异（不可统一，故意分开）：这里对非法
    输入 **抛异常**，而 from_config 静默夹取。webapi 面对的是交互式提交，用户
    需要知道"这个值不合法"；from_config 面对的是磁盘上已存在的配置，抛异常会
    让插件整体加载失败。
    """
    if not isinstance(data, dict):
        raise ValueError("请求体必须是 JSON 对象")

    unknown = sorted(set(data) - CONFIG_SCHEMA_KEYS)
    if unknown:
        raise ValueError(f"未知配置键: {', '.join(unknown)}")

    updates: dict[str, Any] = {}
    for spec in CONFIG_SPECS:
        if spec.key not in data:
            continue
        updates[spec.key] = _strict_value(spec, data)
    return updates


def _strict_value(spec: ConfigSpec, data: dict[str, Any]) -> Any:
    """按规格严格校验单个提交值；不合法抛 ValueError（含字段名，供前端定位）。

    不夹取边界：越界值交给 ``Settings.from_config`` 的 as_int/as_float 夹取，
    与磁盘加载路径共用同一套边界，避免"webapi 一套、加载一套"的第二份镜像。
    """
    raw = data[spec.key]
    if spec.kind == "bool":
        return _strict_bool(raw, spec.key)
    if spec.kind == "int":
        return _strict_int(raw, spec.key)
    if spec.kind == "float":
        return _strict_float(raw, spec.key)
    if spec.kind == "list":
        return _string_list(data, spec.key)
    if spec.kind == "enum":
        value = str(raw or "").strip()
        if value not in spec.options:
            raise ValueError(f"{spec.key} 必须是 {'/'.join(sorted(spec.options))}")
        return value
    if spec.kind == "text":
        # 空提交 = 恢复内置默认（面板留空即复位是产品语义，见 test_config_schema
        # 的 _INTENTIONAL_EMPTY_DEFAULT）
        return str(raw or "").strip() or DEFAULT_DECISION_PROMPT_TEMPLATE
    return str(raw or "").strip()


# 安全敏感配置键：变更记 INFO 审计日志。webapi 无独立鉴权，
# 访问控制依赖宿主 Dashboard；留痕便于事后追溯。
#
# 由规格表的 audited 标记派生：此前是手工名单，与
# `_parse_config_updates` 分处两地，漏一处审计就静默失效——注释里那两条
# 「新增键的前提」正是在手工维护这个约束。现在两者同源于 CONFIG_SPECS，
# 前提由 tests/test_config_schema.py 的守卫强制。
#
# 入表理由（语义仍需人判断，故记录在此）：Provider 类键
# （judge/vision/vision_judge）决定群聊上下文与图片发往哪个上游端点，被改指向
# 攻击者 provider 即为持续数据外泄；vision_*_enabled 是图片外发总开关；
# ignored_sender_ids 能静默屏蔽特定用户（含管理员），是可滥用的隐蔽开关。
def _config_update_was_adjusted(
    spec: ConfigSpec, submitted: Any, normalized: dict[str, Any]
) -> bool:
    actual = normalized[spec.key]
    if spec.container == "set":
        return set(submitted) != set(actual)
    return submitted != actual


_AUDITED_CONFIG_KEYS = tuple(spec.key for spec in CONFIG_SPECS if spec.audited)


def _snapshot_plugin_state(plugin: SelfInitiatedReplyPlugin) -> dict[str, Any]:
    """对应用配置前会变更的全部运行态做快照，供回滚恢复。"""
    return {
        "settings": copy.deepcopy(plugin.settings),
        "runtime_enabled": plugin.runtime_enabled,
        **plugin._coordinator.snapshot(),
        "whitelist_runtime_umos": {
            key: set(values) for key, values in plugin._whitelist_runtime_umos.items()
        },
        "gate": plugin._gate.snapshot(),
        "sessions": dict(plugin.sessions),
        "delay_umos": set(plugin._delay_tasks),
    }


async def _restore_plugin_state(plugin: SelfInitiatedReplyPlugin, snapshot: dict[str, Any]) -> None:
    """恢复配置应用前快照，重建被取消的延迟检查并恢复任务拓扑。"""
    # 原地恢复（保持 Settings 对象身份）：组件构造时各存 self.settings
    # 引用，整体替换会让它们读到过期配置（0.9.0 轴 A）。
    plugin.settings.apply(snapshot["settings"])
    plugin.runtime_enabled = snapshot["runtime_enabled"]
    # 容器也必须原地恢复（B1）：scheduler/coordinator/whitelist 构造时
    # 捕获的是这些 dict 对象本身的引用（main.py 装配段），属性重绑定会让
    # 它们继续写孤儿容器——回滚后 main 从新 dict 读、协作对象写旧 dict，
    # 该会话主动回复静默停止直到重启。clear+update 保持容器身份不变。
    plugin._coordinator.restore_inplace(snapshot)
    plugin._whitelist_runtime_umos.clear()
    plugin._whitelist_runtime_umos.update(snapshot["whitelist_runtime_umos"])
    plugin._gate.restore(snapshot["gate"])
    plugin.sessions.clear()
    plugin.sessions.update(snapshot["sessions"])
    # 回滚后重新调度被白名单变更取消的延迟检查（已取消的任务对象
    # 不可复用，只能按默认 message_delay 语义重建）。
    for umo in snapshot["delay_umos"]:
        if umo in plugin.sessions and not plugin._stopping:
            try:
                plugin._scheduler.schedule_delayed_check(
                    umo, delay_sec=None, trigger=CheckTrigger.MESSAGE_DELAY, force=False
                )
            except Exception as re_exc:
                logger.debug(
                    "[%s] delayed check reschedule failed on rollback session=%s error=%s",
                    PLUGIN_ID,
                    umo,
                    re_exc,
                )
        else:
            logger.debug("[%s] delayed check dropped on rollback session=%s", PLUGIN_ID, umo)
    # 回滚后一律丢弃 parser 缓存，避免残留按失败配置建的实例
    plugin._image_parsers.clear()
    plugin._image_parser_timeout = None
    # 回滚后恢复任务拓扑：禁用路径可能已停掉 patrol/cleanup，
    # 否则出现 runtime_enabled=True 但巡检永久停止的不一致态。
    if snapshot["runtime_enabled"] and not plugin._stopping:
        plugin._scheduler.ensure_patrol()
        plugin._scheduler.ensure_image_cleanup()
    try:
        plugin._sync_whitelist()
        await plugin._save_storage()
    except Exception as rollback_exc:
        logger.error("[%s] config rollback persistence failed: %s", PLUGIN_ID, rollback_exc)


async def _apply_config_updates(
    plugin: SelfInitiatedReplyPlugin, updates: dict[str, Any]
) -> dict[str, Any]:
    """应用配置变更；任何失败回滚全部运行态后重新抛出。"""
    snapshot = _snapshot_plugin_state(plugin)
    try:
        candidate = plugin.settings.to_config_dict()
        normalized_updates = normalize_config_updates(updates)
        for key, value in normalized_updates.items():
            candidate[key] = value
        new_settings = Settings.from_config(candidate)
        vision_changed = any(
            getattr(plugin.settings, spec.attr) != getattr(new_settings, spec.attr)
            for spec in CONFIG_SPECS
            if spec.key.startswith("vision_")
        )
        # 原地应用（保持 Settings 对象身份）：五个组件构造时各存
        # self.settings 引用，整体替换会造成热更新后组件读旧值（0.9.0 轴 A）。
        plugin.settings.apply(new_settings)
        if "whitelist_sessions" in updates:
            plugin._whitelist.replace(new_settings.whitelist)
        if vision_changed:
            plugin._image_parsers.clear()
            plugin._image_parser_timeout = None
        if updates:
            plugin._sync_whitelist()
            await plugin._save_storage()

        # 持久 enabled 真正变化才重置运行态并同步任务拓扑；全量表单重复提交
        # 相同值不得改动运行态。/on /off 自身已落盘，故此处两者通常同值；
        # 本分支守的是「前端提交的 enabled 与现值不同」这一路。
        enabled_persisted_changed = (
            "enabled" in updates and snapshot["settings"].enabled != new_settings.enabled
        )
        if enabled_persisted_changed:
            plugin.runtime_enabled = new_settings.enabled
            if plugin.runtime_enabled:
                plugin._scheduler.ensure_patrol()
                plugin._scheduler.ensure_image_cleanup()
            else:
                plugin._cancel_delay_tasks()
                await plugin._scheduler.stop_patrol()
        elif plugin.runtime_enabled:
            plugin._scheduler.ensure_image_cleanup()
        _log_audited_changes(snapshot, new_settings, updates)
        config = new_settings.to_config_dict()
        adjusted_fields = sorted(
            key
            for key, value in updates.items()
            if _config_update_was_adjusted(CONFIG_SPEC_BY_KEY[key], value, config)
        )
        return {"ok": True, "config": config, "adjusted_fields": adjusted_fields}
    except Exception:
        await _restore_plugin_state(plugin, snapshot)
        raise


def _audit_value(value: Any) -> str:
    if isinstance(value, (set, list)):
        items = sorted(str(item) for item in value)
        digest = hashlib.sha256(
            json.dumps(items, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:12]
        return f"count={len(items)},sha256={digest}"
    return repr(value)


def _log_audited_changes(
    snapshot: dict[str, Any], new_settings: Settings, updates: dict[str, Any]
) -> None:
    """安全敏感键发生实际变化时记 INFO 审计日志。"""
    changed: list[str] = []
    for key in _AUDITED_CONFIG_KEYS:
        if key not in updates:
            continue
        attr = CONFIG_SPEC_BY_KEY[key].attr
        old_value = getattr(snapshot["settings"], attr, None)
        new_value = getattr(new_settings, attr, None)
        old_norm = sorted(old_value) if isinstance(old_value, (set, list)) else old_value
        new_norm = sorted(new_value) if isinstance(new_value, (set, list)) else new_value
        if old_norm != new_norm:
            changed.append(f"{key}={_audit_value(new_value)}")
    if changed:
        logger.info("[%s] webapi config audit: %s", PLUGIN_ID, ", ".join(changed))


async def _api_status(plugin: SelfInitiatedReplyPlugin) -> dict[str, Any]:
    """返回插件集成状态与会话级运行状态（调试面板导出，ticket 14）。

    覆盖：代次快照、运行中集合、任务数（延迟/运行中检查/后台）、缓存规模
    （事件/图片事件/会话）、每会话最近裁决原因。

    包 try/except 的诚实理由：**今天没有可达异常**。逐项核过
    ——全是普通属性或平凡 property（``generation_view`` / ``running_sessions_view``
    只做 ``MappingProxyType`` / ``frozenset`` 包装），且全部在 ``__init__``
    早于 ``register_web_apis`` 完成初始化，仓内也无线程（故 ``dict()`` 复制期间
    被并发改动这种理由不成立，不拿它当依据）。

    真实理由是两条：一是本函数是**唯一**没有 ``except`` 的 ``_api_*`` 处理器，
    未捕获异常将按宿主的方式呈现，而其余处理器加 ``except`` 的初衷正是不让内部
    细节以任何形式流向调用方——这层保证不该有一个缺口；二是本函数按设计会随
    调试面板扩字段而增长，每加一项就是一次新的取值机会，届时"当前无可达异常"
    这个前提不再自动成立。
    """
    try:
        return {
            "ok": True,
            "loaded": True,
            "runtime_enabled": plugin.runtime_enabled,
            "whitelist_count": len(plugin.settings.whitelist),
            "decision_model_enabled": plugin.settings.decision_model_enabled,
            "gate": {
                "generation": dict(getattr(plugin._gate, "generation_view", {})),
                "running": sorted(getattr(plugin._gate, "running_sessions_view", frozenset())),
            },
            "tasks": {
                "delay": len(plugin._delay_tasks),
                "running_check": len(plugin._running_check_tasks),
                "background": len(plugin._background_tasks),
            },
            "caches": {
                "events": len(plugin._last_events),
                "image_events": len(plugin._recent_image_events),
                "sessions": len(plugin.sessions),
            },
            "last_decisions": dict(plugin._last_decisions),
        }
    except Exception as exc:
        # 同 _api_get_config：详情只进服务端日志，不回显给调用方。
        logger.warning("[%s] api status failed: %s", PLUGIN_ID, exc)
        return {"ok": False, "error": "状态读取失败"}


def register_web_apis(plugin: SelfInitiatedReplyPlugin) -> None:
    """注册统一管理页面所需的 Web API。"""
    register = plugin.context.register_web_api
    route = f"/{PLUGIN_ID}"
    register(
        f"{route}/config",
        partial(_api_get_config, plugin),
        ["GET"],
        "获取主动回复插件配置",
    )
    register(
        f"{route}/config",
        partial(_api_post_config, plugin),
        ["POST"],
        "更新主动回复插件配置",
    )
    register(
        f"{route}/status",
        partial(_api_status, plugin),
        ["GET"],
        "获取插件集成状态",
    )
    register(
        f"{route}/providers",
        partial(_api_providers, plugin),
        ["GET"],
        "获取可选判断模型 Provider",
    )
    register(
        f"{route}/image-cache/cleanup",
        partial(_api_cleanup_image_cache, plugin),
        ["POST"],
        "清理主动回复插件过期图片缓存",
    )
    register(
        f"{route}/ui/theme",
        partial(_api_get_ui_theme, plugin),
        ["GET"],
        "获取插件页面 UI 偏好（主题）",
    )
    register(
        f"{route}/ui/theme",
        partial(_api_post_ui_theme, plugin),
        ["POST"],
        "更新插件页面 UI 偏好（主题）",
    )


def bind_api_handlers(plugin: SelfInitiatedReplyPlugin) -> None:
    """在实例上保留历史方法名，供测试与外部以 plugin._api_* 调用。"""
    plugin._api_get_config = partial(_api_get_config, plugin)
    plugin._api_post_config = partial(_api_post_config, plugin)
    plugin._api_get_ui_theme = partial(_api_get_ui_theme, plugin)
    plugin._api_post_ui_theme = partial(_api_post_ui_theme, plugin)


# 公开入口：main.py 初始化时加载主题偏好。
load_ui_theme = _load_ui_theme
