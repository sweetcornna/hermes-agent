"""How one OneBot reply is shaped on the wire: bubbles, or a single card.

The persona writes ``[MSG_BREAK]``-separated bubbles so one logical reply
lands as several short chat messages — which reads like a person typing
rather than a form letter.  ``split_bubbles`` honoured that faithfully and
without any ceiling, so a talkative turn fired **eight** consecutive QQ
notifications at a real user, who asked for it to stop.  The persona file
already asks for restraint in prose ("短句默认。日常 2-3 句"); the model does
not reliably obey prose, so the transport enforces a count.

``cap_bubbles`` merged bubbles 3..N together on every send in an earlier
revision. That produced two short messages followed by one bloated slab, which
the user rejected as unreadable. D88 makes both over-limit forms one
merged-forward card instead: a whole reply over ``forward_threshold`` OR a
reply split into more than ``max_bubbles_per_reply`` bubbles. The card emits
one notification and preserves every persona bubble as its own node.

So ``send`` routes the whole finished reply down one of two lanes:

* **chat lane** — within both limits, bubbles go out exactly as written;
* **answer lane** — too long or too fragmented, the entire reply becomes ONE
  merged-forward card.

Cards may be disabled with ``forward_threshold: 0``. If disabled, or if a
forward action is rejected by a non-terminal backend error, ``cap_bubbles``
merges the overflow as the bounded fallback.

What these tests pin:

* the chat lane never merges anything when the bubble count already fits —
  no message it emits contains a newline join of two bubbles;
* length and bubble-count overflow both produce one card with all original
  bubbles as verbatim nodes and no ``[MSG_BREAK]`` leakage;
* both routing boundaries are tested at the limit and one past it;
* ``cap_bubbles`` merging still sits BEFORE ``chunk_text``, so a merged
  bubble over the protocol's per-message ceiling is length-split normally on
  a degraded route (cards disabled or a card the backend refused);
* a refused card costs notifications, never content;
* ``0`` / negative disables the cap (the escape hatch);
* the reply-quote and @mention ride exactly one successfully delivered lead,
  including when a group card is rejected and falls back to chat;
* **the content policy still runs before the routing decision**, so a refused
  reply reaches the wire by no lane, including the ones that did not exist
  when the gate was written.

Everything runs against a fake client — no sockets, no NapCat, no model call;
no path here can reach a real QQ group.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List, Optional

import pytest

from gateway.config import PlatformConfig

from plugins.platforms.onebot import adapter as A
from plugins.platforms.onebot import protocol as P
from plugins.qzone import policy as POLICY


GROUP = 183287894
DM_PEER = 536132102
SEP = A.BUBBLE_SEPARATOR

#: A phrase the ported Tencent rule table blocks (``fraud.account-abuse``),
#: shared with ``test_onebot_content_gate.py`` so a rule-table revision
#: fails loudly in both suites instead of silently testing nothing.
BLOCKED_TEXT = "QQ解冻教程"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeClient:
    """Records every outbound action; never touches a socket."""

    def __init__(self, responses: Optional[List[Dict[str, Any]]] = None) -> None:
        self.actions: List[Any] = []
        #: Scripted replies, consumed in order; the default is used once
        #: they run out.  Lets a test refuse the forward card specifically.
        self._responses: List[Dict[str, Any]] = list(responses or [])
        self.connected = True
        self.last_self_id = 100
        self.last_event_at_ms = 0
        self.last_status_online = True
        self.inbound_dropped_count = 0
        self.outbound_queue_depth = 0

    async def call_action(self, action, *, timeout=None):
        self.actions.append(action)
        if self._responses:
            return self._responses.pop(0)
        return {"status": "ok", "retcode": 0, "data": {"message_id": 1001}}

    async def send_action(self, action):
        self.actions.append(action)

    async def close(self):
        self.connected = False


def make_adapter(
    extra: Optional[Dict[str, Any]] = None,
    *,
    client: Optional[FakeClient] = None,
) -> A.OneBotAdapter:
    base: Dict[str, Any] = {
        "ws_url": "ws://127.0.0.1:3001",
        # Unmuted, so the mute gate never masks what these tests measure.
        "group_replies_enabled": True,
    }
    base.update(extra or {})
    ad = A.OneBotAdapter(PlatformConfig(enabled=True, extra=base))
    ad._client = client or FakeClient()
    ad._running = True
    ad._semaphore = asyncio.Semaphore(4)
    ad._account_online = True
    ad._group_names[GROUP] = "测试群"
    ad.router.group_replies_enabled = True
    return ad


def wire_messages(ad: A.OneBotAdapter) -> List[str]:
    """Text of each outgoing message, one entry per message on the wire.

    Unlike a flat concatenation this preserves the message *boundaries*,
    which is the whole subject of these tests.
    """
    out: List[str] = []
    for action in ad._client.actions:
        segs = getattr(action, "message", None)
        if segs is None:
            continue
        text = "".join(getattr(s, "text", "") or "" for s in segs)
        out.append(text)
    return out


def segments_of(ad: A.OneBotAdapter, index: int) -> List[Any]:
    return list(getattr(ad._client.actions[index], "message", []) or [])


def has_reply_segment(ad: A.OneBotAdapter, index: int) -> bool:
    return any(isinstance(s, P.ReplySegment) for s in segments_of(ad, index))


def has_at_segment(ad: A.OneBotAdapter, index: int) -> bool:
    return any(isinstance(s, P.AtSegment) for s in segments_of(ad, index))


def cards(ad: A.OneBotAdapter) -> List[Any]:
    """Every merged-forward action the adapter emitted."""
    return [
        a
        for a in ad._client.actions
        if isinstance(a, (P.SendPrivateForwardMsg, P.SendGroupForwardMsg))
    ]


def card_node_texts(ad: A.OneBotAdapter) -> List[str]:
    """Text of each node inside the single card that was sent."""
    card = cards(ad)[0]
    return [
        "".join(getattr(seg, "text", "") or "" for seg in node.content)
        for node in card.messages
    ]


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


@pytest.fixture(autouse=True)
def _no_bubble_gap(monkeypatch):
    """The inter-bubble pause is real politeness, not something to sit through."""
    monkeypatch.setattr(A, "BUBBLE_GAP_SECS", 0)


# ---------------------------------------------------------------------------
# 0. The pure helper, in isolation
# ---------------------------------------------------------------------------


class TestCapBubblesHelper:
    """``cap_bubbles`` decides how many messages fire; it never edits content."""

    def test_overflow_is_merged_into_the_last_survivor(self):
        got = A.cap_bubbles(["a", "b", "c", "d", "e"], 3)
        assert got == ["a", "b", "c\nd\ne"]

    def test_under_the_limit_is_returned_unchanged(self):
        assert A.cap_bubbles(["a", "b"], 3) == ["a", "b"]

    def test_exactly_at_the_limit_is_returned_unchanged(self):
        assert A.cap_bubbles(["a", "b", "c"], 3) == ["a", "b", "c"]

    def test_zero_disables_the_cap(self):
        bubbles = [str(i) for i in range(9)]
        assert A.cap_bubbles(bubbles, 0) == bubbles

    def test_negative_disables_the_cap(self):
        bubbles = [str(i) for i in range(9)]
        assert A.cap_bubbles(bubbles, -1) == bubbles

    def test_a_limit_of_one_merges_everything(self):
        assert A.cap_bubbles(["a", "b", "c"], 1) == ["a\nb\nc"]

    def test_no_character_is_lost(self):
        bubbles = [f"bubble-{i}" for i in range(8)]
        merged = "".join(A.cap_bubbles(bubbles, 3))
        for b in bubbles:
            assert b in merged

    def test_it_does_not_mutate_its_input(self):
        bubbles = ["a", "b", "c", "d"]
        A.cap_bubbles(bubbles, 2)
        assert bubbles == ["a", "b", "c", "d"]

    def test_empty_input_stays_empty(self):
        assert A.cap_bubbles([], 3) == []


# ---------------------------------------------------------------------------
# 1. The chat lane: short and few — delivered exactly as written
# ---------------------------------------------------------------------------


class TestChatLaneSendsBubblesVerbatim:
    """Within the bubble cap, what the persona wrote is what goes out.

    No merging happens when the count already fits. A reply over either limit
    is routed to the forward-card tests below instead.
    """

    @pytest.mark.asyncio
    async def test_two_bubbles_go_out_as_two_messages(self):
        parts = ["先说这个", "再说那个"]
        ad = make_adapter()
        res = await ad.send(str(DM_PEER), SEP.join(parts))
        assert res.success is True
        assert wire_messages(ad) == parts
        assert cards(ad) == [], "a two-line chat reply must not become a card"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("count", range(1, 4))
    async def test_one_through_three_bubbles_go_out_verbatim(self, count):
        parts = [f"第{i}句" for i in range(1, count + 1)]
        ad = make_adapter()
        await ad.send(str(DM_PEER), SEP.join(parts))
        assert wire_messages(ad) == parts
        assert cards(ad) == []

    @pytest.mark.asyncio
    async def test_a_single_bubble_is_untouched(self):
        ad = make_adapter()
        await ad.send(str(DM_PEER), "就一句话")
        assert wire_messages(ad) == ["就一句话"]

    @pytest.mark.asyncio
    async def test_nothing_on_this_lane_is_ever_newline_merged(self):
        """The exact shape the user rejected: one message that is really two."""
        parts = ["先说这个", "再说那个", "还有这个"]
        ad = make_adapter()
        await ad.send(str(DM_PEER), SEP.join(parts))
        for m in wire_messages(ad):
            assert "\n" not in m, f"bubbles were merged into one message: {m!r}"

    @pytest.mark.asyncio
    async def test_a_group_reply_within_the_limits_is_verbatim_too(self):
        parts = ["先说这个", "再说那个"]
        ad = make_adapter()
        res = await ad.send(f"g{GROUP}", SEP.join(parts))
        assert res.success is True
        assert wire_messages(ad) == parts
        assert cards(ad) == []

    @pytest.mark.asyncio
    async def test_disabling_the_cap_keeps_every_bubble_separate(self):
        """``0`` opts out of the count rule entirely.

        With no cap there is no "too many" signal to route on, and this body
        is far under ``forward_threshold`` — so this is the documented escape
        hatch back to the original unbounded behaviour.
        """
        parts = [f"第{i}句话内容" for i in range(1, 9)]
        ad = make_adapter({"max_bubbles_per_reply": 0})
        assert ad.max_bubbles_per_reply == 0
        res = await ad.send(str(DM_PEER), SEP.join(parts))
        assert res.success is True
        assert wire_messages(ad) == parts
        assert cards(ad) == []


# ---------------------------------------------------------------------------
# 2. Configuration surface
# ---------------------------------------------------------------------------


class TestBubbleCapConfig:
    def test_the_default_is_three(self):
        assert A.DEFAULT_MAX_BUBBLES_PER_REPLY == 3
        assert make_adapter().max_bubbles_per_reply == 3

    def test_it_reads_from_platform_extra(self):
        assert make_adapter({"max_bubbles_per_reply": 5}).max_bubbles_per_reply == 5

    def test_it_reads_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_MAX_BUBBLES_PER_REPLY", "4")
        assert make_adapter().max_bubbles_per_reply == 4

    def test_extra_wins_over_the_environment(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_MAX_BUBBLES_PER_REPLY", "9")
        assert make_adapter({"max_bubbles_per_reply": 2}).max_bubbles_per_reply == 2

    def test_a_junk_value_falls_back_to_the_default(self):
        assert make_adapter({"max_bubbles_per_reply": "abc"}).max_bubbles_per_reply == 3

    def test_the_key_is_registered_as_adapter_private(self):
        """Otherwise the gateway would reject or leak the YAML key."""
        assert "max_bubbles_per_reply" in A._PRIVATE_YAML_KEYS

    def test_the_env_var_is_declared_in_plugin_yaml(self):
        import pathlib

        import yaml

        manifest = pathlib.Path(A.__file__).with_name("plugin.yaml")
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        names = {e.get("name") for e in (data.get("optional_env") or [])}
        assert "ONEBOT_MAX_BUBBLES_PER_REPLY" in names


# ---------------------------------------------------------------------------
# 3. Wherever the cap merges, it must not break length splitting
# ---------------------------------------------------------------------------


class TestMergingStillRespectsTheLengthCeiling:
    """The cap limits notifications; ``chunk_text`` enforces the protocol.

    Every test here sets ``forward_threshold: 0`` purely to force the merge
    deterministically. Merging happens on the ``split_bubbles`` result and
    *before* ``chunk_text``, so a merged bubble that overshoots the
    per-message ceiling is still split. That ordering is load-bearing: the
    ceiling is a protocol constraint and the cap must not violate it.
    """

    @pytest.mark.asyncio
    async def test_an_oversized_merged_bubble_is_still_chunked(self):
        # Eight bubbles, each ~900 chars: individually under the ceiling, but
        # the merge of the last six is ~5400 and must not go out as one frame.
        parts = [f"{chr(ord('A') + i)}" * 900 for i in range(8)]
        ad = make_adapter({"forward_threshold": 0})  # no card, force chunking
        res = await ad.send(str(DM_PEER), SEP.join(parts))
        assert res.success is True

        msgs = wire_messages(ad)
        assert len(msgs) > 3, (
            "the merged bubble exceeds MAX_MESSAGE_LENGTH and must be split "
            f"further, but only {len(msgs)} messages went out"
        )
        for m in msgs:
            assert len(m) <= A.MAX_MESSAGE_LENGTH, (
                f"a {len(m)}-char frame exceeds the "
                f"{A.MAX_MESSAGE_LENGTH}-char protocol ceiling"
            )
        # Every character of every original bubble survived somewhere.
        blob = "".join(msgs)
        for i, original in enumerate(parts):
            assert original in blob, f"bubble {i} was lost or corrupted"

    @pytest.mark.asyncio
    async def test_the_chunks_of_a_merged_bubble_are_marked_as_continuations(self):
        """``chunk_text``'s ``(n/N)`` prefix still applies after merging."""
        parts = [f"{chr(ord('A') + i)}" * 900 for i in range(8)]
        ad = make_adapter({"forward_threshold": 0})
        await ad.send(str(DM_PEER), SEP.join(parts))
        msgs = wire_messages(ad)
        continuation = [m for m in msgs if m.startswith("(")]
        assert continuation, "a split bubble should carry (n/N) markers"

    @pytest.mark.asyncio
    async def test_a_merged_bubble_under_the_ceiling_stays_one_frame(self):
        """The counterpart: merging does not gratuitously split short text."""
        parts = [f"短句{i}" for i in range(8)]
        ad = make_adapter({"forward_threshold": 0})
        await ad.send(str(DM_PEER), SEP.join(parts))
        assert len(wire_messages(ad)) == 3


