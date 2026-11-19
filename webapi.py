"""Web API：配置读写、UI 偏好与运维状态。

拥有：HTTP 处理器的注册与绑定、面板配置读视图（``panel`` 面派生 +
``config_revision``）、配置写入的严格校验（未知键 fail loud）、CAS 前置条件、
应用配置时的运行态快照与回滚、安全敏感键审计日志、UI 偏好原子落盘、
provider 列表枚举的宿主形态归一。

不拥有：配置键的机器规则（``models.ConfigSpec``）、状态文件与原子写
（``storage``）、持久配置的真源（``plugin.settings``）、插件运行态容器
（``main``）。

``SelfInitiatedReplyPlugin`` 只在 ``TYPE_CHECKING`` 下导入：运行时与 ``main``
成环，处理器经 ``partial`` 注册，宿主不解析这些注解。
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
from collections import deque
from functools import partial
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .main import SelfInitiatedReplyPlugin

from astrbot.api import logger

try:
    from astrbot.api.web import request
except ImportError:  # pragma: no cover - compatibility with older AstrBot hosts
    from quart import request

try:
    from astrbot.api.web import error_response
except ImportError:  # pragma: no cover - 旧宿主没有统一错误响应构造器

    def error_response(message: str, *, status_code: int = 400) -> Any:
        # Quart 接受 (body, status) 返回值；只返回 dict 会把错误变成 HTTP 200。
        return {"ok": False, "error": message}, status_code


from .models import (
    CONFIG_SPEC_BY_KEY,
    CONFIG_SPECS,
    PLUGIN_ID,
    PRINTABLE_CHAR_MIN,
    CheckTrigger,
    ConfigSpec,
    SessionState,
    Settings,
    config_revision,
    is_utf8_encodable,
    js_trim,
    normalize_string_list,
    panel_config_specs,
    restore_container_inplace,
)
from .plugin_state import apply_enabled_topology
from .storage import write_json_atomic
from .utils import await_rollback, redact_exc_text, session_whitelisted

# 配置 schema 全键（fail loud：名单之外的提交键一律 400 拒绝，防止提交
# 新字段时被静默吞掉）。历史兼容别名由 Settings.from_config 的 legacy_keys
# 回退读取，不入本名单。
CONFIG_SCHEMA_KEYS = frozenset(spec.key for spec in CONFIG_SPECS)

# 主题合法值单点：前端的集合必须与本常量一致，改一处要同步另一处。
UI_THEME_VALUES = frozenset({"auto", "light", "dark"})


def _provider_config(provider: Any) -> dict[str, Any]:
    """宿主 Provider 的配置字典（``get_all_providers`` 与 ``inst_map`` 都返回 Provider 对象）。"""
    config = getattr(provider, "provider_config", None)
    return config if isinstance(config, dict) else {}


def _provider_option(provider: Any, fallback_id: str = "") -> dict[str, str] | None:
    config = _provider_config(provider)
    provider_id = str(config.get("id") or fallback_id or "").strip()
    if not provider_id:
        return None
    label = str(config.get("model") or "").strip() or provider_id
    if label == provider_id:
        return {"id": provider_id, "label": label}
    return {"id": provider_id, "label": f"{label} ({provider_id})"}


def _provider_items(source: Any) -> list[Any]:
    """Provider 集合归一：``inst_map`` 是 dict，键作 fallback id；``get_all_providers`` 是 list。"""
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
            logger.debug("[%s] get_all_providers failed: %s", PLUGIN_ID, redact_exc_text(exc))

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
    """返回当前配置。``enabled`` 是持久配置，``runtime_enabled`` 是运行态，不能合并。"""
    try:
        payload: dict[str, Any] = {
            "ok": True,
            "runtime_enabled": plugin.runtime_enabled,
            "config_revision": config_revision(plugin.settings),
            # 面板「恢复默认」填充值与读侧落盘同源于规格表。
            "decision_prompt_default": CONFIG_SPEC_BY_KEY["decision_prompt_template"].reset_value,
        }
        for spec in panel_config_specs():
            payload[spec.key] = spec.canonical_value(getattr(plugin.settings, spec.attr))
        return payload
    except Exception as exc:
        # 详情只进服务端日志：异常文本可能带绝对路径、内部键名或上游报错原文。
        logger.warning("[%s] api get config failed: %s", PLUGIN_ID, redact_exc_text(exc))
        return {"ok": False, "error": "配置读取失败"}


async def _api_providers(plugin: SelfInitiatedReplyPlugin) -> dict[str, Any]:
    """返回当前可选聊天 Provider。"""
    try:
        return {"ok": True, "providers": _collect_provider_options(plugin)}
    except Exception as exc:
        logger.warning("[%s] api providers failed: %s", PLUGIN_ID, redact_exc_text(exc))
        return {"ok": False, "providers": [], "error": "Provider 列表读取失败"}


async def _api_cleanup_image_cache(plugin: SelfInitiatedReplyPlugin) -> dict[str, Any]:
    """手动清理过期图片缓存；不删除有效窗口内仍受保护的源。"""
    # 只有一道预检，没有锁内复查：本端点没有可回滚的写路径（run_image_cleanup
    # 只在 to_thread 里 unlink 过期文件，且被 not_before 保护窗口挡在冻结中的
    # 文件之外），teardown 与它并发的最坏结果是多删几个本就要过期的文件。
    # 为它引入 _config_lock 反而会让一次慢清理连坐 terminate()。
    if plugin._stopping:
        return {"ok": False, "error": "插件正在关闭"}
    try:
        removed = await plugin._scheduler.run_image_cleanup()
        return {
            "ok": True,
            "removed": removed,
            "max_age_sec": int(plugin.settings.vision_image_age_sec),
        }
    except Exception as exc:
        logger.warning(
            "[%s] manual image cache cleanup failed: %s", PLUGIN_ID, redact_exc_text(exc)
        )
        return {"ok": False, "error": "图片缓存清理失败"}


def _strict_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} 必须是布尔值")
    return value


def load_ui_prefs(plugin: SelfInitiatedReplyPlugin) -> tuple[str, bool, bool]:
    """从 ui_prefs.json 加载主题/压暗/粗体；损坏或缺失回退 auto + 关。"""
    try:
        # utf-8-sig：BOM 头一并吞掉，与状态文件同口径。
        raw = json.loads(plugin._ui_prefs_path.read_text(encoding="utf-8-sig"))
        theme = str(raw.get("theme", "auto")).strip()
        dim = raw.get("dim", False)
        bold = raw.get("bold", False)
    except FileNotFoundError:
        # 首次运行：文件本来就不存在，不是损坏，无需留痕。
        return "auto", False, False
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError, TypeError) as exc:
        # 必须留痕：静默回退 + 随后任何一次 POST 整块三字段覆写，会把原偏好永久
        # 抹掉，而运维在日志里看不到任何一行（不记则无从定位：同仓库的 state.json
        # 损坏路径记了两条 ERROR，这边一条都没有）。
        logger.warning(
            "[%s] ui prefs unreadable, falling back to auto/off/off: %s",
            PLUGIN_ID,
            redact_exc_text(exc),
        )
        return "auto", False, False
    if theme not in UI_THEME_VALUES:
        theme = "auto"
    if not isinstance(dim, bool):
        dim = False
    if not isinstance(bold, bool):
        bold = False
    return theme, dim, bold


def _ui_prefs_payload(plugin: SelfInitiatedReplyPlugin) -> dict[str, Any]:
    return {
        "ok": True,
        "theme": plugin._ui_theme,
        "dim": plugin._ui_dim,
        "bold": plugin._ui_bold,
    }


async def _api_get_ui_theme(plugin: SelfInitiatedReplyPlugin) -> dict[str, Any]:
    """获取插件页面 UI 主题/压暗/粗体偏好。"""
    return _ui_prefs_payload(plugin)


async def _api_post_ui_theme(plugin: SelfInitiatedReplyPlugin) -> dict[str, Any]:
    """更新插件页面 UI 偏好（持久化到 ui_prefs.json）。未带的键保持原值。"""
    # 关停中拒写：teardown 之后落盘的偏好会在下次启动被读回陈旧值。
    if plugin._stopping:
        return {"ok": False, "error": "插件正在关闭"}
    # 与 POST /config 共用同一道 Content-Length 预检：本端点同样读 JSON 并在
    # _config_lock 内写盘，没有上限的请求体读取会白占锁外的内存与连接。
    # 拒绝走 HTTP 200 + {ok:false}：前端 sendThemeWrite 只对非 2xx 抛异常，
    # 非 2xx 会让文案退化成通用「请求失败」。
    rejection = _content_length_rejection(_MAX_UI_THEME_BODY_BYTES, endpoint="post ui theme")
    if rejection is not None:
        return {"ok": False, "error": rejection[0]}
    try:
        data = await _request_json()
    except Exception as exc:
        # 请求体读不出与「空对象」是两回事：吞成 {} 会把人指向字段名而非坏请求体。
        # 详情只进服务端日志（宿主 reader 的错误文本可能含请求体）。
        logger.debug("[%s] ui/theme request body unreadable: %s", PLUGIN_ID, redact_exc_text(exc))
        return {"ok": False, "error": "请求体不是合法 JSON"}
    if not isinstance(data, dict):
        return {"ok": False, "error": "请求体必须是 JSON 对象"}
    # 字段校验只用请求体、不读当前状态，可留在锁外。不回显客户端可控输入。
    submitted: dict[str, Any] = {}
    if "theme" in data:
        theme = str(data.get("theme", "")).strip()
        if theme not in UI_THEME_VALUES:
            return {"ok": False, "error": "无效主题，可选值：auto / light / dark"}
        submitted["theme"] = theme
    if "dim" in data:
        if not isinstance(data["dim"], bool):
            return {"ok": False, "error": "无效压暗开关"}
        submitted["dim"] = data["dim"]
    if "bold" in data:
        if not isinstance(data["bold"], bool):
            return {"ok": False, "error": "无效粗体开关"}
        submitted["bold"] = data["bold"]
    if not submitted:
        return {"ok": False, "error": "未提供任何字段：theme / dim / bold 至少一个"}
    # 读-改-写整块进 _config_lock（与 POST /config 同一把，锁序恒为
    # _config_lock → _save_lock）：写盘含 fsync 必须进线程；未提交字段的基准值
    # 也快不得在锁外读（两个并发 POST 各改一字段会互相覆盖）。锁内复查
    # _stopping：等锁期间 teardown 可能已跑完。
    async with plugin._config_lock:
        if plugin._stopping:
            return {"ok": False, "error": "插件正在关闭"}
        theme = submitted.get("theme", plugin._ui_theme)
        dim = submitted.get("dim", plugin._ui_dim)
        bold = submitted.get("bold", plugin._ui_bold)
        if (theme, dim, bold) != (plugin._ui_theme, plugin._ui_dim, plugin._ui_bold):
            cancelled: asyncio.CancelledError | None = None
            write_task = asyncio.create_task(
                asyncio.to_thread(
                    write_json_atomic,
                    plugin._ui_prefs_path,
                    {"theme": theme, "dim": dim, "bold": bold},
                    abandoned=lambda: plugin._terminated or cancelled is not None,
                )
            )
            # 取消落在发布之后时，仍须同步内存，再放锁并重抛取消。
            while True:
                try:
                    written = await asyncio.shield(write_task)
                    break
                except asyncio.CancelledError as exc:
                    if write_task.cancelled():
                        raise
                    cancelled = exc
            if written and not plugin._terminated:
                plugin._ui_theme = theme
                plugin._ui_dim = dim
                plugin._ui_bold = bold
            if cancelled is not None:
                raise cancelled
            if plugin._terminated:
                return {"ok": False, "error": "插件正在关闭"}
            if not written:
                return {"ok": False, "error": "主题写入失败"}
        return _ui_prefs_payload(plugin)


def _strict_int(value: Any, field: str) -> int:
    # 只接受真 int（bool 是 int 子类须显式排除）：静默截断/解析会让前端无从
    # 得知值被改写，与同文件布尔/枚举/列表的严格 400 口径对齐。
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field} 必须是整数")
    return value


def _string_list(data: dict[str, Any], key: str) -> list[str]:
    """规范化字符串列表；机器规则由对应 ``ConfigSpec`` 唯一拥有。"""
    return normalize_string_list(CONFIG_SPEC_BY_KEY[key], data[key], mode="api")


def _strict_float(value: Any, field: str) -> float:
    # JSON 数字无 int/float 之分，故两者都接受（bool 显式排除）；字符串拒绝，
    # fail loud 优于静默纠偏。
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} 必须是数字")
    try:
        parsed = float(value)
    except OverflowError as exc:
        # JSON 超长整数字面量被解析成任意精度 int，float() 抛 OverflowError。
        # 它是 ArithmeticError 而非 ValueError，会一路逃到最外层兜底、被报成
        # 「请求体不是合法 JSON」——字段值错误被说成请求体错误，前端按错误
        # 文案前缀定位字段的规则随之失效（要字段级文案）。
        raise ValueError(f"{field} 必须是有限数字") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{field} 必须是有限数字")
    return parsed


# 宿主 PluginRequestProxy.json() 读不出 body 时返回 default 而不抛（实测
# astrbot 4.27）：把 default 设成唯一哨兵，才能把「坏请求体」与「真·空对象」
# 区分开——后者是合法的无字段保存，前者必须报错。
_BODY_UNREADABLE = object()

# 配置提交是 JSON 对象，1 MiB 对整份配置（含最长的白名单列表）有大量余量。
_MAX_CONFIG_BODY_BYTES = 1024 * 1024
# UI 偏好只有 theme/dim/bold 三个短字段，4 KiB 足够，且不必跟着配置上限走。
_MAX_UI_THEME_BODY_BYTES = 4 * 1024


def _content_length() -> int | None:
    headers = getattr(request, "headers", None)
    getter = getattr(headers, "get", None)
    raw = getter("content-length") if callable(getter) else None
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdecimal():
        return None
    try:
        return int(raw)
    except ValueError:
        # 超长纯数字串（CPython 3.11+ 的 int 位数上限）同样是不可用的头：
        # 调用点位置在 try 之外，漏接会变成 500 而不是 411。
        return None


def _content_length_rejection(max_bytes: int, *, endpoint: str) -> tuple[str, int] | None:
    """Content-Length 预检的共用实现：返回 ``(错误文案, HTTP 状态码)`` 或 None。

    各端点只差体积上限与日志前缀，拒绝语义（缺头/坏头 411、超限 413）必须一致；
    错误载荷结构由调用方按各自端点的既有格式组装。
    """
    length = _content_length()
    if length is None:
        logger.warning("[%s] api %s missing or invalid content-length", PLUGIN_ID, endpoint)
        return "请求体需提供有效的 Content-Length", 411
    if length > max_bytes:
        logger.warning("[%s] api %s body too large: %s bytes", PLUGIN_ID, endpoint, length)
        return "请求体过大", 413
    return None


async def _request_json() -> Any:
    # Quart.json 是异步 property；先用 get_json，避免创建未消费的协程。
    get_json = getattr(request, "get_json", None)
    if callable(get_json):
        return await get_json(silent=True)
    data = await request.json(default=_BODY_UNREADABLE)
    if data is _BODY_UNREADABLE:
        raise ValueError("请求体不是合法 JSON")
    return data


async def _api_post_config(plugin: SelfInitiatedReplyPlugin) -> Any:
    """更新配置，并在锁内执行可选的 revision 前置条件。"""
    rejection = _content_length_rejection(_MAX_CONFIG_BODY_BYTES, endpoint="post config")
    if rejection is not None:
        return error_response(rejection[0], status_code=rejection[1])
    # 请求体读取与全部纯函数校验留在锁外：等待客户端上传的时长由客户端
    # 决定，持锁等待会连坐 terminate() 与一切 /selfreply 写指令（它们同一把
    # _config_lock）。「在 _config_lock 内比较 revision」不受影响——比较
    # 仍在锁内、且是在任何变更之前。
    try:
        data = await _request_json()
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        base_revision = data.get("base_revision")
        if base_revision is not None and (
            not isinstance(base_revision, str) or not base_revision.strip()
        ):
            raise ValueError("base_revision 必须是非空字符串")
        config_data = {key: value for key, value in data.items() if key != "base_revision"}
        updates = _parse_config_updates(config_data)
    except ValueError as exc:
        # 校验失败的文案要回显：由本模块构造，只含字段名与规则，前端表单
        # 依赖它定位出错字段。
        logger.warning("[%s] api post config rejected: %s", PLUGIN_ID, redact_exc_text(exc))
        return {"ok": False, "error": str(exc)}
    except RuntimeError as exc:
        # 宿主不具备 JSON 请求读取能力是宿主问题，不是客户端的坏请求体：
        # 回「请求体不是合法 JSON」会把排障方向指错。
        logger.warning("[%s] api post config unsupported host: %s", PLUGIN_ID, redact_exc_text(exc))
        return {"ok": False, "error": "配置保存失败，请查看 AstrBot 日志"}
    except Exception as exc:
        logger.warning("[%s] api post config rejected: %s", PLUGIN_ID, redact_exc_text(exc))
        return {"ok": False, "error": "请求体不是合法 JSON"}

    async with plugin._config_lock:
        if plugin._stopping:
            return {"ok": False, "error": "插件正在关闭"}
        try:
            current_revision = config_revision(plugin.settings)
            # 比较前 strip：与入参校验（非空字符串）同口径，避免仅因首尾空白
            # 被判 STALE_WRITE，让用户看到一次无意义的刷新要求。
            if base_revision is not None and base_revision.strip() != current_revision:
                return {
                    "ok": False,
                    "error_code": "STALE_WRITE",
                    "error": "配置已被其他请求修改",
                    "config_revision": current_revision,
                }
            return await _apply_config_updates(
                plugin,
                updates,
                submitted=config_data,
            )
        except ValueError as exc:
            # 校验失败的文案要回显：由本模块构造，只含字段名与规则，前端表单
            # 依赖它定位出错字段。
            logger.warning("[%s] api post config rejected: %s", PLUGIN_ID, redact_exc_text(exc))
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            # 内部异常一律通用文案：OSError 的 str() 带绝对路径，详情只进服务端日志。
            logger.warning("[%s] api post config failed: %s", PLUGIN_ID, redact_exc_text(exc))
            return {"ok": False, "error": "配置保存失败，请查看 AstrBot 日志"}


def _parse_config_updates(data: Any) -> dict[str, Any]:
    """从请求体提取合法配置变更并做严格类型校验；非法字段抛 ValueError。

    表驱动防漏：新增键若不进规格表，这里会被静默丢弃。与
    ``Settings.from_config`` 的关键差异：这里对非法输入抛异常（交互式提交，
    用户需要知道值不合法），from_config 静默夹取（磁盘已存在的配置抛异常
    会让插件加载失败）。
    """
    if not isinstance(data, dict):
        raise ValueError("请求体必须是 JSON 对象")

    unknown = sorted(set(data) - CONFIG_SCHEMA_KEYS)
    if unknown:
        # 键名是客户端可控输入，且这段文本既进响应也进日志：不清掉控制字符就能
        # 伪造日志行（含 ANSI 转义），不截断则一次提交可写出数 KB 的假日志。
        # 孤立代理项编不了 UTF-8：这条文案既进日志也进响应体，留着会让严格的
        # UTF-8 日志 sink 丢掉整条拒绝记录，并让宿主在 ensure_ascii=False 下序列化
        # 响应时直接抛（值侧同一条规则见 models.is_utf8_encodable）。
        shown = [
            "".join(ch for ch in key if ord(ch) >= PRINTABLE_CHAR_MIN and is_utf8_encodable(ch))
            for key in unknown[:20]
        ]
        shown = [text[:40] for text in shown]
        raise ValueError(f"未知配置键: {', '.join(shown)}")

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
    # kind == "text" / "str" 的兜底：只做类型与空白规范化。
    # 拒绝 bool/dict/list：str 化的落盘值既不是用户输入也匹配不到任何
    # provider 或模板。int/float 沿用 falsy 规范化（与历史面板行为一致）。
    # 空提交 = 恢复内置默认，
    # 但该复位只对 text 生效（读侧 coerce 的 text 分支），写侧一律不回落，
    # 否则默认口径有两处。长度上限同样只由读侧按 spec.max_len 截断。
    if isinstance(raw, (bool, dict, list)):
        raise ValueError(f"{spec.key} 必须是字符串")
    return str(raw or "").strip()


def _config_update_was_adjusted(
    spec: ConfigSpec, submitted: Any, normalized: dict[str, Any]
) -> bool:
    actual = normalized[spec.key]
    if spec.container == "set":
        if not isinstance(submitted, list):
            return submitted != actual
        # 提交条数超过容量上限时必然被截断（解析期已先按 max_items 截再规范化），
        # 不必把 10 万条再逐条 str/js_trim 建集合比对：那一步会在事件循环上把
        # 整份提交重扫一遍。正好等于上限的提交仍走下面的精确比对。
        if spec.max_items is not None and len(submitted) > spec.max_items:
            return True
        raw_items = [str(item) for item in submitted]
        # 必须与条目规范化同一个字符集：裸 str.strip() 连 U+0085 一起剥，而
        # 设置页的 String.prototype.trim() 不剥，页面放行的条目会被误报成
        # 「已规范化」——值一个字节都没改，提示却说改过。
        trimmed_items = [js_trim(item) for item in raw_items if js_trim(item)]
        return (
            raw_items != trimmed_items
            or len(trimmed_items) != len(set(trimmed_items))
            or set(trimmed_items) != set(actual)
        )
    return submitted != actual


# 安全敏感配置键（规格表 audited 标记派生，手工名单会漏）：变更记 INFO
# 审计日志。webapi 无独立鉴权，访问控制依赖宿主 Dashboard；留痕便于事后追溯。
# Provider 类键决定上下文与图片发往哪个上游端点；vision_*_enabled 是图片
# 外发总开关；ignored_sender_ids 能静默屏蔽特定用户。
_AUDITED_CONFIG_KEYS = tuple(spec.key for spec in CONFIG_SPECS if spec.audited)


def _snapshot_plugin_state(plugin: SelfInitiatedReplyPlugin) -> dict[str, Any]:
    """保留配置和可能被裁剪的历史；真实活动、发送配额与冷却继续随运行推进。

    被白名单变更摘下的原会话对象由 ``pruned`` 单独保留，恢复时先回填。
    """
    return {
        "settings": copy.deepcopy(plugin.settings),
        "runtime_enabled": plugin.runtime_enabled,
        "whitelist_runtime_umos": {
            key: set(values) for key, values in plugin._whitelist_runtime_umos.items()
        },
        "gate": plugin._gate.snapshot(),
        "sessions": {key: list(state.recent) for key, state in plugin.sessions.items()},
        # 与 whitelist_runtime_umos 同理：白名单变更经 _prune_session 会 pop 掉
        # 非白名单会话的裁决条目，不同样快照的话回滚补不回来。
        # record_decision 是整体替换而非原地改，浅拷贝即够。
        "last_decisions": dict(plugin._last_decisions),
        "delay_umos": set(plugin._delay_tasks),
    }


def _restore_session_history(plugin: SelfInitiatedReplyPlugin, saved: dict[str, list[Any]]) -> None:
    """逐键恢复会话历史，保持 sessions dict 与存活 SessionState 的对象身份。

    在途检查任务持有 ``SessionState`` 引用，整表换对象会让它们写孤儿状态。
    故存活键原地重绑 ``recent``，新增键才插新对象，窗口内新增的键删除。
    ``maxlen`` 用回滚后的 settings（快照时刻的上限可能已被本次失败的应用改小）。
    """
    limit = plugin.settings.recent_message_limit
    for key in list(plugin.sessions):
        if key not in saved and not session_whitelisted(key, plugin.settings.whitelist):
            del plugin.sessions[key]
    for key, records in saved.items():
        state = plugin.sessions.get(key)
        if state is None:
            plugin.sessions[key] = SessionState(recent=deque(records, maxlen=limit))
        else:
            # 消息入口不持配置锁：按对象身份保留 apply await 窗口内追加的新记录。
            saved_ids = {id(record) for record in records}
            new_records = [record for record in state.recent if id(record) not in saved_ids]
            state.recent = deque([*records, *new_records], maxlen=limit)


async def _restore_plugin_state(
    plugin: SelfInitiatedReplyPlugin,
    snapshot: dict[str, Any],
    *,
    pruned: dict[str, Any] | None = None,
    pruned_images: dict[str, Any] | None = None,
    extra_delay_umos: set[str] | None = None,
    cleared_events: dict[str, tuple[Any, float, int]] | None = None,
) -> None:
    """恢复配置应用前快照，重建被取消的延迟检查并恢复任务拓扑。

    ``pruned`` 是 ``_whitelist.replace()`` 摘下的会话状态（键 → 原对象），
    必须在 ``_restore_session_history`` **之前**回填：不填则这些键走新建
    分支，日配额与冷却清零且对象身份丢失；顺序反了则回填会被随后的删键
    抹掉。``replace`` 全程同步且紧跟快照，``pruned`` 的键必然都在快照里。

    ``cleared_events`` 仅含本次白名单或停用操作清掉的事件及失效后的代次。
    窗口内又被失效或被新事件替换的条目不恢复。``pruned_images`` 是白名单收缩
    前摘下的图片索引；只补当前仍缺席的键，保存期间新收的图不受影响。
    """
    # 原地恢复（保持 Settings 与容器对象身份）：组件构造时捕获的是这些
    # 对象本身的引用，属性重绑定会让它们继续写孤儿容器，回滚后该会话
    # 主动回复静默停止直到重启。
    plugin.settings.apply(snapshot["settings"])
    plugin.runtime_enabled = snapshot["runtime_enabled"]
    runtime_umos = dict(plugin._whitelist_runtime_umos)
    restore_container_inplace(plugin._whitelist_runtime_umos, snapshot["whitelist_runtime_umos"])
    # 保存期间的新会话仍需被裸群号巡检找到。
    for key, values in runtime_umos.items():
        kept = {umo for umo in values if session_whitelisted(umo, plugin.settings.whitelist)}
        if kept:
            plugin._whitelist_runtime_umos.setdefault(key, set()).update(kept)
    # 原地 clear+update：scheduler 与 pipeline 持同一个 dict 引用，重绑定会让
    # janitor 继续回收旧容器里的条目，回滚内容对不上。
    restore_container_inplace(plugin._last_decisions, snapshot["last_decisions"])
    plugin._gate.restore(snapshot["gate"])
    if pruned:
        plugin.sessions.update(pruned)
    if pruned_images:
        plugin._coordinator.restore_images(pruned_images)
    _restore_session_history(plugin, snapshot["sessions"])
    for umo, (event, active_at, generation) in (cleared_events or {}).items():
        if (
            plugin._gate.current(umo) == generation
            and umo in plugin.sessions
            and session_whitelisted(umo, plugin.settings.whitelist)
            and umo not in plugin._last_events
        ):
            plugin._last_events[umo] = event
            plugin._last_event_at[umo] = active_at
    # 已取消的任务不可复用；停用窗口内新排的延迟检查也需要重建。
    for umo in snapshot["delay_umos"] | (extra_delay_umos or set()):
        if umo in plugin.sessions and not plugin._stopping:
            try:
                # 显式传当前代次，排队不推进 token；若新消息随后推进代次，调度器
                # 会按自己的代次检查丢弃过期延迟任务。
                plugin._scheduler.schedule_delayed_check(
                    umo,
                    delay_sec=None,
                    trigger=CheckTrigger.MESSAGE_DELAY,
                    force=False,
                    generation=plugin._gate.current(umo),
                )
            except Exception as re_exc:
                logger.debug(
                    "[%s] delayed check reschedule failed on rollback session=%s error=%s",
                    PLUGIN_ID,
                    umo,
                    redact_exc_text(re_exc),
                )
        else:
            logger.debug("[%s] delayed check dropped on rollback session=%s", PLUGIN_ID, umo)
    # 回滚后一律丢弃 parser 缓存，避免残留按失败配置建的实例
    plugin._vision.clear_parsers()
    # 回滚后恢复任务拓扑：禁用路径可能已停掉 patrol/cleanup，
    # 否则出现 runtime_enabled=True 但巡检永久停止的不一致态。
    if snapshot["runtime_enabled"] and not plugin._stopping:
        plugin._scheduler.ensure_patrol()
        plugin._scheduler.ensure_image_cleanup()
    try:
        await plugin._persist_config()
        await plugin._save_storage()
    except Exception as rollback_exc:
        logger.error(
            "[%s] config rollback persistence failed: %s",
            PLUGIN_ID,
            redact_exc_text(rollback_exc),
        )


async def _apply_config_updates(
    plugin: SelfInitiatedReplyPlugin,
    updates: dict[str, Any],
    *,
    submitted: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """应用配置变更；失败时恢复配置和本次清理的会话素材。"""
    snapshot = _snapshot_plugin_state(plugin)
    # 被白名单变更摘下的会话状态（键 → 原对象）。必须在回滚路径可见，
    # 故声明在 try 之外；见 _restore_plugin_state 的 pruned 参数。
    pruned: dict[str, Any] = {}
    pruned_images: dict[str, Any] = {}
    window_delay_umos: set[str] = set()
    cleared_events: dict[str, tuple[Any, float, int]] = {}
    try:
        candidate = plugin.settings.to_config_dict()
        # 提交类型已校验；值的规范化与磁盘加载共用 Settings.from_config。
        candidate.update(updates)
        new_settings = Settings.from_config(candidate)
        vision_changed = any(
            getattr(plugin.settings, spec.attr) != getattr(new_settings, spec.attr)
            for spec in CONFIG_SPECS
            if spec.key.startswith("vision_")
        )
        # 原地应用（保持 Settings 对象身份）：组件构造时各存引用，整体替换
        # 会造成热更新后组件读旧值。
        plugin.settings.apply(new_settings)
        if "whitelist_sessions" in updates:
            events = {
                umo: (event, plugin._last_event_at[umo])
                for umo, event in plugin._last_events.items()
                if umo in plugin._last_event_at
            }
            # 被移出白名单的会话连图片索引一起被 clear_session 摘掉，先留一份；
            # 回滚时这些键如果仍然缺席就补回去，保存期间新收的图不受影响。
            pruned_images = plugin._coordinator.snapshot_images()
            pruned = plugin._whitelist.replace(new_settings.whitelist)
            for umo, (event, active_at) in events.items():
                if umo not in plugin._last_events:
                    # replace 已 prune 代次；临时 token 区分随后发生的 invalidate/prune。
                    cleared_events[umo] = (event, active_at, plugin._gate.advance(umo))
        if vision_changed:
            plugin._vision.clear_parsers()
        if updates:
            # 锁内复查停止态：_mark_degraded 不经 _config_lock 就置位 _stopping
            # （它由生成/判断的隔离回调触发），入口处那一次检查会被横跨 DEGRADED
            # 迁移的请求绕过。此处抛异常走下方 except 的完整回滚，不留下
            # 「磁盘已写新值、内存半应用」的中间态。
            if plugin._stopping:
                raise RuntimeError("插件正在关闭")
            await plugin._persist_config()
            await plugin._save_storage()

        # 持久 enabled 真正变化才重置运行态并同步任务拓扑：重复提交相同值
        # 不得改动运行态。本分支守「前端提交的 enabled 与现值不同」这一路。
        enabled_persisted_changed = (
            "enabled" in updates and snapshot["settings"].enabled != new_settings.enabled
        )
        if enabled_persisted_changed:
            plugin.runtime_enabled = new_settings.enabled
            if not new_settings.enabled:
                window_delay_umos = set(plugin._delay_tasks)
                if not pruned_images:
                    # 停用会经 invalidate→clear_session 摘掉全部图片索引，回滚要能补回。
                    pruned_images = plugin._coordinator.snapshot_images()
            await apply_enabled_topology(
                plugin, enabled=new_settings.enabled, cleared_events=cleared_events
            )
        elif plugin.runtime_enabled:
            plugin._scheduler.ensure_image_cleanup()
        # 影响后台任务拓扑的**其余**配置键也要重算注册：POST /config 是运行期
        # 改这些键的唯一入口（官方 Dashboard 走整插件 reload），只判 enabled
        # 会让「保存成功、状态显示开启、实际巡检不跑」持续到下次重启。
        # enabled_patrol_trigger 的关闭方向由循环自身的 while 条件退出，无需操作。
        if "enabled_patrol_trigger" in updates and plugin.runtime_enabled:
            plugin._scheduler.ensure_patrol()
        _log_audited_changes(snapshot, new_settings, updates)
        config = new_settings.to_config_dict()
        adjusted_fields = sorted(
            key
            for key, value in updates.items()
            if _config_update_was_adjusted(
                CONFIG_SPEC_BY_KEY[key],
                (submitted or {}).get(key, value),
                config,
            )
        )
        return {
            "ok": True,
            "config": config,
            "config_revision": config_revision(config),
            # enabled 是持久配置，runtime_enabled 是本次生效后的运行态，
            # 二者在 POST 边界上可能不同。
            "runtime_enabled": plugin.runtime_enabled,
            "adjusted_fields": adjusted_fields,
        }
    except (asyncio.CancelledError, Exception):
        # 取消也要回滚：CancelledError 不落 except Exception，而异常抛出前已执行
        # 的那些步骤可能已完成，留下"磁盘已写新值、内存未回滚"的半应用态。普通
        # 异常同理。两个分支形状完全相同，合并只为不养两份逐字重复的实现。
        #
        # 回滚必须**跑完**才能放锁：它经 ensure_future 成游离任务后不持
        # _config_lock（_api_post_config 的 async with 随本异常释放锁），于是一
        # 次后来的 POST /config（或 /selfreply add|remove，同一把锁）能拿到锁并
        # 完整落盘，随后这个回滚用它开服时取的旧快照再写一次盘，把那次**成功**
        # 的保存静默撤销——重启后丢失，且全程无告警。
        #
        # 等待继续经 shield：第二次取消不能又把我们打回"立刻放弃等待"。
        await await_rollback(
            asyncio.ensure_future(
                _restore_plugin_state(
                    plugin,
                    snapshot,
                    pruned=pruned,
                    pruned_images=pruned_images,
                    extra_delay_umos=window_delay_umos,
                    cleared_events=cleared_events,
                )
            )
        )
        raise
    finally:
        for umo, (_event, _active_at, generation) in cleared_events.items():
            if not session_whitelisted(umo, plugin.settings.whitelist) and plugin._gate.is_current(
                umo, generation
            ):
                plugin._gate.prune(umo)


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
    """返回插件集成状态与聚合计数。面板零消费，运维/排障专用。

    ``lifecycle`` 是承重字段：生成超时且宿主吞取消即永久 DEGRADED，此后一切
    新工作被拒，而 ``runtime_enabled`` 读持久配置仍显示 True；没有这个字段
    运营者只能翻日志才能发现插件已死。

    只回计数不回明细：同义的 ``/selfreply status`` 只服务本会话，而这里若回
    多个会话的 UMO、代次与派生自群聊原文的 ``reason``，只是为一个面板零消费
    的端点扩大用户内容暴露面。
    """
    try:
        return {
            "ok": True,
            "runtime_enabled": plugin.runtime_enabled,
            "lifecycle": plugin.lifecycle_state,
            "whitelist_count": len(plugin.settings.whitelist),
            "decision_model_enabled": plugin.settings.decision_model_enabled,
            "gate": {
                "sessions": len(plugin._gate.generation_view),
                "running": len(plugin._gate.running_sessions_view),
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
            "decisions": len(plugin._last_decisions),
        }
    except Exception as exc:
        # 同 _api_get_config：详情只进服务端日志，不回显给调用方。
        logger.warning("[%s] api status failed: %s", PLUGIN_ID, redact_exc_text(exc))
        return {"ok": False, "error": "状态读取失败"}


def register_web_apis(plugin: SelfInitiatedReplyPlugin) -> None:
    """注册统一管理页面所需的 Web API。

    这些路由挂在宿主的 /api/plug 下，鉴权由宿主 dashboard 中间件承担；
    metadata.yaml 已把最低宿主版本限制在 4.23.3，插件侧不重复实现鉴权。
    """
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
        "获取插件页面 UI 偏好（主题/压暗/粗体）",
    )
    register(
        f"{route}/ui/theme",
        partial(_api_post_ui_theme, plugin),
        ["POST"],
        "更新插件页面 UI 偏好（主题/压暗/粗体）",
    )
