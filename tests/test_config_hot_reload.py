"""配置热更新一致性红灯测试（0.9.0 轴 A）。

缺陷链：webapi._apply_config_updates 对 plugin.settings 做整体替换，
而 decision/delivery/generation/scheduler/whitelist 五组件在构造时各存
self.settings 旧引用 → 热更新后组件读过期配置，533 基线测试不暴露
（现有断言只看 plugin.settings 新值，不看组件侧读取路径）。

修复后契约：Settings 单一实例，热更新与回滚都保持对象身份（apply 原地
写字段），全部组件经既有引用即时可见新值。
"""

from __future__ import annotations

import ast
import sys

import pytest

from .host_stubs import ROOT, production_py_files, with_plugin
from .source_contract import constructor_param_bindings, module_ast

PACKAGE = "selfreply_main_test_package"
UMO = "fake:group:123"


@pytest.fixture(autouse=True)
def _bootstrap():
    from .host_stubs import load_main

    load_main()
    yield
    web = sys.modules.get("astrbot.api.web")
    if web is not None:
        web.request.payload = {}


def test_hot_reload_reaches_components(tmp_path) -> None:
    """POST /config 改冷却/延迟后，decision/scheduler 组件必须读到新值。"""

    async def scenario(plugin, main):
        decision_identity = plugin._decision.settings
        scheduler_identity = plugin._scheduler.settings

        web = sys.modules["astrbot.api.web"]
        web.request.payload = {"cooldown_sec": 777, "message_delay_sec": 88}
        result = await plugin._api_post_config()
        assert result["ok"] is True

        # 单一实例契约：插件与组件持有同一 Settings 对象
        assert plugin.settings is decision_identity
        assert plugin.settings is scheduler_identity
        assert plugin.settings.cooldown_sec == 777
        assert plugin.settings.message_delay_sec == 88

        # decision 路径：刚主动回复过的会话，新冷却必须立即生效
        # （last_active_at 置非零先过静默门，才能到达冷却判定）
        state = plugin._state_for(UMO)
        state.last_active_at = 50.0
        state.last_proactive_at = 100.0
        plugin._decision._clock = lambda: 200.0  # 距上次回复 100s < 777s
        gate = plugin._decision.local_gate(state, force=False)
        assert "冷却中" in gate, f"组件读到过期 cooldown_sec：{gate!r}"

        # scheduler 路径：消息触发延迟必须读到新 message_delay_sec
        assert plugin._scheduler.message_trigger_delay("message_delay") == 88

    with_plugin(tmp_path, scenario)


def test_hot_reload_reaches_generation_and_whitelist(tmp_path) -> None:
    """generation 与 whitelist 组件同样不得持有过期 Settings。"""

    async def scenario(plugin, main):
        generation_identity = plugin._generation.settings
        whitelist_identity = plugin._whitelist.settings

        web = sys.modules["astrbot.api.web"]
        web.request.payload = {"max_reply_chars": 42}
        result = await plugin._api_post_config()
        assert result["ok"] is True

        assert plugin.settings is generation_identity
        assert plugin.settings is whitelist_identity
        assert plugin._generation.settings.max_reply_chars == 42

    with_plugin(tmp_path, scenario)


def test_rollback_restore_component_visible_settings(tmp_path) -> None:
    """配置应用失败回滚后，组件经既有引用看到恢复的旧值（同一实例）。"""

    async def scenario(plugin, main):
        decision_identity = plugin._decision.settings
        old_cooldown = plugin.settings.cooldown_sec

        async def boom():
            raise OSError("disk full")

        plugin._save_storage = boom
        web = sys.modules["astrbot.api.web"]
        web.request.payload = {"cooldown_sec": 999}
        result = await plugin._api_post_config()
        assert result["ok"] is False

        # 回滚后同一实例恢复旧值，组件立即可见
        assert plugin.settings is decision_identity
        assert plugin.settings.cooldown_sec == old_cooldown
        assert plugin._decision.settings.cooldown_sec == old_cooldown

    with_plugin(tmp_path, scenario)


