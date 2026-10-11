"""Wiring the QQ voice-call session machine into the OneBot adapter.

``plugins/platforms/onebot/voice_call.py`` decides and
``plugins/platforms/onebot/voice.py`` speaks; both are already covered by
their own suites.  What is untested until here is the *plumbing*, and every
one of these tests exists because the corresponding wiring mistake is silent
in production:

* a call hook on the inbound path is one edit away from changing what happens
  to **every** QQ message, so the first test in this file is the regression
  guard: with no live session the path is byte for byte what it was;
* ``voice_call`` is pure and fires nothing on its own — without the resident
  sweeper a ring timeout, an idle timeout and a duration cap simply never
  happen, and a call rings forever;
* while the phone is ringing, "好啊" is an **answer**, not a chat turn.  Run an
  agent turn on it as well and the person is answered twice for one word;
* while a call is up the bot answers in **voice** — that is the entire
  feature — and when TTS is unavailable it answers in text rather than not at
  all, because a silent call is worse than one that fell back to typing;
* a ``voice_call_*`` / ``proactive_voice_*`` / ``proactive_call_*`` key that
  is missing from ``_PRIVATE_YAML_KEYS`` is dropped by the config merge
  *without a word*, so the switch could never be turned on.

Everything is offline: the TTS tool is patched, no audio is synthesized, no
socket is opened, and no path here can reach a real QQ account.
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

from plugins.platforms.onebot import adapter as A
from plugins.platforms.onebot import client as C
from plugins.platforms.onebot import protocol as P
from plugins.platforms.onebot import voice_call as VC


DM_PEER = "2104743984"
DM_CHAT = "2104743984"
GROUP = "183287894"

#: Every key this subtask added to ``_PRIVATE_YAML_KEYS``.  Spelled out rather
#: than derived from the tuple: the point of the test is that the *names* are
#: the ones ``voice_call.resolve_config`` and the proactive lane read, and a
#: test that reads them back out of the same tuple would pass on a typo.
NEW_YAML_KEYS = (
    "voice_call_enabled",
    "voice_call_ring_timeout_secs",
    "voice_call_idle_timeout_secs",
    "voice_call_max_duration_secs",
    "voice_call_max_turns",
    "voice_call_allow_groups",
    "voice_call_allowed_users",
    "proactive_voice_enabled",
    "proactive_voice_probability",
    "proactive_voice_max_chars",
    "proactive_voice_provider",
    "proactive_call_enabled",
    "proactive_call_targets",
    "proactive_call_daily_max",
    "proactive_call_min_gap_minutes",
    "proactive_call_active_start_hour",
    "proactive_call_active_end_hour",
    "proactive_call_probability",
    "proactive_call_prompt",
)


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
        self.closed = False

    async def call_action(self, action, *, timeout=None):  # noqa: ANN001
        self.actions.append(action)
        return {"status": "ok", "retcode": 0, "data": {"message_id": 1001}}

    async def send_action(self, action):  # noqa: ANN001
        self.actions.append(action)

    async def connect(self):
        self.connected = True

    async def inbound(self):
        """Never yields: the dispatch loop must park, not spin."""
        stop = asyncio.Event()
        await stop.wait()
        yield  # pragma: no cover — unreachable, keeps this an async generator

    async def close(self):
        self.connected = False
        self.closed = True


class Recorder:
    """A turn that records the event and answers nothing."""

    def __init__(self) -> None:
        self.events: List[Any] = []

    async def __call__(self, event):  # noqa: ANN001
        self.events.append(event)


class Turn:
    """A turn that answers through ``adapter.send`` — the real reply seam."""

    def __init__(self, adapter: A.OneBotAdapter, reply: str = "我在听着呢") -> None:
        self.adapter = adapter
        self.reply = reply
        self.events: List[Any] = []

    async def __call__(self, event):  # noqa: ANN001
        self.events.append(event)
        await self.adapter.send(event.source.chat_id, self.reply)


class FakeTTS:
    """Stand-in for ``text_to_speech_tool``; writes real files."""

    def __init__(
        self,
        outdir: Path,
        *,
        envelope: Optional[str] = None,
        payload: bytes = b"ID3\x03\x00fake-mp3-bytes",
    ) -> None:
        self.outdir = outdir
        self.envelope = envelope
        self.payload = payload
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.envelope is not None:
            return self.envelope
        path = self.outdir / f"speech_{len(self.calls)}.mp3"
        path.write_bytes(self.payload)
        return json.dumps({
            "success": True,
            "file_path": str(path),
            "file_paths": [str(path)],
            "provider": "edge",
            "chunk_count": 1,
        })

    @property
    def texts(self) -> List[str]:
        return [call.get("text", "") for call in self.calls]


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def make_adapter(
    extra: Optional[Dict[str, Any]] = None,
    *,
    client: Optional[FakeClient] = None,
) -> A.OneBotAdapter:
    """A connected-looking adapter with calls switched on by default."""
    base: Dict[str, Any] = {
        "ws_url": "ws://127.0.0.1:3001",
        "group_replies_enabled": True,
        "group_whitelist": [GROUP],
        "bot_nickname": "格兰",
        "voice_call_enabled": True,
    }
    base.update(extra or {})
    ad = A.OneBotAdapter(PlatformConfig(enabled=True, extra=base))
    ad._client = client or FakeClient()
    ad._running = True
    ad._semaphore = asyncio.Semaphore(4)
    ad._account_online = True
    ad._group_names[int(GROUP)] = "测试群"
    return ad


def private_event(text="hi", *, uid=int(DM_PEER), self_id=100, message_id=1):
    return P.MessageEvent(
        self_id=self_id,
        message_type=P.MessageType.PRIVATE,
        sub_type="friend",
        group_id=None,
        user_id=uid,
        message_id=message_id,
        message=[P.TextSegment(text=text)],
        raw_message=text,
        time=1,
        sender=P.Sender(user_id=uid, nickname="bob"),
    )


def key_for(ad: A.OneBotAdapter, chat_id: str = DM_CHAT) -> str:
    return A.speech_key(ad.instance_id, chat_id)


def ring(ad: A.OneBotAdapter, *, chat_id: str = DM_CHAT, age: float = 0.0) -> str:
    """Put *chat_id* in RINGING without touching the wire.

    ``age`` back-dates ``started_at``, which is how a deadline is crossed in
    this suite: ``voice_call`` floors every timeout at 1.0s, so waiting one
    out in real time would put a second of sleep in a unit test for no gain.
    """
    key = key_for(ad, chat_id)
    decision = VC.invite(
        key,
        chat_id=chat_id,
        is_group=chat_id.startswith("g"),
        peer=DM_PEER,
        now=time.monotonic() - age,
        cfg=VC.live_config(ad),
    )
    assert decision.ok, decision.refusal
    return key


def active(ad: A.OneBotAdapter, *, chat_id: str = DM_CHAT) -> str:
    """Put *chat_id* in ACTIVE without touching the wire."""
    key = ring(ad, chat_id=chat_id)
    assert VC.accept(key, time.monotonic()).ok
    return key


def install_tts(monkeypatch: pytest.MonkeyPatch, outdir: Path, **kwargs) -> FakeTTS:
    fake = FakeTTS(outdir, **kwargs)
    monkeypatch.setattr(TTS, "text_to_speech_tool", fake)
    return fake


def segments_of(action: Any) -> List[Any]:
    return list(getattr(action, "message", []) or [])


def record_segments(ad: A.OneBotAdapter) -> List[P.RecordSegment]:
    return [
        seg
        for action in ad._client.actions
        for seg in segments_of(action)
        if isinstance(seg, P.RecordSegment)
    ]


def text_segments(ad: A.OneBotAdapter) -> List[str]:
    return [
        seg.text
        for action in ad._client.actions
        for seg in segments_of(action)
        if isinstance(seg, P.TextSegment)
    ]


async def wait_until(predicate, timeout: float = 5.0) -> bool:
    """Poll *predicate* until it holds.  Generous bound, event-shaped assert."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


