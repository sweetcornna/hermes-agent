"""Proactive group speech for the OneBot (QQ) adapter.

Ported from the corlinman suites ``test_qq_proactive.py`` /
``test_qq_speech_cap.py`` / ``test_qq_hot_apply.py``.  Everything runs against
a fake OneBot client and a fake message handler — no sockets, no NapCat, no
model call, and no path in this file can reach a real QQ group.

Four properties get their own tests because getting them wrong is silent in
production and expensive in a real group chat:

* the speech cap is **shared** with reactive replies (two counters would
  double how much the bot says);
* ``group_replies_enabled`` — the emergency mute — silences proactive posts
  too, and the proactive lane reads the same flag the reply lane obeys;
* ``proactive_groups`` cannot reach outside ``group_whitelist``;
* the active-hours window is evaluated in an **explicit** timezone, and a bad
  timezone name falls back to ``Asia/Shanghai`` — never to the process zone,
  which on the production host is one hour off.
"""

from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import MessageEvent

from plugins.platforms.onebot import adapter as A
from plugins.platforms.onebot import proactive as PR


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeClient:
    """Records every outbound action; never touches a socket."""

    def __init__(self) -> None:
        self.actions: List[Any] = []
        self.connected = True
        self.last_self_id = 100
        self.last_event_at_ms = 0
        self.last_status_online = True
        self.inbound_dropped_count = 0
        self.outbound_queue_depth = 0

    async def call_action(self, action, *, timeout=None):
        self.actions.append(action)
        return {"status": "ok", "retcode": 0, "data": {"message_id": 1001}}

    async def send_action(self, action):
        self.actions.append(action)

    async def close(self):
        self.connected = False


class FakeHandler:
    """Stand-in for the gateway message handler; returns a fixed reply."""

    def __init__(self, reply: str = "大家下午好呀") -> None:
        self.reply = reply
        self.calls = 0
        self.events: List[MessageEvent] = []

    async def __call__(self, event: MessageEvent):
        self.calls += 1
        self.events.append(event)
        return self.reply

    @property
    def prompts(self) -> List[str]:
        return [e.text for e in self.events]


GROUP = "183287894"


def make_adapter(
    extra: Optional[Dict[str, Any]] = None,
    *,
    handler: Optional[FakeHandler] = None,
    online: bool = True,
) -> A.OneBotAdapter:
    """A connected-looking adapter with groups whitelisted and unmuted."""
    base: Dict[str, Any] = {
        "ws_url": "ws://127.0.0.1:3001",
        "group_replies_enabled": True,
        "group_whitelist": [GROUP],
    }
    base.update(extra or {})
    ad = A.OneBotAdapter(PlatformConfig(enabled=True, extra=base))
    ad._client = FakeClient()
    ad._running = True
    ad._semaphore = asyncio.Semaphore(4)
    ad._account_online = online
    ad._group_names[int(GROUP)] = "测试群"
    ad._message_handler = handler or FakeHandler()
    return ad


def one_beat(monkeypatch, *, on_first=None) -> Dict[str, int]:
    """First sleep proceeds (one beat), the second stops the loop.

    ``on_first`` runs while the loop is "asleep" — that is how the hot-apply
    tests mutate the live config mid-flight.
    """
    calls = {"n": 0}

    async def _sleep(cancel, secs):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            if on_first is not None:
                on_first()
            return False
        return True

    monkeypatch.setattr(PR, "sleep_or_cancel", _sleep)
    return calls


def freeze_clock(monkeypatch, day: str = "2026-08-19", hour: int = 12) -> None:
    monkeypatch.setattr(PR, "now_parts", lambda tz: (day, hour))


def sent_texts(ad: A.OneBotAdapter) -> List[str]:
    """Text of every message the adapter actually put on the wire."""
    out: List[str] = []
    for action in ad._client.actions:
        for seg in getattr(action, "message", []) or []:
            text = getattr(seg, "text", None)
            if text is not None:
                out.append(text)
    return out


@pytest.fixture(autouse=True)
def _reset_state():
    A._reset_module_state()
    PR.set_context_provider(None)
    yield
    A._reset_module_state()
    PR.set_context_provider(None)


# ---------------------------------------------------------------------------
# 1. Configuration
# ---------------------------------------------------------------------------