# ---------------------------------------------------------------------------
# 4. The two lanes, side by side: length OR count cards
# ---------------------------------------------------------------------------


LONG_BODY = "这是一个需要长篇解释的专业问题的回答内容。" * 60  # >1000 chars


class TestLongRepliesBecomeOneCard:
    """A considered answer goes out whole, in a single notification."""

    def test_the_long_fixture_really_is_over_the_threshold(self):
        """Guard the guard: every test below is vacuous if this drifts."""
        assert len(LONG_BODY) > A.FORWARD_TEXT_THRESHOLD

    @pytest.mark.asyncio
    async def test_a_long_dm_reply_is_exactly_one_card_and_nothing_else(self):
        ad = make_adapter()
        res = await ad.send(str(DM_PEER), LONG_BODY)
        assert res.success is True
        assert len(ad._client.actions) == 1, (
            "a long reply must be ONE notification, not loose messages plus a "
            f"card: {[type(a).__name__ for a in ad._client.actions]}"
        )
        assert isinstance(ad._client.actions[0], P.SendPrivateForwardMsg)

    @pytest.mark.asyncio
    async def test_no_plain_message_precedes_the_card_in_a_dm(self):
        """The old shape was 2 loose messages + 1 card.  Pinned dead."""
        ad = make_adapter()
        await ad.send(str(DM_PEER), LONG_BODY)
        assert not any(isinstance(a, P.SendPrivateMsg) for a in ad._client.actions)

    @pytest.mark.asyncio
    async def test_the_card_carries_every_character(self):
        ad = make_adapter()
        await ad.send(str(DM_PEER), LONG_BODY)
        assert "".join(card_node_texts(ad)) == LONG_BODY

    @pytest.mark.asyncio
    async def test_the_card_is_not_chunked_by_the_message_ceiling(self):
        """``chunk_text`` guards ``send_msg``; a card node is not one.

        The body here is well over ``MAX_MESSAGE_LENGTH`` and still travels as
        a single node with no ``(n/N)`` continuation prefix — the ceiling is a
        per-message limit on the plain-text action, and the forward action
        does not go through ``chunk_text`` at all.  Pinned so the distinction
        is not "fixed" by mistake.
        """
        body = "长" * (A.MAX_MESSAGE_LENGTH + 500)
        ad = make_adapter()
        await ad.send(str(DM_PEER), body)
        nodes = card_node_texts(ad)
        assert nodes == [body]
        assert not nodes[0].startswith("(")

    @pytest.mark.asyncio
    async def test_long_code_text_survives_one_dm_card_after_markdown_normalization(
        self,
    ):
        code = "print('ok')\n" * 100
        body = f"```python\n{code}```"
        expected = code.strip()
        assert len(expected) > A.FORWARD_TEXT_THRESHOLD

        ad = make_adapter()
        res = await ad.send(str(DM_PEER), body)

        assert res.success is True
        assert len(ad._client.actions) == 1
        assert isinstance(ad._client.actions[0], P.SendPrivateForwardMsg)
        delivered = "".join(card_node_texts(ad))
        assert delivered == expected
        assert "```" not in delivered
        assert SEP not in delivered


