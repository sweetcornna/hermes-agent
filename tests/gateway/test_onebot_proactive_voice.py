"""Proactive speech that is *heard*: the voice lane and the call lane.

Two behaviours sit on top of the proactive text lane pinned by
``test_onebot_proactive.py``, and both ship **off**:

* **主动语音** — the same group post, delivered as a voice bubble.  A
  per-beat modality, not a gate: it can change *how* a post is delivered and
  never *whether* or *where*, so the properties worth pinning are the ones
  that would silently drift — the shared budgets stay shared, the marker never
  reaches a synthesizer, and the two refusals that mean "do not speak here"
  never become a text message instead.
* **主动语音通话** — the bot placing a call nobody asked for.  Every test here
  is a test that it *stays quiet*: no targets, the call subsystem off, the
  wrong hour, the budget spent, the gap unexpired, a group id pasted where a
  person belongs.

The harness is the one the text lane already uses (a fake client, a fake
message handler, a frozen clock, a one-beat loop).  Everything is offline: the
TTS tool is patched with a fake that writes real files into a temp dir, no
socket is opened, and no path in this file can reach a real QQ group or a real
person.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

import tools.tts_tool as TTS
from gateway.config import PlatformConfig
from gateway.platforms.base import MessageEvent

from plugins.platforms.onebot import adapter as A
from plugins.platforms.onebot import proactive as PR
from plugins.platforms.onebot import proactive_judge as PJ
from plugins.platforms.onebot import protocol as P
from plugins.platforms.onebot import voice_call as VC
from plugins.qzone import policy as POLICY


GROUP = "183287894"
PEER = "536132102"
DAY = "2026-08-19"

#: A phrase the ported rule table blocks (``fraud.account-abuse``), shared
#: verbatim with ``test_onebot_voice.py`` and ``test_onebot_content_gate.py``
#: so a revision of the rule table fails loudly in every suite that leans on
#: it rather than silently testing nothing.
BLOCKED_TEXT = "QQ解冻教程"


def test_the_blocked_fixture_is_actually_blocked():
    """Guard the guard: the policy-refusal test below is empty if this drifts."""
    cfg = POLICY.resolve_config(None)
    assert POLICY.moderate_text(BLOCKED_TEXT, cfg).decision.allowed is False


# ---------------------------------------------------------------------------
# Fakes — the proactive harness, plus a synthesizer that writes real files
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

    async def call_action(self, action, *, timeout=None):  # noqa: ANN001
        self.actions.append(action)
        return {"status": "ok", "retcode": 0, "data": {"message_id": 1001}}

    async def send_action(self, action):  # noqa: ANN001
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


def make_adapter(
    extra: Optional[Dict[str, Any]] = None,
    *,
    handler: Optional[FakeHandler] = None,
    online: bool = True,
) -> A.OneBotAdapter:
    """A connected-looking adapter with the group whitelisted and unmuted."""
    base: Dict[str, Any] = {
        "ws_url": "ws://127.0.0.1:3001",
        "group_replies_enabled": True,
        "group_whitelist": [GROUP],
        "bot_nickname": "格兰",
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


def freeze_clock(monkeypatch, day: str = DAY, hour: int = 12) -> None:
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


def record_segments(ad: A.OneBotAdapter) -> List[P.RecordSegment]:
    """Every voice bubble that reached the wire."""
    return [
        seg
        for action in ad._client.actions
        for seg in getattr(action, "message", []) or []
        if isinstance(seg, P.RecordSegment)
    ]


def pokes(ad: A.OneBotAdapter) -> List[P.SendPoke]:
    return [a for a in ad._client.actions if isinstance(a, P.SendPoke)]


def spy_send(ad: A.OneBotAdapter) -> List[str]:
    """Record every ``adapter.send`` call without changing what it does.

    The refusal tests need this: a muted or policy-refused post produces the
    same *observable wire* whether the lane fell back to text (and had the
    text refused by the same gate) or never fell back at all.  The difference
    that matters — did the lane ask a second time — is only visible here.
    """
    calls: List[str] = []
    original = ad.send

    async def _send(chat_id, content, *args, **kwargs):  # noqa: ANN001
        calls.append(content)
        return await original(chat_id, content, *args, **kwargs)

    ad.send = _send  # type: ignore[method-assign]
    return calls


class FakeTTS:
    """Stand-in for ``text_to_speech_tool``: writes real files, records calls.

    Real files, because ``voice.speak`` vets and base64-inlines what it gets
    back; a mock returning a path that is not there would exercise the error
    branch instead of the success one.
    """

    def __init__(self, outdir: Path, *, envelope: Optional[str] = None) -> None:
        self.outdir = outdir
        self.envelope = envelope
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.envelope is not None:
            return self.envelope
        path = self.outdir / f"speech_{len(self.calls)}.mp3"
        path.write_bytes(b"ID3\x03\x00fake-mp3-bytes")
        return json.dumps({
            "success": True,
            "file_path": str(path),
            "file_paths": [str(path)],
            "provider": kwargs.get("provider") or "edge",
            "voice_compatible": True,
            "chunk_count": 1,
        })

    @property
    def texts(self) -> List[str]:
        return [call.get("text", "") for call in self.calls]

    @property
    def providers(self) -> List[Any]:
        return [call.get("provider") for call in self.calls]


@pytest.fixture
def audio_dir(tmp_path: Path) -> Path:
    out = tmp_path / "audio_cache"
    out.mkdir()
    return out


@pytest.fixture
def tts(monkeypatch: pytest.MonkeyPatch, audio_dir: Path) -> FakeTTS:
    """The default synthesizer: succeeds, writes one file per call."""
    fake = FakeTTS(audio_dir)
    monkeypatch.setattr(TTS, "text_to_speech_tool", fake)
    return fake


@pytest.fixture
def broken_tts(monkeypatch: pytest.MonkeyPatch, audio_dir: Path) -> FakeTTS:
    """A provider that is up but failing — the degrade-to-text case."""
    fake = FakeTTS(
        audio_dir,
        envelope=json.dumps({"success": False, "error": "edge-tts: 503"}),
    )
    monkeypatch.setattr(TTS, "text_to_speech_tool", fake)
    return fake


@pytest.fixture(autouse=True)
def _reset_state():
    A._reset_module_state()
    VC.reset_state()
    PR.set_context_provider(None)
    yield
    A._reset_module_state()
    VC.reset_state()
    PR.set_context_provider(None)


@pytest.fixture(autouse=True)
def _judge_steps_aside(monkeypatch):
    """The pre-judge has its own suite; here it always lets the beat through."""

    async def _speak(adapter, group, cfg):  # noqa: ANN001
        return PJ.JudgeVerdict(speak=True, consulted=False, cause="test")

    monkeypatch.setattr(PR, "judge_room", _speak)


VOICE_ON: Dict[str, Any] = {
    "proactive_enabled": True,
    "proactive_voice_enabled": True,
    "proactive_voice_probability": 1,
}

CALL_ON: Dict[str, Any] = {
    "proactive_call_enabled": True,
    "proactive_call_targets": [PEER],
    "voice_call_enabled": True,
}


async def run_one_beat(ad: A.OneBotAdapter) -> None:
    await PR.proactive_loop(ad, asyncio.Event(), PR.live_config(ad))


async def run_one_call_beat(ad: A.OneBotAdapter) -> None:
    await PR.proactive_call_loop(ad, asyncio.Event(), PR.live_call_config(ad))


def group_key() -> str:
    return A.speech_key("default", GROUP)


def peer_key() -> str:
    return A.speech_key("default", PEER)


# ---------------------------------------------------------------------------
# 1. Both lanes ship off
# ---------------------------------------------------------------------------


class TestShippedOff:
    def test_the_voice_modality_is_off_with_no_config(self):
        assert PR.resolve_voice_config({}) is None
        assert PR.live_voice_config(make_adapter()) is None

    def test_the_call_lane_is_off_with_no_config(self):
        """The single most important assertion in this file."""
        assert PR.resolve_call_config({}) is None
        assert PR.live_call_config(make_adapter()) is None

    def test_enabling_voice_without_a_probability_still_says_nothing_aloud(self):
        """Two keystrokes, not one, before the bot starts talking out loud."""
        cfg = PR.resolve_voice_config({"proactive_voice_enabled": True})
        assert cfg is not None and cfg.probability == 0.0

    def test_voice_defaults(self):
        cfg = PR.resolve_voice_config({"proactive_voice_enabled": "yes"})
        assert (cfg.probability, cfg.max_chars, cfg.provider) == (0.0, 120, "")

    def test_voice_probability_is_clamped_and_max_chars_floored(self):
        base = {"proactive_voice_enabled": True}
        assert (
            PR.resolve_voice_config({
                **base,
                "proactive_voice_probability": 7,
            }).probability
            == 1.0
        )
        assert (
            PR.resolve_voice_config({
                **base,
                "proactive_voice_probability": -3,
            }).probability
            == 0.0
        )
        assert (
            PR.resolve_voice_config({
                **base,
                "proactive_voice_probability": "x",
            }).probability
            == 0.0
        )
        # No "0 means unlimited" escape: the dangerous value is the typo.
        assert (
            PR.resolve_voice_config({**base, "proactive_voice_max_chars": 0}).max_chars
            == 1
        )
        assert (
            PR.resolve_voice_config({**base, "proactive_voice_max_chars": -9}).max_chars
            == 1
        )

    def test_call_defaults_are_narrower_than_the_group_lane(self):
        cfg = PR.resolve_call_config({**CALL_ON})
        assert cfg is not None
        assert cfg.targets == (PEER,)
        assert cfg.daily_max == 1
        assert cfg.min_gap_minutes == 240.0
        # 10–22, not the group lane's 9–23: a call at 09:00 is a phone going off.
        assert (cfg.active_start_hour, cfg.active_end_hour) == (10, 22)
        assert cfg.probability == 1.0
        assert cfg.prompt == PR.DEFAULT_CALL_PROMPT
        assert cfg.timezone == PR.DEFAULT_TIMEZONE

    def test_call_pacing_window_reuses_the_group_lane_widening_rule(self):
        cfg = PR.resolve_call_config({**CALL_ON, "proactive_call_min_gap_minutes": 30})
        assert cfg.max_gap_minutes == 120.0
        # …and the one pacing draw works on it unchanged.
        assert 1800.0 <= PR.next_delay_secs(cfg) <= 7200.0


# ---------------------------------------------------------------------------
# 2. Target resolution — the lane refuses to guess who to call
# ---------------------------------------------------------------------------


class TestCallTargets:
    def test_no_targets_means_off(self):
        for empty in (
            {},
            {"proactive_call_targets": []},
            {"proactive_call_targets": ""},
        ):
            assert (
                PR.resolve_call_config({
                    "proactive_call_enabled": True,
                    "voice_call_enabled": True,
                    **empty,
                })
                is None
            )

    def test_a_group_id_is_refused_rather_than_called(self):
        """A call-shaped session in a group puts every bubble in front of everyone."""
        assert (
            PR.resolve_call_config({
                "proactive_call_enabled": True,
                "proactive_call_targets": ["g183287894"],
            })
            is None
        )
        cfg = PR.resolve_call_config({
            "proactive_call_enabled": True,
            "proactive_call_targets": ["group:123", PEER],
        })
        assert cfg is not None and cfg.targets == (PEER,)

    def test_a_whitelisted_group_id_pasted_as_a_person_is_refused(self):
        assert (
            PR.resolve_call_config({
                "proactive_call_enabled": True,
                "proactive_call_targets": [GROUP],
                "group_whitelist": [GROUP],
            })
            is None
        )

    def test_unparseable_entries_are_dropped_not_fatal(self):
        cfg = PR.resolve_call_config({
            "proactive_call_enabled": True,
            "proactive_call_targets": ["not-a-uin", PEER],
        })
        assert cfg is not None and cfg.targets == (PEER,)

    def test_string_lists_and_duplicates(self):
        cfg = PR.resolve_call_config({
            "proactive_call_enabled": True,
            "proactive_call_targets": f"{PEER}, {PEER};10001",
        })
        assert cfg.targets == (PEER, "10001")


# ---------------------------------------------------------------------------
# 3. The voice modality is a modality, not a gate
# ---------------------------------------------------------------------------


class TestVoiceModality:
    @pytest.mark.asyncio
    async def test_probability_zero_means_every_post_is_text(
        self, monkeypatch, tts: FakeTTS
    ):
        """The existing lane, untouched."""
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        ad = make_adapter(
            {**VOICE_ON, "proactive_voice_probability": 0},
            handler=FakeHandler("大家下午好呀"),
        )
        await run_one_beat(ad)
        assert sent_texts(ad) == ["大家下午好呀"]
        assert record_segments(ad) == []
        assert tts.calls == []

    @pytest.mark.asyncio
    async def test_the_lane_off_entirely_means_every_post_is_text(
        self, monkeypatch, tts: FakeTTS
    ):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        ad = make_adapter(
            {"proactive_enabled": True, "proactive_voice_probability": 1},
            handler=FakeHandler("大家下午好呀"),
        )
        await run_one_beat(ad)
        assert sent_texts(ad) == ["大家下午好呀"]
        assert tts.calls == []

    @pytest.mark.asyncio
    async def test_probability_one_sends_a_record_segment(
        self, monkeypatch, tts: FakeTTS
    ):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        ad = make_adapter(VOICE_ON, handler=FakeHandler("大家下午好呀"))
        await run_one_beat(ad)
        segments = record_segments(ad)
        assert len(segments) == 1
        assert segments[0].file.startswith("base64://")
        assert sent_texts(ad) == []  # a voice bubble, not a bubble plus a caption
        assert tts.texts == ["大家下午好呀"]

    @pytest.mark.asyncio
    async def test_the_voice_post_spends_the_same_shared_speech_budget(
        self, monkeypatch, tts: FakeTTS
    ):
        """The voice analog of ``test_the_post_spends_the_shared_speech_budget``.

        D33 says one window covers replies and proactive posts alike.  A voice
        post is still the bot speaking in that room, so it spends from the same
        window — exactly once.
        """
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        ad = make_adapter({
            **VOICE_ON,
            "group_rate_limit_window_minutes": 3,
            "group_rate_limit_max_messages": 2,
        })
        await run_one_beat(ad)
        assert len(record_segments(ad)) == 1
        # The reactive path now sees ONE unit already spent in this window.
        assert A.group_speech_allowed("default", GROUP, 180.0, 2) is True
        assert A.group_speech_allowed("default", GROUP, 180.0, 2) is False

    @pytest.mark.asyncio
    async def test_the_voice_post_books_the_daily_budget_and_the_gap_clock(
        self, monkeypatch, tts: FakeTTS
    ):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        ad = make_adapter(VOICE_ON)
        await run_one_beat(ad)
        assert PR.sent_today(group_key(), DAY) == 1
        assert group_key() in PR._LAST_POST_MONO

    @pytest.mark.asyncio
    async def test_the_voice_post_is_recorded_as_our_own_with_the_spoken_text(
        self, monkeypatch, tts: FakeTTS
    ):
        """Without this the bot answers itself out loud on the next beat."""
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        ad = make_adapter(VOICE_ON, handler=FakeHandler("大家下午好呀"))
        await run_one_beat(ad)
        buffer = A.recent_group_messages("default", int(GROUP))
        assert buffer and buffer[-1][2] == "大家下午好呀" and buffer[-1][3] is True
        assert PR.last_message_is_self("default", GROUP)

    @pytest.mark.asyncio
    async def test_no_synthesized_file_is_left_behind(
        self, monkeypatch, tts: FakeTTS, audio_dir: Path
    ):
        """The audio cache lives on the same small disk the gateway runs from."""
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        ad = make_adapter(VOICE_ON)
        await run_one_beat(ad)
        assert len(record_segments(ad)) == 1
        assert list(audio_dir.iterdir()) == []

    @pytest.mark.asyncio
    async def test_the_configured_provider_reaches_the_synthesizer(
        self, monkeypatch, tts: FakeTTS
    ):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        ad = make_adapter({**VOICE_ON, "proactive_voice_provider": "fish"})
        await run_one_beat(ad)
        assert tts.providers == ["fish"]

    @pytest.mark.asyncio
    async def test_an_empty_provider_lets_the_tool_pick_its_own_default(
        self, monkeypatch, tts: FakeTTS
    ):
        """Empty string is 'unset', not a provider named ''."""
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        ad = make_adapter(VOICE_ON)
        await run_one_beat(ad)
        assert tts.providers == [None]

    @pytest.mark.asyncio
    async def test_the_dice_roll_cannot_change_which_group_is_eligible(
        self, monkeypatch, tts: FakeTTS
    ):
        """It sits after gate 7, so a spent speech cap still blocks everything.

        If the roll had become a gate — or moved above ``_eligible_groups`` —
        this would post out loud into a group whose window was already full.
        """
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        handler = FakeHandler("不该出现")
        ad = make_adapter(
            {
                **VOICE_ON,
                "group_rate_limit_window_minutes": 10,
                "group_rate_limit_max_messages": 1,
            },
            handler=handler,
        )
        A._GROUP_SPEECH.record(group_key())  # window already spent
        await run_one_beat(ad)
        assert handler.calls == 0
        assert record_segments(ad) == [] and sent_texts(ad) == []
        assert tts.calls == []


# ---------------------------------------------------------------------------
# 4. The prompt knows it will be heard
# ---------------------------------------------------------------------------


class TestVoicePrompt:
    def test_the_text_prompt_is_byte_identical_to_before(self):
        cfg = PR.resolve_config(
            {"proactive_enabled": True, "proactive_groups": [GROUP]}, None
        )
        lines = ["[10:00] 张三: 在吗"]
        assert PR.compose_prompt(cfg, lines) == PR.compose_prompt(
            cfg, lines, voice_max_chars=None
        )
        assert "语音" not in PR.compose_prompt(cfg, lines)

    def test_a_voice_beat_appends_the_hint_before_the_skip_hatch(self):
        cfg = PR.resolve_config(
            {"proactive_enabled": True, "proactive_groups": [GROUP]}, None
        )
        prompt = PR.compose_prompt(cfg, [], voice_max_chars=80)
        assert PR.voice_hint(80) in prompt
        assert "80" in prompt
        # The pinned vocabulary is untouched and the hatch stays last.
        assert PR.DEFAULT_PROMPT in prompt
        assert prompt.rstrip().endswith(PR.SKIP_HINT)
        assert prompt.index(PR.voice_hint(80)) < prompt.index(PR.SKIP_HINT)

    @pytest.mark.asyncio
    async def test_the_hint_reaches_the_model_only_on_a_voice_beat(
        self, monkeypatch, tts: FakeTTS
    ):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        handler = FakeHandler("好嘞")
        ad = make_adapter(VOICE_ON, handler=handler)
        await run_one_beat(ad)
        assert PR.voice_hint(120) in handler.prompts[0]

        A._reset_module_state()
        one_beat(monkeypatch)
        handler2 = FakeHandler("好嘞")
        ad2 = make_adapter(
            {**VOICE_ON, "proactive_voice_probability": 0}, handler=handler2
        )
        await run_one_beat(ad2)
        assert PR.voice_hint(120) not in handler2.prompts[0]
        assert "语音" not in handler2.prompts[0]


# ---------------------------------------------------------------------------
# 5. [MSG_BREAK] cannot survive into speech
# ---------------------------------------------------------------------------


class TestBubbleMarker:
    def test_flatten_bubbles_matches_what_the_text_path_records(self):
        assert (
            PR.flatten_bubbles("今晚有空[MSG_BREAK]一起打球？") == "今晚有空 一起打球？"
        )
        assert PR.flatten_bubbles("  就一句  ") == "就一句"
        assert PR.flatten_bubbles("[MSG_BREAK]") == ""

    @pytest.mark.asyncio
    async def test_the_marker_never_reaches_the_synthesizer_or_the_buffer(
        self, monkeypatch, tts: FakeTTS
    ):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        ad = make_adapter(
            VOICE_ON, handler=FakeHandler("今晚有空[MSG_BREAK]一起打球？")
        )
        await run_one_beat(ad)
        assert len(record_segments(ad)) == 1  # ONE bubble, not two
        assert tts.texts == ["今晚有空 一起打球？"]
        assert A.BUBBLE_SEPARATOR not in tts.texts[0]
        buffered = A.recent_group_messages("default", int(GROUP))[-1][2]
        assert A.BUBBLE_SEPARATOR not in buffered
        assert "今晚有空" in buffered and "一起打球？" in buffered


# ---------------------------------------------------------------------------
# 6. The degrade matrix
# ---------------------------------------------------------------------------


class TestDegrade:
    @pytest.mark.asyncio
    async def test_over_the_char_cap_the_post_goes_out_as_text(
        self, monkeypatch, tts: FakeTTS
    ):
        """A long monologue is obnoxious as speech in a way it is not as text."""
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        long_reply = "唠" * 40
        ad = make_adapter(
            {**VOICE_ON, "proactive_voice_max_chars": 20},
            handler=FakeHandler(long_reply),
        )
        await run_one_beat(ad)
        assert sent_texts(ad) == [long_reply]
        assert record_segments(ad) == []
        assert tts.calls == []  # the cap gates BEFORE paying for synthesis
        # …and the post is still one post.
        assert PR.sent_today(group_key(), DAY) == 1

    @pytest.mark.asyncio
    async def test_the_cap_counts_the_flattened_text_not_the_marker(
        self, monkeypatch, tts: FakeTTS
    ):
        """``[MSG_BREAK]`` is eleven characters nobody will ever hear.

        Two replies of the same *spoken* length must land on the same side of
        the ceiling, whichever one happened to be written in two bubbles.
        """
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        # 10 spoken chars + one joining space; 21 raw characters with the marker.
        ad = make_adapter(
            {**VOICE_ON, "proactive_voice_max_chars": 12},
            handler=FakeHandler("今晚有空吗[MSG_BREAK]一起打球呀"),
        )
        await run_one_beat(ad)
        assert len(record_segments(ad)) == 1
        assert tts.texts == ["今晚有空吗 一起打球呀"]

    @pytest.mark.asyncio
    async def test_tts_failure_falls_back_to_text_and_books_the_budget_once(
        self, monkeypatch, broken_tts: FakeTTS
    ):
        """Degrade, never lose the post — and never charge for it twice."""
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        ad = make_adapter(
            {
                **VOICE_ON,
                "group_rate_limit_window_minutes": 10,
                "group_rate_limit_max_messages": 3,
            },
            handler=FakeHandler("大家下午好呀"),
        )
        sends = spy_send(ad)
        await run_one_beat(ad)
        assert broken_tts.calls  # we did try
        assert record_segments(ad) == []
        assert sends == ["大家下午好呀"]  # …exactly one fallback, not a loop
        assert sent_texts(ad) == ["大家下午好呀"]  # …and the post survived
        assert PR.sent_today(group_key(), DAY) == 1
        # ONE unit of the shared window, not two: a degraded voice attempt
        # delivered one message.
        assert A.group_speech_allowed("default", GROUP, 600.0, 3) is True
        assert A.group_speech_allowed("default", GROUP, 600.0, 3) is True
        assert A.group_speech_allowed("default", GROUP, 600.0, 3) is False

    @pytest.mark.asyncio
    async def test_a_raising_synthesizer_also_falls_back(self, monkeypatch, audio_dir):
        def _boom(**kwargs: Any):
            raise RuntimeError("no TTS backend installed")

        monkeypatch.setattr(TTS, "text_to_speech_tool", _boom)
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        ad = make_adapter(VOICE_ON, handler=FakeHandler("照常营业"))
        await run_one_beat(ad)
        assert sent_texts(ad) == ["照常营业"]
        assert PR.sent_today(group_key(), DAY) == 1

    @pytest.mark.asyncio
    async def test_the_mute_is_not_a_fallback_case(self, monkeypatch, tts: FakeTTS):
        """A mute means "do not speak here", not "speak differently".

        The mute is already gate 5, so reaching ``speak()`` muted means it was
        flipped mid-beat; either way retrying as text would be asking the same
        question of the same gate.
        """
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        handler = FakeHandler("大家下午好呀")
        ad = make_adapter(VOICE_ON, handler=handler)
        sends = spy_send(ad)

        # Flip the mute after the gate ladder has run, immediately before
        # delivery, by muting from inside the model turn.
        async def _mute_then_answer(event):
            ad.config.extra["group_replies_enabled"] = False
            return await handler(event)

        ad._message_handler = _mute_then_answer
        await run_one_beat(ad)

        assert handler.calls == 1  # gate 5 passed; the mute landed on delivery
        assert record_segments(ad) == []
        assert sends == []  # the text lane was never even asked
        assert sent_texts(ad) == []
        assert PR.sent_today(group_key(), DAY) == 0  # nothing booked
        assert A.group_speech_allowed("default", GROUP, 600.0, 1) is True

    @pytest.mark.asyncio
    async def test_a_content_policy_refusal_is_not_a_fallback_case(
        self, monkeypatch, tts: FakeTTS
    ):
        """Retrying refused words as text would defeat the gate."""
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        ad = make_adapter(VOICE_ON, handler=FakeHandler(BLOCKED_TEXT))
        sends = spy_send(ad)
        await run_one_beat(ad)
        assert record_segments(ad) == []
        assert sends == []  # the refused words are not offered a second time
        assert sent_texts(ad) == []
        assert PR.sent_today(group_key(), DAY) == 0
        assert A.group_speech_allowed("default", GROUP, 600.0, 1) is True

    @pytest.mark.asyncio
    async def test_a_refusal_does_not_kill_the_loop(self, monkeypatch, tts: FakeTTS):
        calls = one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        ad = make_adapter(VOICE_ON, handler=FakeHandler(BLOCKED_TEXT))
        await run_one_beat(ad)
        assert calls["n"] == 2  # the loop went round again instead of dying


# ---------------------------------------------------------------------------
# 7. The call lane stays quiet
# ---------------------------------------------------------------------------


class TestCallLaneStaysQuiet:
    @pytest.mark.asyncio
    async def test_no_targets_places_no_call(self, monkeypatch, tts: FakeTTS):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        handler = FakeHandler("不该出现")
        ad = make_adapter(
            {"proactive_call_enabled": True, "voice_call_enabled": True},
            handler=handler,
        )
        await run_one_call_beat(ad)
        assert handler.calls == 0
        assert ad._client.actions == []
        assert VC.active_sessions() == ()

    @pytest.mark.asyncio
    async def test_the_call_subsystem_off_places_no_call(
        self, monkeypatch, tts: FakeTTS
    ):
        """This lane narrows ``voice_call``; it can never switch it on."""
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        handler = FakeHandler("不该出现")
        ad = make_adapter(
            {"proactive_call_enabled": True, "proactive_call_targets": [PEER]},
            handler=handler,
        )
        await run_one_call_beat(ad)
        assert handler.calls == 0
        assert ad._client.actions == []
        assert VC.active_sessions() == ()

    @pytest.mark.asyncio
    async def test_voice_call_allowed_users_narrows_this_lane_too(
        self, monkeypatch, tts: FakeTTS
    ):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        handler = FakeHandler("不该出现")
        ad = make_adapter(
            {**CALL_ON, "voice_call_allowed_users": ["10001"]}, handler=handler
        )
        await run_one_call_beat(ad)
        assert handler.calls == 0
        assert ad._client.actions == []

    @pytest.mark.asyncio
    async def test_outside_its_own_active_hours(self, monkeypatch, tts: FakeTTS):
        """09:00 is inside the group lane's window and outside this one's."""
        one_beat(monkeypatch)
        freeze_clock(monkeypatch, hour=9)
        handler = FakeHandler("不该出现")
        ad = make_adapter(CALL_ON, handler=handler)
        await run_one_call_beat(ad)
        assert handler.calls == 0
        assert ad._client.actions == []

    @pytest.mark.asyncio
    async def test_the_daily_max_is_enforced(self, monkeypatch, tts: FakeTTS):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        handler = FakeHandler("不该出现")
        ad = make_adapter(CALL_ON, handler=handler)
        PR.mark_called(peer_key(), DAY)
        await run_one_call_beat(ad)
        assert handler.calls == 0
        assert ad._client.actions == []

    @pytest.mark.asyncio
    async def test_the_min_gap_is_enforced(self, monkeypatch, tts: FakeTTS):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        handler = FakeHandler("不该出现")
        ad = make_adapter({**CALL_ON, "proactive_call_daily_max": 5}, handler=handler)
        PR._LAST_CALL_MONO[peer_key()] = time.monotonic()
        await run_one_call_beat(ad)
        assert handler.calls == 0
        assert ad._client.actions == []

    @pytest.mark.asyncio
    async def test_probability_zero_never_calls(self, monkeypatch, tts: FakeTTS):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        handler = FakeHandler("不该出现")
        ad = make_adapter({**CALL_ON, "proactive_call_probability": 0}, handler=handler)
        await run_one_call_beat(ad)
        assert handler.calls == 0

    @pytest.mark.asyncio
    async def test_an_offline_account_never_calls(self, monkeypatch, tts: FakeTTS):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        handler = FakeHandler("不该出现")
        ad = make_adapter(CALL_ON, handler=handler, online=False)
        await run_one_call_beat(ad)
        assert handler.calls == 0

    @pytest.mark.asyncio
    async def test_an_unknown_identity_never_calls(self, monkeypatch, tts: FakeTTS):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        handler = FakeHandler("不该出现")
        ad = make_adapter(CALL_ON, handler=handler)
        ad._client.last_self_id = None
        ad.self_ids = []
        await run_one_call_beat(ad)
        assert handler.calls == 0

    @pytest.mark.asyncio
    async def test_a_live_session_blocks_a_second_invite(
        self, monkeypatch, tts: FakeTTS
    ):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        handler = FakeHandler("不该出现")
        ad = make_adapter({**CALL_ON, "proactive_call_daily_max": 9}, handler=handler)
        VC.invite(
            peer_key(),
            chat_id=PEER,
            is_group=False,
            peer=PEER,
            now=time.monotonic(),
            cfg=VC.resolve_config({"voice_call_enabled": True}),
        )
        await run_one_call_beat(ad)
        assert handler.calls == 0

    @pytest.mark.asyncio
    async def test_the_model_can_decline_to_call(self, monkeypatch, tts: FakeTTS):
        """The SKIP hatch, before anything reaches the wire."""
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        handler = FakeHandler("SKIP")
        ad = make_adapter(CALL_ON, handler=handler)
        await run_one_call_beat(ad)
        assert handler.calls == 1  # the turn ran…
        assert ad._client.actions == []  # …and nobody's phone rang
        assert VC.active_sessions() == ()
        assert PR.called_today(peer_key(), DAY) == 0

    @pytest.mark.asyncio
    async def test_a_failing_turn_does_not_kill_the_loop(
        self, monkeypatch, tts: FakeTTS
    ):
        calls = one_beat(monkeypatch)
        freeze_clock(monkeypatch)

        class _Boom(FakeHandler):
            async def __call__(self, event):
                self.calls += 1
                raise RuntimeError("model down")

        ad = make_adapter(CALL_ON, handler=_Boom())
        await run_one_call_beat(ad)
        assert ad._client.actions == []
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_an_idle_call_loop_sleeps_the_recheck_interval(self, monkeypatch):
        """A disabled lane must keep waking up, or nothing can hot-apply."""
        freeze_clock(monkeypatch)
        delays: List[float] = []

        async def _sleep(cancel, secs):  # noqa: ANN001
            delays.append(secs)
            return len(delays) > 1

        monkeypatch.setattr(PR, "sleep_or_cancel", _sleep)
        await PR.proactive_call_loop(make_adapter(), asyncio.Event(), None)
        assert delays[0] == PR.IDLE_RECHECK_SECS


# ---------------------------------------------------------------------------
# 8. The call lane, when every gate does pass
# ---------------------------------------------------------------------------


class TestCallLanePlacesACall:
    @pytest.mark.asyncio
    async def test_it_pokes_then_speaks_the_line_the_model_wrote(
        self, monkeypatch, tts: FakeTTS
    ):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        handler = FakeHandler("在忙吗，我刚想到个事儿想跟你说")
        ad = make_adapter(CALL_ON, handler=handler)
        await run_one_call_beat(ad)

        # The phone rings before the voice arrives.
        assert [type(a).__name__ for a in ad._client.actions] == [
            "SendPoke",
            "SendPrivateMsg",
        ]
        assert pokes(ad)[0].user_id == int(PEER)
        assert len(record_segments(ad)) == 1
        # The persona's own words, not the canned ``voice_call.RING_LINE``.
        assert tts.texts == ["在忙吗，我刚想到个事儿想跟你说"]
        assert VC.RING_LINE not in tts.texts

    @pytest.mark.asyncio
    async def test_the_session_is_left_ringing_for_the_sweeper(
        self, monkeypatch, tts: FakeTTS
    ):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        ad = make_adapter(CALL_ON)
        await run_one_call_beat(ad)
        session = VC.snapshot(peer_key())
        assert session is not None
        assert session.state is VC.CallState.RINGING
        assert session.outgoing is True
        assert session.peer == PEER

    @pytest.mark.asyncio
    async def test_the_adapters_own_sweeper_can_collect_the_session(
        self, monkeypatch, tts: FakeTTS
    ):
        """This lane and the adapter's sweeper must agree on the session key.

        Both build it as ``speech_key(instance_id, chat_id)`` over the bare
        uin.  If they ever disagreed, an unanswered proactive call would sit
        RINGING forever and refuse every later invite for that person.
        """
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        ad = make_adapter(CALL_ON)
        await run_one_call_beat(ad)
        cfg = VC.resolve_config({"voice_call_enabled": True})
        expired = VC.sweep(time.monotonic() + 10_000.0, cfg, instance_id=ad.instance_id)
        assert [e.key for e in expired] == [peer_key()]
        assert expired[0].reason is VC.EndReason.RING_TIMEOUT

    @pytest.mark.asyncio
    async def test_the_prompt_says_it_will_be_heard_and_offers_the_hatch(
        self, monkeypatch, tts: FakeTTS
    ):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        handler = FakeHandler("喂，在吗")
        ad = make_adapter(CALL_ON, handler=handler)
        await run_one_call_beat(ad)
        prompt = handler.prompts[0]
        assert PR.DEFAULT_CALL_PROMPT in prompt
        assert PR.CALL_SKIP_HINT in prompt
        assert "SKIP" in prompt
        assert "markdown" in prompt  # the voice hint

    @pytest.mark.asyncio
    async def test_the_turn_runs_in_a_private_chat_on_its_own_session_lane(
        self, monkeypatch, tts: FakeTTS
    ):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        handler = FakeHandler("喂，在吗")
        ad = make_adapter(CALL_ON, handler=handler)
        await run_one_call_beat(ad)
        event = handler.events[0]
        assert event.source.chat_id == PEER
        assert event.source.chat_type == "dm"
        assert event.user_id == PR.PROACTIVE_SENDER_ID
        assert event.metadata["onebot_message_type"] == "private"
        assert event.metadata["onebot_group_id"] is None
        assert event.metadata["onebot_proactive"] is True
        assert event.allow_gateway_control is False
        assert event.is_command() is False

    @pytest.mark.asyncio
    async def test_the_call_books_its_own_budget(self, monkeypatch, tts: FakeTTS):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        ad = make_adapter(CALL_ON)
        await run_one_call_beat(ad)
        assert PR.called_today(peer_key(), DAY) == 1
        assert peer_key() in PR._LAST_CALL_MONO

    @pytest.mark.asyncio
    async def test_a_silent_tts_provider_still_charges_the_budget_and_clears_the_session(
        self, monkeypatch, broken_tts: FakeTTS
    ):
        """The poke already rang a real phone; charging only for successes
        would ring somebody once per beat for as long as TTS stayed down."""
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        ad = make_adapter(CALL_ON)
        await run_one_call_beat(ad)
        assert record_segments(ad) == []
        assert PR.called_today(peer_key(), DAY) == 1
        # No zombie: the session is collected rather than left RINGING forever.
        assert VC.snapshot(peer_key()) is None


# ---------------------------------------------------------------------------
# 9. The two lanes do not share a budget
# ---------------------------------------------------------------------------


class TestBudgetsAreIndependent:
    @pytest.mark.asyncio
    async def test_a_group_post_does_not_spend_the_call_allowance(
        self, monkeypatch, tts: FakeTTS
    ):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        ad = make_adapter({**VOICE_ON, **CALL_ON})
        await run_one_beat(ad)
        assert PR.sent_today(group_key(), DAY) == 1
        assert PR.called_today(peer_key(), DAY) == 0
        assert PR.called_today(group_key(), DAY) == 0

    @pytest.mark.asyncio
    async def test_a_call_does_not_spend_the_group_speech_budget(
        self, monkeypatch, tts: FakeTTS
    ):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        ad = make_adapter({
            **CALL_ON,
            "group_rate_limit_window_minutes": 10,
            "group_rate_limit_max_messages": 1,
        })
        await run_one_call_beat(ad)
        assert PR.called_today(peer_key(), DAY) == 1
        assert PR.sent_today(group_key(), DAY) == 0
        # The group's window is untouched — a DM is not speech in that room.
        assert A.group_speech_allowed("default", GROUP, 600.0, 1) is True

    def test_a_uin_that_collides_with_a_group_id_keeps_its_own_budget(self):
        """Group ids and uins share one numeric space; the dicts must not."""
        key = A.speech_key("default", "12345")
        PR.mark_sent(key, DAY)
        assert PR.sent_today(key, DAY) == 1
        assert PR.called_today(key, DAY) == 0
        PR.mark_called(key, DAY)
        PR.mark_called(key, DAY)
        assert PR.called_today(key, DAY) == 2
        assert PR.sent_today(key, DAY) == 1

    def test_reset_state_clears_both(self):
        key = A.speech_key("default", "12345")
        PR.mark_sent(key, DAY)
        PR.mark_called(key, DAY)
        PR._LAST_POST_MONO[key] = 1.0
        PR._LAST_CALL_MONO[key] = 1.0
        PR.reset_state()
        assert PR.sent_today(key, DAY) == 0
        assert PR.called_today(key, DAY) == 0
        assert PR._LAST_POST_MONO == {} and PR._LAST_CALL_MONO == {}


# ---------------------------------------------------------------------------
# 10. Hot apply — a config save takes effect on the next beat
# ---------------------------------------------------------------------------


class TestCallHotApply:
    @pytest.mark.asyncio
    async def test_enable_mid_loop_takes_effect(self, monkeypatch, tts: FakeTTS):
        """Loop starts DISABLED; the config changes while it sleeps."""
        freeze_clock(monkeypatch)
        handler = FakeHandler("喂，在吗")
        ad = make_adapter(handler=handler)
        one_beat(monkeypatch, on_first=lambda: ad.config.extra.update(CALL_ON))
        await PR.proactive_call_loop(ad, asyncio.Event(), None)
        assert handler.calls == 1
        assert len(record_segments(ad)) == 1

    @pytest.mark.asyncio
    async def test_disable_mid_loop_goes_silent(self, monkeypatch, tts: FakeTTS):
        freeze_clock(monkeypatch)
        handler = FakeHandler("不该出现")
        ad = make_adapter(CALL_ON, handler=handler)
        one_beat(
            monkeypatch,
            on_first=lambda: ad.config.extra.update(proactive_call_enabled=False),
        )
        await PR.proactive_call_loop(ad, asyncio.Event(), PR.live_call_config(ad))
        assert handler.calls == 0
        assert ad._client.actions == []

    @pytest.mark.asyncio
    async def test_switching_off_voice_call_mid_loop_silences_the_lane(
        self, monkeypatch, tts: FakeTTS
    ):
        freeze_clock(monkeypatch)
        handler = FakeHandler("不该出现")
        ad = make_adapter(CALL_ON, handler=handler)
        one_beat(
            monkeypatch,
            on_first=lambda: ad.config.extra.update(voice_call_enabled=False),
        )
        await PR.proactive_call_loop(ad, asyncio.Event(), PR.live_call_config(ad))
        assert handler.calls == 0
        assert ad._client.actions == []

    @pytest.mark.asyncio
    async def test_enabling_voice_mid_loop_switches_the_modality(
        self, monkeypatch, tts: FakeTTS
    ):
        freeze_clock(monkeypatch)
        handler = FakeHandler("热启用后冒个泡")
        ad = make_adapter({"proactive_enabled": True}, handler=handler)
        one_beat(
            monkeypatch,
            on_first=lambda: ad.config.extra.update(
                proactive_voice_enabled=True, proactive_voice_probability=1
            ),
        )
        await PR.proactive_loop(ad, asyncio.Event(), PR.live_config(ad))
        assert len(record_segments(ad)) == 1
        assert sent_texts(ad) == []


# ---------------------------------------------------------------------------
# 11. The shared gate helpers really are shared
# ---------------------------------------------------------------------------


class TestSharedGates:
    def test_link_healthy_reads_the_health_snapshot(self):
        ad = make_adapter()
        assert PR.link_healthy(ad) is True
        ad._client.connected = False
        assert PR.link_healthy(ad) is False

    def test_a_backend_that_never_reports_status_is_not_treated_as_offline(self):
        ad = make_adapter()
        ad._account_online = None
        assert PR.link_healthy(ad) is True

    def test_identity_known(self):
        ad = make_adapter()
        assert PR.identity_known(ad) is True
        ad._client.last_self_id = None
        ad.self_ids = []
        assert PR.identity_known(ad) is False

    def test_passes_probability_is_the_gate_both_loops_use(self):
        import random as _random

        assert PR.passes_probability(1.0) is True
        assert PR.passes_probability(2.0) is True
        assert PR.passes_probability(0.0) is False
        rng = _random.Random(0)
        draws = [PR.passes_probability(0.5, rng) for _ in range(400)]
        assert 120 < sum(draws) < 280

    def test_always_on_does_not_consume_the_rng_stream(self):
        import random as _random

        rng = _random.Random(7)
        expected = _random.Random(7).random()
        assert PR.passes_probability(1.0, rng) is True
        assert rng.random() == expected
