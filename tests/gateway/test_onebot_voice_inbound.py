"""Inbound QQ voice: a ``record`` segment has to reach the agent as audio.

A whole class of voice notes used to disappear before anything tried to fetch
them.  NapCat ships a *name-only* ``record`` — a ``file`` id and no ``url`` —
and ``protocol.segments_to_media`` skipped every url-less segment, so the
attachment was never created, never logged, and the user's message arrived
empty.  The recovery path is the OneBot v11 ``get_record`` action, which asks
the backend to resolve and transcode the file by its id.

What is pinned here, and why each one is worth a test:

* the **url-bearing** record still downloads exactly as it did (the fix must
  not pay for itself out of the path that already worked);
* a **url-less** record reaches ``get_record``, and all three response shapes
  the backends actually use — ``url`` / ``base64`` / a local ``file`` path —
  produce a real cached file;
* a ``file`` path that exists only on the **backend's** host is refused:
  NapCat normally runs in another container, and a path we cannot read would
  enter ``media_urls`` as a phantom attachment that fails far from its cause;
* a backend that cannot do ``get_record`` at all (go-cqhttp) **degrades** —
  the turn happens, one warning is logged, nothing raises;
* the event is ``MessageType.VOICE`` with an ``audio/*`` MIME, which is the
  contract the gateway's STT gate hangs off (``_event_media_is_stt_input``);
* ``media_urls`` and ``media_types`` stay the same length on every path —
  they are positionally parallel, and a skew misroutes every later
  attachment in the same message.

Fully offline: a fake OneBot client, a stubbed downloader, and audio "bytes"
that are just magic-byte headers.  Nothing here can reach a socket.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import MessageType
from gateway.run import _event_media_is_stt_input

from plugins.platforms.onebot import adapter as A
from plugins.platforms.onebot import protocol as P


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------

#: A minimal MP3: the sniffer in ``tools/audio_container.py`` recognises the
#: ID3 header, so whatever extension we guessed gets corrected to ``.mp3``.
MP3_BYTES = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 64

USER = 2104743984


class FakeClient:
    """Records every outbound action; answers ``get_record`` from a script."""

    def __init__(
        self,
        record_response: Optional[Dict[str, Any]] = None,
        *,
        raises: Optional[BaseException] = None,
    ) -> None:
        self.actions: List[Any] = []
        self.connected = True
        self.last_self_id = 100
        self.last_event_at_ms = 0
        self.last_status_online = True
        self.inbound_dropped_count = 0
        self.outbound_queue_depth = 0
        self.record_response = record_response
        self.raises = raises

    async def call_action(self, action, *, timeout=None):  # noqa: ANN001
        self.actions.append(action)
        if isinstance(action, P.GetRecord):
            if self.raises is not None:
                raise self.raises
            if self.record_response is None:
                # What a go-cqhttp build without the action answers.
                return {"status": "failed", "retcode": 1404, "message": "unsupported"}
            return dict(self.record_response)
        return {"status": "ok", "retcode": 0, "data": {"message_id": 1001}}

    async def send_action(self, action):  # noqa: ANN001
        self.actions.append(action)

    async def close(self):
        self.connected = False


def make_adapter(client: Optional[FakeClient] = None) -> A.OneBotAdapter:
    """A connected-looking adapter that never opens a socket."""
    ad = A.OneBotAdapter(
        PlatformConfig(enabled=True, extra={"ws_url": "ws://127.0.0.1:3001"})
    )
    ad._client = client or FakeClient()
    ad._running = True
    ad._semaphore = asyncio.Semaphore(4)
    return ad


def voice_event(*segments: P.MessageSegment) -> P.MessageEvent:
    """A private message carrying the given segments."""
    return P.MessageEvent(
        self_id=100,
        message_type=P.MessageType.PRIVATE,
        sub_type="friend",
        group_id=None,
        user_id=USER,
        message_id=7,
        message=list(segments),
        raw_message="",
        time=1_700_000_000,
        sender=P.Sender(user_id=USER, nickname="bob"),
    )


def stub_download(monkeypatch, returns: str) -> List[Tuple[str, str]]:
    """Replace the HTTP audio downloader; return the ``(url, ext)`` log."""
    calls: List[Tuple[str, str]] = []

    async def _fake(url: str, ext: str = ".ogg", retries: int = 2) -> str:
        calls.append((url, ext))
        return returns

    monkeypatch.setattr(A, "cache_audio_from_url", _fake)
    return calls


def get_record_actions(ad: A.OneBotAdapter) -> List[P.GetRecord]:
    return [a for a in ad._client.actions if isinstance(a, P.GetRecord)]


def assert_parallel(urls: List[str], types: List[str]) -> None:
    """``media_urls`` / ``media_types`` are indexed together, always."""
    assert len(urls) == len(types), (urls, types)


@pytest.fixture(autouse=True)
def _reset_state():
    A._reset_module_state()
    yield
    A._reset_module_state()


# ---------------------------------------------------------------------------
# 1. The path that already worked must keep working
# ---------------------------------------------------------------------------


class TestUrlBearingRecord:
    @pytest.mark.asyncio
    async def test_a_url_record_downloads_without_touching_get_record(
        self, monkeypatch, tmp_path
    ):
        cached = str(tmp_path / "voice.amr")
        calls = stub_download(monkeypatch, cached)
        ad = make_adapter()

        urls, types = await ad._download_media(
            voice_event(P.RecordSegment(url="https://cdn/voice.amr", file="voice.amr"))
        )

        assert calls == [("https://cdn/voice.amr", ".amr")]
        assert urls == [cached]
        assert types == ["audio/amr"]
        assert get_record_actions(ad) == []
        assert_parallel(urls, types)

    @pytest.mark.asyncio
    async def test_a_guessed_extension_is_not_asserted_as_a_container(
        self, monkeypatch, tmp_path
    ):
        # No file name at all: ".amr" is our placeholder, not knowledge, and
        # the sniffer did not override it. Claiming "audio/amr" here is a coin
        # flip that tools/audio_container.py would contradict.
        cached = str(tmp_path / "audio_abc.amr")
        stub_download(monkeypatch, cached)
        ad = make_adapter()

        urls, types = await ad._download_media(
            voice_event(P.RecordSegment(url="https://cdn/x", file=None))
        )

        assert types == ["audio/*"]
        assert urls == [cached]
        assert_parallel(urls, types)

    @pytest.mark.asyncio
    async def test_a_sniffed_container_wins_over_our_guess(self, monkeypatch, tmp_path):
        # The cache renames the file to whatever the magic bytes say; reading
        # the MIME back off the cached path is how that verdict reaches the
        # gateway.
        cached = str(tmp_path / "audio_abc.ogg")
        stub_download(monkeypatch, cached)
        ad = make_adapter()

        _urls, types = await ad._download_media(
            voice_event(P.RecordSegment(url="https://cdn/x", file=None))
        )

        assert types == ["audio/ogg"]


# ---------------------------------------------------------------------------
# 2. The url-less record: resolve by id
# ---------------------------------------------------------------------------


class TestGetRecordResolution:
    @pytest.mark.asyncio
    async def test_a_urlless_record_asks_the_backend_for_mp3(
        self, monkeypatch, tmp_path
    ):
        cached = str(tmp_path / "audio_abc.mp3")
        calls = stub_download(monkeypatch, cached)
        ad = make_adapter(
            FakeClient({"status": "ok", "retcode": 0, "data": {"url": "https://cdn/r"}})
        )

        urls, types = await ad._download_media(
            voice_event(P.RecordSegment(url="", file="ABCD1234.amr"))
        )

        assert [(a.file, a.out_format) for a in get_record_actions(ad)] == [
            ("ABCD1234.amr", "mp3")
        ]
        assert calls == [("https://cdn/r", ".mp3")]
        assert urls == [cached]
        assert types == ["audio/mp3"]
        assert_parallel(urls, types)

    @pytest.mark.asyncio
    async def test_a_base64_response_lands_in_the_cache(self):
        ad = make_adapter(
            FakeClient({
                "status": "ok",
                "retcode": 0,
                "data": {"base64": base64.b64encode(MP3_BYTES).decode()},
            })
        )

        urls, types = await ad._download_media(
            voice_event(P.RecordSegment(url="", file="ABCD1234.silk"))
        )

        assert len(urls) == 1
        assert Path(urls[0]).read_bytes() == MP3_BYTES
        assert types == ["audio/mp3"]
        assert_parallel(urls, types)

    @pytest.mark.asyncio
    async def test_a_data_uri_base64_response_is_decoded(self):
        payload = base64.b64encode(MP3_BYTES).decode()
        ad = make_adapter(
            FakeClient({
                "status": "ok",
                "retcode": 0,
                "data": {"base64": f"data:audio/mpeg;base64,{payload}"},
            })
        )

        urls, _types = await ad._download_media(
            voice_event(P.RecordSegment(url="", file="ABCD1234.silk"))
        )

        assert Path(urls[0]).read_bytes() == MP3_BYTES

    @pytest.mark.asyncio
    async def test_a_local_file_response_is_copied_into_the_cache(self, tmp_path):
        # The backend shares our filesystem here, so the path resolves. We
        # still copy: the backend is free to clean up its own temp file.
        shared = tmp_path / "napcat-out.mp3"
        shared.write_bytes(MP3_BYTES)
        ad = make_adapter(
            FakeClient({"status": "ok", "retcode": 0, "data": {"file": str(shared)}})
        )

        urls, types = await ad._download_media(
            voice_event(P.RecordSegment(url="", file="ABCD1234.silk"))
        )

        assert len(urls) == 1
        assert urls[0] != str(shared)
        assert Path(urls[0]).read_bytes() == MP3_BYTES
        assert types == ["audio/mp3"]
        assert_parallel(urls, types)

    @pytest.mark.asyncio
    async def test_a_file_uri_response_is_accepted(self, tmp_path):
        shared = tmp_path / "napcat-out.mp3"
        shared.write_bytes(MP3_BYTES)
        ad = make_adapter(
            FakeClient({
                "status": "ok",
                "retcode": 0,
                "data": {"file": f"file://{shared}"},
            })
        )

        urls, _types = await ad._download_media(
            voice_event(P.RecordSegment(url="", file="ABCD1234.silk"))
        )

        assert Path(urls[0]).read_bytes() == MP3_BYTES

    @pytest.mark.asyncio
    async def test_url_is_preferred_over_base64_and_file(self, monkeypatch, tmp_path):
        cached = str(tmp_path / "audio_abc.mp3")
        calls = stub_download(monkeypatch, cached)
        shared = tmp_path / "napcat-out.mp3"
        shared.write_bytes(MP3_BYTES)
        ad = make_adapter(
            FakeClient({
                "status": "ok",
                "retcode": 0,
                "data": {
                    "url": "https://cdn/r",
                    "base64": base64.b64encode(MP3_BYTES).decode(),
                    "file": str(shared),
                },
            })
        )

        urls, _types = await ad._download_media(
            voice_event(P.RecordSegment(url="", file="ABCD1234.silk"))
        )

        assert calls == [("https://cdn/r", ".mp3")]
        assert urls == [cached]


# ---------------------------------------------------------------------------
# 3. Degradation — a gap the user can see, never a crash or a silent drop
# ---------------------------------------------------------------------------


class TestGetRecordDegrades:
    @pytest.mark.asyncio
    async def test_a_path_only_the_backend_can_see_is_refused(self, tmp_path, caplog):
        missing = tmp_path / "not-here" / "voice.mp3"
        ad = make_adapter(
            FakeClient({"status": "ok", "retcode": 0, "data": {"file": str(missing)}})
        )

        with caplog.at_level(logging.WARNING):
            urls, types = await ad._download_media(
                voice_event(P.RecordSegment(url="", file="ABCD1234.silk"))
            )

        assert urls == []
        assert types == []
        assert_parallel(urls, types)
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert len(warnings) == 1, warnings
        assert str(missing) in warnings[0]

    @pytest.mark.asyncio
    async def test_a_backend_that_rejects_the_action_degrades(self, caplog):
        ad = make_adapter(FakeClient(None))  # "unsupported" envelope

        with caplog.at_level(logging.WARNING):
            urls, types = await ad._download_media(
                voice_event(P.RecordSegment(url="", file="ABCD1234.silk"))
            )

        assert urls == [] and types == []
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert len(warnings) == 1, warnings
        assert "ABCD1234.silk" in warnings[0]

    @pytest.mark.asyncio
    async def test_a_raising_backend_does_not_escape(self, caplog):
        ad = make_adapter(FakeClient(raises=asyncio.TimeoutError()))

        with caplog.at_level(logging.WARNING):
            urls, types = await ad._download_media(
                voice_event(P.RecordSegment(url="", file="ABCD1234.silk"))
            )

        assert urls == [] and types == []
        assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1

    @pytest.mark.asyncio
    async def test_an_empty_data_payload_degrades(self, caplog):
        ad = make_adapter(FakeClient({"status": "ok", "retcode": 0, "data": {}}))

        with caplog.at_level(logging.WARNING):
            urls, types = await ad._download_media(
                voice_event(P.RecordSegment(url="", file="ABCD1234.silk"))
            )

        assert urls == [] and types == []
        assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1

    @pytest.mark.asyncio
    async def test_a_dropped_voice_note_does_not_shift_the_other_attachments(
        self, monkeypatch, tmp_path
    ):
        # The lists are read by index. If a failed voice note appended to one
        # list and not the other, every attachment after it would be
        # classified as the wrong kind.
        async def _fake_image(url: str, ext: str = ".jpg", retries: int = 2) -> str:
            return str(tmp_path / f"{Path(url).name}{ext}")

        monkeypatch.setattr(A, "cache_image_from_url", _fake_image)
        ad = make_adapter(FakeClient(None))

        urls, types = await ad._download_media(
            voice_event(
                P.ImageSegment(url="https://cdn/a", file="a.jpg"),
                P.RecordSegment(url="", file="ABCD1234.silk"),
                P.ImageSegment(url="https://cdn/b", file="b.png"),
            )
        )

        assert types == ["image/jpeg", "image/png"]
        assert [Path(u).name for u in urls] == ["a.jpg", "b.png"]
        assert_parallel(urls, types)


# ---------------------------------------------------------------------------
# 4. The STT gate contract
# ---------------------------------------------------------------------------


class TestSttGateContract:
    @pytest.mark.asyncio
    async def test_a_resolved_voice_note_is_VOICE_and_enters_stt(self):
        ad = make_adapter(
            FakeClient({
                "status": "ok",
                "retcode": 0,
                "data": {"base64": base64.b64encode(MP3_BYTES).decode()},
            })
        )

        event = await ad._build_message_event(
            voice_event(P.RecordSegment(url="", file="ABCD1234.silk")), ""
        )

        assert event.message_type == MessageType.VOICE
        assert event.media_types[0].startswith("audio/")
        assert _event_media_is_stt_input(event, 0)
        assert_parallel(event.media_urls, event.media_types)

    @pytest.mark.asyncio
    async def test_an_unknown_container_still_enters_stt(self, monkeypatch, tmp_path):
        # ``audio/*`` is the honest answer when nothing identified the bytes,
        # and it has to keep passing the gate — the prefix is what routes.
        stub_download(monkeypatch, str(tmp_path / "audio_abc.amr"))
        ad = make_adapter()

        event = await ad._build_message_event(
            voice_event(P.RecordSegment(url="https://cdn/x", file=None)), ""
        )

        assert event.media_types == ["audio/*"]
        assert event.message_type == MessageType.VOICE
        assert _event_media_is_stt_input(event, 0)

    @pytest.mark.asyncio
    async def test_an_unresolvable_voice_note_still_produces_a_turn(self):
        ad = make_adapter(FakeClient(None))

        event = await ad._build_message_event(
            voice_event(
                P.RecordSegment(url="", file="ABCD1234.silk"),
                P.TextSegment(text="听得到吗"),
            ),
            "听得到吗",
        )

        assert event.text == "听得到吗"
        assert event.media_urls == [] and event.media_types == []
        assert event.message_type == MessageType.TEXT
