"""Two gaps between what the OneBot voice lanes do and what they were wired to do.

Both are the kind of defect that is silent in production and only shows up as
the bot behaving slightly wrong in a real QQ chat, so each gets pinned as a
contract rather than as line coverage:

* **The proactive CALL lane was never started.**  ``proactive.proactive_call_loop``
  is a resident loop that rings a real person's phone on the bot's own
  initiative, and nothing in production created the task — a whole lane of dead
  code.  ``OneBotAdapter._start_proactive_call_loop`` starts it, on the same
  terms as its text twin: resident even while the feature is off (so the YAML
  switch hot-applies), guarded against a reconnect starting a second caller,
  asked to stop before it is cancelled, and never able to cost the channel its
  connection when the module behind it will not import.

* **A voice/media bubble never told the recent-chatter buffer it spoke.**
  ``send()`` records every outbound group message there; ``_send_attachment``
  — the path behind ``send_voice`` / ``send_image_file`` / ``send_video`` /
  ``send_document`` — did not.  That buffer is the transcript the proactive
  persona reads back AND the input to ``proactive.last_message_is_self``, so
  after posting a voice note the bot believed it had said nothing and the
  proactive lane could talk straight over itself.

The third assertion here is about a **caption**: the inline branch writes its
caption to the wire itself instead of going through ``send()``, so it used to
skip the Tencent content policy entirely.  A caption is text the bot is saying,
so it now gets the same gate and fails the same way — closed.  (The media
*bytes* are still ungated; that remains the acknowledged ``TODO(media)``.)

Everything is offline: a fake OneBot client, no socket, no NapCat, no model
call, no TTS, and no path in this file can reach a real QQ group or place a
real call.
"""

from __future__ import annotations

import asyncio
import builtins
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from gateway.config import PlatformConfig

from plugins.platforms.onebot import adapter as A
from plugins.platforms.onebot import protocol as P
from plugins.platforms.onebot import proactive as PR
from plugins.qzone import policy as POLICY


GROUP = "183287894"
DM_PEER = "536132102"

#: A phrase the ported rule table blocks (``fraud.account-abuse``), shared
#: verbatim with ``test_onebot_voice.py`` / ``test_onebot_content_gate.py`` so a
#: revision of the rule table fails loudly here too instead of silently testing
#: nothing.
BLOCKED_TEXT = "QQ解冻教程"


def test_the_blocked_fixture_is_actually_blocked():
    """Guard the guard: the caption-refusal test below is meaningless if this drifts."""
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
        self.last_self_id = 100
        self.last_event_at_ms = 0
        self.last_status_online = True
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

    async def connect(self):
        self.connected = True

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


