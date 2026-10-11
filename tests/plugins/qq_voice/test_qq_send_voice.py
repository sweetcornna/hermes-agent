"""``qq_send_voice``: argument shaping, audio vetting, and the path choice.

The load-bearing assertion in this file is the last group: **when a gateway
adapter is published, ``voice.speak()`` is what runs and the raw OneBot
transport is never touched.** That is the difference between a send the
emergency mute, the Tencent content policy and the recent-chatter buffer all
saw, and one that slipped past all three. Every other test here protects an
input from reaching either path in a state the backend would reject with a
code the model cannot read.

Fully offline: the TTS tool is patched, the transport is patched, the
live-adapter accessor is patched, and no path can reach a real QQ account.
"""

from __future__ import annotations

import asyncio
import base64
import json
import threading

import pytest

import plugins.qq_voice.common as common
import plugins.qq_voice.send_voice as SV
import tools.onebot_client as OBC
import tools.tts_tool as TTS
from gateway.platforms.base import SendResult
from plugins.platforms.onebot import voice as V

GROUP = 183287894
PEER = 536132102


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Transport:
    """Stands in for ``onebot_call``. Records; never raises unless told to."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.error = error

    def __call__(self, action, params=None, **kw):
        self.calls.append((action, dict(params or {})))
        if self.error is not None:
            raise self.error
        return {"message_id": 4242}

    @property
    def actions(self) -> list[str]:
        return [a for a, _ in self.calls]


class _Adapter:
    """The two coroutine methods the tool reaches for, and nothing else."""

    def __init__(self, result: SendResult | None = None) -> None:
        self.voice_sends: list[tuple[str, str]] = []
        self.result = result or SendResult(success=True, message_id="99")

    async def send_voice(self, chat_id, audio_path, *a, **kw):
        self.voice_sends.append((chat_id, audio_path))
        return self.result


@pytest.fixture
def transport(monkeypatch):
    """Patch the raw transport and hand the spy back."""
    spy = _Transport()
    monkeypatch.setattr(OBC, "onebot_call", spy)
    return spy


@pytest.fixture
def no_gateway(monkeypatch):
    """The CLI / cron case: nothing has published an adapter."""
    monkeypatch.setattr(SV, "live_adapter", lambda: (None, None))


@pytest.fixture
def loop_thread():
    """A real event loop on another thread — the shape a live gateway has.

    Using a real loop rather than a stub keeps ``run_on_adapter_loop``'s
    ``run_coroutine_threadsafe`` hand-off under test instead of mocked away.
    """
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    loop.close()


@pytest.fixture
def gateway(monkeypatch, loop_thread):
    """Publish a live adapter for the handler to find."""
    adapter = _Adapter()
    monkeypatch.setattr(SV, "live_adapter", lambda: (adapter, loop_thread))
    return adapter


def _tts(monkeypatch, tmp_path, *, files=1, ok=True, error="boom"):
    """Patch ``text_to_speech_tool`` to write *files* real (tiny) mp3s."""
    paths = []
    for index in range(files):
        path = tmp_path / f"speech{index}.mp3"
        path.write_bytes(b"ID3" + bytes([index]) * 32)
        paths.append(str(path))

    calls: list[str] = []

    def fake(text=None, **kw):
        calls.append(text)
        if not ok:
            return json.dumps({"success": False, "error": error})
        return json.dumps({"success": True, "file_paths": paths, "provider": "edge"})

    monkeypatch.setattr(TTS, "text_to_speech_tool", fake)
    return paths, calls


def _err(raw: str) -> dict:
    payload = json.loads(raw)
    assert "error" in payload, f"expected a tool error, got {payload}"
    return payload


def _ok(raw: str) -> dict:
    payload = json.loads(raw)
    assert payload.get("success") is True, f"expected success, got {payload}"
    return payload


# ---------------------------------------------------------------------------
# Pure argument shaping
# ---------------------------------------------------------------------------


class TestCoerceQqId:
    def test_accepts_an_int(self):
        assert common._coerce_qq_id(12345, "user_id") == 12345

    def test_accepts_a_numeric_string(self):
        assert common._coerce_qq_id("12345", "user_id") == 12345

    def test_accepts_a_padded_numeric_string(self):
        assert common._coerce_qq_id("  12345 ", "group_id") == 12345

    @pytest.mark.parametrize("junk", ["", "abc", "12a", None, "1.5", []])
    def test_rejects_junk(self, junk):
        with pytest.raises(ValueError, match="numeric QQ id"):
            common._coerce_qq_id(junk, "user_id")

    @pytest.mark.parametrize("value", [0, -1, "-42"])
    def test_rejects_non_positive(self, value):
        with pytest.raises(ValueError, match="positive QQ id"):
            common._coerce_qq_id(value, "group_id")

    def test_names_the_offending_field(self):
        with pytest.raises(ValueError, match="'group_id'"):
            common._coerce_qq_id("nope", "group_id")


class TestExactlyOneTarget:
    def test_neither_target_is_refused(self, transport, no_gateway):
        payload = _err(SV.handle_qq_send_voice({"text": "hi"}))
        assert "exactly one target" in payload["error"]
        assert transport.calls == []

    def test_both_targets_are_refused(self, transport, no_gateway):
        payload = _err(
            SV.handle_qq_send_voice({"text": "hi", "user_id": PEER, "group_id": GROUP})
        )
        assert "exactly one target" in payload["error"]
        assert transport.calls == []

    def test_a_junk_group_id_is_refused(self, transport, no_gateway):
        payload = _err(SV.handle_qq_send_voice({"text": "hi", "group_id": "abc"}))
        assert "numeric QQ id" in payload["error"]
        assert transport.calls == []

    def test_a_zero_user_id_is_refused(self, transport, no_gateway):
        payload = _err(SV.handle_qq_send_voice({"text": "hi", "user_id": 0}))
        assert "positive QQ id" in payload["error"]
        assert transport.calls == []

    def test_a_negative_user_id_is_refused(self, transport, no_gateway):
        payload = _err(SV.handle_qq_send_voice({"text": "hi", "user_id": -7}))
        assert "positive QQ id" in payload["error"]
        assert transport.calls == []


class TestTextXorAudioFile:
    def test_neither_is_refused(self, transport, no_gateway):
        payload = _err(SV.handle_qq_send_voice({"group_id": GROUP}))
        assert "'text'" in payload["error"] and "'audio_file'" in payload["error"]
        assert transport.calls == []

    def test_both_are_refused(self, transport, no_gateway, tmp_path):
        audio = tmp_path / "a.mp3"
        audio.write_bytes(b"x" * 16)
        payload = _err(
            SV.handle_qq_send_voice({
                "group_id": GROUP,
                "text": "hi",
                "audio_file": str(audio),
            })
        )
        assert "not both" in payload["error"]
        assert transport.calls == []

    def test_whitespace_only_text_counts_as_absent(self, transport, no_gateway):
        payload = _err(SV.handle_qq_send_voice({"group_id": GROUP, "text": "   "}))
        assert "'audio_file'" in payload["error"]
        assert transport.calls == []


# ---------------------------------------------------------------------------
# Audio vetting
# ---------------------------------------------------------------------------


class TestAudioVetting:
    def test_missing_file(self, transport, no_gateway, tmp_path):
        payload = _err(
            SV.handle_qq_send_voice({
                "group_id": GROUP,
                "audio_file": str(tmp_path / "nope.mp3"),
            })
        )
        assert "file not found" in payload["error"]
        assert transport.calls == []

    def test_empty_file(self, transport, no_gateway, tmp_path):
        audio = tmp_path / "empty.mp3"
        audio.write_bytes(b"")
        payload = _err(
            SV.handle_qq_send_voice({"group_id": GROUP, "audio_file": str(audio)})
        )
        assert "empty" in payload["error"]
        assert transport.calls == []

    def test_oversize_file(self, transport, no_gateway, tmp_path, monkeypatch):
        # The ceiling is read at call time, so shrinking it here exercises the
        # real branch without writing 8 MiB to disk.
        monkeypatch.setattr(common, "MAX_AUDIO_BYTES", 16)
        audio = tmp_path / "big.mp3"
        audio.write_bytes(b"x" * 64)
        payload = _err(
            SV.handle_qq_send_voice({"group_id": GROUP, "audio_file": str(audio)})
        )
        assert "inlining ceiling" in payload["error"]
        assert transport.calls == []

    def test_wrong_extension(self, transport, no_gateway, tmp_path):
        doc = tmp_path / "notes.txt"
        doc.write_text("not audio")
        payload = _err(
            SV.handle_qq_send_voice({"group_id": GROUP, "audio_file": str(doc)})
        )
        assert "unsupported audio type" in payload["error"]
        assert transport.calls == []

    def test_a_directory_is_not_a_file(self, transport, no_gateway, tmp_path):
        payload = _err(
            SV.handle_qq_send_voice({"group_id": GROUP, "audio_file": str(tmp_path)})
        )
        assert "file not found" in payload["error"]
        assert transport.calls == []

    def test_every_advertised_extension_is_accepted(self, tmp_path):
        for ext in common.AUDIO_EXTS:
            path = tmp_path / f"clip{ext}"
            path.write_bytes(b"x" * 8)
            resolved, size = common.vet_audio_path(str(path))
            assert resolved == str(path) and size == 8


# ---------------------------------------------------------------------------
# The direct (no-gateway) path
# ---------------------------------------------------------------------------


class TestDirectPath:
    def test_text_synthesizes_and_posts_a_record_segment(
        self, transport, no_gateway, tmp_path, monkeypatch
    ):
        paths, calls = _tts(monkeypatch, tmp_path)
        payload = _ok(SV.handle_qq_send_voice({"group_id": GROUP, "text": "早上好"}))

        assert calls, "the TTS backend should have been asked to speak"
        assert transport.actions == ["send_msg"]
        _, params = transport.calls[0]
        assert params["message_type"] == "group"
        assert params["group_id"] == GROUP
        segment = params["message"][0]
        assert segment["type"] == "record"
        # base64:// is the only form NapCat can resolve from its own
        # container; a host path would be silence the sender believes it sent.
        assert segment["data"]["file"].startswith("base64://")
        assert payload["synthesized"] is True
        assert payload["delivery_path"] == "direct"
        assert "no gateway" in payload["delivery_note"]

    def test_a_private_target_uses_the_private_message_type(
        self, transport, no_gateway, tmp_path, monkeypatch
    ):
        _tts(monkeypatch, tmp_path)
        _ok(SV.handle_qq_send_voice({"user_id": str(PEER), "text": "hi"}))
        _, params = transport.calls[0]
        assert params["message_type"] == "private"
        assert params["user_id"] == PEER

    def test_an_existing_file_is_sent_without_synthesis(
        self, transport, no_gateway, tmp_path, monkeypatch
    ):
        def explode(*a, **kw):  # pragma: no cover — must never run
            raise AssertionError("audio_file must not go near the TTS backend")

        monkeypatch.setattr(TTS, "text_to_speech_tool", explode)
        audio = tmp_path / "clip.mp3"
        audio.write_bytes(b"ID3" + b"z" * 20)

        payload = _ok(
            SV.handle_qq_send_voice({"group_id": GROUP, "audio_file": str(audio)})
        )
        _, params = transport.calls[0]
        expected = base64.b64encode(audio.read_bytes()).decode("ascii")
        assert params["message"][0]["data"]["file"] == f"base64://{expected}"
        assert payload["synthesized"] is False
        assert payload["audio_bytes"] == audio.stat().st_size

    def test_a_chunked_synthesis_posts_every_chunk(
        self, transport, no_gateway, tmp_path, monkeypatch
    ):
        """Long text comes back as several files; dropping any truncates it."""
        _tts(monkeypatch, tmp_path, files=3)
        payload = _ok(SV.handle_qq_send_voice({"group_id": GROUP, "text": "long"}))
        assert transport.actions == ["send_msg"] * 3
        assert len(payload["message_ids"]) == 3

    def test_synthesized_files_are_cleaned_up(
        self, transport, no_gateway, tmp_path, monkeypatch
    ):
        paths, _ = _tts(monkeypatch, tmp_path, files=2)
        _ok(SV.handle_qq_send_voice({"group_id": GROUP, "text": "hi"}))
        import os

        assert not any(os.path.exists(p) for p in paths)

    def test_a_tts_failure_is_surfaced_verbatim(
        self, transport, no_gateway, tmp_path, monkeypatch
    ):
        _tts(monkeypatch, tmp_path, ok=False, error="no voice configured")
        payload = _err(SV.handle_qq_send_voice({"group_id": GROUP, "text": "hi"}))
        assert "no voice configured" in payload["error"]
        assert transport.calls == []

    def test_a_backend_failure_is_surfaced_and_never_retried(
        self, no_gateway, tmp_path, monkeypatch
    ):
        _tts(monkeypatch, tmp_path)
        spy = _Transport(error=RuntimeError("OneBot HTTP 502 for action 'send_msg'."))
        monkeypatch.setattr(OBC, "onebot_call", spy)

        payload = _err(SV.handle_qq_send_voice({"group_id": GROUP, "text": "hi"}))
        assert "502" in payload["error"]
        assert payload["delivery_path"] == "direct"
        # One attempt, exactly. A retried voice message is a double-post, and
        # a voice bubble is the one form a reader cannot skim past.
        assert len(spy.calls) == 1


# ---------------------------------------------------------------------------
# The gateway path — the guarantee this whole design exists for
# ---------------------------------------------------------------------------


class TestGatewayPathIsPreferred:
    def test_speak_runs_and_the_raw_transport_does_not(
        self, transport, gateway, loop_thread, monkeypatch
    ):
        """The load-bearing test.

        A published adapter means the send MUST go through ``voice.speak``,
        which applies the emergency mute, the Tencent content policy and the
        self-recording into the recent-chatter buffer. A raw ``send_msg``
        here would skip all three.
        """
        seen: list[tuple] = []

        async def fake_speak(adapter, chat_id, text, **kw):
            seen.append((adapter, chat_id, text))
            return V.SpeakOutcome(spoke=True, message_ids=("777",), audio_bytes=1234)

        monkeypatch.setattr(V, "speak", fake_speak)

        payload = _ok(SV.handle_qq_send_voice({"group_id": GROUP, "text": "早上好"}))

        assert len(seen) == 1
        adapter, chat_id, text = seen[0]
        assert adapter is gateway
        assert chat_id == f"g{GROUP}"  # the adapter's own chat-id spelling
        assert text == "早上好"
        assert transport.calls == [], "the guarded path must not touch send_msg"
        assert payload["delivery_path"] == "gateway"
        assert payload["message_id"] == "777"
        assert payload["audio_bytes"] == 1234
        assert "content policy" in payload["delivery_note"]

    def test_a_dm_uses_the_bare_uid_as_the_chat_id(
        self, transport, gateway, monkeypatch
    ):
        seen: list[str] = []

        async def fake_speak(adapter, chat_id, text, **kw):
            seen.append(chat_id)
            return V.SpeakOutcome(spoke=True, message_ids=("1",))

        monkeypatch.setattr(V, "speak", fake_speak)
        _ok(SV.handle_qq_send_voice({"user_id": PEER, "text": "hi"}))
        assert seen == [str(PEER)]
        assert transport.calls == []

    def test_a_muted_group_becomes_a_readable_error_not_a_false_success(
        self, transport, gateway, monkeypatch
    ):
        failure = SendResult(
            success=False,
            error="group speech is muted",
            error_kind="forbidden",
        )

        async def fake_speak(adapter, chat_id, text, **kw):
            return V.SpeakOutcome(
                spoke=False, reason="group speech is muted", failure=failure
            )

        monkeypatch.setattr(V, "speak", fake_speak)

        payload = _err(SV.handle_qq_send_voice({"group_id": GROUP, "text": "hi"}))
        assert "muted" in payload["error"]
        assert payload["code"] == "forbidden"
        assert payload["delivery_path"] == "gateway"
        # And it must not have quietly re-tried over the raw transport.
        assert transport.calls == []

    def test_a_policy_refusal_is_also_an_error(self, transport, gateway, monkeypatch):
        async def fake_speak(adapter, chat_id, text, **kw):
            return V.SpeakOutcome(spoke=False, reason="refused by the content policy")

        monkeypatch.setattr(V, "speak", fake_speak)
        payload = _err(SV.handle_qq_send_voice({"group_id": GROUP, "text": "hi"}))
        assert "content policy" in payload["error"]
        assert payload["code"] == "not_spoken"
        assert transport.calls == []

    def test_speak_raising_is_one_clear_error(self, transport, gateway, monkeypatch):
        async def fake_speak(adapter, chat_id, text, **kw):
            raise RuntimeError("adapter exploded")

        monkeypatch.setattr(V, "speak", fake_speak)
        payload = _err(SV.handle_qq_send_voice({"group_id": GROUP, "text": "hi"}))
        assert "adapter exploded" in payload["error"]
        assert payload["code"] == "gateway_failed"
        assert transport.calls == []

    def test_a_partial_send_reports_a_warning_alongside_success(
        self, transport, gateway, monkeypatch
    ):
        async def fake_speak(adapter, chat_id, text, **kw):
            return V.SpeakOutcome(
                spoke=True, message_ids=("1",), reason="second chunk failed"
            )

        monkeypatch.setattr(V, "speak", fake_speak)
        payload = _ok(SV.handle_qq_send_voice({"group_id": GROUP, "text": "hi"}))
        assert payload["warning"] == "second chunk failed"

    def test_an_audio_file_goes_through_the_adapter_not_the_raw_transport(
        self, transport, gateway, tmp_path
    ):
        """``speak`` synthesizes, so a file send takes the adapter's own path.

        That path still honours the emergency mute and still reuses the
        gateway's healthy socket — strictly better than ``send_msg``.
        """
        audio = tmp_path / "clip.ogg"
        audio.write_bytes(b"OggS" + b"q" * 40)

        payload = _ok(
            SV.handle_qq_send_voice({"group_id": GROUP, "audio_file": str(audio)})
        )
        assert gateway.voice_sends == [(f"g{GROUP}", str(audio))]
        assert transport.calls == []
        assert payload["delivery_path"] == "gateway"
        assert payload["synthesized"] is False
        assert payload["audio_bytes"] == audio.stat().st_size

    def test_an_adapter_refusing_a_file_send_is_an_error(
        self, transport, monkeypatch, loop_thread, tmp_path
    ):
        adapter = _Adapter(
            SendResult(success=False, error="muted", error_kind="forbidden")
        )
        monkeypatch.setattr(SV, "live_adapter", lambda: (adapter, loop_thread))
        audio = tmp_path / "clip.ogg"
        audio.write_bytes(b"OggS" + b"q" * 40)

        payload = _err(
            SV.handle_qq_send_voice({"group_id": GROUP, "audio_file": str(audio)})
        )
        assert "muted" in payload["error"]
        assert transport.calls == []


class TestFallbackPathIsUsedWhenNothingIsPublished:
    def test_no_adapter_means_the_direct_transport_and_the_result_says_so(
        self, transport, monkeypatch, tmp_path
    ):
        """``get_live_adapter() == (None, None)`` is the CLI case, not an error."""
        from plugins.platforms.onebot import client as C

        monkeypatch.setattr(C, "get_live_adapter", lambda: (None, None))

        def explode(*a, **kw):  # pragma: no cover — must never run
            raise AssertionError("speak() must not run without a live adapter")

        monkeypatch.setattr(V, "speak", explode)
        _tts(monkeypatch, tmp_path)

        payload = _ok(SV.handle_qq_send_voice({"group_id": GROUP, "text": "hi"}))
        assert payload["delivery_path"] == "direct"
        assert "best-effort" in payload["delivery_note"]
        assert transport.actions == ["send_msg"]
