"""``bubble_overflow: merge`` — over-fragmented replies capped, not carded.

D88 routes a reply split into more than ``max_bubbles_per_reply`` bubbles down
the answer lane as one merged-forward card, on the grounds that merging the
overflow produces "two short messages followed by one bloated slab".

That reasoning is a property of the *persona*, and one OneBot account now
serves channels bound to different ones.  For a character whose bubbles run
about six characters, merging bubbles 3 and 4 yields something still shorter
than one ordinary sentence — while a forward card reads as a document and
destroys the illusion outright.  Prose in the persona file does not fix this:
the model wrote four bubbles anyway, every time, and the transport turned all
of them into cards.

So ``extra.bubble_overflow: {"<chat id>": "merge"}`` flips the *fragmentation*
branch for that channel only.  Length is deliberately untouched: a reply over
``forward_threshold`` is still a card everywhere, because a wall of text in a
group is a real reading problem no persona is served by.

What these tests pin:

* an unconfigured channel keeps D88's behaviour exactly — a card;
* a ``merge`` channel gets ``max_bubbles_per_reply`` real chat messages with
  the overflow folded into the last one, and no card at all;
* ``merge`` does NOT rescue an over-long reply from the card;
* the flag is per-channel, matches with or without the ``g`` prefix, and an
  unknown value is not "merge";
* a reply already within the cap is untouched on either setting.

Runs against the same fake client as ``test_onebot_bubble_cap`` — no sockets,
no NapCat, no model call.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List, Optional

import pytest

from gateway.config import PlatformConfig

from plugins.platforms.onebot import adapter as A
from plugins.platforms.onebot import protocol as P


GROUP = 894800697
CHAT = f"g{GROUP}"
MERGE_EXTRA: Dict[str, Any] = {"bubble_overflow": {str(GROUP): "merge"}}

FOUR = "嗷？[MSG_BREAK]为啥突然叫我鸭[MSG_BREAK]我哪有多嘴嘛（）[MSG_BREAK]好叭……"


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


def cards(ad: A.OneBotAdapter) -> List[Any]:
    return [
        a
        for a in ad._client.actions
        if isinstance(a, (P.SendPrivateForwardMsg, P.SendGroupForwardMsg))
    ]


def wire_messages(ad: A.OneBotAdapter) -> List[str]:
    out: List[str] = []
    for a in ad._client.actions:
        segments = getattr(a, "message", None)
        if segments is None or isinstance(a, (P.SendGroupForwardMsg, P.SendPrivateForwardMsg)):
            continue
        text = "".join(getattr(s, "text", "") or "" for s in segments)
        if text:
            out.append(text)
    return out


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


# ── the flag itself ───────────────────────────────────────────────────────


@pytest.mark.parametrize("chat_id", [str(GROUP), CHAT])
def test_flag_matches_with_and_without_the_g_prefix(chat_id):
    assert A.bubble_overflow_is_merge(MERGE_EXTRA, chat_id) is True


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"bubble_overflow": {}},
        {"bubble_overflow": {"149881991": "merge"}},
        {"bubble_overflow": {str(GROUP): "card"}},
        {"bubble_overflow": {str(GROUP): "yes"}},
        {"bubble_overflow": "merge"},  # not a map
    ],
)
def test_anything_else_is_not_merge(extra):
    assert A.bubble_overflow_is_merge(extra, CHAT) is False


def test_no_chat_id_is_not_merge():
    assert A.bubble_overflow_is_merge(MERGE_EXTRA, None) is False


# ── routing ───────────────────────────────────────────────────────────────


def test_unconfigured_channel_still_gets_a_card():
    ad = make_adapter()
    asyncio.run(ad.send(CHAT, FOUR))
    assert len(cards(ad)) == 1, "D88 behaviour must be untouched where unset"


def test_merge_channel_gets_capped_bubbles_and_no_card():
    ad = make_adapter(MERGE_EXTRA)
    asyncio.run(ad.send(CHAT, FOUR))
    assert cards(ad) == []
    msgs = wire_messages(ad)
    assert len(msgs) == 3, msgs
    assert msgs[0] == "嗷？"
    assert msgs[1] == "为啥突然叫我鸭"
    # Overflow folded into the last surviving bubble, never dropped.
    assert msgs[2] == "我哪有多嘴嘛（）\n好叭……"


def test_merge_does_not_rescue_an_over_long_reply_from_the_card():
    ad = make_adapter({**MERGE_EXTRA, "forward_threshold": 20})
    asyncio.run(ad.send(CHAT, FOUR))
    assert len(cards(ad)) == 1, "length must still win a card"


def test_reply_within_the_cap_is_untouched_under_merge():
    ad = make_adapter(MERGE_EXTRA)
    asyncio.run(ad.send(CHAT, "嗷？[MSG_BREAK]好叭"))
    assert cards(ad) == []
    msgs = wire_messages(ad)
    assert msgs == ["嗷？", "好叭"]
    assert not any("\n" in m for m in msgs)


def test_another_channel_on_the_same_adapter_still_cards():
    ad = make_adapter(MERGE_EXTRA)
    ad._group_names[149881991] = "别的群"
    asyncio.run(ad.send("g149881991", FOUR))
    assert len(cards(ad)) == 1


def test_no_msg_break_marker_survives_the_merge():
    ad = make_adapter(MERGE_EXTRA)
    asyncio.run(ad.send(CHAT, FOUR))
    assert not any(A.BUBBLE_SEPARATOR in m for m in wire_messages(ad))