def test_recent_message_limit_hot_reload_rebuilds_existing_deques(tmp_path) -> None:
    """recent_message_limit 热更新必须对存量会话生效（0.9.3 A6）。

    缺陷：deque 的 maxlen 是构造期常量，`apply()` 只改 Settings 字段，
    存量会话的 deque 仍持旧上限——调大后新上限永不兑现，且设置页保存
    不触发插件重载，用户看到的值与实际生效值长期不一致且不报错。
    修复后契约：读取路径（_state_for）惰性重建，调大调小都即时兑现。
    """

    async def scenario(plugin, main):
        models = sys.modules[f"{PACKAGE}.models"]
        state = plugin._state_for(UMO)
        assert state.recent.maxlen == plugin.settings.recent_message_limit

        for index in range(8):
            state.recent.append(
                models.MessageRecord(role="user", name="U", text=f"m{index}", at=float(index))
            )

        # 调大：存量会话的上限必须跟着涨
        web = sys.modules["astrbot.api.web"]
        web.request.payload = {"recent_message_limit": 50}
        assert (await plugin._api_post_config())["ok"] is True
        grown = plugin._state_for(UMO)
        assert grown.recent.maxlen == 50, "调大后存量会话仍持旧上限"
        assert [item.text for item in grown.recent] == [f"m{i}" for i in range(8)], "重建丢历史"

        # 调小：立即截断，且保留最近的而非最早的
        web.request.payload = {"recent_message_limit": 3}
        assert (await plugin._api_post_config())["ok"] is True
        shrunk = plugin._state_for(UMO)
        assert shrunk.recent.maxlen == 3
        assert [item.text for item in shrunk.recent] == ["m5", "m6", "m7"], "截断须保留最近条目"

    with_plugin(tmp_path, scenario)


def test_settings_apply_preserves_identity() -> None:
    """Settings.apply 原地写入全部字段，对象身份不变（无 __slots__/frozen 前提）。"""
    from .host_stubs import load_package

    models = load_package(PACKAGE, "models")
    settings = models.Settings.from_config({})
    other = models.Settings.from_config({"cooldown_sec": 123, "max_reply_chars": 9})
    settings.apply(other)
    assert settings.cooldown_sec == 123
    assert settings.max_reply_chars == 9
    # 未被显式覆盖的字段与来源一致（from_config 同默认值）
    assert settings.min_silence_sec == other.min_silence_sec


def test_from_config_migrates_legacy_alias_keys() -> None:
    """0.9.2 B2 迁移护栏：旧配置文件只有别名键时不丢值；正式键优先。"""
    from .host_stubs import load_package

    models = load_package(PACKAGE, "models")
    legacy = models.Settings.from_config(
        {
            "whitelist": ["旧白名单"],
            "cooldown_seconds": 33,
            "idle_trigger_seconds": 66,
            "min_context_messages": 7,
        }
    )
    assert legacy.whitelist == {"旧白名单"}
    assert legacy.cooldown_sec == 33
    assert legacy.message_delay_sec == 66
    assert legacy.decision_history_min_messages == 7

    # proactive_threshold 为二级回退；正式键始终优先于别名
    threshold = models.Settings.from_config({"proactive_threshold": 9})
    assert threshold.decision_history_min_messages == 9
    precedence = models.Settings.from_config(
        {"decision_history_min_messages": 4, "min_context_messages": 9}
    )
    assert precedence.decision_history_min_messages == 4

    # 落盘只写正式键：一次 load+save 后别名自然消失
    persisted = legacy.to_config_dict()
    assert persisted["whitelist_sessions"] == ["旧白名单"]
    assert "whitelist" not in persisted
    assert "cooldown_seconds" not in persisted


def test_config_rollback_preserves_container_identity(tmp_path) -> None:
    """B1 回归：回滚必须原地恢复共享容器，不得属性重绑定。

    缺陷链：``_restore_plugin_state`` 曾用 ``plugin._last_events = snapshot[...]``
    整体替换 dict，而 scheduler/coordinator/whitelist 构造时捕获的是原 dict
    对象引用 → 一次失败的配置 POST 后协作对象继续写孤儿容器、main 从新容器
    读，该会话主动回复静默停止直到重启。

    与 Settings 身份断言（0.9.0 轴 A）同构：容器身份必须存活。
    """

    async def scenario(plugin, main):
        scheduler_events = plugin._scheduler._last_events
        coordinator_events = plugin._coordinator._events
        whitelist_sessions = plugin._whitelist._sessions
        coordinator_event_at = plugin._coordinator._event_at

        # 先注入一份运行态，确保快照有内容可回滚
        event = object()
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0
        plugin._state_for(UMO).daily_count = 7

        # 让配置持久化失败触发回滚
        original_sync = plugin._sync_whitelist

        def failing_sync():
            raise OSError("sync failed")

        plugin._sync_whitelist = failing_sync
        try:
            web = sys.modules["astrbot.api.web"]
            web.request.payload = {"cooldown_sec": 777}
            result = await plugin._api_post_config()
            assert result.get("ok") is False
        finally:
            plugin._sync_whitelist = original_sync

        # 容器身份必须存活：协作对象仍持有同一 dict 对象
        assert plugin._last_events is scheduler_events
        assert plugin._coordinator._events is coordinator_events
        assert plugin._coordinator._event_at is coordinator_event_at
        assert plugin._whitelist._sessions is whitelist_sessions
        assert plugin._scheduler._last_events is plugin._last_events
        # 回滚后内容恢复：事件与时间戳仍在
        assert plugin._last_events.get(UMO) is event
        assert plugin._last_event_at.get(UMO) == 1.0

    with_plugin(tmp_path, scenario)