class TestTooManyBubblesBecomeCards:
    """Bubble COUNT overflow preserves every bubble in one forward card."""

    @pytest.mark.asyncio
    async def test_eight_short_bubbles_become_one_card(self):
        parts = [f"第{i}句话的内容大概二十个字左右" for i in range(1, 9)]
        body = SEP.join(parts)
        assert len(body) < A.FORWARD_TEXT_THRESHOLD, "must prove count alone routes"
        ad = make_adapter()
        res = await ad.send(str(DM_PEER), body)
        assert res.success is True
        assert len(cards(ad)) == 1
        assert not wire_messages(ad)

    @pytest.mark.asyncio
    async def test_eight_short_bubbles_keep_each_break_as_a_card_node(self):
        parts = [f"第{i}句话的内容大概二十个字左右" for i in range(1, 9)]
        ad = make_adapter()
        await ad.send(str(DM_PEER), SEP.join(parts))
        assert card_node_texts(ad) == parts

    @pytest.mark.asyncio
    async def test_no_character_or_break_marker_is_lost_or_leaked(self):
        parts = [f"第{i}句话的内容大概二十个字左右" for i in range(1, 9)]
        ad = make_adapter()
        await ad.send(str(DM_PEER), SEP.join(parts))
        nodes = card_node_texts(ad)
        blob = "".join(nodes)
        for original in parts:
            assert original in blob
        assert SEP not in blob

    @pytest.mark.asyncio
    async def test_a_custom_limit_routes_one_bubble_past_it_to_a_card(self):
        parts = ["先说这个", "再说那个", "还有这个"]
        ad = make_adapter({"max_bubbles_per_reply": 2})
        await ad.send(str(DM_PEER), SEP.join(parts))
        assert card_node_texts(ad) == parts

    @pytest.mark.asyncio
    async def test_count_overflow_becomes_a_card_in_groups_too(self):
        parts = [f"第{i}句话的内容大概二十个字左右" for i in range(1, 9)]
        ad = make_adapter()
        res = await ad.send(f"g{GROUP}", SEP.join(parts))
        assert res.success is True
        assert card_node_texts(ad) == parts

    @pytest.mark.asyncio
    async def test_content_that_is_both_long_and_many_bubbles_still_cards(self):
        """Length wins the priority: this is not a case cap_bubbles ever sees."""
        parts = [("很长的一段内容" * 20 + f"第{i}句") for i in range(1, 9)]
        body = SEP.join(parts)
        assert len(body) > A.FORWARD_TEXT_THRESHOLD
        ad = make_adapter()
        res = await ad.send(str(DM_PEER), body)
        assert res.success is True
        assert len(cards(ad)) == 1
        assert card_node_texts(ad) == parts, "the card preserves every bubble, uncapped"


