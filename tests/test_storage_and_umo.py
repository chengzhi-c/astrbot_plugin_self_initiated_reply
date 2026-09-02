from __future__ import annotations

import inspect
import json
from collections import deque
from pathlib import Path

from .host_stubs import install_astrbot_stubs, load_package

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


def test_parse_decision_json_rejects_missing_or_invalid_should_reply() -> None:
    _, utils, _ = _load_modules()

    assert utils.parse_decision_json('{"reason": "missing flag"}') is None
    assert utils.parse_decision_json('{"should_reply": []}') is None


def test_session_whitelisted_rejects_empty_umo() -> None:
    _, utils, _ = _load_modules()

    assert not utils.session_whitelisted("   ", {"12345"})


def test_session_is_private_treats_non_group_as_private() -> None:
    """私聊门闩按「不是群」判，不写死 FriendMessage。"""
    _, utils, _ = _load_modules()

    assert utils.session_is_private("qq:FriendMessage:user-1")
    assert utils.session_is_private("qq:PrivateMessage:user-1")
    assert not utils.session_is_private("qq:GroupMessage:123")
    assert not utils.session_is_private("fake:group:123")


def test_raw_umo_returns_empty_when_host_callable_fails() -> None:
    _, utils, _ = _load_modules()

    class BrokenEvent:
        def unified_msg_origin(self) -> str:
            raise RuntimeError("host failure")

    assert utils.raw_umo(BrokenEvent()) == ""


def test_event_self_id_returns_empty_when_host_getter_fails() -> None:
    _, utils, _ = _load_modules()

    class BrokenEvent:
        def get_self_id(self) -> str:
            raise RuntimeError("host failure")

    assert utils.event_self_id(BrokenEvent()) == ""


def test_event_extra_uses_two_arguments_when_host_signature_is_opaque() -> None:
    _, utils, _ = _load_modules()

    class OpaqueExtra:
        received: tuple[str, str] | None = None

        @property
        def __signature__(self):
            raise ValueError("signature unavailable")

        def __call__(self, key: str, default: str) -> str:
            self.received = (key, default)
            return "value"

    class Event:
        pass

    event = Event()
    getter = OpaqueExtra()
    event.get_extra = getter

    assert utils.event_extra(event, "key", default="fallback") == "value"
    assert getter.received == ("key", "fallback")


def test_event_extra_supports_single_argument_host_getter() -> None:
    _, utils, _ = _load_modules()

    class Event:
        pass

    event = Event()
    event.get_extra = lambda key: f"value:{key}"

    assert utils.event_extra(event, "key", default="fallback") == "value:key"


def test_event_extra_returns_default_for_incompatible_host_getter() -> None:
    _, utils, _ = _load_modules()

    class IncompatibleExtra:
        called = False

        @property
        def __signature__(self):
            return inspect.Signature([inspect.Parameter("key", inspect.Parameter.KEYWORD_ONLY)])

        def __call__(self, *_args: object) -> str:
            self.called = True
            return "unexpected"

    class Event:
        pass

    event = Event()
    getter = IncompatibleExtra()
    event.get_extra = getter

    result = utils.event_extra(event, "key", default="fallback")
    assert not getter.called
    assert result == "fallback"


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


# ============================================================================
# 外部时间戳两侧钳位（配对 models.MAX_CLOCK_SKEW_SEC）
# ============================================================================


def test_as_timestamp_clamps_both_directions() -> None:
    """``as_timestamp`` 的纯函数语义：负值归 0，远未来钳到 now+skew，NaN/inf 归 0。

    注入 ``now`` 而非取真实时钟——上界是动态的，用真实时钟断言会因毫秒级漂移偶发
    flaky（实测漂移 4.2ms 就足以让「等于上界」的断言翻面）。
    """
    models, _, _ = _load_modules()
    now = 1_000_000.0
    ceiling = now + models.MAX_CLOCK_SKEW_SEC

    assert models.as_timestamp(-1.0e9, now=now) == 0.0  # 负值 → 从未活跃
    assert models.as_timestamp(now + 1.0e9, now=now) == ceiling  # 远未来 → 钳到上界
    assert models.as_timestamp(float("nan"), now=now) == 0.0
    assert models.as_timestamp(float("inf"), now=now) == 0.0
    assert models.as_timestamp("bad", now=now) == 0.0
    assert models.as_timestamp(None, now=now) == 0.0
    assert models.as_timestamp(now - 300.0, now=now) == now - 300.0  # 正常值原样保留
    assert models.as_timestamp(ceiling, now=now) == ceiling  # 恰在上界：不改


