"""The QQ voice path: what ``voice.speak`` guarantees that ``send_voice`` did not.

``OneBotAdapter.send_voice`` reaches the wire through ``_send_attachment``,
which bypasses ``OneBotAdapter.send`` and therefore three of its guarantees.
``plugins/platforms/onebot/voice.py`` exists to put them back in one place, so
this suite is written as the contract for that place rather than as coverage
of its lines:

* a **muted** group is not spoken into — and the mute is checked *before* the
  expensive part, so a pressed panic switch does not also run a TTS job;
* the **Tencent content policy** blocks the send and fails closed;
* a successful group post lands in the shared recent-chatter buffer with
  ``is_self=True`` and with the *spoken text*, because that buffer is what
  ``proactive.last_message_is_self`` reads to avoid talking over itself;
* the payload really is a ``record`` segment carrying ``base64://`` (a
  literal host path would resolve to nothing inside NapCat's container);
* a caption is a separate, **earlier** message — QQ drops anything sitting
  beside a voice bubble;
* every synthesized file is deleted, on the success path and on each failure
  path, asserted by watching a temp directory rather than by trusting a call
  count.

Everything is offline: the TTS tool is patched, no audio is synthesized, no
socket is opened, and no path here can reach a real QQ group.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

import tools.tts_tool as TTS
from gateway.config import PlatformConfig

from plugins.platforms.onebot import adapter as A
from plugins.platforms.onebot import protocol as P
from plugins.platforms.onebot import voice as V
from plugins.qzone import policy as POLICY


GROUP = "183287894"
DM_PEER = "536132102"

#: A phrase the ported rule table blocks (``fraud.account-abuse``), shared
#: verbatim with ``test_onebot_content_gate.py`` so a revision of the rule
#: table fails loudly in both suites instead of silently testing nothing.
BLOCKED_TEXT = "QQ解冻教程"


def test_the_blocked_fixture_is_actually_blocked():
    """Guard the guard: every refusal test below is meaningless if this drifts."""
    cfg = POLICY.resolve_config(None)
    assert POLICY.moderate_text(BLOCKED_TEXT, cfg).decision.allowed is False


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeClient:
    """Records every outbound action; never touches a socket."""

    def __init__(self, response: Optional[Dict[str, Any]] = None) -> None:
        self.actions: List[Any] = []
        self.connected = True
        self.response = response or {
            "status": "ok",
            "retcode": 0,
            "data": {"message_id": 1001},
        }

    async def call_action(self, action, *, timeout=None):  # noqa: ANN001
        self.actions.append(action)
        return self.response

    async def send_action(self, action):  # noqa: ANN001
        self.actions.append(action)

    async def close(self):
        self.connected = False


def make_adapter(
    extra: Optional[Dict[str, Any]] = None,
    *,
    response: Optional[Dict[str, Any]] = None,
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
    ad._client = FakeClient(response)
    ad._running = True
    ad._semaphore = asyncio.Semaphore(4)
    ad._account_online = True
    return ad


class FakeTTS:
    """Stand-in for ``text_to_speech_tool``: writes real files, records calls.

    Real files, because the cleanup contract is asserted by looking at the
    directory afterwards — a mock that only counts ``unlink`` calls would pass
    while leaking every byte in production.
    """

    def __init__(
        self,
        outdir: Path,
        *,
        files: int = 1,
        envelope: Optional[str] = None,
        payload: bytes = b"ID3\x03\x00fake-mp3-bytes",
    ) -> None:
        self.outdir = outdir
        self.files = files
        self.envelope = envelope
        self.payload = payload
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.envelope is not None:
            return self.envelope
        paths: List[str] = []
        for index in range(self.files):
            path = self.outdir / f"speech_{len(self.calls)}_{index}.mp3"
            path.write_bytes(self.payload)
            paths.append(str(path))
        return json.dumps({
            "success": True,
            "file_path": paths[0],
            "file_paths": paths,
            "media_tag": "\n".join(f"MEDIA:{p}" for p in paths),
            "provider": "edge",
            "voice_compatible": True,
            "chunk_count": len(paths),
        })

    @property
    def texts(self) -> List[str]:
        return [call.get("text", "") for call in self.calls]


@pytest.fixture
def audio_dir(tmp_path: Path) -> Path:
    """Where the fake synthesizer writes; asserted empty after every speak."""
    out = tmp_path / "audio_cache"
    out.mkdir()
    return out


@pytest.fixture
def tts(monkeypatch: pytest.MonkeyPatch, audio_dir: Path) -> FakeTTS:
    fake = FakeTTS(audio_dir)
    monkeypatch.setattr(TTS, "text_to_speech_tool", fake)
    return fake


def install_tts(monkeypatch: pytest.MonkeyPatch, fake: FakeTTS) -> FakeTTS:
    monkeypatch.setattr(TTS, "text_to_speech_tool", fake)
    return fake


@pytest.fixture(autouse=True)
def _reset_state():
    A._reset_module_state()
    yield
    A._reset_module_state()


# ---------------------------------------------------------------------------
# Wire inspection helpers
# ---------------------------------------------------------------------------


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


def remaining(audio_dir: Path) -> List[str]:
    return sorted(p.name for p in audio_dir.iterdir())


# ---------------------------------------------------------------------------
# 1. The mute — before the expensive work
# ---------------------------------------------------------------------------


def test_a_muted_group_is_not_spoken_into(tts: FakeTTS):
    ad = make_adapter({"group_replies_enabled": False})

    outcome = asyncio.run(V.speak(ad, f"g{GROUP}", "在的"))

    assert outcome.spoke is False
    assert ad._client.actions == []
    assert outcome.failure is not None
    assert A.is_muted_send_result(outcome.failure)


def test_the_mute_gates_before_any_synthesis_happens(tts: FakeTTS):
    """The panic switch must not also spend a TTS job.

    Same principle the proactive suite pins for the speech cap: an outbound
    gate that only stops the *send* still pays for the whole turn, and here
    that means a network round-trip and an ffmpeg run per refused message.
    """
    ad = make_adapter({"group_replies_enabled": False})

    asyncio.run(V.speak(ad, f"g{GROUP}", "在的"))

    assert tts.calls == []


def test_the_mute_does_not_touch_private_chats(tts: FakeTTS, audio_dir: Path):
    """The mute is about speaking in a room full of people, not about DMs."""
    ad = make_adapter({"group_replies_enabled": False})

    outcome = asyncio.run(V.speak(ad, DM_PEER, "在的"))

    assert outcome.spoke is True
    assert len(record_segments(ad)) == 1
    assert remaining(audio_dir) == []


# ---------------------------------------------------------------------------
# 2. The content policy — blocks, and fails closed
# ---------------------------------------------------------------------------


def test_content_policy_rejection_blocks_the_voice_send(tts: FakeTTS):
    ad = make_adapter()

    outcome = asyncio.run(V.speak(ad, f"g{GROUP}", BLOCKED_TEXT))

    assert outcome.spoke is False
    assert ad._client.actions == []
    assert tts.calls == []  # refused text is not synthesized either
    assert outcome.failure is not None
    raw = outcome.failure.raw_response
    assert isinstance(raw, dict) and raw.get(A.POLICY_MARKER) is True
    assert outcome.failure.retryable is False


def test_the_policy_also_audits_the_caption(tts: FakeTTS):
    """The caption is a second message the reader sees; it is not exempt."""
    ad = make_adapter()

    outcome = asyncio.run(
        V.speak(ad, f"g{GROUP}", "今天天气不错", caption=BLOCKED_TEXT)
    )

    assert outcome.spoke is False
    assert ad._client.actions == []


def test_the_policy_gate_fails_closed(monkeypatch: pytest.MonkeyPatch, tts: FakeTTS):
    """An exploding classifier blocks the send; it never becomes an open gate.

    Patched on the adapter rather than on ``voice``: the voice lane no longer
    carries its own copy of the gate, it calls ``adapter.guard_outbound_text``
    like every other outbound path. That is the behaviour under test here —
    if this module ever grows a private classifier again, this patch stops
    reaching the code that runs and the test fails, which is what we want.
    """

    def boom(*_a: Any, **_kw: Any):
        raise RuntimeError("classifier exploded")

    monkeypatch.setattr(A.content_policy, "moderate_text_all", boom)
    ad = make_adapter()

    outcome = asyncio.run(V.speak(ad, f"g{GROUP}", "完全无害的一句话"))

    assert outcome.spoke is False
    assert ad._client.actions == []
    raw = outcome.failure.raw_response if outcome.failure else None
    assert isinstance(raw, dict) and raw.get(A.POLICY_MARKER) is True


def test_the_policy_applies_to_private_chats_too(tts: FakeTTS):
    """The freeze risk is account-level; Tencent does not care which window."""
    ad = make_adapter()

    outcome = asyncio.run(V.speak(ad, DM_PEER, BLOCKED_TEXT))

    assert outcome.spoke is False
    assert ad._client.actions == []


# ---------------------------------------------------------------------------
# 3. Self-recording — what the proactive lane reads back
# ---------------------------------------------------------------------------


def test_a_group_speak_records_itself_with_the_spoken_text(
    tts: FakeTTS, audio_dir: Path
):
    ad = make_adapter()

    outcome = asyncio.run(V.speak(ad, f"g{GROUP}", "**今晚**吃什么"))

    assert outcome.spoke is True
    buffer = A.recent_group_messages(ad.instance_id, int(GROUP))
    assert len(buffer) == 1
    _ts, sender, text, is_self = buffer[0]
    assert is_self is True
    assert sender == "格兰"
    # The spoken script, not the raw markdown and not a "[语音]" placeholder:
    # the proactive persona reads this buffer back as the room's transcript.
    assert text == "今晚吃什么"
    assert remaining(audio_dir) == []


def test_the_recorded_post_makes_the_proactive_lane_see_itself(tts: FakeTTS):
    """The concrete symptom the missing record caused: talking over itself."""
    from plugins.platforms.onebot import proactive as PR

    ad = make_adapter()
    assert PR.last_message_is_self(ad.instance_id, GROUP) is False

    asyncio.run(V.speak(ad, f"g{GROUP}", "我先去忙了"))

    assert PR.last_message_is_self(ad.instance_id, GROUP) is True


def test_record_self_can_be_switched_off(tts: FakeTTS):
    ad = make_adapter()

    asyncio.run(V.speak(ad, f"g{GROUP}", "静音记录", record_self=False))

    assert A.recent_group_messages(ad.instance_id, int(GROUP)) == []


def test_a_private_speak_does_not_touch_the_group_buffer(tts: FakeTTS):
    ad = make_adapter()

    outcome = asyncio.run(V.speak(ad, DM_PEER, "私聊语音"))

    assert outcome.spoke is True
    assert A.recent_group_messages(ad.instance_id, int(GROUP)) == []
    assert A.recent_group_messages(ad.instance_id, int(DM_PEER)) == []


# ---------------------------------------------------------------------------
# 4. The wire form
# ---------------------------------------------------------------------------


def test_the_payload_is_a_record_segment_carrying_base64(tts: FakeTTS):
    """A literal host path would resolve to nothing inside NapCat's container."""
    ad = make_adapter()

    asyncio.run(V.speak(ad, f"g{GROUP}", "听得到吗"))

    records = record_segments(ad)
    assert len(records) == 1
    assert records[0].file.startswith("base64://")
    wire = P.action_to_wire(P.SendGroupMsg(group_id=int(GROUP), message=[records[0]]))
    assert wire["params"]["message"][0]["type"] == "record"