class TestResolveConfig:
    def test_disabled_by_default(self):
        assert PR.resolve_config({}, frozenset({"1"})) is None

    def test_enabled_with_explicit_groups(self):
        cfg = PR.resolve_config(
            {"proactive_enabled": True, "proactive_groups": [123, "456"]}, None
        )
        assert cfg is not None
        assert cfg.groups == ("123", "456")
        assert cfg.min_gap_minutes == 45
        assert cfg.max_gap_minutes == 45 * 4
        assert cfg.daily_max == 4
        assert cfg.prompt == PR.DEFAULT_PROMPT

    def test_groups_fall_back_to_whitelist(self):
        cfg = PR.resolve_config(
            {"proactive_enabled": True}, frozenset({"777", "888"})
        )
        assert cfg is not None
        assert cfg.groups == ("777", "888")

    def test_enabled_without_any_target_stays_off(self):
        assert PR.resolve_config({"proactive_enabled": True}, None) is None
        assert PR.resolve_config({"proactive_enabled": True}, frozenset()) is None

    def test_custom_pacing_and_prompt(self):
        cfg = PR.resolve_config(
            {
                "proactive_enabled": True,
                "proactive_groups": [1],
                "proactive_min_gap_minutes": 10,
                "proactive_max_gap_minutes": 30,
                "proactive_daily_max": 2,
                "proactive_active_start_hour": 8,
                "proactive_active_end_hour": 22,
                "proactive_prompt": "说点什么",
            },
            None,
        )
        assert (cfg.min_gap_minutes, cfg.max_gap_minutes) == (10, 30)
        assert cfg.daily_max == 2
        assert (cfg.active_start_hour, cfg.active_end_hour) == (8, 22)
        assert cfg.prompt == "说点什么"

    def test_explicit_groups_intersect_whitelist(self):
        cfg = PR.resolve_config(
            {"proactive_enabled": True, "proactive_groups": [1, 2, 3]},
            frozenset({"2", "3"}),
        )
        assert cfg.groups == ("2", "3")

    def test_groups_entirely_outside_whitelist_stay_off(self):
        """Fail closed, not open.

        The source fell through to "no groups ⇒ use the whole whitelist" here,
        contradicting its own docstring and turning a mistyped id in a
        narrowing config into unprompted speech in EVERY whitelisted group.
        A deliberate correction, safe because the feature never ran.
        """
        assert (
            PR.resolve_config(
                {"proactive_enabled": True, "proactive_groups": [1]}, frozenset({"9"})
            )
            is None
        )

    def test_an_unset_group_list_still_falls_back_to_the_whitelist(self):
        """The other empty keeps its old meaning: 'my whitelisted groups'."""
        for absent in ({}, {"proactive_groups": []}, {"proactive_groups": ""}):
            cfg = PR.resolve_config(
                {"proactive_enabled": True, **absent}, frozenset({"9"})
            )
            assert cfg is not None and cfg.groups == ("9",)

    def test_a_partly_valid_list_keeps_only_the_whitelisted_entries(self):
        """Between the two empties: some survive, so those are the targets."""
        cfg = PR.resolve_config(
            {"proactive_enabled": True, "proactive_groups": [1, 9]}, frozenset({"9"})
        )
        assert cfg is not None and cfg.groups == ("9",)

    def test_probability_parsing_and_clamping(self):
        base = {"proactive_enabled": True, "proactive_groups": [1]}
        assert PR.resolve_config(base, None).probability == 1.0
        assert PR.resolve_config({**base, "proactive_probability": 0.4}, None).probability == 0.4
        assert PR.resolve_config({**base, "proactive_probability": 0}, None).probability == 0.0
        assert PR.resolve_config({**base, "proactive_probability": 7}, None).probability == 1.0
        assert PR.resolve_config({**base, "proactive_probability": "junk"}, None).probability == 1.0

    def test_timezone_defaults_explicit_and_context_messages(self):
        base = {"proactive_enabled": True, "proactive_groups": [1]}
        # The divergence from the source: an unset timezone resolves to an
        # explicit zone, never to whatever the process happens to run in.
        assert PR.resolve_config(base, None).timezone == "Asia/Shanghai"
        cfg = PR.resolve_config(
            {**base, "proactive_timezone": "Asia/Tokyo", "proactive_context_messages": 5},
            None,
        )
        assert cfg.timezone == "Asia/Tokyo"
        assert cfg.context_messages == 5
        assert PR.resolve_config({**base, "proactive_context_messages": 0}, None).context_messages == 0
        assert PR.resolve_config(base, None).context_messages == A._GROUP_RECENT_MAX

    def test_string_group_list_is_accepted(self):
        cfg = PR.resolve_config(
            {"proactive_enabled": "true", "proactive_groups": "1, 2;3"}, None
        )
        assert cfg.groups == ("1", "2", "3")


