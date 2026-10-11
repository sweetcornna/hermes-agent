"""Every OneBot lane that puts caller-supplied text on the wire is moderated.

The Tencent content gate exists because the risk it contains is *account*
level: one bad sentence can freeze the QQ account, so ``send()`` audits every
outbound body and fails closed.  The gate was then ported onto the inline
branch of ``_send_attachment`` for its caption — but ``send_image``, the
separate method for a **remote image URL**, builds its own segment list and
wrote its caption straight to the wire.  Its local-path sibling delegates to
``send_image_file`` → ``_send_attachment`` and *was* gated, so the hole was
exactly one branch wide, on the sibling call path of a bug already fixed.

What this file pins:

* the remote-URL ``send_image`` caption is audited, fails closed, and a
  refusal blocks the **whole** send — the picture does not go out beside a
  silently deleted sentence, because publishing media next to a removed
  caption says something nobody wrote;
* the non-``http(s)`` branch still delegates, and is still gated there;
* the full sweep: every other place in ``adapter.py`` where caller-supplied
  text reaches the wire gets one test proving it passes the same gate — the
  chat lane, the forward-card lane (lead line and node bodies), both caption
  branches of ``_send_attachment``, and the out-of-process ``_standalone_send``
  lane a cron job takes when no gateway is running;
* the caption send result on ``_send_attachment``'s inline branch is no longer
  discarded: a failed caption aborts the media too, the way ``voice.speak``
  already does, so the recent-chatter buffer can never record a sentence that
  never arrived;
* the ``get_record`` container question: the sniffer's verdict, not our
  ``out_format`` request, is what the reported MIME asserts.

Everything is offline: a fake OneBot client, a fake socket, no NapCat, no
model call, and no path in this file can reach a real QQ account.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
import websockets

from gateway.config import PlatformConfig
from gateway.platforms.base import classify_send_error

from plugins.platforms.onebot import adapter as A
from plugins.platforms.onebot import protocol as P
from plugins.qzone import policy as POLICY


GROUP = "183287894"
DM_PEER = "536132102"

#: A phrase the ported rule table blocks (``fraud.account-abuse``), shared
#: verbatim with ``test_onebot_content_gate.py`` / ``test_onebot_voice_gaps.py``
#: so a revision of the rule table fails loudly here too instead of silently
#: testing nothing.
BLOCKED_TEXT = "QQ解冻教程"
BENIGN_TEXT = "看看这张图"

REMOTE_URL = "https://cdn.example.invalid/pic.png"

OK_RESPONSE: Dict[str, Any] = {
    "status": "ok",
    "retcode": 0,
    "data": {"message_id": 1001},
}
FAIL_RESPONSE: Dict[str, Any] = {
    "status": "failed",
    "retcode": 1200,
    "message": "发送失败",
}


def test_the_fixtures_are_actually_what_they_claim():
    """Guard the guard: every assertion below is meaningless if these drift."""
    cfg = POLICY.resolve_config(None)
    assert POLICY.moderate_text(BLOCKED_TEXT, cfg).decision.allowed is False
    assert POLICY.moderate_text(BENIGN_TEXT, cfg).decision.allowed is True


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeClient:
    """Records every outbound action; never touches a socket.

    *responses* is consumed one per action, and the final entry then repeats.
    That is how a test can fail the **caption** message while still letting the
    media message through — the exact split the discarded-result defect is
    about, and one a single fixed response cannot express.
    """

    def __init__(self, *responses: Optional[Dict[str, Any]]) -> None:
        self.actions: List[Any] = []
        self.connected = True
        self.last_self_id = 100
        self.last_event_at_ms = 0
        self.last_status_online = True
        self.inbound_dropped_count = 0
        self.outbound_queue_depth = 0
        self._responses: List[Optional[Dict[str, Any]]] = list(responses) or [
            OK_RESPONSE
        ]

    async def call_action(self, action, *, timeout=None):  # noqa: ANN001
        self.actions.append(action)
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]

    async def send_action(self, action):  # noqa: ANN001
        self.actions.append(action)

    async def close(self):
        self.connected = False


def make_adapter(
    extra: Optional[Dict[str, Any]] = None,
    *,
    client: Optional[FakeClient] = None,
) -> A.OneBotAdapter:
    """A connected-looking adapter with the group whitelisted and unmuted."""
    base: Dict[str, Any] = {
        "ws_url": "ws://127.0.0.1:3001",
        # Unmuted, so the mute never masks what these tests are measuring.
        "group_replies_enabled": True,
        "group_whitelist": [GROUP],
        "bot_nickname": "格兰",
    }
    base.update(extra or {})
    ad = A.OneBotAdapter(PlatformConfig(enabled=True, extra=base))
    ad._client = client or FakeClient()
    ad._running = True
    ad._semaphore = asyncio.Semaphore(4)
    ad._account_online = True
    ad._group_names[int(GROUP)] = "测试群"
    ad.router.group_replies_enabled = True
    return ad


def wire_text(ad: A.OneBotAdapter) -> str:
    """Every text segment the adapter actually put on the wire, concatenated."""
    out: List[str] = []
    for action in ad._client.actions:
        for seg in getattr(action, "message", []) or []:
            text = getattr(seg, "text", None)
            if text:
                out.append(text)
        for node in getattr(action, "messages", []) or []:
            for seg in getattr(node, "content", []) or []:
                text = getattr(seg, "text", None)
                if text:
                    out.append(text)
    return "".join(out)


def image_segments(ad: A.OneBotAdapter) -> List[Any]:
    """Every image segment that reached the wire."""
    return [
        seg
        for action in ad._client.actions
        for seg in getattr(action, "message", []) or []
        if isinstance(seg, P.ImageSegment)
    ]


def buffered(ad: A.OneBotAdapter, group: str = GROUP) -> List[Tuple[str, str, bool]]:
    """``(sender, text, is_self)`` for every entry in the chatter buffer."""
    return [
        (sender, text, is_self)
        for _ts, sender, text, is_self in A.recent_group_messages(ad.instance_id, group)
    ]


def png_file(tmp_path: Path, name: str = "pic.png") -> str:
    path = tmp_path / name
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    return str(path)


def voice_file(tmp_path: Path, name: str = "clip.silk") -> str:
    path = tmp_path / name
    path.write_bytes(b"\x02#!SILK_V3" + b"0" * 32)
    return str(path)


def exploding_classifier(monkeypatch: pytest.MonkeyPatch) -> None:
    """A classifier that raises — the fail-closed case, not the refusal case."""

    def _boom(*args: Any, **kwargs: Any):
        raise RuntimeError("classifier is down")

    monkeypatch.setattr(A.content_policy, "moderate_text_all", _boom)


@pytest.fixture(autouse=True)
def _reset_state():
    A._reset_module_state()
    yield
    A._reset_module_state()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("ONEBOT_"):
            monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# 1. The defect — ``send_image``'s remote-URL caption
# ---------------------------------------------------------------------------


class TestRemoteImageCaptionIsModerated:
    """The finding: it was not.

    ``send_image`` builds its own segment list for the remote-URL branch and
    appended the caption straight to it.  Nothing between the caller and the
    wire audited that sentence.
    """

    @pytest.mark.asyncio
    async def test_a_clean_caption_still_goes_out_unchanged(self):
        """The regression guard first: the gate must not cost a good caption."""
        ad = make_adapter()
        res = await ad.send_image(f"g{GROUP}", REMOTE_URL, caption=BENIGN_TEXT)
        assert res.success is True
        assert BENIGN_TEXT in wire_text(ad)
        assert [seg.url for seg in image_segments(ad)] == [REMOTE_URL]

    @pytest.mark.asyncio
    async def test_a_blocked_caption_blocks_the_whole_send(self):
        """Not "the image minus the words" — nothing at all leaves."""
        ad = make_adapter()
        res = await ad.send_image(f"g{GROUP}", REMOTE_URL, caption=BLOCKED_TEXT)
        assert res.success is False
        assert A.is_policy_blocked_send_result(res) is True
        # The image segment is the point: publishing the picture with the
        # sentence quietly deleted would say something nobody wrote.
        assert ad._client.actions == []
        assert image_segments(ad) == []

    @pytest.mark.asyncio
    async def test_a_blocked_caption_is_refused_in_a_dm_as_well(self):
        """The freeze risk is account-level; Tencent does not care where."""
        ad = make_adapter()
        res = await ad.send_image(DM_PEER, REMOTE_URL, caption=BLOCKED_TEXT)
        assert res.success is False
        assert ad._client.actions == []

    @pytest.mark.asyncio
    async def test_an_exploding_classifier_fails_closed(self, monkeypatch):
        """An exploding gate is a closed gate, exactly as in ``send()``."""
        exploding_classifier(monkeypatch)
        ad = make_adapter()
        res = await ad.send_image(f"g{GROUP}", REMOTE_URL, caption=BENIGN_TEXT)
        assert res.success is False
        assert ad._client.actions == []

    @pytest.mark.asyncio
    async def test_the_refusal_cannot_be_read_as_a_dead_target(self):
        """``send()``'s shape, verbatim: a refused sentence is not a dead chat.

        Asserted through the real classifier rather than by eyeballing the
        wording, so a reworded refusal that starts tripping the retry ladder
        fails here.
        """
        ad = make_adapter()
        res = await ad.send_image(f"g{GROUP}", REMOTE_URL, caption=BLOCKED_TEXT)
        assert res.error_kind == "unknown"
        assert res.retryable is False
        assert classify_send_error(res.error or "") not in ("forbidden", "not_found")

    @pytest.mark.asyncio
    async def test_an_uncaptioned_image_never_consults_the_classifier(
        self, monkeypatch
    ):
        """No text, nothing to audit: a bare picture must not pay for the gate.

        Pinned with the exploding classifier because that is the only way to
        tell "allowed" apart from "never asked".
        """
        exploding_classifier(monkeypatch)
        ad = make_adapter()
        res = await ad.send_image(f"g{GROUP}", REMOTE_URL)
        assert res.success is True
        assert [seg.url for seg in image_segments(ad)] == [REMOTE_URL]

    @pytest.mark.asyncio
    async def test_the_image_bytes_are_still_ungated(self):
        """Pinning the known gap, not endorsing it: no media classifier exists.

        A URL that would point at refused *pixels* still goes out — that is the
        acknowledged ``TODO(media)`` in ``send()``, and closing it needs a
        media classifier this repo does not have.
        """
        ad = make_adapter()
        res = await ad.send_image(f"g{GROUP}", "https://cdn.example.invalid/x.png")
        assert res.success is True
        assert len(image_segments(ad)) == 1


class TestTheLocalPathStillDelegates:
    """The non-``http(s)`` branch is the half that was already covered.

    It reaches ``_send_attachment`` through ``send_image_file``, which runs the
    same audit.  Pinned so the delegation cannot regress into a second copy of
    the segment-building code — which is how the remote branch grew its hole.
    """

    @pytest.mark.asyncio
    async def test_a_local_path_is_delivered_as_an_inline_file(self, tmp_path):
        ad = make_adapter()
        res = await ad.send_image(f"g{GROUP}", png_file(tmp_path), caption=BENIGN_TEXT)
        assert res.success is True
        # Inline base64, not a URL: proof the attachment path was taken.
        segs = image_segments(ad)
        assert len(segs) == 1
        assert (segs[0].file or "").startswith("base64://")
        assert BENIGN_TEXT in wire_text(ad)

    @pytest.mark.asyncio
    async def test_a_local_path_with_a_blocked_caption_is_still_refused(self, tmp_path):
        ad = make_adapter()
        res = await ad.send_image(f"g{GROUP}", png_file(tmp_path), caption=BLOCKED_TEXT)
        assert res.success is False
        assert ad._client.actions == []

    @pytest.mark.asyncio
    async def test_a_file_url_scheme_is_not_mistaken_for_a_remote_url(self, tmp_path):
        """Only ``http(s)`` is remote; everything else is a path to delegate."""
        ad = make_adapter()
        res = await ad.send_image(
            f"g{GROUP}", f"file://{png_file(tmp_path)}", caption=BLOCKED_TEXT
        )
        # Refused either way — what matters is that it never reached the wire.
        assert res.success is False
        assert ad._client.actions == []


# ---------------------------------------------------------------------------
# 2. The sweep — every other text-to-wire path in the adapter
# ---------------------------------------------------------------------------


class TestEveryTextToWirePathIsGated:
    """One test per site where a ``TextSegment`` is built from caller text.

    The sweep is the point: the remote-URL hole existed because the fix for the
    inline caption stopped at the site the reporter hit.  These pin the rest of
    the class so the next one cannot be found in production.
    """

    @pytest.mark.asyncio
    async def test_the_chat_lane_is_gated(self):
        """``send()`` — the original gate, and the one the others answer to."""
        ad = make_adapter()
        res = await ad.send(f"g{GROUP}", BLOCKED_TEXT)
        assert res.success is False
        assert ad._client.actions == []

    @pytest.mark.asyncio
    async def test_the_forward_card_lane_is_gated(self):
        """The card lead line AND the node bodies, behind the same one gate.

        ``_deliver_forward`` builds two more ``TextSegment`` sites, but it is
        reached only from inside ``send()``, downstream of the audit — so a
        refused reply long enough to be a card produces no lead and no nodes.
        """
        ad = make_adapter({"forward_threshold": 10})
        long_blocked = f"{BLOCKED_TEXT}[MSG_BREAK]{BLOCKED_TEXT}"
        res = await ad.send(
            f"g{GROUP}", long_blocked, metadata={"onebot_at_user_id": "42"}
        )
        assert res.success is False
        assert ad._client.actions == []
        assert wire_text(ad) == ""

    @pytest.mark.asyncio
    async def test_a_clean_forward_card_still_goes_out(self):
        """Regression guard for the card lane: the gate cost it nothing."""
        ad = make_adapter({"forward_threshold": 10})
        res = await ad.send(f"g{GROUP}", f"{BENIGN_TEXT}[MSG_BREAK]{BENIGN_TEXT}")
        assert res.success is True
        assert BENIGN_TEXT in wire_text(ad)

    @pytest.mark.asyncio
    async def test_the_inline_attachment_caption_is_gated(self, tmp_path):
        """``_send_attachment``'s inline branch writes its caption itself."""
        ad = make_adapter()
        res = await ad.send_voice(
            f"g{GROUP}", voice_file(tmp_path), caption=BLOCKED_TEXT
        )
        assert res.success is False
        assert ad._client.actions == []

    @pytest.mark.asyncio
    async def test_the_upload_branch_caption_is_gated_by_send(self, tmp_path):
        """The upload branch hands its caption to ``send()`` — gated there.

        The file itself still uploads: it is media, and the media bytes are the
        acknowledged ``TODO(media)`` gap.  The caption never reaches the wire.
        """
        ad = make_adapter()
        doc = tmp_path / "report.pdf"
        doc.write_bytes(b"%PDF-1.4" + b"0" * 32)
        res = await ad.send_document(f"g{GROUP}", str(doc), caption=BLOCKED_TEXT)
        assert res.success is True
        assert isinstance(ad._client.actions[-1], P.UploadGroupFile)
        assert BLOCKED_TEXT not in wire_text(ad)

    @pytest.mark.asyncio
    async def test_the_sticker_only_reply_carries_no_ungated_text(self):
        """A reply that is nothing but a sticker marker.

        It builds an image-only message with no ``TextSegment`` at all, and it
        is still downstream of the gate — the marker is lifted out of the body
        *before* the audit, so a refused sentence takes its sticker with it.
        """
        ad = make_adapter()
        res = await ad.send(f"g{GROUP}", f"{BLOCKED_TEXT}[STICKER:thumbs-up]")
        assert res.success is False
        assert ad._client.actions == []