def test_the_group_action_targets_the_group(tts: FakeTTS):
    ad = make_adapter()

    asyncio.run(V.speak(ad, f"g{GROUP}", "听得到吗"))

    action = ad._client.actions[0]
    assert isinstance(action, P.SendGroupMsg)
    assert action.group_id == int(GROUP)


def test_a_private_speak_uses_the_private_action(tts: FakeTTS):
    ad = make_adapter()

    asyncio.run(V.speak(ad, DM_PEER, "听得到吗"))

    action = ad._client.actions[0]
    assert isinstance(action, P.SendPrivateMsg)
    assert action.user_id == int(DM_PEER)


# ---------------------------------------------------------------------------
# 5. The caption
# ---------------------------------------------------------------------------


def test_the_caption_is_a_separate_earlier_message(tts: FakeTTS):
    ad = make_adapter()

    outcome = asyncio.run(
        V.speak(ad, f"g{GROUP}", "语音内容", caption="给你录了段语音")
    )

    assert outcome.spoke is True
    assert len(ad._client.actions) == 2
    first, second = ad._client.actions
    # The caption goes first, alone — QQ renders a voice message as its own
    # bubble and drops anything sitting beside it.
    assert [type(s) for s in segments_of(first)] == [P.TextSegment]
    assert "给你录了段语音" in segments_of(first)[0].text
    assert [type(s) for s in segments_of(second)] == [P.RecordSegment]
    assert text_segments(ad) == [segments_of(first)[0].text]


