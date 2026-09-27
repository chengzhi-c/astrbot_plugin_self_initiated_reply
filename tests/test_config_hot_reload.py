"""配置热更新一致性红灯测试。

缺陷链：webapi._apply_config_updates 对 plugin.settings 做整体替换，
而 decision/delivery/generation/scheduler/whitelist 五组件在构造时各存
self.settings 旧引用 → 热更新后组件读过期配置，533 基线测试不暴露
（现有断言只看 plugin.settings 新值，不看组件侧读取路径）。

修复后契约：Settings 单一实例，热更新与回滚都保持对象身份（apply 原地
写字段），全部组件经既有引用即时可见新值。
"""

from __future__ import annotations

import sys

import pytest

from .host_stubs import (
    webapi_module,
    with_plugin,
)

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
        result = await webapi_module(main)._api_post_config(plugin)
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
        assert plugin._scheduler.message_trigger_delay() == 88

    with_plugin(tmp_path, scenario)


def test_hot_reload_reaches_generation_and_whitelist(tmp_path) -> None:
    """generation 与 whitelist 组件同样不得持有过期 Settings。"""

    async def scenario(plugin, main):
        generation_identity = plugin._generation.settings
        whitelist_identity = plugin._whitelist.settings

        web = sys.modules["astrbot.api.web"]
        web.request.payload = {"max_reply_chars": 42}
        result = await webapi_module(main)._api_post_config(plugin)
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
        result = await webapi_module(main)._api_post_config(plugin)
        assert result["ok"] is False

        # 回滚后同一实例恢复旧值，组件立即可见
        assert plugin.settings is decision_identity
        assert plugin.settings.cooldown_sec == old_cooldown
        assert plugin._decision.settings.cooldown_sec == old_cooldown

    with_plugin(tmp_path, scenario)