class TestTheRoutingBoundaries:
    """Length and bubble count both route only when strictly over the limit."""

    @pytest.mark.asyncio
    async def test_a_body_exactly_at_the_length_threshold_stays_chat(self):
        body = "字" * A.FORWARD_TEXT_THRESHOLD
        ad = make_adapter()
        assert len(body) == ad.forward_threshold
        await ad.send(str(DM_PEER), body)
        assert cards(ad) == []
        assert wire_messages(ad) == [body]

    @pytest.mark.asyncio
    async def test_one_character_past_the_length_threshold_is_a_card(self):
        body = "字" * (A.FORWARD_TEXT_THRESHOLD + 1)
        ad = make_adapter()
        await ad.send(str(DM_PEER), body)
        assert len(cards(ad)) == 1
        assert card_node_texts(ad) == [body]

    @pytest.mark.asyncio
    async def test_exactly_the_bubble_limit_stays_chat_untouched(self):
        parts = ["一", "二", "三"]
        ad = make_adapter()
        await ad.send(str(DM_PEER), SEP.join(parts))
        assert cards(ad) == []
        assert wire_messages(ad) == parts

    @pytest.mark.asyncio
    async def test_one_bubble_past_the_limit_is_a_card(self):
        parts = ["一", "二", "三", "四"]
        ad = make_adapter()
        await ad.send(str(DM_PEER), SEP.join(parts))
        assert card_node_texts(ad) == parts