class TestLiveConfigReads:
    def test_live_config_follows_platform_config_extra(self):
        ad = make_adapter()
        assert PR.live_config(ad) is None
        ad.config.extra["proactive_enabled"] = True
        cfg = PR.live_config(ad)
        assert cfg is not None and cfg.groups == (GROUP,)

    def test_speech_window_reads_live_values(self):
        ad = make_adapter(
            {"group_rate_limit_window_minutes": 3, "group_rate_limit_max_messages": 5}
        )
        assert PR.speech_window(ad) == (180.0, 5)
        ad.config.extra["group_rate_limit_max_messages"] = 1
        assert PR.speech_window(ad) == (180.0, 1)

    def test_speech_window_defaults_off(self):
        assert PR.speech_window(make_adapter()) == (0.0, 0)

    def test_muted_when_either_source_says_so(self):
        ad = make_adapter()
        assert PR.group_speech_muted(ad) is False
        ad.config.extra["group_replies_enabled"] = False
        assert PR.group_speech_muted(ad) is True
        # Router flag off is enough on its own — the reactive lane is the
        # authority on whether this bot is allowed to speak in groups at all.
        ad.config.extra["group_replies_enabled"] = True
        ad.router.group_replies_enabled = False
        assert PR.group_speech_muted(ad) is True


# ---------------------------------------------------------------------------
# 2. Clock and pacing
# ---------------------------------------------------------------------------


class TestActiveHours:
    def test_normal_window(self):
        assert PR.in_active_hours(9, 9, 23)
        assert PR.in_active_hours(22, 9, 23)
        assert not PR.in_active_hours(23, 9, 23)
        assert not PR.in_active_hours(3, 9, 23)

    def test_overnight_window_wraps(self):
        assert PR.in_active_hours(23, 22, 2)
        assert PR.in_active_hours(1, 22, 2)
        assert not PR.in_active_hours(12, 22, 2)

    def test_degenerate_window_is_always_on(self):
        assert PR.in_active_hours(5, 9, 9)


class TestNowParts:
    def test_timezone_is_applied(self):
        day, hour = PR.now_parts("Asia/Shanghai")
        expected = datetime.now(ZoneInfo("Asia/Shanghai"))
        assert day == expected.strftime("%Y-%m-%d")
        assert hour == expected.hour

    def test_invalid_timezone_falls_back_to_the_explicit_default(self):
        """Not to the process zone — that is the drift this port refuses."""
        day, hour = PR.now_parts("Not/AZone")
        expected = datetime.now(ZoneInfo(PR.DEFAULT_TIMEZONE))
        assert (day, hour) == (expected.strftime("%Y-%m-%d"), expected.hour)

    def test_no_usable_timezone_returns_none(self, monkeypatch):
        import zoneinfo

        class _Boom:
            def __init__(self, *a, **kw):
                raise zoneinfo.ZoneInfoNotFoundError("no tzdata")

        monkeypatch.setattr(zoneinfo, "ZoneInfo", _Boom)
        assert PR.now_parts("Asia/Shanghai") is None


class TestDelayDraw:
    def test_delay_within_configured_window(self):
        cfg = PR.resolve_config(
            {
                "proactive_enabled": True,
                "proactive_groups": [1],
                "proactive_min_gap_minutes": 10,
                "proactive_max_gap_minutes": 20,
            },
            None,
        )
        rng = random.Random(42)
        for _ in range(50):
            assert 600.0 <= PR.next_delay_secs(cfg, rng) <= 1200.0


class TestSleep:
    @pytest.mark.asyncio
    async def test_cancel_interrupts_sleep(self):
        cancel = asyncio.Event()
        asyncio.get_running_loop().call_later(0.05, cancel.set)
        assert await PR.sleep_or_cancel(cancel, 30.0) is True

    @pytest.mark.asyncio
    async def test_timeout_returns_false(self):
        assert await PR.sleep_or_cancel(asyncio.Event(), 0.0) is False


