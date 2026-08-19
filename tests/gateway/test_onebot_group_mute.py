"""The emergency mute, on the OUTBOUND path (E0 task A / decision D44).

``group_replies_enabled`` reads — to the README, to the migration documents and
to operator intuition — as the master switch for group speech.  In the source
system it was: it silenced every group post, monitor digests included, which is
exactly why corlinman's ``qunjlu`` digest never reached a group in seven days
of production logs.

In this port the flag was consumed on the INBOUND side only (the router's reply
gate) plus the proactive loop's own ladder.  Anything arriving from the other
direction — a cron job delivering to ``onebot:g<id>``, a model calling
``send_message``, a media upload — went out while the operator believed the bot
was muted.  That is the failure that matters: the switch gets pressed during an
incident, and the incident keeps talking (00-PLAN.md §19).

What these tests pin:

* a group target is refused on **every** outbound path — text, chunked text,
  forward cards, inline media, file uploads, and the out-of-process standalone
  sender;
* **direct messages are untouched**, on all of them;
* the refusal is a ``SendResult``, never an exception and never a silent
  pretend-success, and it is distinguishable from a real send failure;
* the refusal must **not** be mistaken for a permanently dead target — that
  would outlive un-muting and turn a reversible switch into a sticky one;
* the three lanes (inbound gate / proactive gate / outbound send) stack into
  one coherent behaviour, and read one implementation of "am I muted".

Everything runs against a fake client — no sockets, no NapCat, no model call;
no path here can reach a real QQ group.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List, Optional

import pytest

from gateway.config import PlatformConfig
from gateway.dead_targets import DeadTargetRegistry
from gateway.platforms.base import MessageEvent, classify_send_error

from plugins.platforms.onebot import adapter as A
from plugins.platforms.onebot import proactive as PR
from plugins.platforms.onebot import protocol as P


GROUP = 183287894
DM_PEER = 536132102


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeClient:
    """Records every outbound action; never touches a socket."""

    def __init__(self) -> None:
        self.actions: List[Any] = []
        self.fire_and_forget: List[Any] = []
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
        self.fire_and_forget.append(action)

    async def close(self):
        self.connected = False


class FakeHandler:
    """Stand-in for the gateway message handler."""

    def __init__(self, reply: str = "嗯") -> None:
        self.reply = reply
        self.events: List[MessageEvent] = []

    async def __call__(self, event: MessageEvent):
        self.events.append(event)
        return self.reply


def make_adapter(
    extra: Optional[Dict[str, Any]] = None,
    *,
    client: Optional[FakeClient] = None,
    handler: Optional[FakeHandler] = None,
) -> A.OneBotAdapter:
    base: Dict[str, Any] = {"ws_url": "ws://127.0.0.1:3001"}
    base.update(extra or {})
    ad = A.OneBotAdapter(PlatformConfig(enabled=True, extra=base))
    ad._client = client or FakeClient()
    ad._running = True
    ad._semaphore = asyncio.Semaphore(4)
    ad._account_online = True
    ad._group_names[GROUP] = "测试群"
    ad._message_handler = handler or FakeHandler()
    return ad


def group_event(text="hi", *, gid=GROUP, uid=555, message_id=1, segments=None):
    return P.MessageEvent(
        self_id=100, message_type=P.MessageType.GROUP, sub_type="normal",
        group_id=gid, user_id=uid, message_id=message_id,
        message=segments if segments is not None else [P.TextSegment(text=text)],
        raw_message=text, time=1_700_000_000,
        sender=P.Sender(user_id=uid, nickname="alice"),
    )


def mention_event(message_id=1, uid=555):
    """A message that the default reply policy accepts."""
    return group_event(
        "", uid=uid, message_id=message_id,
        segments=[P.AtSegment(qq="100"), P.TextSegment(text=" 在吗")],
    )


@pytest.fixture(autouse=True)
def _clean_module_state():
    A._reset_module_state()
    yield
    A._reset_module_state()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("ONEBOT_"):
            monkeypatch.delenv(key, raising=False)




class TestMutedGroupSend:
    """A muted group must not receive anything, on any outbound path."""

    @pytest.mark.asyncio
    async def test_group_text_never_reaches_the_wire(self):
        client = FakeClient()
        ad = make_adapter(client=client)          # muted: the default
        res = await ad.send(f"g{GROUP}", "偷偷发一条")
        assert res.success is False
        assert client.actions == []

    @pytest.mark.asyncio
    async def test_unmuted_group_text_goes_out(self):
        client = FakeClient()
        ad = make_adapter({"group_replies_enabled": True}, client=client)
        res = await ad.send(f"g{GROUP}", "正常发言")
        assert res.success is True
        assert len(client.actions) == 1

    @pytest.mark.asyncio
    async def test_a_dm_is_never_affected(self):
        """The mute is about speaking in a room full of people."""
        client = FakeClient()
        ad = make_adapter(client=client)          # muted
        res = await ad.send(str(DM_PEER), "私聊照常")
        assert res.success is True
        assert isinstance(client.actions[0], P.SendPrivateMsg)

    @pytest.mark.asyncio
    async def test_a_long_group_reply_is_refused_whole(self):
        """Not "the first chunk is blocked and the rest leaks"."""
        client = FakeClient()
        ad = make_adapter({"forward_threshold": 0}, client=client)
        res = await ad.send(f"g{GROUP}", "y" * 9000)
        assert res.success is False
        assert client.actions == []

    @pytest.mark.asyncio
    async def test_a_forward_card_is_refused_too(self):
        client = FakeClient()
        ad = make_adapter(client=client)
        res = await ad.send(f"g{GROUP}", "z" * 1500)
        assert res.success is False
        assert client.actions == []

    @pytest.mark.asyncio
    async def test_a_refused_post_is_not_recorded_as_something_we_said(self):
        """Otherwise the proactive lane reads its own un-sent post as context
        — and as "we spoke last", which suppresses the next real post."""
        ad = make_adapter()
        await ad.send(f"g{GROUP}", "没发出去的话")
        assert A.recent_group_messages("default", GROUP) == []

    @pytest.mark.asyncio
    async def test_the_choke_point_refuses_even_a_hand_built_send(self):
        """Defence in depth: a future code path that skips ``send()`` still
        cannot post into a muted group."""
        client = FakeClient()
        ad = make_adapter(client=client)
        ok, mid, failure = await ad._send_segments(
            True, GROUP, [P.TextSegment(text="绕过 send()")]
        )
        assert ok is False and mid is None
        assert A.is_muted_send_result(failure) is True
        assert client.actions == []


class TestMutedGroupMedia:
    """A picture in a muted group is still the bot talking in that group."""

    @pytest.mark.asyncio
    async def test_group_file_upload_is_refused(self, tmp_path):
        f = tmp_path / "report.pdf"
        f.write_bytes(b"%PDF-1.4" + b"0" * 32)
        client = FakeClient()
        ad = make_adapter(client=client)
        res = await ad.send_document(f"g{GROUP}", str(f))
        assert A.is_muted_send_result(res) is True
        assert client.actions == []

    @pytest.mark.asyncio
    async def test_group_inline_image_is_refused(self, tmp_path):
        f = tmp_path / "pic.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        client = FakeClient()
        ad = make_adapter(client=client)
        res = await ad.send_image_file(f"g{GROUP}", str(f))
        assert A.is_muted_send_result(res) is True
        assert client.actions == []

    @pytest.mark.asyncio
    async def test_group_remote_image_url_is_refused(self):
        client = FakeClient()
        ad = make_adapter(client=client)
        res = await ad.send_image(f"g{GROUP}", "https://cdn.example/pic.png")
        assert A.is_muted_send_result(res) is True
        assert client.actions == []

    @pytest.mark.asyncio
    async def test_dm_media_still_works_while_groups_are_muted(self, tmp_path):
        f = tmp_path / "pic.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        client = FakeClient()
        ad = make_adapter(client=client)
        res = await ad.send_image_file(str(DM_PEER), str(f))
        assert res.success is True
        assert A.is_muted_send_result(res) is False


class TestMutedResultContract:
    """What the caller gets back, and what it must NOT be mistaken for."""

    @pytest.mark.asyncio
    async def test_it_is_a_failure_not_a_silent_pretend_success(self):
        res = await make_adapter().send(f"g{GROUP}", "hi")
        assert isinstance(res, A.SendResult)
        assert res.success is False
        assert "muted" in (res.error or "")

    @pytest.mark.asyncio
    async def test_it_never_raises(self):
        """A mute must not blow up a delivery chain that expects a result."""
        res = await make_adapter().send(f"g{GROUP}", "hi")
        assert res is not None

    @pytest.mark.asyncio
    async def test_muted_is_distinguishable_from_a_send_failure(self):
        class _Rejecting(FakeClient):
            async def call_action(self, action, *, timeout=None):
                self.actions.append(action)
                return {"status": "failed", "retcode": 1403,
                        "message": "no permission"}

        muted = await make_adapter().send(f"g{GROUP}", "hi")
        rejected = await make_adapter(
            {"group_replies_enabled": True}, client=_Rejecting()
        ).send(f"g{GROUP}", "hi")

        assert muted.success is rejected.success is False   # both are failures
        assert A.is_muted_send_result(muted) is True
        assert A.is_muted_send_result(rejected) is False
        assert muted.raw_response[A.MUTED_MARKER] is True
        assert muted.raw_response["group_id"] == GROUP
        assert muted.raw_response["reason"] == A.MUTED_REASON

    @pytest.mark.asyncio
    async def test_it_never_marks_the_group_permanently_dead(self):
        """``forbidden``/``not_found`` would be the natural-sounding kinds and
        are exactly the two ``gateway.dead_targets`` treats as permanent.  A
        mute is an operator toggle; it must not outlive un-muting."""
        res = await make_adapter().send(f"g{GROUP}", "hi")
        assert DeadTargetRegistry.is_dead_error_kind(res.error_kind) is False
        # The delivery layer also re-classifies from the raised error *text*,
        # so the wording must not read as a permanent failure either.
        assert classify_send_error(None, res.error or "") == "unknown"

    @pytest.mark.asyncio
    async def test_it_is_not_retried_into_existence(self):
        res = await make_adapter().send(f"g{GROUP}", "hi")
        assert res.retryable is False
        assert res.retry_after is None

    @pytest.mark.asyncio
    async def test_the_mute_wins_over_a_disconnected_link(self):
        """"Not connected" is retryable; "muted" is a decision.  Reporting the
        transient would make the caller retry a message that must never go."""
        ad = make_adapter()
        ad._client = None
        res = await ad.send(f"g{GROUP}", "hi")
        assert A.is_muted_send_result(res) is True
        assert res.retryable is False

    @pytest.mark.asyncio
    async def test_a_bad_chat_id_still_reports_the_bad_chat_id(self):
        """Parsing comes first — a typo is not a mute."""
        res = await make_adapter().send("not-a-chat", "hi")
        assert res.error_kind == "not_found"
        assert A.is_muted_send_result(res) is False


class TestStandaloneMute:
    """The out-of-process cron path (``standalone_sender_fn``) obeys it too."""

    @pytest.mark.asyncio
    async def test_group_delivery_is_refused_before_any_socket(self):
        res = await A._standalone_send(
            PlatformConfig(enabled=True, extra={"ws_url": "ws://127.0.0.1:1"}),
            f"g{GROUP}", "定时任务的播报",
        )
        assert A.is_muted_send_result(res) is True
        assert res.get("success") is None
        assert "error" in res

    @pytest.mark.asyncio
    async def test_a_dm_is_not_short_circuited_by_the_mute(self):
        """It fails on the (refused) connection, not on the mute — which is
        how we know the DM path was never gated."""
        res = await A._standalone_send(
            PlatformConfig(enabled=True, extra={"ws_url": "ws://127.0.0.1:1"}),
            str(DM_PEER), "私聊照常",
        )
        assert A.is_muted_send_result(res) is False
        assert "error" in res

    @pytest.mark.asyncio
    async def test_the_env_var_can_unmute_it(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_GROUP_REPLIES_ENABLED", "true")
        res = await A._standalone_send(
            PlatformConfig(enabled=True, extra={"ws_url": "ws://127.0.0.1:1"}),
            f"g{GROUP}", "hi",
        )
        assert A.is_muted_send_result(res) is False


class TestThreeLanesStack:
    """Inbound gate / proactive gate / outbound send, resolved together.

    The rule (B4's, kept): the router flag the reactive lane was built with
    AND the live config value must both be on.  Either off means muted.
    """

    @staticmethod
    def _adapter(router_flag: bool, live_flag: bool) -> A.OneBotAdapter:
        ad = make_adapter({"group_replies_enabled": router_flag})
        ad.config.extra["group_replies_enabled"] = live_flag
        return ad

    @pytest.mark.parametrize(
        "router_flag,live_flag,inbound_replies,muted",
        [
            (False, False, False, True),
            (False, True, False, True),   # router off is enough on its own
            (True, False, True, True),    # stale router flag: the turn runs…
            (True, True, True, False),
        ],
    )
    @pytest.mark.asyncio
    async def test_matrix(self, router_flag, live_flag, inbound_replies, muted):
        ad = self._adapter(router_flag, live_flag)
        handler = FakeHandler()
        ad._message_handler = handler
        ad.handle_message = handler  # type: ignore[assignment]

        await ad._on_message_event(mention_event())
        await asyncio.sleep(0.05)
        assert bool(handler.events) is inbound_replies

        assert PR.group_speech_muted(ad) is muted
        assert A.group_speech_muted(ad) is muted

        res = await ad.send(f"g{GROUP}", "回复内容")
        assert res.success is (not muted)
        assert A.is_muted_send_result(res) is muted

    @pytest.mark.asyncio
    async def test_a_hot_mute_stops_the_reply_that_is_already_in_flight(self):
        """The (True, False) row is the one that matters operationally: the
        router still has the flag it was built with, so the turn runs — and
        the answer is refused at the door instead of reaching the group."""
        ad = self._adapter(True, True)
        handler = FakeHandler()
        ad._message_handler = handler
        ad.handle_message = handler  # type: ignore[assignment]
        await ad._on_message_event(mention_event())
        await asyncio.sleep(0.05)
        assert handler.events                       # the turn happened

        ad.config.extra["group_replies_enabled"] = False   # operator hits mute
        res = await ad.send(f"g{GROUP}", handler.reply)
        assert A.is_muted_send_result(res) is True

    def test_all_three_lanes_read_one_function(self):
        """Not "two implementations that currently agree"."""
        ad = self._adapter(True, True)
        for router_flag in (True, False):
            for live_flag in (True, False):
                ad.router.group_replies_enabled = router_flag
                ad.config.extra["group_replies_enabled"] = live_flag
                assert PR.group_speech_muted(ad) is A.group_speech_muted(ad)

    @pytest.mark.asyncio
    async def test_the_proactive_lane_still_stops_at_its_own_gate(self):
        """The send-side gate is additive; it did not replace B4's ladder."""
        ad = self._adapter(True, False)
        assert PR.group_speech_muted(ad) is True


class TestStructuralSuppressionIsUnchanged:
    """D45: the QQ monitors stay suppressed structurally, not by this flag."""

    def test_qunjlu_is_still_suppressed_structurally(self):
        """Not by this flag — by having no delivery target and no send tool.
        Structural suppression does not depend on any runtime config being
        read correctly, which is why D45 keeps it *on top of* D44."""
        from plugins.corlinman_jobs import specs

        by_name = {s.name: s for s in specs.ALL_SPECS}
        qunjlu = by_name["qunjlu"]
        assert qunjlu.deliver == "local"
        assert not qunjlu.enabled_toolsets

    def test_no_migrated_job_is_installed_enabled(self):
        from plugins.corlinman_jobs import specs

        assert all(s.install_enabled is False for s in specs.ALL_SPECS)

    def test_the_two_qq_digests_deliver_to_a_dm_which_the_mute_never_touches(self):
        """Recorded deliberately: ``sanhu``/``jlu`` deliver to ``onebot:<uin>``
        (a DM), so D44 does NOT gate them.  That is the intended reading of
        the mute — it silences group speech — but it means the flag is not a
        kill switch for these two, and nobody should believe it is."""
        from plugins.corlinman_jobs import specs

        by_name = {s.name: s for s in specs.ALL_SPECS}
        for name in ("sanhu", "jlu"):
            deliver = by_name[name].deliver
            assert deliver.startswith("onebot:")
            assert not deliver.startswith("onebot:g")   # a uin, not a group