def test_far_future_timestamp_cannot_lock_session_forever(tmp_path: Path) -> None:
    """状态文件里的远未来时间戳不得让会话永久锁死。

    修复前实测：``last_active_at = now + 1e9`` 使 ``remaining_silence_sec`` 得到
    1000000045 秒 ≈ 31.69 年，该会话永久不可主动发言；延迟检查还会以该值为
    timeout 停放，``notify_activity`` 唤醒后重算仍是巨值又停回去（实测连续 4 次），
    形成不死任务；巡检侧 ``now - last_active_at`` 为负，永远不大于
    ``patrol_inactive_after_sec``，于是每轮巡检都白跑这个已锁死的会话。

    断言用「调用后取时钟」作上界：``load_sessions`` 内部取自己的时钟（必然不早于
    调用前），拿调用前的时钟当上界会因亚秒漂移偶发红灯。
    """
    import time

    models, _, storage = _load_modules()
    key = "qq:GroupMessage:123"
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps({"sessions": {key: {"last_active_at": time.time() + 1.0e9}}}),
        encoding="utf-8",
    )

    sessions = storage.load_sessions(path, {"123"}, 5)
    ceiling_after = time.time() + models.MAX_CLOCK_SKEW_SEC

    state = sessions[key]
    assert 0.0 <= state.last_active_at <= ceiling_after
    # 核心不变式：静默剩余量必须有界，不能是数十年
    silence_left = state.remaining_silence_sec(45.0, time.time())
    assert silence_left <= 45.0 + models.MAX_CLOCK_SKEW_SEC
    # age_sec 不得为大负数（巡检的 inactive 判据依赖它）
    assert state.age_sec(time.time()) >= -models.MAX_CLOCK_SKEW_SEC


def test_negative_timestamps_cannot_bypass_proactive_gate(tmp_path: Path) -> None:
    """负值时间戳不得成为「已回复过」判据的旁路。

    修复前实测：只毒 ``last_active_at`` 会被「这条消息之后已经主动回复过」拦住，
    但把 ``last_proactive_observed_at`` 一并毒成更负（-1e10 < -1e9）即可放行——
    全新会话被拦、投毒会话放行，是真实的能力提升。钳位后两者同归 0.0，
    与全新会话完全同构，能力提升消失。
    """
    _, _, storage = _load_modules()
    key = "qq:GroupMessage:123"
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "sessions": {
                    key: {
                        "last_active_at": -1.0e9,
                        "last_proactive_observed_at": -1.0e10,
                        "last_proactive_at": -1.0e10,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    state = storage.load_sessions(path, {"123"}, 5)[key]

    assert state.last_active_at == 0.0
    assert state.last_proactive_observed_at == 0.0
    assert state.last_proactive_at == 0.0
    # 与全新会话同构：observed >= last_active 成立，且静默门按「从未活跃」处理
    assert state.last_proactive_observed_at >= state.last_active_at


def test_recent_record_timestamps_are_clamped(tmp_path: Path) -> None:
    """``recent[].at`` 同样来自外部文件，同样要钳。"""
    import time

    models, _, storage = _load_modules()
    key = "qq:GroupMessage:123"
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "sessions": {
                    key: {
                        "recent": [
                            {"role": "user", "text": "future", "at": time.time() + 1.0e9},
                            {"role": "user", "text": "negative", "at": -1.0e9},
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    records = list(storage.load_sessions(path, {"123"}, 5)[key].recent)
    ceiling_after = time.time() + models.MAX_CLOCK_SKEW_SEC

    assert len(records) == 2
    assert all(0.0 <= item.at <= ceiling_after for item in records)
    assert records[1].at == 0.0  # 负值归 0


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

    调用方 `SessionPipeline.check_session_locked` 的跨天刷新发生在判断+生成之前，二者相隔
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


def test_collapse_whitespace_collapses_runs_and_strips() -> None:
    """历史去重、聊天清洗、工具直发去重必须共用同一空白归一。"""
    _, utils, _ = _load_modules()
    assert utils.collapse_whitespace("  你 \n 好\t ") == "你 好"
    assert utils.collapse_whitespace("") == ""
    assert utils.collapse_whitespace(None) == ""
    assert utils.collapse_whitespace("你  好") == utils.collapse_whitespace("你\n好")


def test_history_display_name_assistant_is_bot() -> None:
    """历史展示名：助手固定 Bot，其他人用名字，缺名才回落用户。"""
    models, utils, _ = _load_modules()
    assert models.history_display_name("assistant") == "Bot"
    assert models.history_display_name("assistant", "自定义") == "Bot"
    assert models.history_display_name("user") == "用户"
    assert models.history_display_name("user", "小明") == "小明"
    rec = models.MessageRecord(role="assistant", name="x", text="hi")
    assert utils.format_message_records([rec], limit=1) == "Bot: hi"


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