# _restore_plugin_state 原地恢复的 5 个容器 → 全部持有者绑定。
# 上一条守卫只钉了 4 个绑定（_last_events 的 scheduler/coordinator 侧、
# _coordinator._event_at、_whitelist._sessions），其余 7 个当时无人看守：
# delivery/generation 侧的 _last_events、scheduler 侧的 _last_event_at 与
# _recent_image_events/_whitelist_runtime_umos、coordinator 侧的 _images、
# whitelist 侧的 _runtime_umos——任一处退回属性重绑定都不会变红。
# 表驱动而非逐行 assert，是为了新增持有者时只加一行、且失败信息能点名是谁。
CONTAINER_HOLDERS: tuple[tuple[str, str, str], ...] = (
    ("_last_events", "_scheduler", "_last_events"),
    ("_last_events", "_delivery", "_last_events"),
    ("_last_events", "_generation", "_last_events"),
    ("_last_events", "_coordinator", "_events"),
    ("_last_event_at", "_scheduler", "_last_event_at"),
    ("_last_event_at", "_coordinator", "_event_at"),
    ("_recent_image_events", "_scheduler", "_recent_image_events"),
    ("_recent_image_events", "_coordinator", "_images"),
    ("_whitelist_runtime_umos", "_scheduler", "_whitelist_runtime_umos"),
    # 第 11 个绑定：由 test_container_holder_table_is_complete 从源码枚举出来，
    # 手写表原先漏了。它不是只读——whitelist.py:97/99 会写回 self._runtime_umos，
    # 正是 B1 的失效形态（回滚后写孤儿表 → 裸群号映射丢失）。
    ("_whitelist_runtime_umos", "_whitelist", "_runtime_umos"),
    ("sessions", "_whitelist", "_sessions"),
)


def test_config_rollback_preserves_every_container_holder(tmp_path) -> None:
    """回滚后**每一个**持有者都必须仍指向 main 侧的同一容器对象。

    与上一条的区别是覆盖面：这条按 ``CONTAINER_HOLDERS`` 表枚举全部 11 个
    绑定，而非抽查 4 个。缺陷模式同 B1——``_restore_plugin_state`` 里任何一
    行退回 ``plugin.X = snapshot[...]``，该容器的所有持有者都会继续读写孤儿
    对象，主动回复静默停止直到重启。
    """

    async def scenario(plugin, main):
        before = {
            (owner, attr): getattr(getattr(plugin, owner), attr)
            for _, owner, attr in CONTAINER_HOLDERS
        }

        # 灌入运行态，确保快照非空（空快照下身份断言可能因巧合而通过）
        event = object()
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0
        plugin._recent_image_events.setdefault(UMO, [])
        plugin._whitelist_runtime_umos.setdefault("12345", {UMO})
        plugin._state_for(UMO).daily_count = 7

        original_sync = plugin._sync_whitelist

        def failing_sync():
            raise OSError("sync failed")

        plugin._sync_whitelist = failing_sync
        try:
            web = sys.modules["astrbot.api.web"]
            web.request.payload = {"cooldown_sec": 777}
            result = await plugin._api_post_config()
            assert result.get("ok") is False, "配置持久化未失败，回滚路径没被触发"
        finally:
            plugin._sync_whitelist = original_sync

        for main_attr, owner, attr in CONTAINER_HOLDERS:
            main_container = getattr(plugin, main_attr)
            holder_container = getattr(getattr(plugin, owner), attr)
            assert holder_container is main_container, (
                f"回滚后 {owner}.{attr} 不再指向 plugin.{main_attr}："
                f"该持有者会读写孤儿容器，主动回复静默停止直到重启"
            )
            assert holder_container is before[(owner, attr)], (
                f"{owner}.{attr} 的容器对象在回滚中被换掉（应原地 clear+update）"
            )

        # 内容也必须回来，否则"身份保住了但数据清零"同样是静默失效
        assert plugin._last_events.get(UMO) is event
        assert plugin._last_event_at.get(UMO) == 1.0

    with_plugin(tmp_path, scenario)