# ---------------------------------------------------------------------------
# 5. Degraded routes — where cap_bubbles still earns its keep
# ---------------------------------------------------------------------------


class TestDegradedRoutes:
    """With no card available, merging is what stops the flooding."""

    @pytest.mark.asyncio
    async def test_cards_disabled_falls_back_to_merging(self):
        parts = [f"第{i}句话内容" for i in range(1, 9)]
        ad = make_adapter({"forward_threshold": 0})
        res = await ad.send(str(DM_PEER), SEP.join(parts))
        assert res.success is True
        assert cards(ad) == [], "forward_threshold=0 must never emit a card"
        msgs = wire_messages(ad)
        assert len(msgs) == 3, "without the card route the cap must still bound it"
        assert msgs[2] == "\n".join(parts[2:])

    @pytest.mark.asyncio
    async def test_cards_disabled_still_sends_an_oversized_body_as_chunks(self):
        """No card lane left, so the protocol ceiling does the bounding."""
        body = "字" * (A.MAX_MESSAGE_LENGTH * 2)
        ad = make_adapter({"forward_threshold": 0})
        res = await ad.send(str(DM_PEER), body)
        assert res.success is True
        assert cards(ad) == []
        msgs = wire_messages(ad)
        assert len(msgs) > 1, "an oversized body must be split"
        for m in msgs:
            assert len(m) <= A.MAX_MESSAGE_LENGTH
        assert sum(m.count("字") for m in msgs) == len(body)

    @pytest.mark.asyncio
    async def test_a_refused_card_falls_back_to_bubbles_without_losing_text(self):
        """A bubble-overflow card refusal must cost notifications, never text."""
        client = FakeClient(
            responses=[
                {"status": "failed", "retcode": 1200, "message": "forward unsupported"},
            ]
        )
        parts = [f"第{i}句话内容" for i in range(1, 9)]
        body = SEP.join(parts)
        assert len(body) < A.FORWARD_TEXT_THRESHOLD, "count alone must trigger the card"
        ad = make_adapter(client=client)
        res = await ad.send(str(DM_PEER), body)
        assert res.success is True
        assert len(cards(ad)) == 1, "the card was attempted"
        plain = [a for a in client.actions if isinstance(a, P.SendPrivateMsg)]
        assert plain, "and then the content went out as plain messages"
        blob = "".join(wire_messages(ad))
        for original in parts:
            assert original in blob

    @pytest.mark.asyncio
    async def test_the_fallback_is_still_capped_not_unbounded(self):
        """Falling back must not reopen the original flooding bug."""
        client = FakeClient(
            responses=[
                {"status": "failed", "retcode": 1200, "message": "forward unsupported"},
            ]
        )
        parts = [f"第{i}句话内容" for i in range(1, 9)]
        body = SEP.join(parts)
        assert len(body) < A.FORWARD_TEXT_THRESHOLD, "count alone must trigger the card"
        ad = make_adapter(client=client)
        await ad.send(str(DM_PEER), body)
        plain = [a for a in client.actions if isinstance(a, P.SendPrivateMsg)]
        assert len(plain) == 3

    @pytest.mark.asyncio
    async def test_group_card_rejection_does_not_repeat_a_successful_lead(self):
        client = FakeClient(
            responses=[
                {"status": "ok", "retcode": 0, "data": {"message_id": 41}},
                {"status": "failed", "retcode": 1200, "message": "unsupported"},
            ]
        )
        parts = ["第一句", "第二句", "第三句", "第四句"]
        ad = make_adapter(client=client)
        res = await ad.send(
            f"g{GROUP}",
            SEP.join(parts),
            reply_to="55501",
            metadata={"onebot_at_user_id": "777"},
        )

        assert res.success is True
        assert isinstance(client.actions[0], P.SendGroupMsg)
        assert isinstance(client.actions[1], P.SendGroupForwardMsg)
        assert all(isinstance(action, P.SendGroupMsg) for action in client.actions[2:])
        assert sum(has_at_segment(ad, i) for i in range(len(client.actions))) == 1
        assert sum(has_reply_segment(ad, i) for i in range(len(client.actions))) == 1
        assert all(not has_at_segment(ad, i) for i in range(2, len(client.actions)))
        assert all(not has_reply_segment(ad, i) for i in range(2, len(client.actions)))
        fallback_text = "".join(wire_messages(ad)[1:])
        for part in parts:
            assert part in fallback_text

    @pytest.mark.asyncio
    async def test_group_card_rejection_without_a_lead_keeps_the_fallback_quote(self):
        client = FakeClient(
            responses=[
                {"status": "failed", "retcode": 1200, "message": "unsupported"},
            ]
        )
        parts = ["第一句", "第二句", "第三句", "第四句"]
        ad = make_adapter(client=client)
        res = await ad.send(f"g{GROUP}", SEP.join(parts), reply_to="55501")

        assert res.success is True
        assert isinstance(client.actions[0], P.SendGroupForwardMsg)
        assert isinstance(client.actions[1], P.SendGroupMsg)
        assert has_reply_segment(ad, 1) is True
        assert "第一句" in wire_messages(ad)[0]

    @pytest.mark.asyncio
    async def test_nonterminal_lead_failure_retries_the_lead_on_chat_fallback(self):
        client = FakeClient(
            responses=[
                {"status": "failed", "retcode": 1200, "message": "send failed"},
            ]
        )
        parts = ["第一句", "第二句", "第三句", "第四句"]
        ad = make_adapter(client=client)
        res = await ad.send(
            f"g{GROUP}",
            SEP.join(parts),
            reply_to="55501",
            metadata={"onebot_at_user_id": "777"},
        )

        assert res.success is True
        assert cards(ad) == [], "a failed lead must stop the card attempt"
        assert has_at_segment(ad, 1) is True
        assert has_reply_segment(ad, 1) is True
        assert "第一句" in wire_messages(ad)[1]

    @pytest.mark.asyncio
    async def test_terminal_lead_failure_keeps_its_failure_classification(self):
        client = FakeClient(
            responses=[
                {"status": "failed", "retcode": 1403, "message": "forbidden"},
            ]
        )
        ad = make_adapter(client=client)
        res = await ad.send(
            f"g{GROUP}",
            SEP.join(["第一句", "第二句", "第三句", "第四句"]),
            reply_to="55501",
            metadata={"onebot_at_user_id": "777"},
        )

        assert res.success is False
        assert res.error_kind == "forbidden"
        assert len(client.actions) == 1
        assert cards(ad) == []

    @pytest.mark.asyncio
    async def test_terminal_card_failure_after_a_lead_does_not_fall_back(self):
        client = FakeClient(
            responses=[
                {"status": "ok", "retcode": 0, "data": {"message_id": 41}},
                {"status": "failed", "retcode": 1403, "message": "forbidden"},
            ]
        )
        ad = make_adapter(client=client)
        res = await ad.send(
            f"g{GROUP}",
            SEP.join(["第一句", "第二句", "第三句", "第四句"]),
            reply_to="55501",
            metadata={"onebot_at_user_id": "777"},
        )

        assert res.success is False
        assert res.error_kind == "forbidden"
        assert len(client.actions) == 2
        assert isinstance(client.actions[0], P.SendGroupMsg)
        assert isinstance(client.actions[1], P.SendGroupForwardMsg)

    @pytest.mark.asyncio
    async def test_dm_card_rejection_keeps_the_quote_on_the_chat_fallback(self):
        client = FakeClient(
            responses=[
                {"status": "failed", "retcode": 1200, "message": "unsupported"},
            ]
        )
        parts = ["第一句", "第二句", "第三句", "第四句"]
        ad = make_adapter(client=client)
        res = await ad.send(str(DM_PEER), SEP.join(parts), reply_to="55501")

        assert res.success is True
        assert isinstance(client.actions[0], P.SendPrivateForwardMsg)
        assert isinstance(client.actions[1], P.SendPrivateMsg)
        assert has_reply_segment(ad, 1) is True
        assert sum(has_reply_segment(ad, i) for i in range(len(client.actions))) == 1
        fallback_text = "".join(wire_messages(ad))
        for part in parts:
            assert part in fallback_text

    @pytest.mark.asyncio
    async def test_a_dead_target_is_reported_rather_than_retried_as_chunks(self):
        client = FakeClient(
            responses=[
                {"status": "failed", "retcode": 1404, "message": "unknown target"},
            ]
        )
        ad = make_adapter(client=client)
        res = await ad.send(str(DM_PEER), LONG_BODY)
        assert res.success is False
        assert res.error_kind == "not_found"
        assert not any(isinstance(a, P.SendPrivateMsg) for a in client.actions)