# ---------------------------------------------------------------------------
# 3. SKIP
# ---------------------------------------------------------------------------


class TestSkipDetection:
    @pytest.mark.parametrize("text", ["SKIP", "skip", " Skip ", "[SKIP]", "SKIP。", "[skip]."])
    def test_skip_variants(self, text):
        assert PR.is_skip(text)

    @pytest.mark.parametrize("text", ["skip今天不聊", "我先skip一下", "好的"])
    def test_non_skip_text(self, text):
        assert not PR.is_skip(text)

    @pytest.mark.parametrize("text", ["[SILENT]", "NO_REPLY", "[SILENT] 没什么新鲜事"])
    def test_hermes_autonomous_silence_markers_also_count(self, text):
        """Cron and the webhook lane teach models these; honour them too."""
        assert PR.is_skip(text)

    def test_empty_is_silence(self):
        assert PR.is_skip("   ")


# ---------------------------------------------------------------------------
# 4. Context buffer, retrieval query and prompt
# ---------------------------------------------------------------------------


def _cfg(**over) -> PR.ProactiveConfig:
    base = {"proactive_enabled": True, "proactive_groups": [GROUP]}
    base.update(over)
    return PR.resolve_config(base, None)


class TestContextAndPrompt:
    def test_record_and_render_context_lines(self):
        A.record_group_message("default", GROUP, "张三", "今晚打球吗", False)
        A.record_group_message("default", GROUP, "李四", "打！", False)
        A.record_group_message("default", GROUP, "", "", False)  # ignored
        lines = PR.context_lines("default", GROUP, _cfg())
        assert len(lines) == 2
        assert "张三: 今晚打球吗" in lines[0]
        assert "李四: 打！" in lines[1]

    def test_context_respects_limit_and_off_switch(self):
        for i in range(20):
            A.record_group_message("default", GROUP, "u", f"msg{i}", False)
        lines = PR.context_lines("default", GROUP, _cfg(proactive_context_messages=3))
        assert [ln.split(": ")[-1] for ln in lines] == ["msg17", "msg18", "msg19"]
        assert PR.context_lines("default", GROUP, _cfg(proactive_context_messages=0)) == []

    def test_self_posts_render_under_a_fixed_label(self):
        A.record_group_message("default", GROUP, "张三", "机器人能修 bug 吗", False)
        A.record_group_message("default", GROUP, "", "能，发过来看看", True)
        lines = PR.context_lines("default", GROUP, _cfg())
        assert "张三: 机器人能修 bug 吗" in lines[0]
        assert f"{PR.SELF_LABEL}: 能，发过来看看" in lines[1]

    def test_last_message_is_self_detection(self):
        assert not PR.last_message_is_self("default", GROUP)  # empty buffer
        A.record_group_message("default", GROUP, "张三", "在吗", False)
        assert not PR.last_message_is_self("default", GROUP)
        A.record_group_message("default", GROUP, "", "在的", True)
        assert PR.last_message_is_self("default", GROUP)
        A.record_group_message("default", GROUP, "李四", "聊聊", False)
        assert not PR.last_message_is_self("default", GROUP)

    def test_blank_inbound_never_masks_our_own_last_word(self):
        """A sticker is not somebody speaking — it must not unblock a post."""
        A.record_group_message("default", GROUP, "", "在的", True)
        A.record_group_message("default", GROUP, "张三", "   ", False)
        assert PR.last_message_is_self("default", GROUP)

    def test_compose_prompt_includes_context_and_skip_hint(self):
        cfg = _cfg(proactive_prompt="冒个泡")
        with_ctx = PR.compose_prompt(cfg, ["[10:00] a: hi"])
        assert "[10:00] a: hi" in with_ctx
        assert "冒个泡" in with_ctx
        assert "SKIP" in with_ctx
        assert PR.SELF_LABEL in with_ctx
        bare = PR.compose_prompt(cfg, [])
        assert "聊天记录" not in bare
        assert "SKIP" in bare

    def test_compose_prompt_renders_snippets(self):
        cfg = _cfg()
        prompt = PR.compose_prompt(
            cfg,
            ["[10:00] a: 今晚吃什么"],
            ["食堂周三有烤鸭", "", "  ", "x" * 1000, "四", "五(超出上限)"],
        )
        assert "食堂周三有烤鸭" in prompt
        assert "资料库" in prompt
        assert "x" * (PR.RETRIEVAL_SNIPPET_CHARS + 1) not in prompt
        assert "五(超出上限)" not in prompt
        assert "资料库" not in PR.compose_prompt(cfg, [])

    def test_retrieval_query_uses_human_chatter_only(self):
        assert PR.retrieval_query("default", GROUP) == ""
        A.record_group_message("default", GROUP, "张三", "GPU 报错了", False)
        A.record_group_message("default", GROUP, "", "我看看日志", True)
        query = PR.retrieval_query("default", GROUP)
        assert "GPU 报错了" in query
        assert "我看看日志" not in query