# 上面两条守卫都靠手写的 CONTAINER_HOLDERS 表。表本身会过期：新增一个持有
# 共享容器的组件、或给现有组件加一个容器参数，表不会自动跟上，于是那个新
# 绑定又变成无人看守——正是 B1 当初的成因。下面这条从源码反推真实持有者，
# 钉死"表必须完整"，让漏登记在测试层就变红，而不是等线上静默失效。
COMPONENT_CONSTRUCTORS: tuple[tuple[str, str, str], ...] = (
    ("scheduler.py", "SessionScheduler", "_scheduler"),
    ("session_coordinator.py", "SessionCoordinator", "_coordinator"),
    ("delivery.py", "DeliveryRunner", "_delivery"),
    ("generation.py", "GenerationRunner", "_generation"),
    ("whitelist.py", "WhitelistManager", "_whitelist"),
)


def _restored_containers() -> set[str]:
    """从 ``_restore_plugin_state`` 反推被原地恢复的 main 侧容器名。

    判据是同一属性上先 ``.clear()`` 再以任意方式原地写入。写入方不限于
    ``.update()``——``clear()`` 后逐项下标赋值、``setdefault`` 都是语义等价的
    原地恢复，只认 ``update`` 会把等价重写误判成"该容器不再被原地恢复"，
    进而连带把它的持有者全部报成失效断言（假阳性）。

    反过来，``plugin.X = ...`` 属性重绑定要**从结果里剔除**：它不是写入方式之一，
    而是原地恢复被破坏的信号。

    此处的覆盖边界经实测厘清，别按直觉复述：**已登记**容器退回属性重绑定，即使
    没有这条剔除也会红——``clear()`` 进 ``cleared``、``update`` 消失使它不进
    ``written``，容器被剔出返回集，其持有者随即变成失效断言（``stale`` 方向）。
    实测把 ``plugin._last_events.update(...)`` 改成重绑定，未加本剔除时
    ``test_container_holder_table_is_complete`` 已经变红。

    这条剔除补的是另一个形态：**新增**容器一开始就用属性重绑定、且从未登记进
    ``CONTAINER_HOLDERS``。那时 ``missing`` 与 ``stale`` 两侧都空，守卫全绿，
    B1 复发而无人知晓。显式剔除让"重绑定"成为可被观察的信号而不是沉默的缺席。

    ``_gate`` 走 ``restore()`` 整表替换，不在此列（它没有外部持有者，
    另有 ``test_session_gate_tables_have_no_external_holders`` 看守）。
    """
    node = _lookup_restore()
    cleared: set[str] = set()
    written: set[str] = set()
    rebound: set[str] = set()

    def _plugin_attr(expr: ast.AST) -> str | None:
        """``plugin.X`` → ``"X"``；其余返回 None。"""
        if isinstance(expr, ast.Attribute) and ast.unparse(expr.value) == "plugin":
            return expr.attr
        return None

    for child in ast.walk(node):
        # 形态一：plugin.X.clear() / plugin.X.update(...) / plugin.X.setdefault(...)
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
            attr = _plugin_attr(child.func.value)
            if attr is not None:
                if child.func.attr == "clear":
                    cleared.add(attr)
                elif child.func.attr in {"update", "setdefault"}:
                    written.add(attr)
        # 形态二：plugin.X[k] = v（clear 后逐项赋值）
        # 形态三：plugin.X = ...（属性重绑定——**破坏**原地恢复，见下）
        elif isinstance(child, ast.Assign):
            for target in child.targets:
                if isinstance(target, ast.Subscript):
                    attr = _plugin_attr(target.value)
                    if attr is not None:
                        written.add(attr)
                else:
                    attr = _plugin_attr(target)
                    if attr is not None:
                        rebound.add(attr)
    # 属性重绑定不是等价写入：它换掉容器对象本身，组件手里的旧引用就此失联，
    # 正是 B1 的成因。所以不能并进 written（那会把「已破坏」记成「已恢复」，
    # 让持有者继续被登记、守卫继续绿）。从原地恢复集里剔除，使该容器的持有者
    # 变成 stale，test_container_holder_table_is_complete 的第二条断言随即变红。
    return (cleared & written) - rebound


def _lookup_restore() -> ast.AST:
    for node in ast.walk(module_ast("webapi.py")):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_restore_plugin_state":
            return node
    raise AssertionError("webapi.py 中找不到 _restore_plugin_state")