def test_only_the_first_message_carries_the_reply_lead(tts: FakeTTS):
    """Leading both messages would quote and ping the recipient twice."""
    ad = make_adapter()

    asyncio.run(
        V.speak(
            ad,
            f"g{GROUP}",
            "语音内容",
            caption="说明",
            reply_to="8899",
            metadata={"onebot_at_user_id": "42"},
        )
    )

    first, second = ad._client.actions
    assert isinstance(segments_of(first)[0], P.ReplySegment)
    assert isinstance(segments_of(first)[1], P.AtSegment)
    assert not any(
        isinstance(seg, (P.ReplySegment, P.AtSegment)) for seg in segments_of(second)
    )


def test_without_a_caption_the_record_message_carries_the_lead(tts: FakeTTS):
    ad = make_adapter()

    asyncio.run(V.speak(ad, f"g{GROUP}", "语音内容", reply_to="8899"))

    (action,) = ad._client.actions
    assert isinstance(segments_of(action)[0], P.ReplySegment)


# ---------------------------------------------------------------------------
# 6. Cleanup — success and every failure path
# ---------------------------------------------------------------------------


def test_the_synthesized_file_is_unlinked_on_success(tts: FakeTTS, audio_dir: Path):
    ad = make_adapter()

    outcome = asyncio.run(V.speak(ad, f"g{GROUP}", "清理测试"))

    assert outcome.spoke is True
    assert remaining(audio_dir) == []