async def wait_until(predicate, timeout: float = 5.0) -> bool:
    """Poll *predicate* until it holds.  Generous bound, event-shaped assert."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


def buffered(ad: A.OneBotAdapter, group: str = GROUP) -> List[Tuple[str, str, bool]]:
    """``(sender, text, is_self)`` for every entry in the chatter buffer."""
    return [
        (sender, text, is_self)
        for _ts, sender, text, is_self in A.recent_group_messages(ad.instance_id, group)
    ]


def voice_file(tmp_path: Path, name: str = "clip.silk") -> str:
    path = tmp_path / name
    path.write_bytes(b"SILK" + b"0" * 32)
    return str(path)


@pytest.fixture
def connectable(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    """Make ``adapter.connect()`` run for real against a fake socket."""
    client = FakeClient()
    monkeypatch.setattr(A, "OneBotClient", lambda cfg, on_self_id=None: client)

    async def _preflight(self) -> bool:  # noqa: ANN001
        return False

    monkeypatch.setattr(A.OneBotAdapter, "_preflight", _preflight)
    return client


@contextmanager
def broken_proactive_import():
    """Make ``from . import proactive`` raise, the way a syntax error would.

    Patched at ``__import__`` rather than by poking ``sys.modules``, because the
    package already carries the submodule as an attribute and ``from . import``
    would hand that back instead of re-importing.  Undone by this context
    manager rather than by ``monkeypatch``, because the autouse reset fixture's
    teardown imports the very module being broken and runs after monkeypatch's.
    """
    real_import = builtins.__import__

    def _guarded(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: ANN001
        if level and "proactive" in (fromlist or ()):
            raise ImportError("simulated broken proactive module")
        return real_import(name, globals, locals, fromlist, level)

    builtins.__import__ = _guarded
    try:
        yield
    finally:
        builtins.__import__ = real_import


@pytest.fixture(autouse=True)
def _reset_state():
    A._reset_module_state()
    PR.set_context_provider(None)
    yield
    A._reset_module_state()
    PR.set_context_provider(None)


# ---------------------------------------------------------------------------
# 1. Defect 1 — the proactive CALL lane is started, and stopped, by the adapter
# ---------------------------------------------------------------------------


class TestProactiveCallLaneLifecycle:
    @pytest.mark.asyncio
    async def test_connect_starts_the_lane_and_disconnect_stops_it(self, connectable):
        """The whole point: without this the loop was dead code nothing ran."""
        ad = make_adapter()
        assert await ad.connect() is True
        try:
            assert ad._proactive_call_task is not None
            assert not ad._proactive_call_task.done()
            assert ad._proactive_call_cancel is not None
        finally:
            await ad.disconnect()
        assert ad._proactive_call_task is None
        assert ad._proactive_call_cancel is None

    @pytest.mark.asyncio
    async def test_the_lane_gets_its_own_task_name(self, connectable):
        """Consistent with ``onebot-dispatch`` / ``-health`` / ``-proactive``."""
        ad = make_adapter()
        assert await ad.connect() is True
        try:
            assert ad._proactive_call_task.get_name() == "onebot-proactive-call"
        finally:
            await ad.disconnect()

    @pytest.mark.asyncio
    async def test_a_reconnect_does_not_start_a_second_caller(self):
        """Two callers means a real person's phone rings twice for one decision."""
        ad = make_adapter()
        ad._start_proactive_call_loop()
        first = ad._proactive_call_task
        assert first is not None
        ad._start_proactive_call_loop()
        assert ad._proactive_call_task is first
        await ad.disconnect()

    @pytest.mark.asyncio
    async def test_the_lane_is_separate_from_the_text_lane(self):
        """One shared handle could not stop one lane without stopping the other."""
        ad = make_adapter()
        ad._start_proactive_loop()
        ad._start_proactive_call_loop()
        assert ad._proactive_task is not ad._proactive_call_task
        assert ad._proactive_cancel is not ad._proactive_call_cancel
        await ad.disconnect()

    @pytest.mark.asyncio
    async def test_the_cancel_event_is_set_before_the_task_is_cancelled(self):
        """Ask first, cancel second, so a half-placed call is not stranded."""
        ad = make_adapter()
        ad._start_proactive_call_loop()
        task = ad._proactive_call_task
        cancel = ad._proactive_call_cancel
        assert task is not None
        assert cancel is not None and not cancel.is_set()

        order: List[str] = []
        real_cancel_event_set = cancel.set

        def _record_set() -> None:
            order.append("event")
            real_cancel_event_set()

        cancel.set = _record_set  # type: ignore[method-assign]
        real_task_cancel = task.cancel

        def _record_cancel(*args: Any, **kwargs: Any) -> bool:
            order.append("cancel")
            return real_task_cancel(*args, **kwargs)

        task.cancel = _record_cancel  # type: ignore[method-assign]

        await ad.disconnect()
        assert order == ["event", "cancel"]
        assert cancel.is_set()

    @pytest.mark.asyncio
    async def test_the_lane_starts_even_with_the_feature_disabled(self):
        """It resolves its own config each beat; not starting it kills hot-apply."""
        ad = make_adapter()
        assert PR.live_call_config(ad) is None  # shipped default: OFF
        ad._start_proactive_call_loop()
        assert ad._proactive_call_task is not None
        assert not ad._proactive_call_task.done()
        await ad.disconnect()

    @pytest.mark.asyncio
    async def test_enabling_the_lane_mid_flight_hot_applies(self, monkeypatch):
        """Started OFF, switched ON while asleep — no channel restart involved."""
        monkeypatch.setattr(PR, "now_parts", lambda tz: ("2026-08-19", 12))
        beats: List[Any] = []

        async def _call_beat(adapter, cfg):  # noqa: ANN001
            beats.append(cfg)

        monkeypatch.setattr(PR, "_call_beat", _call_beat)

        # The loop "sleeps" on this gate, so the test controls exactly when the
        # next beat happens instead of racing a wall clock.
        gate = asyncio.Event()

        async def _sleep(cancel, secs):  # noqa: ANN001
            await gate.wait()
            gate.clear()
            return cancel.is_set()

        monkeypatch.setattr(PR, "sleep_or_cancel", _sleep)

        ad = make_adapter()
        ad._start_proactive_call_loop()
        assert ad._proactive_call_task is not None

        ad.config.extra.update(
            proactive_call_enabled=True,
            proactive_call_targets=[DM_PEER],
            proactive_call_probability=1.0,
        )
        gate.set()
        try:
            assert await wait_until(lambda: bool(beats))
            assert beats[0].targets == (DM_PEER,)
        finally:
            await ad.disconnect()

    @pytest.mark.asyncio
    async def test_the_initial_config_is_pre_resolved_by_the_adapter(self, monkeypatch):
        """``live_call_config(self)`` at start time, like the text lane's twin."""
        seen: List[Any] = []
        real_loop = PR.proactive_call_loop

        async def _spy(adapter, cancel, cfg=None):  # noqa: ANN001
            seen.append(cfg)
            return await real_loop(adapter, cancel, cfg)

        monkeypatch.setattr(PR, "proactive_call_loop", _spy)
        ad = make_adapter({
            "proactive_call_enabled": True,
            "proactive_call_targets": [DM_PEER],
        })
        ad._start_proactive_call_loop()
        try:
            assert await wait_until(lambda: bool(seen))
            assert seen[0] is not None and seen[0].targets == (DM_PEER,)
        finally:
            await ad.disconnect()

    @pytest.mark.asyncio
    async def test_a_broken_proactive_module_does_not_break_connect(self, connectable):
        """An optional lane that will not import must never cost us the channel."""
        ad = make_adapter()
        with broken_proactive_import():
            assert await ad.connect() is True
            try:
                assert ad._proactive_call_task is None
                # The socket and the lanes that DID import are unaffected.
                assert ad._dispatch_task is not None
                assert ad._client is connectable
            finally:
                await ad.disconnect()

    @pytest.mark.asyncio
    async def test_a_broken_proactive_module_leaves_nothing_to_stop(self):
        ad = make_adapter()
        with broken_proactive_import():
            ad._start_proactive_call_loop()
            assert ad._proactive_call_task is None
            await ad.disconnect()
        assert ad._proactive_call_cancel is None


