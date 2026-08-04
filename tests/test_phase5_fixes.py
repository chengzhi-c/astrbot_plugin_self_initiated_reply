"""红灯测试（阶段 5 补）：P3 拾遗项的固化验证

复审发现阶段 5 的三项新逻辑无测试覆盖，此处补红灯测试：
- whitelist 校验：超长/非法字符条目必须被 _api_post_config 拒绝
- _whitelist_runtime_umos 回收：长期无活动映射在清理循环末尾被回收
- 管理员热读：cmd_config.json mtime 变化后运行期生效

每项均用 mutation 实测过：注入缺陷后对应测试变红。
"""

from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

from .host_stubs import with_plugin
from .test_main_runtime import _make_event


def test_api_post_config_rejects_oversized_whitelist_item(tmp_path: Path) -> None:
    """超过 MAX_WHITELIST_ITEM_LEN 的白名单条目必须被拒绝且不落库。"""

    async def scenario(plugin, main):
        models = importlib.import_module(f"{main.__package__}.models")
        web = sys.modules["astrbot.api.web"]
        web.request.payload = {"whitelist": ["x" * (models.MAX_WHITELIST_ITEM_LEN + 1)]}
        result = await plugin._api_post_config()
        assert result.get("ok") is False
        assert "过长" in result.get("error", "")
        assert all(len(item) <= models.MAX_WHITELIST_ITEM_LEN for item in plugin.settings.whitelist)
        # 合法更新仍须生效（防误杀正常白名单）
        web.request.payload = {"whitelist": ["正常会话"]}
        result = await plugin._api_post_config()
        assert result.get("ok") is True
        assert "正常会话" in plugin.settings.whitelist

    with_plugin(tmp_path, scenario)


def test_api_post_config_rejects_illegal_whitelist_chars(tmp_path: Path) -> None:
    """含控制符/引号/反斜杠的白名单条目必须被拒绝。"""

    async def scenario(plugin, main):
        web = sys.modules["astrbot.api.web"]
        for bad in ['bad"quote', "bad\\slash", "bad\x01ctrl"]:
            web.request.payload = {"whitelist": [bad]}
            result = await plugin._api_post_config()
            assert result.get("ok") is False, f"应拒绝 {bad!r}"
            assert "非法字符" in result.get("error", "")

    with_plugin(tmp_path, scenario)


def test_whitelist_runtime_umos_reclaimed_when_inactive(tmp_path: Path) -> None:
    """清理循环末尾必须回收长期无活动的运行时 UMO 映射，避免只增不减。"""

    async def scenario(plugin, main):
        stale_at = main.now_ts() - main.EVENT_CLEANUP_INTERVAL_SEC * 2
        plugin._whitelist_runtime_umos["group:1"] = {"group:1:user:a", "group:1:user:b"}
        plugin._last_events["group:1:user:a"] = _make_event()
        plugin._last_event_at["group:1:user:a"] = stale_at
        plugin._last_event_cleanup = 0  # 强制本次执行清理
        plugin._cleanup_old_events_if_needed()
        # a 的活动事件已陈旧：两个 UMO 都离开活跃集 → 整组回收
        assert "group:1" not in plugin._whitelist_runtime_umos

        # 对照组：有新鲜事件的会话必须保留
        fresh_at = main.now_ts()
        plugin._whitelist_runtime_umos["group:2"] = {"group:2:user:c"}
        plugin._last_events["group:2:user:c"] = _make_event()
        plugin._last_event_at["group:2:user:c"] = fresh_at
        plugin._last_event_cleanup = 0
        plugin._cleanup_old_events_if_needed()
        assert plugin._whitelist_runtime_umos.get("group:2") == {"group:2:user:c"}

    with_plugin(tmp_path, scenario)


def test_admin_ids_hot_reload_on_file_change(tmp_path: Path) -> None:
    """运行期修改 cmd_config.json 必须生效（mtime 缓存热读）。"""

    async def scenario(plugin, main):
        cmd = plugin._data_path / "cmd_config.json"
        assert plugin._refresh_admin_ids() == set()  # 初始无文件

        cmd.write_text('{"admins_id": ["111"]}', encoding="utf-8")
        assert plugin._refresh_admin_ids() == {"111"}

        time.sleep(0.02)  # 保证 mtime 变化
        cmd.write_text('{"admins_id": ["222"]}', encoding="utf-8")
        assert plugin._refresh_admin_ids() == {"222"}

        # 文件被删除：回退到最近一次缓存，不崩溃
        cmd.unlink()
        assert plugin._refresh_admin_ids() == {"222"}

    with_plugin(tmp_path, scenario)