def test_the_synthesized_file_is_unlinked_when_the_send_is_rejected(
    tts: FakeTTS, audio_dir: Path
):
    ad = make_adapter(
        response={"status": "failed", "retcode": 1403, "message": "no permission"}
    )

    outcome = asyncio.run(V.speak(ad, f"g{GROUP}", "清理测试"))

    assert outcome.spoke is False
    assert outcome.failure is not None
    assert outcome.failure.error_kind == "forbidden"
    assert remaining(audio_dir) == []


def test_the_synthesized_file_is_unlinked_when_it_is_too_large(
    monkeypatch: pytest.MonkeyPatch, tts: FakeTTS, audio_dir: Path
):
    """Oversize is a reported failure, not a silent literal-path fallback."""
    monkeypatch.setattr(V, "MAX_AUDIO_BYTES", 4)
    ad = make_adapter()

    outcome = asyncio.run(V.speak(ad, f"g{GROUP}", "清理测试"))

    assert outcome.spoke is False
    assert "inlining ceiling" in (outcome.reason or "")
    assert ad._client.actions == []
    assert remaining(audio_dir) == []


def test_the_synthesized_files_are_unlinked_when_one_of_them_is_not_audio(
    monkeypatch: pytest.MonkeyPatch, audio_dir: Path
):
    """The whole batch is vetted before anything is sent."""

    def synth(**kwargs: Any) -> str:
        good = audio_dir / "part0.mp3"
        good.write_bytes(b"ID3audio")
        bad = audio_dir / "part1.txt"
        bad.write_bytes(b"not audio")
        return json.dumps({
            "success": True,
            "file_paths": [str(good), str(bad)],
            "provider": "edge",
        })

    monkeypatch.setattr(TTS, "text_to_speech_tool", synth)
    ad = make_adapter()

    outcome = asyncio.run(V.speak(ad, f"g{GROUP}", "清理测试"))

    assert outcome.spoke is False
    assert ad._client.actions == []  # nothing half-posted
    assert remaining(audio_dir) == []