class TestDailyBudgetState:
    def test_counts_roll_over_by_day(self):
        key = "default:42"
        assert PR.sent_today(key, "2026-08-19") == 0
        PR.mark_sent(key, "2026-08-19")
        PR.mark_sent(key, "2026-08-19")
        assert PR.sent_today(key, "2026-08-19") == 2
        assert PR.sent_today(key, "2026-08-20") == 0
        PR.mark_sent(key, "2026-08-20")
        assert PR.sent_today(key, "2026-08-20") == 1


# ---------------------------------------------------------------------------
# 5. One turn
# ---------------------------------------------------------------------------


class TestGenerate:
    @pytest.mark.asyncio
    async def test_runs_a_turn_in_its_own_session_lane(self):
        handler = FakeHandler("早上好，今天有点忙。")
        ad = make_adapter(handler=handler)
        text = await PR.generate(ad, GROUP, "说点什么")
        assert text == "早上好，今天有点忙。"
        event = handler.events[0]
        assert event.text == "说点什么"
        assert event.source.chat_id == f"g{GROUP}"
        assert event.source.chat_type == "group"
        # A dedicated sender slot keeps the proactive thread out of whatever
        # session a human is mid-way through.
        assert event.user_id == PR.PROACTIVE_SENDER_ID
        assert event.source.user_id == PR.PROACTIVE_SENDER_ID

    @pytest.mark.asyncio
    async def test_event_pings_nobody_and_is_not_a_gateway_command(self):
        handler = FakeHandler()
        ad = make_adapter(handler=handler)
        await PR.generate(ad, GROUP, "/status 看起来像命令")
        event = handler.events[0]
        assert "onebot_at_user_id" not in event.metadata
        assert event.metadata["onebot_proactive"] is True
        assert event.allow_gateway_control is False
        assert event.is_command() is False

    @pytest.mark.asyncio
    async def test_no_handler_wired_returns_empty(self):
        ad = make_adapter()
        ad._message_handler = None
        assert await PR.generate(ad, GROUP, "说点什么") == ""

    @pytest.mark.asyncio
    async def test_handler_failure_propagates_to_the_loop(self):
        class _Boom(FakeHandler):
            async def __call__(self, event):
                raise RuntimeError("boom")

        ad = make_adapter(handler=_Boom())
        with pytest.raises(RuntimeError, match="boom"):
            await PR.generate(ad, GROUP, "说点什么")


# ---------------------------------------------------------------------------
# 6. The loop's gate ladder
# ---------------------------------------------------------------------------


async def run_one_beat(ad: A.OneBotAdapter, cfg=None) -> None:
    await PR.proactive_loop(ad, asyncio.Event(), cfg or PR.live_config(ad))