class TestTheOutOfProcessLaneIsGated:
    """``_standalone_send`` — the lane a cron job takes with no gateway up.

    It builds its own ``TextSegment`` list and writes it to a throwaway socket
    without ever touching ``send()``, so it never saw the gate.  The emergency
    mute was already ported here for exactly this reason ("gating only the live
    adapter would leave the hole one process over"); the account-freeze gate is
    the same argument, one gate over.
    """

    @staticmethod
    def _pconfig() -> PlatformConfig:
        return PlatformConfig(
            enabled=True,
            extra={
                "ws_url": "ws://127.0.0.1:3001",
                "group_replies_enabled": True,
            },
        )

    @staticmethod
    def _fake_socket(monkeypatch: pytest.MonkeyPatch) -> List[str]:
        """Replace ``websockets.connect`` with a recorder; return the frames."""
        frames: List[str] = []

        class _Socket:
            async def send(self, payload: str) -> None:
                frames.append(payload)

        class _Connect:
            async def __aenter__(self) -> "_Socket":
                return _Socket()

            async def __aexit__(self, *exc: Any) -> bool:
                return False

        monkeypatch.setattr(websockets, "connect", lambda *a, **k: _Connect())
        return frames

    @pytest.mark.asyncio
    async def test_a_blocked_message_never_opens_the_socket(self, monkeypatch):
        """A refusal is a decision, so it lands before the transient work."""
        frames = self._fake_socket(monkeypatch)
        res = await A._standalone_send(self._pconfig(), f"g{GROUP}", BLOCKED_TEXT)
        assert frames == []
        assert res.get("error")
        assert A.is_policy_blocked_send_result(res) is True
        assert res.get("group_id") == int(GROUP)

    @pytest.mark.asyncio
    async def test_an_exploding_classifier_fails_closed(self, monkeypatch):
        frames = self._fake_socket(monkeypatch)
        exploding_classifier(monkeypatch)
        res = await A._standalone_send(self._pconfig(), f"g{GROUP}", BENIGN_TEXT)
        assert frames == []
        assert res.get("error")

    @pytest.mark.asyncio
    async def test_a_clean_message_still_reaches_the_socket(self, monkeypatch):
        """Regression guard: cron delivery still works."""
        frames = self._fake_socket(monkeypatch)
        res = await A._standalone_send(self._pconfig(), DM_PEER, BENIGN_TEXT)
        assert res.get("success") is True
        assert len(frames) == 1
        # Parsed, not substring-matched: ``json.dumps`` escapes the CJK.
        wire = json.loads(frames[0])
        assert wire["action"] == "send_private_msg"
        assert wire["params"]["message"][0]["data"]["text"] == BENIGN_TEXT


