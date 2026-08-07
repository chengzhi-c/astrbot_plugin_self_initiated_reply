"""storage.py 覆盖率补盲（0.9.0 轴 D）：配置对象多态、原子写异常与容错分支。

盲区背景（补盲前 storage.py 72%）：_config_to_dict/_update_config_obj/
_persist_config_obj 的多态宿主对象分支、原子写未预期异常、加载容错的
未预期异常与会话级异常路径、迁移/同步的持久化失败出口。
"""

from __future__ import annotations

import json
from pathlib import Path

from .test_storage_and_umo import PACKAGE_NAME, _load_modules


def _storage_module():
    from .host_stubs import load_package

    return load_package(PACKAGE_NAME, "storage")


# ============================================================================
# 宿主配置对象多态分支
# ============================================================================


def test_config_to_dict_fallbacks() -> None:
    storage = _storage_module()

    class ItemsBoom:
        """items() 抛错 → 退回 dict() 构造路径。"""

        def items(self):
            raise RuntimeError("items broken")

        def keys(self):
            return ["a"]

        def __getitem__(self, key):
            return 1

    assert storage._config_to_dict(ItemsBoom()) == {"a": 1}

    class NotMappable:
        """dict() 也失败 → 空 dict 兜底。"""

    assert storage._config_to_dict(NotMappable()) == {}


def test_update_config_obj_variants() -> None:
    storage = _storage_module()
    assert storage._update_config_obj(None, {"a": 1}) is True

    class ItemSetter:
        """无 clear：逐键写入路径。"""

        def __init__(self) -> None:
            self.data: dict = {}

        def __setitem__(self, key, value) -> None:
            self.data[key] = value

    setter = ItemSetter()
    assert storage._update_config_obj(setter, {"a": 1}) is True
    assert setter.data == {"a": 1}

    class SetBoom:
        def __setitem__(self, key, value) -> None:
            raise RuntimeError("setitem broken")

    assert storage._update_config_obj(SetBoom(), {"a": 1}) is False


def test_persist_config_obj_variants() -> None:
    storage = _storage_module()
    assert storage._persist_config_obj(None, {}) is True

    class NoSave:
        pass

    assert storage._persist_config_obj(NoSave(), {}) is True

    class SaveOK:
        def save_config(self, data):
            self.data = data

    ok = SaveOK()
    assert storage._persist_config_obj(ok, {"a": 1}) is True
    assert ok.data == {"a": 1}

    class TypeErrThenOK:
        """save_config(data) 不支持 → 回退无参形态。"""

        def save_config(self, data=None):
            if data is not None:
                raise TypeError("no args supported")

    assert storage._persist_config_obj(TypeErrThenOK(), {"a": 1}) is True

    class TypeErrThenBoom:
        def save_config(self, data=None):
            raise TypeError("always broken")

    assert storage._persist_config_obj(TypeErrThenBoom(), {}) is False

    class SaveBoom:
        def save_config(self, data):
            raise RuntimeError("disk broken")

    assert storage._persist_config_obj(SaveBoom(), {}) is False


# ============================================================================
# 原子写与加载的未预期异常
# ============================================================================


def test_write_json_atomic_unexpected_error(tmp_path: Path) -> None:
    """parent 解析抛非 OSError：走未预期异常分支且不残留临时文件。"""
    storage = _storage_module()

    class WeirdPath:
        @property
        def parent(self):
            raise RuntimeError("parent broken")

    assert storage._write_json_atomic(WeirdPath(), {"a": 1}) is False


def test_load_config_data_unexpected_error() -> None:
    """读取配置抛未预期异常：error 留痕后回退宿主传入值。"""
    storage = _storage_module()

    class ReadBoomPath:
        def exists(self) -> bool:
            return True

        def read_text(self, encoding=None):
            raise RuntimeError("read broken")

    data = storage.load_config_data(ReadBoomPath(), {"base": 1})
    assert data == {"base": 1}


def test_load_sessions_unexpected_error_backs_up(tmp_path: Path) -> None:
    """解析抛未预期异常：备份原文件并返回空会话表。"""
    storage = _storage_module()
    path = tmp_path / "state.json"
    path.write_text("{}", encoding="utf-8")
    orig_loads = storage.json.loads

    def boom(text):
        raise RuntimeError("json broken")

    storage.json.loads = boom
    try:
        sessions = storage.load_sessions(path, set(), 5)
    finally:
        storage.json.loads = orig_loads
    assert sessions == {}
    assert not path.exists()  # 原文件已被备份移走


def test_load_sessions_non_dict_sessions(tmp_path: Path) -> None:
    """sessions 非 dict：归一为空表而非半加载。"""
    storage = _storage_module()
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"version": storage.STATE_VERSION, "sessions": "bad"}), "utf-8")
    assert storage.load_sessions(path, set(), 5) == {}


def test_load_sessions_malformed_recent_items(tmp_path: Path) -> None:
    """recent 中非 dict 条目与空文本条目跳过，合法条目保留。"""
    storage = _storage_module()
    path = tmp_path / "state.json"
    umo = "plat:GroupMessage:1"
    path.write_text(
        json.dumps(
            {
                "version": storage.STATE_VERSION,
                "sessions": {
                    umo: {
                        "recent": [
                            "not-a-dict",
                            {"text": ""},
                            {"text": "kept", "role": "user"},
                        ]
                    }
                },
            }
        ),
        "utf-8",
    )
    sessions = storage.load_sessions(path, {umo}, 5)
    assert [item.text for item in sessions[umo].recent] == ["kept"]


def test_load_sessions_record_exception_skips_session(tmp_path: Path) -> None:
    """单个会话解析抛错：warning 跳过，不中断其余加载。"""
    storage = _storage_module()
    path = tmp_path / "state.json"
    path.write_text("{}", encoding="utf-8")

    class GetBoom(dict):
        def get(self, key, default=None):
            if key == "last_active_at":
                raise RuntimeError("field broken")
            return super().get(key, default)

    orig_loads = storage.json.loads
    storage.json.loads = lambda text: {
        "version": storage.STATE_VERSION,
        "sessions": {"plat:GroupMessage:1": GetBoom()},
    }
    try:
        sessions = storage.load_sessions(path, {"plat:GroupMessage:1"}, 5)
    finally:
        storage.json.loads = orig_loads
    # 解析失败的会话被跳过；白名单 setdefault 重建的空壳不携带任何历史
    assert len(sessions["plat:GroupMessage:1"].recent) == 0
    assert sessions["plat:GroupMessage:1"].daily_count == 0


# ============================================================================
# 迁移/同步的持久化失败出口
# ============================================================================


def test_migrate_and_sync_fail_on_config_update(tmp_path: Path) -> None:
    """宿主配置对象写回失败：迁移/同步返回 False（磁盘 JSON 已写成功）。"""
    models, _, storage = _load_modules()
    settings = models.Settings.from_config({})
    path = tmp_path / "config.json"

    class SetBoom:
        def __setitem__(self, key, value) -> None:
            raise RuntimeError("config object broken")

    assert storage.migrate_config_file(path, SetBoom(), settings) is False
    assert storage.sync_config_whitelist(path, SetBoom(), settings) is False