class TestLoopGates:
    @pytest.mark.asyncio
    async def test_one_beat_posts_and_books_the_budget(self, monkeypatch):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        handler = FakeHandler("大家下午好呀")
        ad = make_adapter({"proactive_enabled": True}, handler=handler)
        await run_one_beat(ad)
        assert sent_texts(ad) == ["大家下午好呀"]
        key = A.speech_key("default", GROUP)
        assert PR.sent_today(key, "2026-08-19") == 1
        assert key in PR._LAST_POST_MONO

    @pytest.mark.asyncio
    async def test_the_post_spends_the_shared_speech_budget(self, monkeypatch):
        """D33: one window for replies and proactive posts, not two."""
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        ad = make_adapter(
            {
                "proactive_enabled": True,
                "group_rate_limit_window_minutes": 3,
                "group_rate_limit_max_messages": 2,
            }
        )
        await run_one_beat(ad)
        assert len(sent_texts(ad)) == 1
        # The reactive path now sees one unit already spent in this window.
        assert A.group_speech_allowed("default", GROUP, 180.0, 2) is True
        assert A.group_speech_allowed("default", GROUP, 180.0, 2) is False

    @pytest.mark.asyncio
    async def test_emergency_mute_blocks_proactive(self, monkeypatch):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        handler = FakeHandler("不该出现")
        ad = make_adapter(
            {"proactive_enabled": True, "group_replies_enabled": False}, handler=handler
        )
        await run_one_beat(ad)
        assert sent_texts(ad) == []
        assert handler.calls == 0

    @pytest.mark.asyncio
    async def test_outside_active_hours_stays_silent(self, monkeypatch):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch, hour=3)
        handler = FakeHandler("不该出现")
        ad = make_adapter({"proactive_enabled": True}, handler=handler)
        await run_one_beat(ad)
        assert handler.calls == 0

    @pytest.mark.asyncio
    async def test_account_offline_stays_silent(self, monkeypatch):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        handler = FakeHandler("不该出现")
        ad = make_adapter({"proactive_enabled": True}, handler=handler, online=False)
        await run_one_beat(ad)
        assert handler.calls == 0

    @pytest.mark.asyncio
    async def test_link_down_stays_silent(self, monkeypatch):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        handler = FakeHandler("不该出现")
        ad = make_adapter({"proactive_enabled": True}, handler=handler)
        ad._client.connected = False
        await run_one_beat(ad)
        assert handler.calls == 0

    @pytest.mark.asyncio
    async def test_unknown_identity_stays_silent(self, monkeypatch):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        handler = FakeHandler("不该出现")
        ad = make_adapter({"proactive_enabled": True}, handler=handler)
        ad._client.last_self_id = None
        ad.self_ids = []
        await run_one_beat(ad)
        assert handler.calls == 0

    @pytest.mark.asyncio
    async def test_probability_zero_never_posts(self, monkeypatch):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        handler = FakeHandler("不该出现")
        ad = make_adapter(
            {"proactive_enabled": True, "proactive_probability": 0}, handler=handler
        )
        await run_one_beat(ad)
        assert handler.calls == 0

    @pytest.mark.asyncio
    async def test_model_skip_stays_silent(self, monkeypatch):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        handler = FakeHandler("SKIP")
        ad = make_adapter({"proactive_enabled": True}, handler=handler)
        await run_one_beat(ad)
        assert handler.calls == 1  # the turn ran…
        assert sent_texts(ad) == []  # …and the persona chose silence
        assert PR.sent_today(A.speech_key("default", GROUP), "2026-08-19") == 0

    @pytest.mark.asyncio
    async def test_speech_cap_blocks_eligibility_before_the_model_call(self, monkeypatch):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        handler = FakeHandler("不该出现")
        ad = make_adapter(
            {
                "proactive_enabled": True,
                "group_rate_limit_window_minutes": 10,
                "group_rate_limit_max_messages": 1,
            },
            handler=handler,
        )
        A._GROUP_SPEECH.record(A.speech_key("default", GROUP))  # window already spent
        await run_one_beat(ad)
        assert handler.calls == 0
        assert sent_texts(ad) == []

    @pytest.mark.asyncio
    async def test_daily_budget_blocks_further_posts(self, monkeypatch):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        handler = FakeHandler("不该出现")
        ad = make_adapter(
            {"proactive_enabled": True, "proactive_daily_max": 1}, handler=handler
        )
        PR.mark_sent(A.speech_key("default", GROUP), "2026-08-19")
        await run_one_beat(ad)
        assert handler.calls == 0

    @pytest.mark.asyncio
    async def test_min_gap_blocks_a_repeat(self, monkeypatch):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        handler = FakeHandler("不该出现")
        ad = make_adapter({"proactive_enabled": True}, handler=handler)
        PR._LAST_POST_MONO[A.speech_key("default", GROUP)] = time.monotonic()
        await run_one_beat(ad)
        assert handler.calls == 0

    @pytest.mark.asyncio
    async def test_bot_spoke_last_blocks_the_group(self, monkeypatch):
        """The anti-spam rule: wait for a human before speaking again."""
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        handler = FakeHandler("不该出现")
        ad = make_adapter({"proactive_enabled": True}, handler=handler)
        A.record_group_message("default", GROUP, "张三", "帮我修个 bug", False)
        A.record_group_message("default", GROUP, "", "能，发过来", True)
        await run_one_beat(ad)
        assert handler.calls == 0

    @pytest.mark.asyncio
    async def test_the_post_is_recorded_as_our_own(self, monkeypatch):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        ad = make_adapter({"proactive_enabled": True}, handler=FakeHandler("大家下午好呀"))
        await run_one_beat(ad)
        buffer = A.recent_group_messages("default", int(GROUP))
        assert buffer and buffer[-1][2] == "大家下午好呀" and buffer[-1][3] is True
        assert PR.last_message_is_self("default", GROUP)

    @pytest.mark.asyncio
    async def test_recent_chatter_reaches_the_prompt(self, monkeypatch):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        handler = FakeHandler("好嘞")
        ad = make_adapter({"proactive_enabled": True}, handler=handler)
        A.record_group_message("default", GROUP, "张三", "今晚食堂吃什么", False)
        await run_one_beat(ad)
        assert "今晚食堂吃什么" in handler.prompts[0]

    @pytest.mark.asyncio
    async def test_bubbles_are_split_by_the_shared_send_path(self, monkeypatch):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        ad = make_adapter(
            {"proactive_enabled": True},
            handler=FakeHandler("今晚有空[MSG_BREAK]一起打球？"),
        )
        await run_one_beat(ad)
        assert sent_texts(ad) == ["今晚有空", "一起打球？"]
        # …and the buffer holds the flattened form, not the raw marker.
        assert "[MSG_BREAK]" not in A.recent_group_messages("default", int(GROUP))[-1][2]

    @pytest.mark.asyncio
    async def test_a_failing_turn_does_not_kill_the_loop(self, monkeypatch):
        calls = one_beat(monkeypatch)
        freeze_clock(monkeypatch)

        class _Boom(FakeHandler):
            async def __call__(self, event):
                self.calls += 1
                raise RuntimeError("model down")

        ad = make_adapter({"proactive_enabled": True}, handler=_Boom())
        await run_one_beat(ad)
        assert sent_texts(ad) == []
        assert calls["n"] == 2  # the loop went round again instead of dying

    @pytest.mark.asyncio
    async def test_an_unwhitelisted_only_target_silences_the_loop(self, monkeypatch):
        """A mistyped narrowing config must not become five open groups."""
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        handler = FakeHandler("不该出现")
        ad = make_adapter(
            {"proactive_enabled": True, "proactive_groups": ["99999999"]},
            handler=handler,
        )
        await run_one_beat(ad)
        assert handler.calls == 0
        assert sent_texts(ad) == []

    @pytest.mark.asyncio
    async def test_posts_only_into_the_whitelisted_half_of_a_mixed_list(self, monkeypatch):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        handler = FakeHandler("大家下午好呀")
        ad = make_adapter(
            {"proactive_enabled": True, "proactive_groups": ["99999999", GROUP]},
            handler=handler,
        )
        await run_one_beat(ad)
        assert handler.events[0].source.chat_id == f"g{GROUP}"
        for action in ad._client.actions:
            assert action.group_id == int(GROUP)


