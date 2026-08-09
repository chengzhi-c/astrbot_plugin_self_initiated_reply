from __future__ import annotations

import json
from collections import deque
from pathlib import Path

from .host_stubs import install_astrbot_stubs, load_package
from .source_contract import call_order, method_source, params_of

PACKAGE_NAME = "selfreply_test_package"


def _load_modules():
    install_astrbot_stubs()
    models = load_package(PACKAGE_NAME, "models")
    utils = load_package(PACKAGE_NAME, "utils")
    storage = load_package(PACKAGE_NAME, "storage")
    return models, utils, storage


def test_tool_call_marker_is_not_sent_as_a_reply() -> None:
    _, utils, _ = _load_modules()
    assert (
        utils.clean_reply("[tool call] send_emoji_by_id", allow_multiline=True, max_chars=200) == ""
    )
    assert (
        utils.clean_reply(
            "[historical tool call] search_emoji", allow_multiline=True, max_chars=200
        )
        == ""
    )
    assert (
        utils.clean_reply("[tool call]send_emoji_by_id", allow_multiline=True, max_chars=200) == ""
    )
    assert (
        utils.clean_reply("[historical tool call]search_emoji", allow_multiline=True, max_chars=200)
        == ""
    )
    assert utils.clean_reply("自然回复", allow_multiline=True, max_chars=200) == "自然回复"


def test_final_send_path_rechecks_generation_after_decorating_hook() -> None:
    """装饰钩子之后、真正发送之前必须再复核一次代次（旧回复不得越过钩子发出）。

    行为侧由 test_delivery_blindspots 的 ``_FlipGate(true_times=N)`` 逐点锚定；
    本条守的是结构：复核点存在且**位置在钩子之后、发送之前**。
    """
    assert "expected_generation" in params_of("delivery.py", "DeliveryRunner.send_reply")

    order = call_order(
        "delivery.py",
        "DeliveryRunner.send_reply",
        ("self._call_hook", "self._gate.is_current", "outbound.send"),
    )
    # 取第一个装饰钩子之后的片段：必须先出现 is_current，才允许出现 outbound.send
    hook_at = order.index("self._call_hook")
    after_hook = order[hook_at + 1 :]
    assert "self._gate.is_current" in after_hook, "装饰钩子后没有任何代次复核"
    assert after_hook.index("self._gate.is_current") < after_hook.index("outbound.send"), (
        f"发送发生在钩子后的代次复核之前，旧回复会越过失效边界发出：{order}"
    )

    # 委托壳必须把代次透传下去，否则 delivery 侧复核永远拿到 None（等于不复核）
    shell = method_source("main.py", "SelfInitiatedReplyPlugin._send_reply")
    assert "expected_generation=expected_generation" in shell


def test_bare_group_whitelist_keeps_platform_state_isolated(tmp_path: Path) -> None:
    _, utils, storage = _load_modules()
    whitelist = {"12345"}
    qq = "qq:GroupMessage:12345"
    telegram = "telegram:GroupMessage:12345"

    assert utils.session_whitelisted(qq, whitelist)
    assert utils.session_whitelisted(telegram, whitelist)
    assert utils.whitelist_storage_key(qq) == qq
    assert utils.whitelist_storage_key(telegram) == telegram

    state = storage.SessionState(recent=deque(maxlen=5))
    state.daily_count = 2
    sessions = {qq: state, telegram: storage.SessionState(recent=deque(maxlen=5))}
    path = tmp_path / "state.json"
    assert storage.write_sessions_payload(
        path, storage.build_sessions_payload(sessions, whitelist, 5)
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert set(payload["sessions"]) == {qq, telegram}


def test_malformed_session_record_does_not_abort_load(tmp_path: Path) -> None:
    _, _, storage = _load_modules()
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "sessions": {
                    "qq:GroupMessage:123": {
                        "last_active_at": "bad",
                        "daily_count": -99,
                        "recent": "not-a-list",
                    },
                    "qq:GroupMessage:456": {"recent": [{"role": "unknown", "text": "kept"}]},
                    "qq:GroupMessage:789": "not-an-object",
                }
            }
        ),
        encoding="utf-8",
    )

    sessions = storage.load_sessions(path, {"123", "456", "789"}, 5)
    assert sessions["qq:GroupMessage:123"].last_active_at == 0.0
    assert sessions["qq:GroupMessage:123"].daily_count == 0
    assert sessions["qq:GroupMessage:456"].recent[0].role == "user"
    assert "qq:GroupMessage:789" not in sessions