# ---------------------------------------------------------------------------
# 3. The discarded caption send result on the inline branch
# ---------------------------------------------------------------------------


class TestAFailedCaptionAbortsTheMedia:
    """The inline branch threw its caption's send result away.

    Two consequences, one fix: the reader got a media bubble with no sentence
    beside it (``voice.speak`` calls that "the bot talking to itself" and
    aborts), and ``_record_self_media`` then taught the recent-chatter buffer a
    caption that never arrived — so the persona would later read back words it
    never actually said.  Aligned with ``speak()``: stop, and let the caller's
    retry deliver a coherent pair.
    """

    @pytest.mark.asyncio
    async def test_a_failed_caption_stops_the_media(self, tmp_path):
        # Fail the caption, then answer OK — proof the media was never even
        # attempted rather than attempted and rejected.
        ad = make_adapter(client=FakeClient(FAIL_RESPONSE, OK_RESPONSE))
        res = await ad.send_voice(
            f"g{GROUP}", voice_file(tmp_path), caption=BENIGN_TEXT
        )
        assert res.success is False
        assert len(ad._client.actions) == 1
        assert isinstance(ad._client.actions[0].message[-1], P.TextSegment)

    @pytest.mark.asyncio
    async def test_a_failed_caption_teaches_the_buffer_nothing(self, tmp_path):
        """The persona must never read back a sentence that never arrived."""
        ad = make_adapter(client=FakeClient(FAIL_RESPONSE, OK_RESPONSE))
        await ad.send_voice(f"g{GROUP}", voice_file(tmp_path), caption=BENIGN_TEXT)
        assert buffered(ad) == []

    @pytest.mark.asyncio
    async def test_the_failure_reaches_the_caller_unrewritten(self, tmp_path):
        """The backend's own verdict, so a retry ladder can classify it."""
        ad = make_adapter(client=FakeClient(FAIL_RESPONSE, OK_RESPONSE))
        res = await ad.send_voice(
            f"g{GROUP}", voice_file(tmp_path), caption=BENIGN_TEXT
        )
        assert "发送失败" in (res.error or "")

    @pytest.mark.asyncio
    async def test_a_failed_image_caption_stops_the_picture_too(self, tmp_path):
        """Same branch, same rule — an image is no different from a voice note."""
        ad = make_adapter(client=FakeClient(FAIL_RESPONSE, OK_RESPONSE))
        res = await ad.send_image_file(
            f"g{GROUP}", png_file(tmp_path), caption=BENIGN_TEXT
        )
        assert res.success is False
        assert image_segments(ad) == []

    @pytest.mark.asyncio
    async def test_a_delivered_caption_still_carries_its_media(self, tmp_path):
        """The regression guard: the ordinary pair is untouched."""
        ad = make_adapter()
        res = await ad.send_voice(
            f"g{GROUP}", voice_file(tmp_path), caption=BENIGN_TEXT
        )
        assert res.success is True
        actions = ad._client.actions
        assert len(actions) == 2
        assert isinstance(actions[0].message[-1], P.TextSegment)
        assert isinstance(actions[1].message[-1], P.RecordSegment)
        assert buffered(ad) == [("格兰", BENIGN_TEXT, True)]

    @pytest.mark.asyncio
    async def test_a_failed_media_after_a_good_caption_still_records_nothing(
        self, tmp_path
    ):
        """Unchanged behaviour, pinned beside its new sibling for contrast."""
        ad = make_adapter(client=FakeClient(OK_RESPONSE, FAIL_RESPONSE))
        res = await ad.send_voice(
            f"g{GROUP}", voice_file(tmp_path), caption=BENIGN_TEXT
        )
        assert res.success is False
        assert buffered(ad) == []