class TestRetrievalSeam:
    @pytest.mark.asyncio
    async def test_snippets_reach_the_prompt_when_a_provider_is_set(self, monkeypatch):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        queries: List[tuple] = []

        async def _provider(query: str, k: int):
            queries.append((query, k))
            return ["食堂周三有烤鸭"]

        PR.set_context_provider(_provider)
        handler = FakeHandler("好嘞")
        ad = make_adapter({"proactive_enabled": True}, handler=handler)
        A.record_group_message("default", GROUP, "张三", "今晚食堂吃什么", False)
        await run_one_beat(ad)
        assert queries and "今晚食堂吃什么" in queries[0][0]
        assert "食堂周三有烤鸭" in handler.prompts[0]

    @pytest.mark.asyncio
    async def test_provider_failure_never_blocks_the_post(self, monkeypatch):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)

        async def _provider(query: str, k: int):
            raise RuntimeError("corpus offline")

        PR.set_context_provider(_provider)
        ad = make_adapter({"proactive_enabled": True}, handler=FakeHandler("照常营业"))
        A.record_group_message("default", GROUP, "张三", "在吗", False)
        await run_one_beat(ad)
        assert sent_texts(ad) == ["照常营业"]

    @pytest.mark.asyncio
    async def test_no_provider_means_no_snippet_section(self, monkeypatch):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        handler = FakeHandler("好嘞")
        ad = make_adapter({"proactive_enabled": True}, handler=handler)
        A.record_group_message("default", GROUP, "张三", "在吗", False)
        await run_one_beat(ad)
        assert "资料库" not in handler.prompts[0]