def test_corrupt_state_file_is_backed_up_and_load_continues(tmp_path: Path) -> None:
    """JSON 损坏时备份原文件（corrupt-<ts>）并继续以空状态加载，不得抛错。"""
    _, _, storage = _load_modules()
    path = tmp_path / "state.json"
    path.write_text("{not valid json", encoding="utf-8")

    sessions = storage.load_sessions(path, {"123"}, 5)

    backups = sorted(tmp_path.glob("state.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{not valid json"
    assert "123" in sessions  # 白名单会话仍以空状态创建


def test_version_mismatch_state_file_is_backed_up_and_best_effort_loaded(tmp_path: Path) -> None:
    """version 不符时备份原文件，仍尽力解析仍兼容的数据。"""
    _, _, storage = _load_modules()
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "version": 999,
                "sessions": {"qq:GroupMessage:123": {"last_active_at": 1.5, "daily_count": 3}},
            }
        ),
        encoding="utf-8",
    )

    sessions = storage.load_sessions(path, {"123"}, 5)

    backups = sorted(tmp_path.glob("state.json.corrupt-*"))
    assert len(backups) == 1
    assert sessions["qq:GroupMessage:123"].last_active_at == 1.5
    assert sessions["qq:GroupMessage:123"].daily_count == 3


def test_atomic_state_writer_leaves_previous_file_on_serialization_failure(tmp_path: Path) -> None:
    _, _, storage = _load_modules()
    path = tmp_path / "state.json"
    path.write_text('{"previous": true}', encoding="utf-8")
    assert not storage.write_sessions_payload(path, {"bad": object()})
    assert json.loads(path.read_text(encoding="utf-8")) == {"previous": True}


def test_session_state_record_proactive_attempt_confirmed_and_unconfirmed() -> None:
    """record_proactive_attempt 单点写入语义：confirmed 写历史，unconfirmed 只消耗配额。"""
    models, _, _ = _load_modules()

    state = models.SessionState()
    state.record_proactive_attempt(confirmed=True, text="你好呀", at=10.0)
    assert state.last_proactive_at == 10.0
    assert state.daily_count == 1
    assert state.last_proactive_text == "你好呀"
    assert len(state.recent) == 1
    assert state.recent[-1].role == "assistant"
    assert state.recent[-1].text == "你好呀"

    state.record_proactive_attempt(confirmed=False, text="", at=20.0)
    assert state.last_proactive_at == 20.0
    assert state.daily_count == 2
    # UNKNOWN 投递不写历史条目
    assert len(state.recent) == 1
    assert state.last_proactive_text == "你好呀"


def test_record_proactive_attempt_refreshes_day_before_counting() -> None:
    """跨零点记账必须落在当日键上，否则今日配额被白送一次。

    调用方 `_check_session_locked` 的跨天刷新发生在判断+生成之前，二者相隔
    可达数十秒（判断超时 20s + 生成超时 60s）。若本方法不自带刷新，跨零点的
    自增会记到昨日键，随下一次 refresh_day 归零 —— 日配额闸门被绕过一次。
    """
    models, _, _ = _load_modules()

    state = models.SessionState()
    state.daily_key = "2000-01-01"  # 伪造昨日键
    state.daily_count = 5

    state.record_proactive_attempt(confirmed=True, text="跨天", at=30.0)

    assert state.daily_key == models.today_key(), "记账未刷新到当日键"
    assert state.daily_count == 1, "昨日计数未归零，配额被白送"


# ============================================================================
# event_umo 归一化（0.9.3 C2：盲区分类后确认为真实逻辑，补测）
# ============================================================================


class _UmoEvent:
    """最小事件桩：只提供 event_umo 依赖的三个宿主字段。"""

    def __init__(self, umo: str, group_id: str = "") -> None:
        self.unified_msg_origin = umo
        self._group_id = group_id

    def get_group_id(self) -> str:
        return self._group_id


def test_event_umo_rewrites_group_session_to_group_id() -> None:
    """群聊 UMO 的第三段必须改写为 group_id，否则同群不同发言者被算作不同会话。

    宿主给出的 session_id 在部分适配器上是「发言者」而非「群」。不改写会让
    白名单按人生效、每人各自计一份日配额——主动回复的会话粒度直接失效。
    """
    _, utils, _ = _load_modules()

    # 群聊：第三段被 group_id 覆盖
    assert utils.event_umo(_UmoEvent("qq:GroupMessage:sender-1", "group-9")) == (
        "qq:GroupMessage:group-9"
    )
    # 非群聊（msg_type 不含 group）：保留原 session_id，不得被 group_id 污染
    assert utils.event_umo(_UmoEvent("qq:FriendMessage:user-1", "group-9")) == (
        "qq:FriendMessage:user-1"
    )
    # 群聊但取不到 group_id：退回原 session_id，不产生空段
    assert utils.event_umo(_UmoEvent("qq:GroupMessage:sender-1", "")) == (
        "qq:GroupMessage:sender-1"
    )


def test_event_umo_passes_through_non_triplet_shapes() -> None:
    """段数不足 3 的 UMO 原样返回；空值返回空串——不得拼出畸形键。

    畸形键会成为 state.json 里永不回收的孤儿记录（白名单永不匹配它）。
    """
    _, utils, _ = _load_modules()

    assert utils.event_umo(_UmoEvent("qq:GroupMessage", "group-9")) == "qq:GroupMessage"
    assert utils.event_umo(_UmoEvent("bare-token", "group-9")) == "bare-token"
    assert utils.event_umo(_UmoEvent("", "group-9")) == ""
    # 第三段带空白：strip 后返回，避免 " x" 与 "x" 成为两个会话
    assert utils.event_umo(_UmoEvent("qq:FriendMessage:  user-1  ", "")) == (
        "qq:FriendMessage:user-1"
    )


