"""配置热更新一致性红灯测试（0.9.0 轴 A）。

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

from .host_stubs import with_plugin

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