# ---------------------------------------------------------------------------
# 7. Synthesis failures are results, never exceptions
# ---------------------------------------------------------------------------


def test_tts_failure_carries_the_providers_own_error_text(
    monkeypatch: pytest.MonkeyPatch, audio_dir: Path
):
    """Verbatim: a rewritten message is one the operator cannot grep for."""
    provider_error = "edge-tts: No audio was received from the service"
    install_tts(
        monkeypatch,
        FakeTTS(
            audio_dir,
            envelope=json.dumps({"success": False, "error": provider_error}),
        ),
    )
    ad = make_adapter()

    outcome = asyncio.run(V.speak(ad, f"g{GROUP}", "说点什么"))

    assert outcome.spoke is False
    assert outcome.reason == provider_error
    assert ad._client.actions == []
    assert remaining(audio_dir) == []


def test_an_unparseable_tts_envelope_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch, audio_dir: Path
):
    install_tts(monkeypatch, FakeTTS(audio_dir, envelope="Traceback (most recent"))
    ad = make_adapter()

    outcome = asyncio.run(V.speak(ad, f"g{GROUP}", "说点什么"))

    assert outcome.spoke is False
    assert "unparseable" in (outcome.reason or "")
    assert ad._client.actions == []


def test_a_raising_tts_tool_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch, audio_dir: Path
):
    def boom(**_kw: Any) -> str:
        raise RuntimeError("ffmpeg is not installed")

    monkeypatch.setattr(TTS, "text_to_speech_tool", boom)
    ad = make_adapter()

    outcome = asyncio.run(V.speak(ad, f"g{GROUP}", "说点什么"))

    assert outcome.spoke is False
    assert "ffmpeg is not installed" in (outcome.reason or "")


def test_success_without_a_file_is_a_failure(
    monkeypatch: pytest.MonkeyPatch, audio_dir: Path
):
    install_tts(
        monkeypatch,
        FakeTTS(
            audio_dir,
            envelope=json.dumps({
                "success": True,
                "file_paths": ["/nonexistent/a.mp3"],
            }),
        ),
    )
    ad = make_adapter()

    outcome = asyncio.run(V.speak(ad, f"g{GROUP}", "说点什么"))

    assert outcome.spoke is False
    assert "no readable file" in (outcome.reason or "")


# ---------------------------------------------------------------------------
# 8. Multi-file synthesis
# ---------------------------------------------------------------------------


def test_multi_file_tts_output_sends_both_and_cleans_up_both(
    monkeypatch: pytest.MonkeyPatch, audio_dir: Path
):
    """Long text is chunked then repacked, so two files is a normal result."""
    install_tts(monkeypatch, FakeTTS(audio_dir, files=2))
    ad = make_adapter()

    outcome = asyncio.run(V.speak(ad, f"g{GROUP}", "很长的一段话"))

    assert outcome.spoke is True
    records = record_segments(ad)
    assert len(records) == 2
    assert all(seg.file.startswith("base64://") for seg in records)
    assert outcome.audio_bytes == len(b"ID3\x03\x00fake-mp3-bytes") * 2
    assert remaining(audio_dir) == []


