"""The inbound gate: what the room says must not end up in what the bot says.

The outbound gates decide what leaves. This one decides what gets *stored*,
and the distinction is the whole point of the module.

A bot cannot be frozen for what it reads. It can very easily be led into
saying something it will be frozen for, and the mechanism is not subtle: three
separate stores take inbound text, and two of them are replayed back into the
model later.

    1. the turn        — the message being answered
    2. the room buffer — last 30 lines, replayed into every later prompt
    3. the archive     — 24h window, read back and **summarised** by cron

Declining to answer a blocked message closes (1) and leaves (2) and (3) wide
open: the text is still sitting in both stores, and the next innocuous "在吗"
replays it into the prompt while the summariser is free to restate it in a
message the bot sends itself. So the gate redacts at the point of *write*, not
at the point of use.

These tests pin all three, plus the two behavioural choices that are easy to
get backwards: groups get silence rather than a refusal, and the DM notice is
rate-limited so it cannot be farmed into a metronome.
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
from plugins.platforms.onebot.inbound_gate import (
    REDACTION_PLACEHOLDER,
    InboundGateConfig,
    NoticeCooldown,
    resolve_inbound_gate_config,
    screen_inbound,
)
from plugins.qzone import policy as POLICY

GROUP = 183287894
DM_PEER = 536132102
KEYWORD = "格兰"

BLOCKED = "打倒习近平"
BENIGN = "今天天气不错，一起去吃饭吗"


def test_the_fixtures_are_what_this_file_claims():
    assert POLICY.classify_text_all(BLOCKED).allowed is False
    assert POLICY.classify_text_all(BENIGN).allowed is True


# ---------------------------------------------------------------------------
# The screening function on its own
# ---------------------------------------------------------------------------


class TestScreenInbound:
    def test_benign_text_passes_through_untouched(self):
        verdict = screen_inbound(BENIGN)
        assert verdict.allowed is True
        assert verdict.text == BENIGN

    def test_blocked_text_becomes_the_placeholder(self):
        verdict = screen_inbound(BLOCKED)
        assert verdict.allowed is False
        assert verdict.text == REDACTION_PLACEHOLDER
        assert BLOCKED not in verdict.text
        assert verdict.category_codes

    def test_the_safe_text_is_always_usable(self):
        """Callers use ``.text`` unconditionally, so it is never None/empty."""
        for probe in (BENIGN, BLOCKED, "", "   "):
            assert isinstance(screen_inbound(probe).text, str)

    def test_it_fails_closed(self, monkeypatch):
        """An exploding classifier redacts; it never becomes an open gate."""

        def _boom(*_a, **_k):
            raise RuntimeError("classifier exploded")

        monkeypatch.setattr(
            "plugins.platforms.onebot.inbound_gate.content_policy.classify_text_all",
            _boom,
        )
        verdict = screen_inbound(BENIGN)
        assert verdict.allowed is False
        assert verdict.text == REDACTION_PLACEHOLDER

    def test_the_master_switch_disables_it(self):
        cfg = InboundGateConfig(enabled=False)
        assert screen_inbound(BLOCKED, cfg).text == BLOCKED

    def test_audit_fields_never_include_the_message(self):
        fields = screen_inbound(BLOCKED).audit_fields()
        assert BLOCKED not in repr(fields)

    def test_config_fails_safe_to_on(self):
        for junk in (None, "nonsense", 42, object()):
            cfg = resolve_inbound_gate_config(junk)
            assert cfg.enabled is True
        assert resolve_inbound_gate_config({"enabled": "false"}).enabled is True
        assert resolve_inbound_gate_config({"enabled": False}).enabled is False


class TestNoticeCooldown:
    def test_it_allows_once_then_suppresses(self):
        cd = NoticeCooldown()
        assert cd.should_notify("u1", now=0.0, cooldown_secs=300.0) is True
        assert cd.should_notify("u1", now=10.0, cooldown_secs=300.0) is False
        assert cd.should_notify("u1", now=400.0, cooldown_secs=300.0) is True

    def test_users_do_not_share_a_cooldown(self):
        cd = NoticeCooldown()
        assert cd.should_notify("u1", now=0.0, cooldown_secs=300.0) is True
        assert cd.should_notify("u2", now=0.0, cooldown_secs=300.0) is True

    def test_it_is_bounded(self):
        """A months-long gateway must not keep one stamp per person, forever."""
        cd = NoticeCooldown(limit=8)
        for i in range(100):
            cd.should_notify(f"u{i}", now=float(i), cooldown_secs=300.0)
        assert len(cd._seen) <= 8


# ---------------------------------------------------------------------------
# Wired into the adapter
# ---------------------------------------------------------------------------


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


class Turn:
    """Synchronous stand-in for ``handle_message``; records what got through."""

    def __init__(self) -> None:
        self.events: List[MessageEvent] = []

    async def __call__(self, event: MessageEvent) -> None:
        self.events.append(event)


class FakeHistoryWriter:
    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []

    def record(self, **kwargs: Any) -> None:
        self.rows.append(kwargs)


def make_adapter(extra: Optional[Dict[str, Any]] = None) -> A.OneBotAdapter:
    base: Dict[str, Any] = {
        "ws_url": "ws://127.0.0.1:3001",
        "group_replies_enabled": True,
        "group_reply_policy": "all",
    }
    base.update(extra or {})
    ad = A.OneBotAdapter(PlatformConfig(enabled=True, extra=base))
    ad._client = FakeClient()
    ad._running = True
    ad._semaphore = None  # run the turn inline, no background race
    ad._account_online = True
    ad._group_names[GROUP] = "测试群"
    ad.router.group_replies_enabled = True
    ad.turns = Turn()
    ad.handle_message = ad.turns
    ad._history_writer = FakeHistoryWriter()
    return ad


def group_event(text, *, uid=555, message_id=1):
    return P.MessageEvent(
        self_id=100, message_type=P.MessageType.GROUP, sub_type="normal",
        group_id=GROUP, user_id=uid, message_id=message_id,
        message=[P.TextSegment(text=text)], raw_message=text,
        time=1_700_000_000,
        sender=P.Sender(user_id=uid, nickname="alice"),
    )


def private_event(text, *, uid=DM_PEER, message_id=1):
    return P.MessageEvent(
        self_id=100, message_type=P.MessageType.PRIVATE, sub_type="friend",
        group_id=None, user_id=uid, message_id=message_id,
        message=[P.TextSegment(text=text)], raw_message=text, time=1,
        sender=P.Sender(user_id=uid, nickname="bob"),
    )


def wire_text(ad: A.OneBotAdapter) -> str:
    out: List[str] = []
    for action in ad._client.actions:
        for seg in getattr(action, "message", []) or []:
            text = getattr(seg, "text", None)
            if text:
                out.append(text)
    return "".join(out)


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


class TestTheTurnIsStopped:
    @pytest.mark.asyncio
    async def test_a_blocked_group_message_runs_no_turn(self):
        ad = make_adapter()
        await ad._on_message_event(group_event(BLOCKED))
        assert ad.turns.events == []

    @pytest.mark.asyncio
    async def test_a_benign_group_message_still_runs_one(self):
        ad = make_adapter()
        await ad._on_message_event(group_event(BENIGN))
        assert len(ad.turns.events) == 1

    @pytest.mark.asyncio
    async def test_a_blocked_dm_runs_no_turn(self):
        ad = make_adapter()
        await ad._on_message_event(private_event(BLOCKED))
        assert ad.turns.events == []


class TestTheStoresAreRedacted:
    """The half that answering-or-not cannot fix."""

    @pytest.mark.asyncio
    async def test_the_room_buffer_never_holds_the_raw_text(self):
        ad = make_adapter()
        await ad._on_message_event(group_event(BLOCKED))
        buffered = A.recent_group_messages(ad.instance_id, GROUP)
        assert buffered, "the message should still be recorded, just redacted"
        bodies = [row[2] for row in buffered]
        assert BLOCKED not in "".join(bodies)
        assert REDACTION_PLACEHOLDER in "".join(bodies)

    @pytest.mark.asyncio
    async def test_the_archive_never_holds_the_raw_text(self):
        ad = make_adapter()
        await ad._on_message_event(group_event(BLOCKED))
        rows = ad._history_writer.rows
        assert rows, "a blocked message is still archived, redacted"
        assert BLOCKED not in "".join(str(r.get("text")) for r in rows)
        assert REDACTION_PLACEHOLDER in "".join(str(r.get("text")) for r in rows)

    @pytest.mark.asyncio
    async def test_benign_traffic_is_stored_verbatim(self):
        ad = make_adapter()
        await ad._on_message_event(group_event(BENIGN))
        buffered = A.recent_group_messages(ad.instance_id, GROUP)
        assert BENIGN in "".join(row[2] for row in buffered)
        assert REDACTION_PLACEHOLDER not in "".join(row[2] for row in buffered)

    @pytest.mark.asyncio
    async def test_a_blocked_message_cannot_reach_a_later_prompt(self):
        """The end-to-end version: bait, then an innocent follow-up."""
        ad = make_adapter()
        await ad._on_message_event(group_event(BLOCKED, message_id=1))
        await ad._on_message_event(group_event(BENIGN, message_id=2))
        assert len(ad.turns.events) == 1
        # Whatever room context the surviving turn carries, the bait is not in it.
        assert BLOCKED not in repr(ad.turns.events[-1].__dict__)


class TestGroupsGetSilence:
    @pytest.mark.asyncio
    async def test_no_refusal_is_posted_to_a_group(self):
        """Refusing out loud attaches the bot to the message, publicly."""
        ad = make_adapter()
        await ad._on_message_event(group_event(BLOCKED))
        assert ad._client.actions == []

    @pytest.mark.asyncio
    async def test_a_dm_gets_one_notice(self):
        ad = make_adapter()
        await ad._on_message_event(private_event(BLOCKED))
        assert POLICY.QQ_SAFE_REFUSAL_TEXT in wire_text(ad)

    @pytest.mark.asyncio
    async def test_the_dm_notice_is_rate_limited(self):
        """A spammer must not be able to farm the refusal into a metronome."""
        ad = make_adapter()
        for i in range(5):
            await ad._on_message_event(private_event(BLOCKED, message_id=i + 1))
        assert wire_text(ad).count(POLICY.QQ_SAFE_REFUSAL_TEXT) == 1

    @pytest.mark.asyncio
    async def test_the_dm_notice_can_be_switched_off(self):
        ad = make_adapter({"inbound_gate": {"dm_notice": False}})
        await ad._on_message_event(private_event(BLOCKED))
        assert ad._client.actions == []

    @pytest.mark.asyncio
    async def test_the_whole_gate_can_be_switched_off(self):
        ad = make_adapter({"inbound_gate": {"enabled": False}})
        await ad._on_message_event(group_event(BLOCKED))
        assert len(ad.turns.events) == 1