# ============================================================================
# 历史记录归一化（0.9.3 C2：盲区分类后确认为真实逻辑，补测）
# ============================================================================


def test_dedupe_keeps_latest_occurrence_in_original_slot() -> None:
    """同 (role, 归一化文本) 重复时保留**最新**那条，但占用最早出现的位置。

    这两条语义各自都有理由，且都零测试守护过：
    - 保留最新：后到的记录带更新的 ``at``/``sender_id``，是判断"谁刚说过"的依据；
      改成保留最早会让 last_active 类判断读到过期时间戳。
    - 占用最早位置：聊天记录必须保持时间顺序，把重复项移到末尾会让模型
      误读对话顺序。
    """
    models, utils, _ = _load_modules()

    def rec(text, at, sender):
        return models.MessageRecord(role="user", name="u", text=text, sender_id=sender, at=at)

    result = utils.dedupe_message_records(
        [
            rec("重复内容", 100.0, "s1"),
            rec("其他内容", 200.0, "s2"),
            rec("重复内容", 300.0, "s3"),  # 与第一条重复
        ]
    )

    assert len(result) == 2, "重复项未被合并"
    assert result[0].text == "重复内容"
    assert result[0].at == 300.0, "保留了最早那条，last_active 类判断会读到过期时间"
    assert result[0].sender_id == "s3", "保留了最早那条的发送者"
    assert result[1].text == "其他内容", "去重打乱了时间顺序"


def test_dedupe_normalizes_whitespace_and_drops_blank() -> None:
    """空白差异视为同一条；纯空白记录直接丢弃，不占历史预算。"""
    models, utils, _ = _load_modules()

    def rec(text, at=0.0):
        return models.MessageRecord(role="user", name="u", text=text, sender_id="s", at=at)

    result = utils.dedupe_message_records(
        [rec("你  好"), rec("你 好"), rec("你\n好"), rec("   "), rec("")]
    )
    assert len(result) == 1, "空白归一化失效或空记录未被丢弃"

    # role 参与键：同文本不同角色不得合并（否则用户话被机器人回复顶掉）
    assistant = models.MessageRecord(
        role="assistant", name="bot", text="你好", sender_id="", at=0.0
    )
    both = utils.dedupe_message_records([rec("你好"), assistant])
    assert len(both) == 2, "role 未参与去重键，用户与助手消息被错误合并"


def test_parse_decision_json_truncates_overlong_reason() -> None:
    """裁决理由超长必须截断——它会进日志与 /status 面板，不设上限即放大攻击面。

    理由文本来自判断模型，而模型输入含不可信的群聊内容。不截断的话，
    一条超长理由会淹没日志（可用于掩盖其他审计记录）。
    """
    _, utils, _ = _load_modules()

    long_reason = "长" * 500
    parsed = utils.parse_decision_json(f'{{"should_reply": true, "reason": "{long_reason}"}}')
    assert parsed is not None
    assert len(parsed["reason"]) == 203, "截断长度偏离契约（200 + 省略号）"
    assert parsed["reason"].endswith("..."), "截断未标记，读者无法判断内容不完整"
    assert parsed["should_reply"] is True

    # 未超长的理由原样保留，截断不得误伤正常输出
    ok = utils.parse_decision_json('{"should_reply": false, "reason": "群里在聊别的"}')
    assert ok is not None and ok["reason"] == "群里在聊别的"


def test_content_to_text_handles_all_host_content_shapes() -> None:
    """宿主消息 content 有四种形态，任一形态取不到文本即丢失该条历史。

    形态来源：不同 Provider 对 message.content 的建模不同——纯字符串、
    多模态分片列表（dict 或对象）、单个 dict、单个对象。这里逐形态锁定，
    因为漏掉任一种不会报错，只会让历史记录静默变空，判断模型据此误判。
    """
    _, utils, _ = _load_modules()

    # 1) 纯字符串：去首尾空白
    assert utils.content_to_text("  你好  ") == "你好"

    # 2) 分片列表 - dict 形态：按顺序拼接，空 text 跳过
    assert utils.content_to_text([{"text": "第一句"}, {"text": ""}, {"text": "第二句"}]) == (
        "第一句\n第二句"
    )

    # 3) 分片列表 - 对象形态（无 text 属性的分片必须被跳过，如 image_url 分片）
    class Part:
        def __init__(self, text=None):
            if text is not None:
                self.text = text

    assert utils.content_to_text([Part("对象分片"), Part(), Part("另一片")]) == ("对象分片\n另一片")

    # 4) 单个 dict：text 优先，content 兜底
    assert utils.content_to_text({"text": "取 text"}) == "取 text"
    assert utils.content_to_text({"content": "回退 content"}) == "回退 content"

    # 5) 单个对象：读 text 属性；取不到返回空串而非抛异常
    assert utils.content_to_text(Part("裸对象")) == "裸对象"
    assert utils.content_to_text(object()) == ""
    assert utils.content_to_text(None) == ""