# ---------------------------------------------------------------------------
# 9. Target resolution and connection state
# ---------------------------------------------------------------------------


def test_an_unparseable_chat_id_is_a_structured_failure(tts: FakeTTS):
    ad = make_adapter()

    outcome = asyncio.run(V.speak(ad, "not-a-chat-id", "喂"))

    assert outcome.spoke is False
    assert outcome.failure is not None
    assert outcome.failure.error_kind == "not_found"
    assert tts.calls == []


def test_a_disconnected_adapter_fails_before_synthesizing(tts: FakeTTS):
    """Being disconnected is a retryable transient — and not worth a TTS run."""
    ad = make_adapter()
    ad._client = None

    outcome = asyncio.run(V.speak(ad, f"g{GROUP}", "喂"))

    assert outcome.spoke is False
    assert outcome.failure is not None
    assert outcome.failure.retryable is True
    assert tts.calls == []


def test_empty_text_says_nothing(tts: FakeTTS):
    ad = make_adapter()

    outcome = asyncio.run(V.speak(ad, f"g{GROUP}", "   \n  "))

    assert outcome.spoke is False
    assert tts.calls == []


def test_the_synthesizer_receives_the_spoken_script(tts: FakeTTS):
    """Markdown is written for eyes; a provider reads it out literally."""
    ad = make_adapter()

    asyncio.run(V.speak(ad, f"g{GROUP}", "## 标题\n\n- **重点**"))

    assert tts.calls
    assert "**" not in tts.texts[0]
    assert "##" not in tts.texts[0]


# ---------------------------------------------------------------------------
# 10. Pure units
# ---------------------------------------------------------------------------


class TestParseTtsEnvelope:
    def test_unparseable_json(self):
        result = V.parse_tts_envelope("<html>502</html>")
        assert result.ok is False
        assert "unparseable" in (result.error or "")

    def test_none(self):
        assert V.parse_tts_envelope(None).ok is False

    def test_non_object_json(self):
        result = V.parse_tts_envelope("[1, 2]")
        assert result.ok is False
        assert "expected an object" in (result.error or "")

    def test_provider_error_is_verbatim(self):
        result = V.parse_tts_envelope(
            json.dumps({"success": False, "error": "quota exceeded", "provider": "xai"})
        )
        assert result.error == "quota exceeded"
        assert result.provider == "xai"

    def test_file_paths_beats_file_path(self, tmp_path: Path):
        one = tmp_path / "a.mp3"
        two = tmp_path / "b.mp3"
        one.write_bytes(b"a")
        two.write_bytes(b"b")
        result = V.parse_tts_envelope(
            json.dumps({
                "success": True,
                "file_path": str(one),
                "file_paths": [str(one), str(two)],
            })
        )
        assert result.ok is True
        assert result.paths == (str(one), str(two))

    def test_singular_file_path_still_works(self, tmp_path: Path):
        one = tmp_path / "a.mp3"
        one.write_bytes(b"a")
        result = V.parse_tts_envelope(
            json.dumps({"success": True, "file_path": str(one)})
        )
        assert result.paths == (str(one),)