# ---------------------------------------------------------------------------
# 2. Defect 2 — an outbound file tells the chatter buffer that it spoke
# ---------------------------------------------------------------------------


class TestOutboundMediaIsRecorded:
    @pytest.mark.asyncio
    async def test_a_group_voice_file_lands_in_the_buffer_as_self(self, tmp_path):
        ad = make_adapter()
        res = await ad.send_voice(f"g{GROUP}", voice_file(tmp_path))
        assert res.success is True
        assert buffered(ad) == [("格兰", "[语音]", True)]

    @pytest.mark.asyncio
    async def test_the_marker_flips_the_anti_self_spam_check(self, tmp_path):
        """The behaviour that was actually missing: the bot can see itself."""
        ad = make_adapter()
        assert PR.last_message_is_self(ad.instance_id, GROUP) is False
        await ad.send_voice(f"g{GROUP}", voice_file(tmp_path))
        assert PR.last_message_is_self(ad.instance_id, GROUP) is True

    @pytest.mark.asyncio
    async def test_the_marker_is_not_blank_so_the_transcript_keeps_it(self, tmp_path):
        """A blank entry is dropped by ``record_group_message`` — invisible again."""
        ad = make_adapter()
        await ad.send_voice(f"g{GROUP}", voice_file(tmp_path))
        assert PR._visible_buffer(ad.instance_id, GROUP) != []

    @pytest.mark.asyncio
    async def test_each_media_kind_records_the_label_a_qq_client_shows(self, tmp_path):
        ad = make_adapter()
        img = tmp_path / "pic.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        vid = tmp_path / "clip.mp4"
        vid.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 32)
        doc = tmp_path / "report.pdf"
        doc.write_bytes(b"%PDF-1.4" + b"0" * 32)

        await ad.send_image_file(f"g{GROUP}", str(img))
        await ad.send_voice(f"g{GROUP}", voice_file(tmp_path))
        await ad.send_video(f"g{GROUP}", str(vid))
        await ad.send_document(f"g{GROUP}", str(doc))
        assert [text for _s, text, _is_self in buffered(ad)] == [
            "[图片]",
            "[语音]",
            "[视频]",
            "[文件]",
        ]

    @pytest.mark.asyncio
    async def test_the_caption_is_preferred_over_the_marker(self, tmp_path):
        """A caption is real words the bot typed; the marker is only a stand-in."""
        ad = make_adapter()
        await ad.send_voice(f"g{GROUP}", voice_file(tmp_path), caption="听听这个")
        assert buffered(ad) == [("格兰", "听听这个", True)]

    @pytest.mark.asyncio
    async def test_an_uploaded_file_with_a_caption_is_recorded_once(self, tmp_path):
        """The upload branch's caption goes out via ``send()``, which records it.

        Recording the marker here as well would put one act in the transcript
        twice — as a file AND as the sentence beside it.
        """
        ad = make_adapter()
        doc = tmp_path / "report.pdf"
        doc.write_bytes(b"%PDF-1.4" + b"0" * 32)
        await ad.send_document(f"g{GROUP}", str(doc), caption="报告发你了")
        assert buffered(ad) == [("格兰", "报告发你了", True)]

    @pytest.mark.asyncio
    async def test_a_private_send_does_not_touch_the_group_buffer(self, tmp_path):
        """The buffer is per-group context; a DM has no business in it."""
        ad = make_adapter()
        res = await ad.send_voice(DM_PEER, voice_file(tmp_path), caption="悄悄话")
        assert res.success is True
        assert buffered(ad) == []
        assert buffered(ad, DM_PEER) == []

    @pytest.mark.asyncio
    async def test_a_failed_send_records_nothing(self, tmp_path):
        """Never teach the buffer about a message that never arrived."""
        ad = make_adapter(
            response={"status": "failed", "retcode": 1200, "message": "发送失败"}
        )
        res = await ad.send_voice(f"g{GROUP}", voice_file(tmp_path))
        assert res.success is False
        assert buffered(ad) == []

    @pytest.mark.asyncio
    async def test_a_failed_upload_records_nothing(self, tmp_path):
        ad = make_adapter(
            response={"status": "failed", "retcode": 1200, "message": "上传失败"}
        )
        doc = tmp_path / "report.pdf"
        doc.write_bytes(b"%PDF-1.4" + b"0" * 32)
        res = await ad.send_document(f"g{GROUP}", str(doc))
        assert res.success is False
        assert buffered(ad) == []

    @pytest.mark.asyncio
    async def test_a_muted_group_records_nothing(self, tmp_path):
        """The emergency mute means nothing was said, so nothing was said."""
        ad = make_adapter({"group_replies_enabled": False})
        res = await ad.send_voice(f"g{GROUP}", voice_file(tmp_path))
        assert res.success is False
        assert buffered(ad) == []

    @pytest.mark.asyncio
    async def test_a_rejected_path_records_nothing(self):
        ad = make_adapter()
        res = await ad.send_voice(f"g{GROUP}", "/nonexistent/secret.silk")
        assert res.success is False and res.error_kind == "forbidden"
        assert buffered(ad) == []