class TestTheContentGateStillPrecedesTheRouting:
    """SECURITY: routing must not become a way around the policy gate.

    ``plugins/qzone/policy.py`` is what stands between a bad sentence and a
    frozen QQ account.  The card lane is new code placed after the gate, and
    these pin that ordering — a refused reply must reach the wire by NO lane,
    including the ones that did not exist when the gate was written.
    """

    def test_the_blocked_fixture_is_actually_blocked(self):
        cfg = POLICY.resolve_config(None)
        assert POLICY.moderate_text(BLOCKED_TEXT, cfg).decision.allowed is False

    @pytest.mark.asyncio
    async def test_a_long_refused_reply_reaches_the_wire_nowhere(self):
        body = BLOCKED_TEXT + "。" + LONG_BODY
        assert len(body) > A.FORWARD_TEXT_THRESHOLD
        ad = make_adapter()
        res = await ad.send(str(DM_PEER), body)
        assert res.success is False
        assert ad._client.actions == [], "refused text escaped through the card lane"

    @pytest.mark.asyncio
    async def test_a_many_bubble_refused_reply_reaches_the_wire_nowhere(self):
        body = SEP.join([BLOCKED_TEXT] + [f"第{i}句话内容" for i in range(1, 9)])
        assert len(body) < A.FORWARD_TEXT_THRESHOLD
        ad = make_adapter()
        res = await ad.send(str(DM_PEER), body)
        assert res.success is False
        assert ad._client.actions == []

    @pytest.mark.asyncio
    async def test_a_refused_group_reply_sends_no_card_lead_line_either(self):
        """The group lead line goes out BEFORE the card — also gated."""
        body = BLOCKED_TEXT + "。" + LONG_BODY
        ad = make_adapter()
        res = await ad.send(f"g{GROUP}", body, metadata={"onebot_at_user_id": "777"})
        assert res.success is False
        assert ad._client.actions == []