def test_recent_message_limit_hot_reload_rebuilds_existing_deques(tmp_path) -> None:
    """recent_message_limit 热更新必须对存量会话生效。

    缺陷：deque 的 maxlen 是构造期常量，`apply()` 只改 Settings 字段，
    存量会话的 deque 仍持旧上限，调大后新上限永不兑现，且设置页保存
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
        assert (await webapi_module(main)._api_post_config(plugin))["ok"] is True
        grown = plugin._state_for(UMO)
        assert grown.recent.maxlen == 50, "调大后存量会话仍持旧上限"
        assert [item.text for item in grown.recent] == [f"m{i}" for i in range(8)], "重建丢历史"

        # 调小：立即截断，且保留最近的而非最早的
        web.request.payload = {"recent_message_limit": 3}
        assert (await webapi_module(main)._api_post_config(plugin))["ok"] is True
        shrunk = plugin._state_for(UMO)
        assert shrunk.recent.maxlen == 3
        assert [item.text for item in shrunk.recent] == ["m5", "m6", "m7"], "截断须保留最近条目"

    with_plugin(tmp_path, scenario)


def test_rollback_restores_session_history_trimmed_during_apply_window(tmp_path) -> None:
    """回滚快照必须深保护会话历史：应用窗口内被新 limit 裁剪的历史不可丢。

    缺陷：快照对 sessions 只做浅拷贝（dict(plugin.sessions)），SessionState
    是共享引用。应用失败回滚恢复的是同一个已被新 recent_message_limit
    裁小的 deque，历史消息永久丢失，与 `_apply_config_updates` 自称的
    "任何失败回滚全部运行态"不符。
    """

    async def scenario(plugin, main):
        models = sys.modules[f"{PACKAGE}.models"]
        state = plugin._state_for(UMO)
        for index in range(8):
            state.recent.append(
                models.MessageRecord(role="user", name="U", text=f"m{index}", at=float(index))
            )
        original_limit = plugin.settings.recent_message_limit
        original_state = plugin.sessions[UMO]
        assert original_limit >= 8

        real_persist = plugin._persist_config
        calls = {"count": 0}

        async def trim_then_fail():
            calls["count"] += 1
            if calls["count"] == 1:
                # 模拟 await 窗口内到达的消息事件：读取路径按已应用的新
                # limit 惰性重建 deque，把历史裁小
                plugin._state_for(UMO)
                raise OSError("disk full")
            await real_persist()

        plugin._persist_config = trim_then_fail
        try:
            web = sys.modules["astrbot.api.web"]
            web.request.payload = {"recent_message_limit": 3}
            result = await webapi_module(main)._api_post_config(plugin)
            assert result["ok"] is False
        finally:
            plugin._persist_config = real_persist

        # 回滚后：对象身份不变（在途任务持引用）、上限恢复、历史完整
        restored = plugin.sessions[UMO]
        assert restored is original_state
        assert restored.recent.maxlen == original_limit, "回滚未恢复 deque 上限"
        assert [item.text for item in restored.recent] == [f"m{i}" for i in range(8)], (
            "回滚未恢复被窗口内裁剪的历史"
        )

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
    """迁移护栏：旧配置文件只有别名键时不丢值；正式键优先。"""
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


# _restore_plugin_state 原地恢复的 5 个容器 → 全部持有者绑定。
# 这 11 个绑定任一处退回属性重绑定都不会变红：scheduler/coordinator/whitelist
# 构造时捕获的是容器对象本身的引用，main 从新 dict 读而它们继续写旧 dict，
# 该会话主动回复静默停止直到重启，且不抛异常、无日志。
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
    # 手写表原先漏了。它不是只读，whitelist.py:97/99 会写回 self._runtime_umos，
    # 正是 B1 的失效形态（回滚后写孤儿表 → 裸群号映射丢失）。
    ("_whitelist_runtime_umos", "_whitelist", "_runtime_umos"),
    ("sessions", "_whitelist", "_sessions"),
)


def test_config_rollback_preserves_every_container_holder(tmp_path) -> None:
    """回滚后**每一个**持有者都必须仍指向 main 侧的同一容器对象。

    按 ``CONTAINER_HOLDERS`` 表枚举全部 11 个绑定。缺陷模式同 B1，
    ``_restore_plugin_state`` 里任何一行退回 ``plugin.X = snapshot[...]``，
    该容器的所有持有者都会继续读写孤儿对象，主动回复静默停止直到重启。
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

        original_persist = plugin._persist_config

        async def failing_persist():
            raise OSError("sync failed")

        plugin._persist_config = failing_persist
        try:
            web = sys.modules["astrbot.api.web"]
            web.request.payload = {"cooldown_sec": 777}
            result = await webapi_module(main)._api_post_config(plugin)
            assert result.get("ok") is False, "配置持久化未失败，回滚路径没被触发"
        finally:
            plugin._persist_config = original_persist

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


def test_assembled_components_share_plugin_containers(tmp_path) -> None:
    """装配后协作对象必须持有 plugin 侧同一份容器，而不是拷贝。"""

    async def scenario(plugin, main):
        containers = {
            id(plugin._last_events): "_last_events",
            id(plugin._last_event_at): "_last_event_at",
            id(plugin._recent_image_events): "_recent_image_events",
            id(plugin._whitelist_runtime_umos): "_whitelist_runtime_umos",
            id(plugin.sessions): "sessions",
        }
        owners = {
            "_scheduler": plugin._scheduler,
            "_coordinator": plugin._coordinator,
            "_delivery": plugin._delivery,
            "_generation": plugin._generation,
            "_whitelist": plugin._whitelist,
        }
        actual: set[tuple[str, str, str]] = set()
        for owner_name, owner in owners.items():
            for attr, value in vars(owner).items():
                name = containers.get(id(value))
                if name is not None:
                    actual.add((name, owner_name, attr))
        declared = set(CONTAINER_HOLDERS)
        assert actual == declared, (
            f"共享容器持有者漂移：missing={sorted(actual - declared)} "
            f"stale={sorted(declared - actual)}"
        )
        for main_attr, owner_name, attr in CONTAINER_HOLDERS:
            assert getattr(getattr(plugin, owner_name), attr) is getattr(plugin, main_attr)

    with_plugin(tmp_path, scenario)