# ---------------------------------------------------------------------------
# 4. ``get_record``'s ``out_format``: the sniffer has the last word
# ---------------------------------------------------------------------------


class TestTheVoiceContainerIsSniffedNotAsserted:
    """The prior worry — ``out_format="mp3"`` ignored by the backend, and the
    cached file then labelled ``audio/mp3`` regardless — is closed for the
    containers QQ actually ships.

    ``tools/audio_container.py`` gained AMR and SILK detection, and
    ``cache_audio_from_bytes`` renames the cached file to the sniffer's
    verdict, which is the string ``_voice_mime`` reads back.  So a backend that
    ignores the request and hands back the raw voice note is reported as what
    it is.  No code change; these pin the property that makes that true, since
    it is the sniffer's coverage — not the adapter — that closes the gap.
    """

    @staticmethod
    async def _cache(adapter: A.OneBotAdapter, payload: bytes) -> Tuple[str, str]:
        client = FakeClient({
            "status": "ok",
            "retcode": 0,
            "data": {"base64": base64.b64encode(payload).decode()},
        })
        adapter._client = client
        cached = await adapter._cache_voice(
            P.MediaRef("audio", "", "audio/*", "ABCD1234.silk")
        )
        assert cached is not None
        return cached

    @pytest.mark.asyncio
    async def test_silk_bytes_are_not_reported_as_mp3(self):
        """The realistic case: NapCat ignores ``out_format`` and returns SILK."""
        path, mime = await self._cache(make_adapter(), b"\x02#!SILK_V3" + b"0" * 64)
        assert Path(path).suffix == ".silk"
        assert mime == "audio/silk"

    @pytest.mark.asyncio
    async def test_amr_bytes_are_not_reported_as_mp3(self):
        path, mime = await self._cache(make_adapter(), b"#!AMR\n" + b"0" * 64)
        assert Path(path).suffix == ".amr"
        assert mime == "audio/amr"

    @pytest.mark.asyncio
    async def test_a_backend_that_honours_the_request_is_believed(self):
        """Regression guard: an honest MP3 is still reported as one."""
        _path, mime = await self._cache(
            make_adapter(), b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 64
        )
        assert mime == "audio/mp3"

    @pytest.mark.asyncio
    async def test_the_request_itself_is_unchanged(self):
        """Still ``mp3``: every STT backend takes it, and the sniffer corrects
        the label when a backend ignores the ask."""
        ad = make_adapter()
        await self._cache(ad, b"\x02#!SILK_V3" + b"0" * 64)
        records = [a for a in ad._client.actions if isinstance(a, P.GetRecord)]
        assert [a.out_format for a in records] == ["mp3"]