# ---------------------------------------------------------------------------
# 6. Regression guard: the quote / @mention behaviour is UNCHANGED
# ---------------------------------------------------------------------------


class TestLeadSegmentsAreUnaffected:
    """The reply-quote card was deliberately kept — pinned here verbatim.

    ``_lead_segments`` was not touched by any of this work, but the routing
    around it was rewritten twice, so these guard the invariant rather than
    the implementation: the quote and the @mention ride the FIRST outgoing
    message and nothing else.
    """

    @pytest.mark.asyncio
    async def test_only_the_first_message_carries_the_quote(self):
        parts = ["先说这个", "再说那个", "还有这个"]
        ad = make_adapter()
        res = await ad.send(str(DM_PEER), SEP.join(parts), reply_to="55501")
        assert res.success is True
        assert len(ad._client.actions) == 3
        assert has_reply_segment(ad, 0) is True
        assert has_reply_segment(ad, 1) is False
        assert has_reply_segment(ad, 2) is False

    @pytest.mark.asyncio
    async def test_the_quote_id_is_the_one_the_caller_passed(self):
        ad = make_adapter()
        await ad.send(str(DM_PEER), "一句话", reply_to="55501")
        quote = next(s for s in segments_of(ad, 0) if isinstance(s, P.ReplySegment))
        assert quote.id == "55501"

    @pytest.mark.asyncio
    async def test_only_the_first_message_carries_the_mention(self):
        parts = ["先说这个", "再说那个", "还有这个"]
        ad = make_adapter()
        res = await ad.send(
            f"g{GROUP}",
            SEP.join(parts),
            reply_to="55501",
            metadata={"onebot_at_user_id": "777"},
        )
        assert res.success is True
        assert len(ad._client.actions) == 3
        assert has_at_segment(ad, 0) is True
        assert has_reply_segment(ad, 0) is True
        for i in (1, 2):
            assert has_at_segment(ad, i) is False
            assert has_reply_segment(ad, i) is False

    @pytest.mark.asyncio
    async def test_a_reply_without_an_anchor_has_no_quote_at_all(self):
        parts = ["先说这个", "再说那个", "还有这个"]
        ad = make_adapter()
        await ad.send(str(DM_PEER), SEP.join(parts))
        for i in range(len(ad._client.actions)):
            assert has_reply_segment(ad, i) is False

    @pytest.mark.asyncio
    async def test_the_merged_bubble_on_the_degraded_path_has_no_lead_segments(self):
        """With cards off the cap still merges — the merged one stays plain."""
        parts = [f"第{i}句话内容" for i in range(1, 9)]
        ad = make_adapter({"forward_threshold": 0})
        await ad.send(
            f"g{GROUP}",
            SEP.join(parts),
            reply_to="55501",
            metadata={"onebot_at_user_id": "777"},
        )
        assert has_at_segment(ad, 0) is True
        assert has_reply_segment(ad, 0) is True
        assert segments_of(ad, 2) == [
            P.TextSegment(text="\n".join(parts[2:])),
        ]

    @pytest.mark.asyncio
    async def test_the_group_card_lead_line_carries_the_quote_and_mention(self):
        """A card cannot hold an @, so the ping rides the lead line before it.

        That lead line is the first outgoing message, so it is exactly where
        the quote belongs too — the invariant holds on the card lane as well.
        """
        ad = make_adapter()
        res = await ad.send(
            f"g{GROUP}",
            "很长的回答" * 300,
            reply_to="55501",
            metadata={"onebot_at_user_id": "777"},
        )
        assert res.success is True
        assert len(ad._client.actions) == 2
        assert isinstance(ad._client.actions[0], P.SendGroupMsg)
        assert has_at_segment(ad, 0) is True
        assert has_reply_segment(ad, 0) is True
        assert A.FORWARD_LEAD_TEXT in wire_messages(ad)[0]
        assert isinstance(ad._client.actions[1], P.SendGroupForwardMsg)

    @pytest.mark.asyncio
    async def test_a_dm_card_needs_no_lead_line_at_all(self):
        """One notification, which is the whole point of the card lane.

        A DM has no @mention to deliver, so nothing precedes the card.  The
        ``reply_to`` anchor is therefore unused on this path — documented
        here rather than silently.
        """
        ad = make_adapter()
        await ad.send(str(DM_PEER), "很长的回答" * 300, reply_to="55501")
        assert len(ad._client.actions) == 1
        assert isinstance(ad._client.actions[0], P.SendPrivateForwardMsg)
