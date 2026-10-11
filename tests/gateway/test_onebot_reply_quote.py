"""A QQ answer points at the message it is answering.

``_lead_segments`` has always known how to emit a ``reply`` segment, and the
gateway has always threaded a ``reply_to`` argument down to ``send`` — but on
this lane nothing ever set one.  The result in a group was an answer floating
free: "对，那个包得先装" arriving four messages after the question it belongs
to, with nothing tying the two together.  The @mention says *who* the answer
is for; the quote says *what* it is about.

The anchor travels as inbound-event metadata (``REPLY_ANCHOR_META``) rather
than as a new argument on the gateway's send signature, which is what confines
it to the reply lane — and these tests pin that confinement, because a
proactive post or a cron delivery that quoted some older message would be the
bot answering a question nobody had just asked.

Fake client, no sockets, no model call.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List, Optional

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import MessageEvent

from plugins.platforms.onebot import adapter as A
from plugins.platforms.onebot import protocol as P


GROUP = 149881991
CHAT = f"g{GROUP}"
TRIGGER_ID = 4242


class FakeClient:
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


def make_adapter(extra: Optional[Dict[str, Any]] = None) -> A.OneBotAdapter:
    base: Dict[str, Any] = {
        "ws_url": "ws://127.0.0.1:3001",
        "group_replies_enabled": True,
        "max_bubbles_per_reply": 3,
        "forward_threshold": 1000,
    }
    base.update(extra or {})
    ad = A.OneBotAdapter(PlatformConfig(enabled=True, extra=base))
    ad._client = FakeClient()
    ad._running = True
    ad._semaphore = asyncio.Semaphore(4)
    ad._account_online = True
    ad._group_names[GROUP] = "测试群"
    ad.router.group_replies_enabled = True
    return ad


def group_event(text="格兰 在吗", *, uid=555, message_id=TRIGGER_ID):
    return P.MessageEvent(
        self_id=100, message_type=P.MessageType.GROUP, sub_type="normal",
        group_id=GROUP, user_id=uid, message_id=message_id,
        message=[P.TextSegment(text=text)], raw_message=text,
        time=1_700_000_000,
        sender=P.Sender(user_id=uid, nickname="alice"),
    )


def quotes(ad: A.OneBotAdapter) -> List[str]:
    """Every ``reply`` segment id this adapter put on the wire, in order."""
    out: List[str] = []
    for action in ad._client.actions:
        for seg in getattr(action, "message", None) or ():
            if isinstance(seg, P.ReplySegment):
                out.append(seg.id)
    return out


TURN_META = {"onebot_at_user_id": "555", A.REPLY_ANCHOR_META: str(TRIGGER_ID)}


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
    monkeypatch.setenv("ONEBOT_STICKER_PROBABILITY", "0")


# ── the mode ──────────────────────────────────────────────────────────────


def test_quoting_is_on_by_default():
    assert A.reply_anchor_mode({}) == "first"


@pytest.mark.parametrize("raw", ["off", "OFF", " off "])
def test_off_is_off(raw):
    assert A.reply_anchor_mode({"reply_to_mode": raw}) == "off"


@pytest.mark.parametrize("raw", ["first", "all", "", None, "nonsense"])
def test_everything_else_quotes_the_first_chunk(raw):
    """``all`` is accepted for parity with Discord, not honoured: QQ draws a
    quote as an inset block, so one per bubble triples the answer's height."""
    assert A.reply_anchor_mode({"reply_to_mode": raw}) == "first"


def test_env_is_the_fallback_source(monkeypatch):
    monkeypatch.setenv("ONEBOT_REPLY_TO_MODE", "off")
    assert A.reply_anchor_mode({}) == "off"
    assert A.reply_anchor_mode({"reply_to_mode": "first"}) == "first"


# ── the anchor ────────────────────────────────────────────────────────────


def test_the_inbound_event_carries_the_id_to_quote():
    ad = make_adapter()
    event = asyncio.run(ad._build_message_event(group_event(), "格兰 在吗"))
    assert event.metadata[A.REPLY_ANCHOR_META] == str(TRIGGER_ID)


def test_an_explicit_reply_to_wins_over_the_turn_anchor():
    ad = make_adapter()
    assert ad._reply_anchor("999", TURN_META) == "999"


def test_off_suppresses_even_a_caller_supplied_anchor():
    ad = make_adapter({"reply_to_mode": "off"})
    assert ad._reply_anchor("999", TURN_META) is None


def test_a_send_with_no_turn_metadata_quotes_nothing():
    ad = make_adapter()
    assert ad._reply_anchor(None, {}) is None


# ── on the wire ───────────────────────────────────────────────────────────


def test_a_group_answer_quotes_the_message_it_answers():
    ad = make_adapter()
    asyncio.run(ad.send(CHAT, "装一下就好了", metadata=TURN_META))
    assert quotes(ad) == [str(TRIGGER_ID)]


def test_only_the_first_bubble_carries_the_quote():
    ad = make_adapter()
    asyncio.run(ad.send(CHAT, "嗷[MSG_BREAK]装一下[MSG_BREAK]就好了", metadata=TURN_META))
    assert quotes(ad) == [str(TRIGGER_ID)], "one quote for a three-bubble reply"


def test_off_puts_no_quote_on_the_wire():
    ad = make_adapter({"reply_to_mode": "off"})
    asyncio.run(ad.send(CHAT, "装一下就好了", metadata=TURN_META))
    assert quotes(ad) == []


def test_a_live_config_reconcile_switches_quoting_off():
    ad = make_adapter()
    ad.config.extra["reply_to_mode"] = "off"
    asyncio.run(ad.send(CHAT, "装一下就好了", metadata=TURN_META))
    assert quotes(ad) == []


def test_a_dm_answer_quotes_too():
    ad = make_adapter()
    asyncio.run(ad.send("536132102", "好", metadata={A.REPLY_ANCHOR_META: "77"}))
    assert quotes(ad) == ["77"]


def test_a_proactive_post_quotes_nothing():
    """It is not answering anything — a quote would invent a question."""
    ad = make_adapter()
    asyncio.run(ad.send(CHAT, "在干嘛呢"))
    assert quotes(ad) == []


def test_a_card_lead_carries_the_quote_and_the_card_does_not():
    ad = make_adapter({"forward_threshold": 20})
    asyncio.run(ad.send(CHAT, "很长的一段回答" * 10, metadata=TURN_META))
    cards = [
        a for a in ad._client.actions
        if isinstance(a, (P.SendGroupForwardMsg, P.SendPrivateForwardMsg))
    ]
    assert cards, "this reply must have gone out as a card"
    assert quotes(ad) == [str(TRIGGER_ID)], "the lead line quotes; the card cannot"