@pytest.fixture
def audio_dir(tmp_path: Path) -> Path:
    out = tmp_path / "audio_cache"
    out.mkdir()
    return out


@pytest.fixture(autouse=True)
def _reset_state():
    A._reset_module_state()
    C.set_live_client(None, None)
    C.set_live_adapter(None)
    yield
    A._reset_module_state()
    C.set_live_client(None, None)
    C.set_live_adapter(None)


# ---------------------------------------------------------------------------
# 1. The regression guard — no session, no change
# ---------------------------------------------------------------------------


class TestNoSessionIsUnchanged:
    """The hook sits on the path every QQ message takes.  This is the guard."""

    @pytest.mark.asyncio
    async def test_an_ordinary_message_still_runs_a_text_turn(
        self, monkeypatch, audio_dir
    ):
        ad = make_adapter()
        turn = Turn(ad)
        ad.handle_message = turn  # type: ignore[assignment]
        tts = install_tts(monkeypatch, audio_dir)

        await ad._on_message_event(private_event("在吗"))
        assert await wait_until(lambda: len(turn.events) == 1)
        await wait_until(lambda: bool(ad._client.actions))

        assert turn.events[0].text == "在吗"
        assert text_segments(ad) == ["我在听着呢"]
        assert record_segments(ad) == []
        # Not merely "no bubble": nothing was even synthesized.
        assert tts.calls == []

    @pytest.mark.asyncio
    async def test_the_feature_being_off_changes_nothing_either(self, monkeypatch):
        ad = make_adapter({"voice_call_enabled": False})
        rec = Recorder()
        ad.handle_message = rec  # type: ignore[assignment]

        await ad._on_message_event(private_event("好啊"))
        assert await wait_until(lambda: len(rec.events) == 1)
        # "好啊" is accept vocabulary.  With no session it is just a message.
        assert VC.snapshot(key_for(ad)) is None