def _actual_holders() -> set[tuple[str, str, str]]:
    """扫 main.py 的组件装配段，反推 (main 容器名, 组件属性, 组件字段) 三元组。"""
    restored = _restored_containers()
    holders: set[tuple[str, str, str]] = set()
    for rel, cls, owner_attr in COMPONENT_CONSTRUCTORS:
        bindings = constructor_param_bindings(rel, cls)
        for node in ast.walk(module_ast("main.py")):
            if not (isinstance(node, ast.Call) and ast.unparse(node.func) == cls):
                continue
            for kw in node.keywords:
                if kw.arg is None:
                    continue
                value = ast.unparse(kw.value)
                if not value.startswith("self."):
                    continue
                container = value[len("self.") :]
                if container in restored and kw.arg in bindings:
                    holders.add((container, owner_attr, bindings[kw.arg]))
    return holders


def test_container_holder_table_is_complete() -> None:
    """``CONTAINER_HOLDERS`` 必须等于源码里真实存在的持有者集合。

    漏登记 = 那个绑定无人看守：``_restore_plugin_state`` 里对应的一行退回
    属性重绑定时，没有任何测试会红，该会话主动回复静默停止直到重启（B1）。
    多登记 = 表里有已不存在的绑定，属于失效断言。两个方向都要红。
    """
    actual = _actual_holders()
    declared = set(CONTAINER_HOLDERS)

    missing = actual - declared
    stale = declared - actual
    assert not missing, (
        f"源码中存在未登记的共享容器持有者 {sorted(missing)}："
        f"请补进 CONTAINER_HOLDERS，否则该绑定退回属性重绑定时不会变红"
    )
    assert not stale, f"CONTAINER_HOLDERS 中的绑定在源码里已不存在（失效断言）：{sorted(stale)}"


GATE_RESTORED_TABLES = ("_session_generation", "_running_sessions", "_session_locks")


def _gate_restore_node() -> ast.FunctionDef:
    return next(
        node
        for node in ast.walk(module_ast("session_gate.py"))
        if isinstance(node, ast.FunctionDef) and node.name == "restore"
    )


def test_session_gate_restore_is_in_place_only() -> None:
    """``SessionGate.restore`` 必须原地 clear+update，禁止属性重绑定（契约 §11 B1）。

    历史形态是三次属性重绑定（``self._session_generation = snap[...]``），与 B1
    的缺陷写法同构；当时"安全"的唯一理由是"没有外部持有者"这个易失前提，且该
    前提对 release 表根本不成立——等待者持有具体 Event 对象（0.9.4 阶段 1.1）。
    改为原地恢复后 B1 合规由**结构**保证，本守卫钉死这一点。
    """
    restore_node = _gate_restore_node()
    rebound = {
        target.attr
        for stmt in ast.walk(restore_node)
        if isinstance(stmt, ast.Assign)
        for target in stmt.targets
        if isinstance(target, ast.Attribute) and ast.unparse(target.value) == "self"
    }
    assert not rebound, (
        f"SessionGate.restore 出现属性重绑定 {sorted(rebound)}：等待者与运行中的 "
        f"async with 持有容器/Event/Lock 对象本身的引用，换掉容器身份会让它们读写"
        f"孤儿表（契约 §11 B1）。改回 clear()+update() 原地恢复。"
    )
    cleared = {
        ast.unparse(node.func.value).removeprefix("self.")
        for node in ast.walk(restore_node)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "clear"
        and ast.unparse(node.func.value).startswith("self.")
    }
    assert set(GATE_RESTORED_TABLES) <= cleared, (
        f"restore 未清空全部三张表（实际 clear：{sorted(cleared)}）：漏清的表会残留回滚前的脏条目"
    )


def test_session_gate_tables_have_no_external_holders() -> None:
    """三张恢复表不得被外部直取——绕过 ``mark_running`` 的 release 语义。

    restore 已改原地（见上一条守卫），孤儿表风险消除；但外部直取仍会绕过
    ``mark_running``/``unmark_running`` 对 release 表的成对维护，制造与 0.9.4 阶段 1.1
    同类的闸门失同步。外界只应经 ``*_view`` property 或语义方法访问。

    ``rglob`` 而非 ``glob``（0.9.4 阶段 1.6）：image/ 子包 1234 行此前完全在视野外。
    """
    leaked: list[str] = []
    for path in production_py_files():
        if path.name == "session_gate.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Attribute) or node.attr not in GATE_RESTORED_TABLES:
                continue
            owner = ast.unparse(node.value)
            if owner.endswith("gate"):
                leaked.append(f"{path.relative_to(ROOT).as_posix()}: {owner}.{node.attr}")
    assert not leaked, (
        f"SessionGate 内部表被外部直取 {leaked}：绕过 mark_running/unmark_running 的 "
        f"release 成对维护会制造闸门失同步（0.9.4 阶段 1.1 同类缺陷）。改经 view property 读。"
    )