class TestVetAudioFile:
    def test_accepts_a_normal_audio_file(self, tmp_path: Path):
        path = tmp_path / "a.mp3"
        path.write_bytes(b"ID3audio")
        safe, size, error = V.vet_audio_file(str(path))
        assert error is None
        assert safe == str(path)
        assert size == 8

    def test_rejects_a_non_audio_extension(self, tmp_path: Path):
        path = tmp_path / "a.txt"
        path.write_bytes(b"hello")
        safe, _size, error = V.vet_audio_file(str(path))
        assert safe is None
        assert "not an audio file" in (error or "")

    def test_rejects_an_empty_file(self, tmp_path: Path):
        path = tmp_path / "a.mp3"
        path.write_bytes(b"")
        safe, _size, error = V.vet_audio_file(str(path))
        assert safe is None
        assert "empty" in (error or "")

    def test_rejects_an_oversize_file(self, tmp_path: Path):
        path = tmp_path / "a.mp3"
        path.write_bytes(b"0123456789")
        safe, size, error = V.vet_audio_file(str(path), max_bytes=4)
        assert safe is None
        assert size == 10
        assert "inlining ceiling" in (error or "")

    def test_rejects_a_missing_file(self, tmp_path: Path):
        safe, _size, error = V.vet_audio_file(str(tmp_path / "nope.mp3"))
        assert safe is None
        assert error

    def test_the_ceiling_is_resolved_at_call_time(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        path = tmp_path / "a.mp3"
        path.write_bytes(b"0123456789")
        monkeypatch.setattr(V, "MAX_AUDIO_BYTES", 4)
        safe, _size, error = V.vet_audio_file(str(path))
        assert safe is None
        assert "inlining ceiling" in (error or "")


# ---------------------------------------------------------------------------
# 11. QQ's own AI voice characters (群 AI 声聊)
# ---------------------------------------------------------------------------


def test_ai_character_posts_the_napcat_action():
    ad = make_adapter()

    outcome = asyncio.run(V.speak_ai_character(ad, f"g{GROUP}", "**下午好**", "lucy"))

    assert outcome.spoke is True
    (action,) = ad._client.actions
    assert isinstance(action, P.SendGroupAiRecord)
    assert action.group_id == int(GROUP)
    assert action.character == "lucy"
    assert action.text == "下午好"  # the spoken script, not raw markdown
    assert outcome.audio_bytes == 0  # synthesized on Tencent's side


def test_ai_character_records_itself():
    ad = make_adapter()

    asyncio.run(V.speak_ai_character(ad, f"g{GROUP}", "下午好", "lucy"))

    buffer = A.recent_group_messages(ad.instance_id, int(GROUP))
    assert [entry[2] for entry in buffer] == ["下午好"]
    assert buffer[0][3] is True


def test_ai_character_obeys_the_mute():
    ad = make_adapter({"group_replies_enabled": False})

    outcome = asyncio.run(V.speak_ai_character(ad, f"g{GROUP}", "下午好", "lucy"))

    assert outcome.spoke is False
    assert ad._client.actions == []
    assert A.is_muted_send_result(outcome.failure)


def test_ai_character_obeys_the_content_policy():
    ad = make_adapter()

    outcome = asyncio.run(V.speak_ai_character(ad, f"g{GROUP}", BLOCKED_TEXT, "lucy"))

    assert outcome.spoke is False
    assert ad._client.actions == []


def test_ai_character_refuses_private_chats():
    """QQ exposes no ``send_private_ai_record``; there is nothing to fall back to."""
    ad = make_adapter()

    outcome = asyncio.run(V.speak_ai_character(ad, DM_PEER, "下午好", "lucy"))

    assert outcome.spoke is False
    assert "private" in (outcome.reason or "")
    assert ad._client.actions == []


def test_ai_character_reports_a_backend_rejection():
    """A Lagrange/go-cqhttp backend rejects the action outright."""
    ad = make_adapter(
        response={"status": "failed", "retcode": 1404, "message": "unknown action"}
    )

    outcome = asyncio.run(V.speak_ai_character(ad, f"g{GROUP}", "下午好", "lucy"))

    assert outcome.spoke is False
    assert outcome.failure is not None
    assert outcome.failure.error_kind == "not_found"
    assert A.recent_group_messages(ad.instance_id, int(GROUP)) == []