# ---------------------------------------------------------------------------
# 3. Defect 2 (second half) — the inline branch's caption is moderated
# ---------------------------------------------------------------------------


class TestInlineCaptionIsModerated:
    """The finding: it was NOT.

    The upload branch hands its caption to ``send()``, which runs the Tencent
    content policy.  The inline (image / voice) branch writes its caption to the
    wire itself and skipped the gate entirely.  It no longer does.
    """

    @pytest.mark.asyncio
    async def test_a_blocked_caption_refuses_the_whole_voice_send(self, tmp_path):
        ad = make_adapter()
        res = await ad.send_voice(
            f"g{GROUP}", voice_file(tmp_path), caption=BLOCKED_TEXT
        )
        assert res.success is False
        assert res.error_kind == "unknown" and res.retryable is False
        # Not merely "no caption": the recording never left either, and the
        # buffer was not told about a bubble that does not exist.
        assert ad._client.actions == []
        assert buffered(ad) == []

    @pytest.mark.asyncio
    async def test_a_blocked_caption_refuses_an_inline_image_too(self, tmp_path):
        ad = make_adapter()
        img = tmp_path / "pic.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        res = await ad.send_image_file(f"g{GROUP}", str(img), caption=BLOCKED_TEXT)
        assert res.success is False
        assert ad._client.actions == []

    @pytest.mark.asyncio
    async def test_a_blocked_caption_is_refused_in_a_dm_as_well(self, tmp_path):
        """The freeze risk is account-level; Tencent does not care where it was said."""
        ad = make_adapter()
        res = await ad.send_voice(DM_PEER, voice_file(tmp_path), caption=BLOCKED_TEXT)
        assert res.success is False
        assert ad._client.actions == []

    @pytest.mark.asyncio
    async def test_an_exploding_classifier_fails_closed(self, tmp_path, monkeypatch):
        def _boom(*args: Any, **kwargs: Any):
            raise RuntimeError("classifier is down")

        monkeypatch.setattr(A.content_policy, "moderate_text", _boom)
        ad = make_adapter()
        res = await ad.send_voice(
            f"g{GROUP}", voice_file(tmp_path), caption=BLOCKED_TEXT
        )
        assert res.success is False
        assert ad._client.actions == []

    @pytest.mark.asyncio
    async def test_a_clean_caption_still_goes_out_first_as_its_own_message(
        self, tmp_path
    ):
        """QQ drops anything sitting beside a voice bubble — order is the contract."""
        ad = make_adapter()
        res = await ad.send_voice(f"g{GROUP}", voice_file(tmp_path), caption="听听这个")
        assert res.success is True
        actions = ad._client.actions
        assert len(actions) == 2
        assert isinstance(actions[0].message[-1], P.TextSegment)
        assert isinstance(actions[1].message[-1], P.RecordSegment)

    @pytest.mark.asyncio
    async def test_an_uploaded_files_caption_is_still_gated_by_send(self, tmp_path):
        """Unchanged, and asserted so the two branches cannot silently diverge."""
        ad = make_adapter()
        doc = tmp_path / "report.pdf"
        doc.write_bytes(b"%PDF-1.4" + b"0" * 32)
        res = await ad.send_document(f"g{GROUP}", str(doc), caption=BLOCKED_TEXT)
        # The upload itself is media and stays ungated (TODO(media)); the
        # caption is text and never reaches the wire.
        assert res.success is True
        assert isinstance(ad._client.actions[-1], P.UploadGroupFile)
        assert buffered(ad) == []

    @pytest.mark.asyncio
    async def test_the_media_bytes_are_still_ungated(self, tmp_path):
        """Pinning the known gap, not endorsing it: no media classifier exists."""
        ad = make_adapter()
        res = await ad.send_voice(f"g{GROUP}", voice_file(tmp_path))
        assert res.success is True