# ---------------------------------------------------------------------------
# 2. Lifecycle: the resident sweeper and the published adapter
# ---------------------------------------------------------------------------


@pytest.fixture
def connectable(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    """Make ``adapter.connect()`` run for real against a fake socket."""
    client = FakeClient()
    monkeypatch.setattr(A, "OneBotClient", lambda cfg, on_self_id=None: client)

    async def _preflight(self) -> bool:  # noqa: ANN001
        return False

    monkeypatch.setattr(A.OneBotAdapter, "_preflight", _preflight)
    return client


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_sweeper_starts_on_connect_and_stops_on_disconnect(self, connectable):
        ad = make_adapter(client=connectable)
        assert await ad.connect() is True
        try:
            assert ad._call_sweeper_task is not None
            assert not ad._call_sweeper_task.done()
            assert ad._call_sweeper_cancel is not None
        finally:
            await ad.disconnect()
        assert ad._call_sweeper_task is None
        assert ad._call_sweeper_cancel is None

    @pytest.mark.asyncio
    async def test_a_reconnect_does_not_start_a_second_sweeper(self):
        """Two sweepers would perform the same expiry twice — two sign-offs."""
        ad = make_adapter()
        ad._start_call_sweeper()
        first = ad._call_sweeper_task
        assert first is not None
        ad._start_call_sweeper()
        assert ad._call_sweeper_task is first
        await ad.disconnect()

    @pytest.mark.asyncio
    async def test_the_cancel_event_is_set_before_the_task_is_cancelled(self):
        """Ask first, cancel second, so a mid-flight sign-off gets its words out."""
        ad = make_adapter()
        ad._start_call_sweeper()
        cancel = ad._call_sweeper_cancel
        assert cancel is not None and not cancel.is_set()
        await ad.disconnect()
        assert cancel.is_set()

    @pytest.mark.asyncio
    async def test_the_live_adapter_is_published_on_connect_and_cleared(
        self, connectable
    ):
        """Without this the voice tools fall back to the un-gated raw transport."""
        assert C.get_live_adapter() == (None, None)
        ad = make_adapter(client=connectable)
        assert await ad.connect() is True
        try:
            published, loop = C.get_live_adapter()
            assert published is ad
            assert loop is asyncio.get_running_loop()
        finally:
            await ad.disconnect()
        assert C.get_live_adapter() == (None, None)

    @pytest.mark.asyncio
    async def test_a_ring_timeout_fires_through_the_resident_loop(
        self, monkeypatch, audio_dir
    ):
        install_tts(monkeypatch, audio_dir)
        monkeypatch.setattr(A, "CALL_SWEEP_SECS", 0.02)
        client = FakeClient()
        ad = make_adapter({"voice_call_ring_timeout_secs": 1.0}, client=client)
        key = ring(ad, age=5.0)  # already past the (floored) 1.0s deadline
        ad._start_call_sweeper()
        try:
            assert await wait_until(lambda: VC.snapshot(key) is None)
            # ...and it said so out loud rather than dropping the call silently.
            assert await wait_until(
                lambda: any(
                    isinstance(seg, P.RecordSegment)
                    for action in client.actions
                    for seg in segments_of(action)
                )
            )
        finally:
            await ad.disconnect()


# ---------------------------------------------------------------------------
# 3. Inbound routing: what a word means depends on the state
# ---------------------------------------------------------------------------


class TestInboundRouting:
    @pytest.mark.asyncio
    async def test_ringing_plus_yes_accepts_and_runs_no_agent_turn(
        self, monkeypatch, audio_dir
    ):
        install_tts(monkeypatch, audio_dir)
        ad = make_adapter()
        rec = Recorder()
        ad.handle_message = rec  # type: ignore[assignment]
        key = ring(ad)

        await ad._on_message_event(private_event("好啊"))

        session = VC.snapshot(key)
        assert session is not None and session.state is VC.CallState.ACTIVE
        # The accept line went out as a voice bubble...
        assert len(record_segments(ad)) == 1
        # ...and "好啊" was NOT also answered as conversation.
        assert rec.events == []

    @pytest.mark.asyncio
    async def test_ringing_plus_no_declines_and_ends_the_session(
        self, monkeypatch, audio_dir
    ):
        install_tts(monkeypatch, audio_dir)
        ad = make_adapter()
        rec = Recorder()
        ad.handle_message = rec  # type: ignore[assignment]
        key = ring(ad)

        await ad._on_message_event(private_event("不用了"))

        assert VC.snapshot(key) is None
        assert len(record_segments(ad)) == 1
        assert rec.events == []

    @pytest.mark.asyncio
    async def test_active_ordinary_message_runs_the_turn_and_counts_it(self):
        ad = make_adapter()
        rec = Recorder()
        ad.handle_message = rec  # type: ignore[assignment]
        key = active(ad)

        await ad._on_message_event(private_event("你在干嘛呢"))
        assert await wait_until(lambda: len(rec.events) == 1)

        session = VC.snapshot(key)
        assert session is not None and session.state is VC.CallState.ACTIVE
        # ``note_remote_turn`` ran: the turn counter and the idle deadline move.
        assert session.turns == 1

    @pytest.mark.asyncio
    async def test_active_plus_goodbye_hangs_up_without_a_turn(
        self, monkeypatch, audio_dir
    ):
        install_tts(monkeypatch, audio_dir)
        ad = make_adapter()
        rec = Recorder()
        ad.handle_message = rec  # type: ignore[assignment]
        key = active(ad)

        await ad._on_message_event(private_event("先挂了"))

        assert VC.snapshot(key) is None
        assert rec.events == []

    @pytest.mark.asyncio
    async def test_a_turn_that_crosses_max_turns_ends_the_call_immediately(
        self, monkeypatch, audio_dir
    ):
        """A cap crossed BY the turn must not wait out a sweeper tick."""
        install_tts(monkeypatch, audio_dir)
        ad = make_adapter({"voice_call_max_turns": 1})
        rec = Recorder()
        ad.handle_message = rec  # type: ignore[assignment]
        key = active(ad)

        await ad._on_message_event(private_event("你在干嘛呢"))

        # No sweeper is running in this test: the post-turn sweep is the only
        # thing that could have collected it.
        assert ad._call_sweeper_task is None
        assert VC.snapshot(key) is None


# ---------------------------------------------------------------------------
# 4. While the call is up, the bot answers with its voice
# ---------------------------------------------------------------------------


class TestActiveRepliesAreSpoken:
    @pytest.mark.asyncio
    async def test_a_reply_goes_out_as_a_record_segment(self, monkeypatch, audio_dir):
        tts = install_tts(monkeypatch, audio_dir)
        ad = make_adapter()
        turn = Turn(ad, reply="我在听着呢")
        ad.handle_message = turn  # type: ignore[assignment]
        active(ad)

        await ad._on_message_event(private_event("你在干嘛呢"))
        assert await wait_until(lambda: bool(record_segments(ad)))

        records = record_segments(ad)
        assert len(records) == 1
        assert records[0].file.startswith("base64://")
        # The reply is spoken, not typed.
        assert text_segments(ad) == []
        assert tts.texts == ["我在听着呢"]

    @pytest.mark.asyncio
    async def test_the_synthesized_files_are_cleaned_up(self, monkeypatch, audio_dir):
        install_tts(monkeypatch, audio_dir)
        ad = make_adapter()
        turn = Turn(ad)
        ad.handle_message = turn  # type: ignore[assignment]
        active(ad)

        await ad._on_message_event(private_event("你在干嘛呢"))
        assert await wait_until(lambda: bool(record_segments(ad)))
        assert list(audio_dir.iterdir()) == []

    @pytest.mark.asyncio
    async def test_our_own_bubble_counts_as_a_turn(self, monkeypatch, audio_dir):
        install_tts(monkeypatch, audio_dir)
        ad = make_adapter()
        turn = Turn(ad)
        ad.handle_message = turn  # type: ignore[assignment]
        key = active(ad)

        await ad._on_message_event(private_event("你在干嘛呢"))
        assert await wait_until(lambda: bool(record_segments(ad)))

        session = VC.snapshot(key)
        # One theirs (note_remote_turn), one ours (note_local_turn): the cap
        # counts bubbles from either side.
        assert session is not None and session.turns == 2

    @pytest.mark.asyncio
    async def test_tts_failure_falls_back_to_text_and_the_reply_arrives(
        self, monkeypatch, audio_dir
    ):
        """A call that fell back to typing still answered.  Silence does not."""
        install_tts(
            monkeypatch,
            audio_dir,
            envelope=json.dumps({"success": False, "error": "no provider configured"}),
        )
        ad = make_adapter()
        turn = Turn(ad, reply="我在听着呢")
        ad.handle_message = turn  # type: ignore[assignment]
        active(ad)

        await ad._on_message_event(private_event("你在干嘛呢"))
        assert await wait_until(lambda: bool(text_segments(ad)))

        assert text_segments(ad) == ["我在听着呢"]
        assert record_segments(ad) == []

    @pytest.mark.asyncio
    async def test_the_plugins_own_markers_never_reach_the_provider(
        self, monkeypatch, audio_dir
    ):
        """A provider reads "[MSG_BREAK]" and "[STICKER:…]" out loud verbatim."""
        tts = install_tts(monkeypatch, audio_dir)
        ad = make_adapter()
        active(ad)

        result = await ad.send(DM_CHAT, "好呀[MSG_BREAK]我这就来 [STICKER:thumbs-up]")

        assert result.success is True
        # The bubble break survives as a pause (``spoken_script`` turns the
        # line break into sentence punctuation); the markers themselves are
        # gone, and both halves of the reply are still there.
        spoken = tts.texts[0]
        assert "MSG_BREAK" not in spoken and "STICKER" not in spoken
        assert "好呀" in spoken and "我这就来" in spoken
        assert len(record_segments(ad)) == 1

    @pytest.mark.asyncio
    async def test_nothing_is_spoken_once_the_call_has_ended(
        self, monkeypatch, audio_dir
    ):
        tts = install_tts(monkeypatch, audio_dir)
        ad = make_adapter()
        key = active(ad)
        assert VC.hangup(key, VC.EndReason.HANGUP_LOCAL, time.monotonic()).ok
        tts.calls.clear()

        result = await ad.send(DM_CHAT, "普通的一句回复")
        assert result.success is True
        assert text_segments(ad) == ["普通的一句回复"]
        assert tts.calls == []

    @pytest.mark.asyncio
    async def test_a_suppressed_system_message_is_never_spoken(
        self, monkeypatch, audio_dir
    ):
        """A hidden heartbeat read out loud is worse than one merely printed."""
        tts = install_tts(monkeypatch, audio_dir)
        ad = make_adapter({"suppress_system_messages": True})
        active(ad)

        result = await ad.send(
            DM_CHAT,
            "⏳ 处理中…",
            metadata={A.NON_CONVERSATIONAL_METADATA_KEY: True},
        )
        assert A.is_suppressed_send_result(result)
        assert tts.calls == []
        assert ad._client.actions == []


# ---------------------------------------------------------------------------
# 5. Programmatic entry points
# ---------------------------------------------------------------------------


class TestEntryPoints:
    @pytest.mark.asyncio
    async def test_start_voice_call_rings_the_peer_and_speaks(
        self, monkeypatch, audio_dir
    ):
        install_tts(monkeypatch, audio_dir)
        ad = make_adapter()

        decision = await ad.start_voice_call(DM_CHAT)

        assert decision is not None and decision.ok
        assert decision.state is VC.CallState.RINGING
        session = VC.snapshot(key_for(ad))
        assert session is not None and session.peer == DM_PEER
        # The phone rings before the voice arrives.
        pokes = [a for a in ad._client.actions if isinstance(a, P.SendPoke)]
        assert len(pokes) == 1 and pokes[0].user_id == int(DM_PEER)
        assert len(record_segments(ad)) == 1

    @pytest.mark.asyncio
    async def test_end_voice_call_hangs_up_and_drops_the_session(
        self, monkeypatch, audio_dir
    ):
        install_tts(monkeypatch, audio_dir)
        ad = make_adapter()
        key = active(ad)

        decision = await ad.end_voice_call(DM_CHAT)

        assert decision is not None and decision.ok
        assert decision.end_reason is VC.EndReason.HANGUP_LOCAL
        assert VC.snapshot(key) is None
        assert len(record_segments(ad)) == 1

    @pytest.mark.asyncio
    async def test_a_refusal_comes_back_as_a_decision_not_an_exception(self):
        """The caller has to be able to tell "disabled" from "already live"."""
        ad = make_adapter({"voice_call_enabled": False})
        decision = await ad.start_voice_call(DM_CHAT)
        assert decision is not None and decision.ok is False
        assert decision.refusal is VC.Refusal.DISABLED
        assert ad._client.actions == []

    @pytest.mark.asyncio
    async def test_a_group_is_refused_unless_the_operator_opted_in(self):
        ad = make_adapter()
        decision = await ad.start_voice_call(f"g{GROUP}", peer=DM_PEER)
        assert decision is not None and decision.refusal is VC.Refusal.GROUPS_DISABLED

    @pytest.mark.asyncio
    async def test_end_voice_call_on_a_quiet_chat_is_a_refusal(self):
        ad = make_adapter()
        decision = await ad.end_voice_call(DM_CHAT)
        assert decision is not None and decision.refusal is VC.Refusal.NO_SESSION


# ---------------------------------------------------------------------------
# 6. Config plumbing
# ---------------------------------------------------------------------------


class TestYamlKeys:
    @pytest.mark.parametrize("key", NEW_YAML_KEYS)
    def test_key_survives_the_top_level_of_platforms_onebot(self, key: str):
        extras = A._apply_yaml_config({}, {key: "sentinel"})
        assert extras is not None and extras[key] == "sentinel"

    @pytest.mark.parametrize("key", NEW_YAML_KEYS)
    def test_key_survives_a_nested_extra_block(self, key: str):
        extras = A._apply_yaml_config({}, {"extra": {key: "sentinel"}})
        assert extras is not None and extras[key] == "sentinel"

    def test_the_switch_reaches_resolve_config_end_to_end(self):
        """The merge is only useful if ``voice_call`` can read what it produced."""
        extras = A._apply_yaml_config(
            {},
            {"voice_call_enabled": True, "voice_call_max_turns": 4},
        )
        ad = A.OneBotAdapter(PlatformConfig(enabled=True, extra=dict(extras or {})))
        cfg = VC.live_config(ad)
        assert cfg is not None and cfg.max_turns == 4

    def test_calls_are_off_in_the_shipped_default(self):
        ad = A.OneBotAdapter(
            PlatformConfig(enabled=True, extra={"ws_url": "ws://127.0.0.1:3001"})
        )
        assert VC.live_config(ad) is None

    def test_no_env_var_can_switch_calls_on(self, monkeypatch):
        """YAML-only, deliberately: see README.md and decision B4-5."""
        for key in NEW_YAML_KEYS:
            monkeypatch.setenv(f"ONEBOT_{key.upper()}", "true")
        ad = A.OneBotAdapter(
            PlatformConfig(enabled=True, extra={"ws_url": "ws://127.0.0.1:3001"})
        )
        assert VC.live_config(ad) is None


# ---------------------------------------------------------------------------
# 7. Test-isolation hook
# ---------------------------------------------------------------------------


def test_reset_module_state_clears_call_sessions():
    """Sessions are process-wide on purpose; without this they leak across files."""
    ad = make_adapter()
    key = active(ad)
    assert VC.snapshot(key) is not None
    A._reset_module_state()
    assert VC.snapshot(key) is None
    assert VC.active_sessions() == ()