# ---------------------------------------------------------------------------
# 7. Hot apply — a config save takes effect on the next beat
# ---------------------------------------------------------------------------


class TestHotApply:
    @pytest.mark.asyncio
    async def test_enable_mid_loop_takes_effect(self, monkeypatch):
        """Loop starts DISABLED; the config changes while it sleeps."""
        freeze_clock(monkeypatch)
        handler = FakeHandler("热启用后冒个泡")
        ad = make_adapter(handler=handler)
        one_beat(monkeypatch, on_first=lambda: ad.config.extra.update(proactive_enabled=True))
        await PR.proactive_loop(ad, asyncio.Event(), None)
        assert handler.calls == 1
        assert sent_texts(ad) == ["热启用后冒个泡"]

    @pytest.mark.asyncio
    async def test_disable_mid_loop_goes_silent(self, monkeypatch):
        freeze_clock(monkeypatch)
        handler = FakeHandler("不该出现")
        ad = make_adapter({"proactive_enabled": True}, handler=handler)
        one_beat(monkeypatch, on_first=lambda: ad.config.extra.update(proactive_enabled=False))
        await PR.proactive_loop(ad, asyncio.Event(), PR.live_config(ad))
        assert handler.calls == 0
        assert sent_texts(ad) == []

    @pytest.mark.asyncio
    async def test_mute_flipped_mid_loop_silences_the_next_beat(self, monkeypatch):
        freeze_clock(monkeypatch)
        handler = FakeHandler("不该出现")
        ad = make_adapter({"proactive_enabled": True}, handler=handler)
        one_beat(
            monkeypatch,
            on_first=lambda: ad.config.extra.update(group_replies_enabled=False),
        )
        await PR.proactive_loop(ad, asyncio.Event(), PR.live_config(ad))
        assert handler.calls == 0

    @pytest.mark.asyncio
    async def test_idle_loop_sleeps_the_recheck_interval(self, monkeypatch):
        """A disabled loop must keep waking up, or nothing can hot-apply."""
        freeze_clock(monkeypatch)
        delays: List[float] = []

        async def _sleep(cancel, secs):  # noqa: ANN001
            delays.append(secs)
            return len(delays) > 1

        monkeypatch.setattr(PR, "sleep_or_cancel", _sleep)
        await PR.proactive_loop(make_adapter(), asyncio.Event(), None)
        assert delays[0] == PR.IDLE_RECHECK_SECS


# ---------------------------------------------------------------------------
# 8. Lifecycle wiring
# ---------------------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_loop_is_resident_and_stops_on_disconnect(self):
        ad = make_adapter()
        ad._start_proactive_loop()
        assert ad._proactive_task is not None and not ad._proactive_task.done()
        await ad.disconnect()
        assert ad._proactive_task is None
        assert ad._proactive_cancel is None

    @pytest.mark.asyncio
    async def test_a_reconnect_does_not_start_a_second_speaker(self):
        ad = make_adapter()
        ad._start_proactive_loop()
        first = ad._proactive_task
        ad._start_proactive_loop()
        assert ad._proactive_task is first
        await ad.disconnect()

    def test_proactive_keys_are_accepted_from_top_level_yaml(self):
        extras = A._apply_yaml_config(
            {}, {"proactive_enabled": True, "proactive_daily_max": 2}
        )
        assert extras["proactive_enabled"] is True
        assert extras["proactive_daily_max"] == 2

    def test_feature_is_off_in_the_shipped_default(self):
        """No config key ⇒ no proactive speech.  The whole point of D31."""
        assert PR.live_config(make_adapter()) is None
