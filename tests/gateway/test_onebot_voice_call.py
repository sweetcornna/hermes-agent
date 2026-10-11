"""Call-shaped voice sessions for the OneBot (QQ) adapter.

Everything here is offline and runs on a frozen clock: the module under test
has no asyncio loop, no I/O and never reads a wall clock, so every deadline is
exercised by handing :func:`voice_call.sweep` an explicit ``now``.  Nothing in
this file can reach NapCat, a socket, or a model.

Five properties get their own tests because getting them wrong is silent in
development and expensive on a real person's phone:

* **ambiguous words are not consent.**  Starting to talk at somebody who never
  agreed is the costly error; making them repeat themselves is cheap, so the
  accept classifier is strict and the decline/hangup ones are generous;
* **a keyword buried in a longer sentence matches nothing** — the same
  protection ``proactive.is_skip`` has, where ``"skip今天不聊"`` must not read
  as ``SKIP``;
* **every call ends on its own.**  Ring, idle, max-duration and max-turns are
  pinned independently, each firing one tick past its deadline and staying
  quiet one tick before it;
* **the registry key is the adapter's own** ``speech_key``, not a second
  scheme that would silently fork per-chat state;
* **the feature ships off**, and a group call needs a second, deliberate
  opt-in on top of that.
"""

from __future__ import annotations

from typing import Any, List, Optional

import pytest

from plugins.platforms.onebot import adapter as A
from plugins.platforms.onebot import voice_call as VC


INSTANCE = "onebot"
DM_CHAT = "1114483029"
GROUP_CHAT = "g183287894"
PEER = "1114483029"

KEY = VC.speech_key(INSTANCE, DM_CHAT)
GROUP_KEY = VC.speech_key(INSTANCE, GROUP_CHAT)

#: Frozen monotonic origin, and the smallest step used to straddle a deadline.
T0 = 1_000.0
TICK = 1.0


def cfg(**over: Any) -> VC.CallConfig:
    """A resolved config with the shipped defaults, overridable per test."""
    base = dict(
        ring_timeout_secs=60.0,
        idle_timeout_secs=180.0,
        max_duration_secs=900.0,
        max_turns=40,
        allow_groups=False,
        allowed_users=(),
    )
    base.update(over)
    return VC.CallConfig(**base)  # type: ignore[arg-type]


def place_call(
    now: float = T0, config: Optional[VC.CallConfig] = None
) -> VC.CallDecision:
    return VC.invite(
        KEY,
        chat_id=DM_CHAT,
        is_group=False,
        peer=PEER,
        now=now,
        cfg=config or cfg(),
    )


def connected(
    now: float = T0, config: Optional[VC.CallConfig] = None
) -> VC.CallSession:
    """An ACTIVE session: invited at ``now``, answered at ``now``."""
    place_call(now, config)
    decision = VC.accept(KEY, now)
    assert decision.session is not None
    return decision.session


@pytest.fixture(autouse=True)
def _reset_state():
    # ``adapter._reset_module_state`` does not know about this module yet —
    # wiring that up belongs to the subtask that owns adapter.py — so the
    # registry is cleared directly here.
    VC.reset_state()
    yield
    VC.reset_state()


# ---------------------------------------------------------------------------
# 1. Keying
# ---------------------------------------------------------------------------


class TestKeying:
    def test_key_is_the_adapters_own_speech_key(self):
        """Not a lookalike — literally the same function.

        ``proactive`` keys its budget with this too.  A second
        ``f"{instance}:{chat}"`` implementation would drift the day one of
        them starts normalising ids and the other doesn't.
        """
        assert VC.speech_key is A.speech_key
        assert VC.speech_key(INSTANCE, DM_CHAT) == f"{INSTANCE}:{DM_CHAT}"


# ---------------------------------------------------------------------------
# 2. Configuration
# ---------------------------------------------------------------------------


class TestResolveConfig:
    def test_ships_off(self):
        assert VC.resolve_config({}) is None

    def test_other_voice_call_keys_do_not_enable_it(self):
        """Tuning a timeout must never be what turns the feature on."""
        assert (
            VC.resolve_config({
                "voice_call_ring_timeout_secs": 5,
                "voice_call_allow_groups": True,
            })
            is None
        )

    def test_enabled_uses_the_documented_defaults(self):
        resolved = VC.resolve_config({"voice_call_enabled": True})
        assert resolved == VC.CallConfig(
            ring_timeout_secs=60.0,
            idle_timeout_secs=180.0,
            max_duration_secs=900.0,
            max_turns=40,
            allow_groups=False,
            allowed_users=(),
        )

    def test_every_key_is_honoured(self):
        resolved = VC.resolve_config({
            "voice_call_enabled": "yes",
            "voice_call_ring_timeout_secs": "20",
            "voice_call_idle_timeout_secs": 45,
            "voice_call_max_duration_secs": 300,
            "voice_call_max_turns": "8",
            "voice_call_allow_groups": "on",
            "voice_call_allowed_users": [1114483029, "7711246035"],
        })
        assert resolved == VC.CallConfig(
            ring_timeout_secs=20.0,
            idle_timeout_secs=45.0,
            max_duration_secs=300.0,
            max_turns=8,
            allow_groups=True,
            allowed_users=("1114483029", "7711246035"),
        )

    def test_hot_applies_from_a_mutated_extra(self):
        """Resolve is fresh on every read — that IS the hot-apply mechanism."""
        extra = {"voice_call_enabled": False}
        assert VC.resolve_config(extra) is None
        extra["voice_call_enabled"] = True
        first = VC.resolve_config(extra)
        assert first is not None and first.max_turns == 40
        extra["voice_call_max_turns"] = 3
        assert VC.resolve_config(extra).max_turns == 3
        extra["voice_call_enabled"] = False
        assert VC.resolve_config(extra) is None

    def test_attribute_style_settings_object(self):
        """The ``get`` shim mirrors ``proactive.resolve_config``."""

        class Settings:
            voice_call_enabled = True
            voice_call_max_turns = 6

        resolved = VC.resolve_config(Settings())
        assert resolved is not None
        assert resolved.max_turns == 6
        assert resolved.ring_timeout_secs == 60.0

    @pytest.mark.parametrize("bad", [0, -5, "abc", None, float("nan"), ""])
    def test_nonsense_durations_clamp_to_the_floor_or_the_default(self, bad):
        """No value may produce a call that ends instantly or never.

        Unparseable input falls back to the documented default; a parseable
        but nonsensical duration (zero, negative, NaN) is floored.  There is
        deliberately no "0 means unlimited".
        """
        resolved = VC.resolve_config({
            "voice_call_enabled": True,
            "voice_call_ring_timeout_secs": bad,
            "voice_call_idle_timeout_secs": bad,
            "voice_call_max_duration_secs": bad,
        })
        assert resolved is not None
        for value in (
            resolved.ring_timeout_secs,
            resolved.idle_timeout_secs,
            resolved.max_duration_secs,
        ):
            assert value >= VC.MIN_TIMEOUT_SECS

    @pytest.mark.parametrize("bad", [0, -3, "abc", None])
    def test_max_turns_is_at_least_one(self, bad):
        resolved = VC.resolve_config({
            "voice_call_enabled": True,
            "voice_call_max_turns": bad,
        })
        assert resolved is not None
        assert resolved.max_turns >= 1

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("1,2", ("1", "2")),
            ("1; 2", ("1", "2")),
            ([1, "2"], ("1", "2")),
            (None, ()),
            ("", ()),
        ],
    )
    def test_allowed_users_parsing_matches_the_plugins_other_id_lists(
        self, raw, expected
    ):
        resolved = VC.resolve_config({
            "voice_call_enabled": True,
            "voice_call_allowed_users": raw,
        })
        assert resolved is not None
        assert resolved.allowed_users == expected


class TestUserAllowed:
    def test_empty_allowlist_means_anyone_the_adapter_admitted(self):
        assert VC.user_allowed(cfg(), "anyone") is True
        assert VC.user_allowed(cfg(), "") is True

    def test_narrows_to_the_listed_users(self):
        narrowed = cfg(allowed_users=("111",))
        assert VC.user_allowed(narrowed, "111") is True
        assert VC.user_allowed(narrowed, "222") is False

    def test_unverifiable_peer_is_refused_when_a_list_is_set(self):
        """An allowlist we cannot evaluate is an allowlist that did not pass."""
        narrowed = cfg(allowed_users=("111",))
        assert VC.user_allowed(narrowed, "") is False
        assert VC.user_allowed(narrowed, "   ") is False

    def test_listing_a_user_cannot_widen_past_a_narrower_gate(self):
        """The filter only ever subtracts.

        Naming the peer in ``voice_call_allowed_users`` does not buy them a
        group call when ``voice_call_allow_groups`` is off — the narrower gate
        still wins, which is what "narrowing filter only" has to mean.
        """
        listed = cfg(allowed_users=(PEER,), allow_groups=False)
        decision = VC.invite(
            GROUP_KEY,
            chat_id=GROUP_CHAT,
            is_group=True,
            peer=PEER,
            now=T0,
            cfg=listed,
        )
        assert decision.ok is False
        assert decision.refusal is VC.Refusal.GROUPS_DISABLED
        assert VC.snapshot(GROUP_KEY) is None


# ---------------------------------------------------------------------------
# 3. The happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_invite_accept_talk_hangup(self):
        ring = place_call(T0)
        assert ring.ok is True
        assert ring.state is VC.CallState.RINGING
        # A placed call rings: poke + a spoken line, in that order.
        assert ring.action is VC.CallAction.RING
        assert ring.line == VC.RING_LINE
        assert VC.snapshot(KEY).state is VC.CallState.RINGING

        answer = VC.accept(KEY, T0 + 5)
        assert answer.ok is True
        assert answer.state is VC.CallState.ACTIVE
        assert answer.action is VC.CallAction.SPEAK
        assert answer.line == VC.ACCEPT_OUTGOING_LINE
        live = VC.snapshot(KEY)
        assert live.answered_at == T0 + 5
        assert live.turns == 0

        for index, at in enumerate((T0 + 10, T0 + 20, T0 + 30), start=1):
            assert VC.note_local_turn(KEY, "我在呢", at).ok is True
            assert VC.note_remote_turn(KEY, "嗯嗯我听着", at + 1).ok is True
            assert VC.snapshot(KEY).turns == index * 2
        assert VC.snapshot(KEY).state is VC.CallState.ACTIVE

        bye = VC.hangup(KEY, VC.EndReason.HANGUP_LOCAL, T0 + 60)
        assert bye.ok is True
        assert bye.state is VC.CallState.ENDED
        assert bye.end_reason is VC.EndReason.HANGUP_LOCAL
        assert bye.action is VC.CallAction.SPEAK and bye.line
        assert bye.session.turns == 6
        assert bye.session.ended_at == T0 + 60
        # ENDED is terminal AND the registry no longer holds it, so nothing
        # stale can block the next call.
        assert VC.snapshot(KEY) is None
        assert VC.active_sessions() == ()

    def test_a_finished_call_does_not_block_the_next_one(self):
        place_call(T0)
        VC.hangup(KEY, VC.EndReason.HANGUP_LOCAL, T0 + 1)
        again = place_call(T0 + 2)
        assert again.ok is True
        assert VC.snapshot(KEY).started_at == T0 + 2


# ---------------------------------------------------------------------------
# 4. Gates on starting a call
# ---------------------------------------------------------------------------


class TestStartGates:
    def test_disabled_refuses_and_creates_nothing(self):
        decision = VC.invite(
            KEY, chat_id=DM_CHAT, is_group=False, peer=PEER, now=T0, cfg=None
        )
        assert decision.ok is False
        assert decision.refusal is VC.Refusal.DISABLED
        assert decision.action is VC.CallAction.NOTHING
        assert decision.line == ""
        assert VC.snapshot(KEY) is None

    def test_group_call_refused_unless_the_operator_opts_in(self):
        decision = VC.invite(
            GROUP_KEY,
            chat_id=GROUP_CHAT,
            is_group=True,
            peer=PEER,
            now=T0,
            cfg=cfg(allow_groups=False),
        )
        assert decision.ok is False
        assert decision.refusal is VC.Refusal.GROUPS_DISABLED
        assert VC.snapshot(GROUP_KEY) is None

    def test_group_call_allowed_once_opted_in(self):
        decision = VC.invite(
            GROUP_KEY,
            chat_id=GROUP_CHAT,
            is_group=True,
            peer=PEER,
            now=T0,
            cfg=cfg(allow_groups=True),
        )
        assert decision.ok is True
        assert VC.snapshot(GROUP_KEY).is_group is True

    def test_group_offer_obeys_the_same_gate(self):
        decision = VC.offer(
            GROUP_KEY,
            chat_id=GROUP_CHAT,
            is_group=True,
            peer=PEER,
            now=T0,
            cfg=cfg(allow_groups=False),
        )
        assert decision.refusal is VC.Refusal.GROUPS_DISABLED

    def test_peer_outside_the_allowlist_is_refused(self):
        decision = VC.invite(
            KEY,
            chat_id=DM_CHAT,
            is_group=False,
            peer="999",
            now=T0,
            cfg=cfg(allowed_users=("111",)),
        )
        assert decision.refusal is VC.Refusal.NOT_ALLOWED
        assert VC.snapshot(KEY) is None

    def test_second_invite_is_refused_and_leaves_the_live_session_untouched(self):
        first = place_call(T0)
        before = VC.snapshot(KEY)
        second = place_call(T0 + 30)
        assert second.ok is False
        assert second.refusal is VC.Refusal.ALREADY_LIVE
        assert second.action is VC.CallAction.NOTHING
        # The refusal reports the state it collided with, and changes nothing.
        assert second.state is VC.CallState.RINGING
        assert VC.snapshot(KEY) == before
        assert VC.snapshot(KEY).started_at == first.session.started_at

    def test_offer_onto_a_live_call_is_refused_too(self):
        connected(T0)
        decision = VC.offer(
            KEY, chat_id=DM_CHAT, is_group=False, peer=PEER, now=T0 + 1, cfg=cfg()
        )
        assert decision.refusal is VC.Refusal.ALREADY_LIVE
        assert decision.state is VC.CallState.ACTIVE
        assert VC.snapshot(KEY).state is VC.CallState.ACTIVE


# ---------------------------------------------------------------------------
# 5. Answering
# ---------------------------------------------------------------------------


class TestAnswering:
    def test_offer_goes_incoming_and_says_nothing_yet(self):
        decision = VC.offer(
            KEY, chat_id=DM_CHAT, is_group=False, peer=PEER, now=T0, cfg=cfg()
        )
        assert decision.ok is True
        assert decision.state is VC.CallState.INCOMING
        # We owe them an answer; an extra bubble before it is just noise.
        assert decision.action is VC.CallAction.NOTHING
        assert decision.line == ""
        assert VC.snapshot(KEY).outgoing is False

    def test_accepting_an_incoming_call_uses_the_answering_line(self):
        VC.offer(KEY, chat_id=DM_CHAT, is_group=False, peer=PEER, now=T0, cfg=cfg())
        decision = VC.accept(KEY, T0 + 3)
        assert decision.state is VC.CallState.ACTIVE
        assert decision.line == VC.ACCEPT_INCOMING_LINE

    def test_decline_from_ringing_ends_it_as_declined(self):
        place_call(T0)
        decision = VC.decline(KEY, T0 + 4)
        assert decision.ok is True
        assert decision.state is VC.CallState.ENDED
        assert decision.end_reason is VC.EndReason.DECLINED
        # They turned us down; we acknowledge rather than answer.
        assert decision.line == VC.DECLINE_REMOTE_LINE
        assert VC.snapshot(KEY) is None

    def test_decline_from_incoming_uses_our_own_line(self):
        VC.offer(KEY, chat_id=DM_CHAT, is_group=False, peer=PEER, now=T0, cfg=cfg())
        decision = VC.decline(KEY, T0 + 4)
        assert decision.end_reason is VC.EndReason.DECLINED
        assert decision.line == VC.DECLINE_LOCAL_LINE

    def test_accept_on_an_active_call_is_refused(self):
        connected(T0)
        decision = VC.accept(KEY, T0 + 5)
        assert decision.ok is False
        assert decision.refusal is VC.Refusal.WRONG_STATE
        assert VC.snapshot(KEY).answered_at == T0

    @pytest.mark.parametrize(
        "call",
        [
            lambda: VC.accept(KEY, T0),
            lambda: VC.decline(KEY, T0),
            lambda: VC.hangup(KEY, VC.EndReason.HANGUP_LOCAL, T0),
            lambda: VC.note_remote_turn(KEY, "在吗", T0),
            lambda: VC.note_local_turn(KEY, "在的", T0),
        ],
    )
    def test_transitions_on_a_missing_session_refuse_rather_than_raise(self, call):
        decision = call()
        assert decision.ok is False
        assert decision.refusal is VC.Refusal.NO_SESSION
        assert decision.state is VC.CallState.IDLE

    def test_hangup_may_cancel_an_unanswered_ring(self):
        place_call(T0)
        decision = VC.hangup(KEY, VC.EndReason.HANGUP_LOCAL, T0 + 2)
        assert decision.state is VC.CallState.ENDED
        assert VC.snapshot(KEY) is None


# ---------------------------------------------------------------------------
# 6. Turns
# ---------------------------------------------------------------------------


class TestTurns:
    def test_remote_turn_refreshes_idle_but_not_max_duration(self):
        """The two anchors are separate on purpose.

        Talking keeps a call alive against the idle timer and gets it nowhere
        against the wall-clock cap — otherwise a busy call would never end.
        """
        connected(T0)
        VC.note_remote_turn(KEY, "还在听", T0 + 100)
        session = VC.snapshot(KEY)
        assert session.last_remote_at == T0 + 100
        assert session.answered_at == T0
        assert session.turns == 1

    def test_local_turn_does_not_refresh_the_idle_deadline(self):
        """Idle measures THEIR silence.

        A bot that reset the timer by speaking could hold a dead call open
        forever by talking to a wall.
        """
        connected(T0)
        VC.note_local_turn(KEY, "你还在吗", T0 + 100)
        session = VC.snapshot(KEY)
        assert session.last_remote_at == T0
        assert session.turns == 1

    def test_blank_remote_bubble_is_presence_but_not_a_turn(self):
        """A sticker says "I'm here" and says nothing to answer."""
        connected(T0)
        VC.note_remote_turn(KEY, "   ", T0 + 50)
        session = VC.snapshot(KEY)
        assert session.last_remote_at == T0 + 50
        assert session.turns == 0

    def test_blank_local_turn_is_not_a_turn_at_all(self):
        connected(T0)
        decision = VC.note_local_turn(KEY, "", T0 + 50)
        assert decision.ok is False
        assert decision.refusal is VC.Refusal.NO_CONTENT
        assert VC.snapshot(KEY).turns == 0

    def test_turns_are_refused_while_the_phone_is_still_ringing(self):
        place_call(T0)
        for decision in (
            VC.note_remote_turn(KEY, "喂喂", T0 + 1),
            VC.note_local_turn(KEY, "在的", T0 + 1),
        ):
            assert decision.ok is False
            assert decision.refusal is VC.Refusal.WRONG_STATE
        assert VC.snapshot(KEY).turns == 0

    def test_a_turn_is_one_bubble_from_either_side(self):
        connected(T0)
        VC.note_local_turn(KEY, "一", T0 + 1)
        VC.note_remote_turn(KEY, "二", T0 + 2)
        assert VC.snapshot(KEY).turns == 2


# ---------------------------------------------------------------------------
# 7. Deadlines — each one alone, on both sides of its boundary
# ---------------------------------------------------------------------------


class TestDeadlines:
    def test_ring_timeout(self):
        place_call(T0, cfg(ring_timeout_secs=60.0))
        assert VC.sweep(T0 + 60 - TICK, cfg(ring_timeout_secs=60.0)) == []
        assert VC.snapshot(KEY).state is VC.CallState.RINGING

        fired = VC.sweep(T0 + 60 + TICK, cfg(ring_timeout_secs=60.0))
        assert [e.reason for e in fired] == [VC.EndReason.RING_TIMEOUT]
        assert fired[0].key == KEY
        assert fired[0].decision.action is VC.CallAction.SPEAK
        assert VC.snapshot(KEY) is None

    def test_ring_timeout_covers_an_unanswered_incoming_request(self):
        VC.offer(KEY, chat_id=DM_CHAT, is_group=False, peer=PEER, now=T0, cfg=cfg())
        assert VC.sweep(T0 + 59, cfg()) == []
        fired = VC.sweep(T0 + 61, cfg())
        assert [e.reason for e in fired] == [VC.EndReason.RING_TIMEOUT]

    def test_idle_timeout(self):
        config = cfg(idle_timeout_secs=180.0)
        connected(T0, config)
        assert VC.sweep(T0 + 180 - TICK, config) == []
        assert VC.snapshot(KEY).state is VC.CallState.ACTIVE

        fired = VC.sweep(T0 + 180 + TICK, config)
        assert [e.reason for e in fired] == [VC.EndReason.IDLE_TIMEOUT]
        assert fired[0].decision.end_reason is VC.EndReason.IDLE_TIMEOUT
        assert VC.snapshot(KEY) is None

    def test_idle_deadline_moves_with_each_remote_turn(self):
        config = cfg(idle_timeout_secs=180.0, max_duration_secs=10_000.0)
        connected(T0, config)
        VC.note_remote_turn(KEY, "我在", T0 + 170)
        # Would have expired at T0+180 without the refresh.
        assert VC.sweep(T0 + 200, config) == []
        fired = VC.sweep(T0 + 170 + 181, config)
        assert [e.reason for e in fired] == [VC.EndReason.IDLE_TIMEOUT]

    def test_max_duration(self):
        config = cfg(max_duration_secs=900.0, idle_timeout_secs=10_000.0)
        connected(T0, config)
        assert VC.sweep(T0 + 900 - TICK, config) == []
        fired = VC.sweep(T0 + 900 + TICK, config)
        assert [e.reason for e in fired] == [VC.EndReason.MAX_DURATION]

    def test_max_duration_is_not_extended_by_talking(self):
        """The cap is the operator's promise; conversation cannot buy past it."""
        config = cfg(max_duration_secs=900.0, idle_timeout_secs=10_000.0)
        connected(T0, config)
        for at in range(100, 900, 100):
            VC.note_remote_turn(KEY, "还在", T0 + at)
            VC.note_local_turn(KEY, "嗯", T0 + at)
        fired = VC.sweep(T0 + 900 + TICK, config)
        assert [e.reason for e in fired] == [VC.EndReason.MAX_DURATION]

    def test_max_turns(self):
        config = cfg(
            max_turns=4, idle_timeout_secs=10_000.0, max_duration_secs=10_000.0
        )
        connected(T0, config)
        for index in range(3):
            VC.note_remote_turn(KEY, f"第{index}句", T0 + index + 1)
        # One turn short of the cap: still up.
        assert VC.snapshot(KEY).turns == 3
        assert VC.sweep(T0 + 10, config) == []

        VC.note_remote_turn(KEY, "第4句", T0 + 11)
        fired = VC.sweep(T0 + 12, config)
        assert [e.reason for e in fired] == [VC.EndReason.MAX_TURNS]
        assert VC.snapshot(KEY) is None

    def test_boundary_is_inclusive(self):
        """``now >= deadline`` fires — pinned so the arithmetic cannot drift."""
        config = cfg(ring_timeout_secs=60.0)
        place_call(T0, config)
        fired = VC.sweep(T0 + 60.0, config)
        assert [e.reason for e in fired] == [VC.EndReason.RING_TIMEOUT]

    def test_the_wall_clock_cap_is_named_ahead_of_the_idle_one(self):
        """When several deadlines cross at once the order is the contract."""
        config = cfg(max_duration_secs=100.0, idle_timeout_secs=50.0, max_turns=2)
        session = connected(T0, config)
        assert VC.expiry_reason(session, T0 + 1000, config) is VC.EndReason.MAX_DURATION

    def test_the_turn_cap_is_named_ahead_of_the_idle_one(self):
        config = cfg(max_duration_secs=10_000.0, idle_timeout_secs=50.0, max_turns=2)
        connected(T0, config)
        VC.note_remote_turn(KEY, "一", T0 + 1)
        VC.note_remote_turn(KEY, "二", T0 + 2)
        session = VC.snapshot(KEY)
        # Turn cap and idle cap have both been crossed; the turn cap is named.
        assert VC.expiry_reason(session, T0 + 90, config) is VC.EndReason.MAX_TURNS

    def test_expiry_reason_is_none_while_everything_is_within_bounds(self):
        session = connected(T0)
        assert VC.expiry_reason(session, T0 + 1, cfg()) is None


# ---------------------------------------------------------------------------
# 8. Sweeping
# ---------------------------------------------------------------------------


class TestSweep:
    def test_no_sessions_is_a_noop(self):
        assert VC.sweep(T0, cfg()) == []
        assert VC.sweep(T0, None) == []
        assert VC.active_sessions() == ()

    def test_only_expired_sessions_are_collected(self):
        other_key = VC.speech_key(INSTANCE, "222")
        place_call(T0, cfg(ring_timeout_secs=60.0))
        VC.invite(
            other_key,
            chat_id="222",
            is_group=False,
            peer="222",
            now=T0 + 100,
            cfg=cfg(ring_timeout_secs=60.0),
        )
        fired = VC.sweep(T0 + 120, cfg(ring_timeout_secs=60.0))
        assert [e.key for e in fired] == [KEY]
        assert VC.snapshot(other_key) is not None
        assert len(VC.active_sessions()) == 1

    def test_sweeping_twice_collects_nothing_the_second_time(self):
        place_call(T0, cfg(ring_timeout_secs=10.0))
        assert len(VC.sweep(T0 + 100, cfg(ring_timeout_secs=10.0))) == 1
        assert VC.sweep(T0 + 200, cfg(ring_timeout_secs=10.0)) == []

    def test_disabling_the_feature_collects_every_live_call_silently(self):
        """Hot-disabling must not leave a session no sweeper will ever visit.

        Nothing is spoken on the way out: the operator just switched the
        feature off, and manufacturing one more voice bubble would be the
        opposite of obeying that.
        """
        connected(T0)
        VC.offer(
            VC.speech_key(INSTANCE, "222"),
            chat_id="222",
            is_group=False,
            peer="222",
            now=T0,
            cfg=cfg(),
        )
        fired = VC.sweep(T0 + 1, None)
        assert {e.reason for e in fired} == {VC.EndReason.DISABLED}
        assert all(e.decision.action is VC.CallAction.NOTHING for e in fired)
        assert all(e.decision.line == "" for e in fired)
        assert VC.active_sessions() == ()

    def test_instance_filter_leaves_another_adapters_calls_alone(self):
        """Two OneBot instances each own a sweeper and their own timeouts."""
        mine = VC.speech_key("a", DM_CHAT)
        theirs = VC.speech_key("b", DM_CHAT)
        for key in (mine, theirs):
            VC.invite(
                key,
                chat_id=DM_CHAT,
                is_group=False,
                peer=PEER,
                now=T0,
                cfg=cfg(ring_timeout_secs=10.0),
            )
        fired = VC.sweep(T0 + 100, cfg(ring_timeout_secs=10.0), instance_id="a")
        assert [e.key for e in fired] == [mine]
        assert VC.snapshot(theirs) is not None

    def test_expired_sessions_report_their_final_shape(self):
        config = cfg(
            max_turns=2, idle_timeout_secs=10_000.0, max_duration_secs=10_000.0
        )
        connected(T0, config)
        VC.note_remote_turn(KEY, "一", T0 + 1)
        VC.note_local_turn(KEY, "二", T0 + 2)
        fired = VC.sweep(T0 + 3, config)
        ended = fired[0].decision.session
        assert ended.state is VC.CallState.ENDED
        assert ended.end_reason is VC.EndReason.MAX_TURNS
        assert ended.turns == 2
        assert ended.ended_at == T0 + 3


# ---------------------------------------------------------------------------
# 9. Reading a human's answer
# ---------------------------------------------------------------------------


class TestClassifyIntent:
    @pytest.mark.parametrize(
        "text",
        [
            "接",
            "接听",
            "接吧",
            "好",
            "好啊",
            "好的",
            "好呀！",
            "可以",
            "可以啊",
            "行",
            "来吧",
            "开始吧",
            "说吧",
            "喂",
            "在",
            "在的",
            "没问题",
            "嗯，可以",
            "那来吧",
            "ok",
            "OK",
            "okay",
            "yes",
            "sure",
            "pick up",
            "go ahead",
        ],
    )
    def test_accept_vocabulary(self, text):
        assert VC.classify_intent(text) is VC.CallIntent.ACCEPT

    @pytest.mark.parametrize(
        "text",
        [
            "不",
            "不用",
            "不用了",
            "不方便",
            "现在不方便",
            "拒绝",
            "待会",
            "等会儿",
            "等下",
            "忙",
            "我在忙",
            "没空",
            "算了",
            "改天吧",
            "下次吧",
            "嗯，不用了",
            "no",
            "nope",
            "not now",
            "later",
            "busy",
        ],
    )
    def test_decline_vocabulary(self, text):
        assert VC.classify_intent(text) is VC.CallIntent.DECLINE

    @pytest.mark.parametrize(
        "text",
        [
            "挂了",
            "挂断",
            "先挂了",
            "先这样",
            "就这样吧",
            "不聊了",
            "拜拜",
            "再见",
            "回聊",
            "回头再聊",
            "bye",
            "byebye",
            "hang up",
            "gotta go",
            "talk later",
            "ttyl",
        ],
    )
    def test_hangup_vocabulary(self, text):
        assert VC.classify_intent(text) is VC.CallIntent.HANGUP

    @pytest.mark.parametrize(
        "text",
        [
            "你好，今天天气不错",
            "我不知道该说什么好",
            "他挂了个电话给别人",
            "那我们接着说这个项目吧",
            "接下来干嘛",
            "我觉得可以再想想",
            "好像不太行",
            "好，我先忙完这个",
        ],
    )
    def test_a_keyword_inside_a_longer_sentence_matches_nothing(self, text):
        """The ``proactive.is_skip`` protection, applied to all three classifiers.

        Patterns are anchored per clause, never swept for as substrings, so a
        sentence that merely *contains* 好 / 不 / 挂了 is not an answer.
        """
        assert VC.classify_intent(text) is VC.CallIntent.NONE

    @pytest.mark.parametrize(
        "text", ["嗯", "嗯嗯", "什么", "在干嘛", "哈哈哈", "?", "", "   ", "在吗", "😅"]
    )
    def test_ambiguous_input_is_neither_accept_nor_decline(self, text):
        """The expensive error is talking at somebody who never agreed.

        Making them repeat themselves is cheap, so anything short of clear
        agreement means "ask again".
        """
        intent = VC.classify_intent(text)
        assert intent is VC.CallIntent.NONE
        assert intent is not VC.CallIntent.ACCEPT

    def test_ending_beats_agreeing(self):
        """Mixed signals resolve to the safe direction."""
        assert VC.classify_intent("好，挂了") is VC.CallIntent.HANGUP
        assert VC.classify_intent("好，不用了") is VC.CallIntent.DECLINE

    def test_partial_agreement_is_not_agreement(self):
        assert VC.classify_intent("好啊，来吧") is VC.CallIntent.ACCEPT
        assert VC.classify_intent("好啊，等我十分钟看看情况") is VC.CallIntent.NONE


# ---------------------------------------------------------------------------
# 10. Routing inbound messages
# ---------------------------------------------------------------------------


class TestOnInbound:
    def test_no_session_falls_through(self):
        decision = VC.on_inbound(KEY, "好啊", T0)
        assert decision.ok is False
        assert decision.refusal is VC.Refusal.NO_SESSION

    def test_agreement_answers_a_ringing_call(self):
        place_call(T0)
        decision = VC.on_inbound(KEY, "好啊", T0 + 5)
        assert decision.state is VC.CallState.ACTIVE
        assert VC.snapshot(KEY).answered_at == T0 + 5

    @pytest.mark.parametrize("text", ["不用了", "挂了"])
    def test_refusal_or_a_hangup_word_turns_a_ringing_call_down(self, text):
        place_call(T0)
        decision = VC.on_inbound(KEY, text, T0 + 5)
        assert decision.end_reason is VC.EndReason.DECLINED
        assert VC.snapshot(KEY) is None

    def test_unrelated_chatter_leaves_the_ring_alone(self):
        place_call(T0)
        decision = VC.on_inbound(KEY, "我刚看到那个新闻了", T0 + 5)
        assert decision.ok is False
        assert decision.refusal is VC.Refusal.UNCLEAR
        assert VC.snapshot(KEY).state is VC.CallState.RINGING

    def test_their_agreement_cannot_answer_a_call_we_owe_the_answer_to(self):
        """INCOMING means *we* have not answered yet.

        Their "好" is not an answer to anything, and reading it as one would
        connect a call nobody on our side agreed to.
        """
        VC.offer(KEY, chat_id=DM_CHAT, is_group=False, peer=PEER, now=T0, cfg=cfg())
        decision = VC.on_inbound(KEY, "好啊", T0 + 1)
        assert decision.refusal is VC.Refusal.UNCLEAR
        assert VC.snapshot(KEY).state is VC.CallState.INCOMING

    def test_withdrawing_an_incoming_request_ends_it(self):
        VC.offer(KEY, chat_id=DM_CHAT, is_group=False, peer=PEER, now=T0, cfg=cfg())
        decision = VC.on_inbound(KEY, "算了", T0 + 1)
        assert decision.end_reason is VC.EndReason.DECLINED
        assert VC.snapshot(KEY) is None

    def test_a_hangup_word_ends_an_active_call_as_theirs(self):
        connected(T0)
        decision = VC.on_inbound(KEY, "先这样吧，拜拜", T0 + 30)
        assert decision.end_reason is VC.EndReason.HANGUP_REMOTE
        assert VC.snapshot(KEY) is None

    def test_ordinary_talk_during_a_call_is_just_their_turn(self):
        connected(T0)
        decision = VC.on_inbound(KEY, "我今天去了趟公司", T0 + 30)
        assert decision.ok is True
        assert decision.state is VC.CallState.ACTIVE
        session = VC.snapshot(KEY)
        assert session.turns == 1
        assert session.last_remote_at == T0 + 30

    def test_accept_words_mid_call_are_talk_not_a_transition(self):
        connected(T0)
        VC.on_inbound(KEY, "好啊", T0 + 30)
        assert VC.snapshot(KEY).state is VC.CallState.ACTIVE
        assert VC.snapshot(KEY).turns == 1


# ---------------------------------------------------------------------------
# 11. The one place a decision meets the wire
# ---------------------------------------------------------------------------


class Recorder:
    """Stand-in for the injected voice sender / poke sender."""

    def __init__(self, fail: bool = False) -> None:
        self.spoken: List[str] = []
        self.pokes = 0
        self.order: List[str] = []
        self.fail = fail

    async def speak(self, line: str) -> None:
        self.spoken.append(line)
        self.order.append("speak")

    async def poke(self) -> None:
        self.pokes += 1
        self.order.append("poke")
        if self.fail:
            raise RuntimeError("send_poke unsupported")


class TestPerform:
    @pytest.mark.asyncio
    async def test_ringing_pokes_before_it_speaks(self):
        rec = Recorder()
        sent = await VC.perform(place_call(T0), speak=rec.speak, poke=rec.poke)
        assert sent is True
        assert rec.order == ["poke", "speak"]
        assert rec.spoken == [VC.RING_LINE]

    @pytest.mark.asyncio
    async def test_speaking_does_not_poke(self):
        place_call(T0)
        rec = Recorder()
        await VC.perform(VC.accept(KEY, T0 + 1), speak=rec.speak, poke=rec.poke)
        assert rec.pokes == 0
        assert rec.spoken == [VC.ACCEPT_OUTGOING_LINE]

    @pytest.mark.asyncio
    async def test_a_refusal_puts_nothing_on_the_wire(self):
        rec = Recorder()
        refusal = VC.invite(
            KEY, chat_id=DM_CHAT, is_group=False, peer=PEER, now=T0, cfg=None
        )
        assert await VC.perform(refusal, speak=rec.speak, poke=rec.poke) is False
        assert rec.order == []

    @pytest.mark.asyncio
    async def test_a_failed_poke_does_not_swallow_the_words(self):
        """Not every backend implements ``send_poke``; the line still goes out."""
        rec = Recorder(fail=True)
        assert await VC.perform(place_call(T0), speak=rec.speak, poke=rec.poke) is True
        assert rec.spoken == [VC.RING_LINE]

    @pytest.mark.asyncio
    async def test_ringing_without_a_poke_sender_still_speaks(self):
        rec = Recorder()
        assert await VC.perform(place_call(T0), speak=rec.speak) is True
        assert rec.spoken == [VC.RING_LINE]

    @pytest.mark.asyncio
    async def test_a_failed_send_reaches_the_caller(self):
        """A call whose voice never went out is a call the caller must end."""

        async def boom(_line: str) -> None:
            raise RuntimeError("ws closed")

        with pytest.raises(RuntimeError):
            await VC.perform(place_call(T0), speak=boom)


# ---------------------------------------------------------------------------
# 12. Module state
# ---------------------------------------------------------------------------


class TestModuleState:
    def test_reset_state_really_clears(self):
        connected(T0)
        VC.offer(
            VC.speech_key(INSTANCE, "222"),
            chat_id="222",
            is_group=False,
            peer="222",
            now=T0,
            cfg=cfg(),
        )
        assert len(VC.active_sessions()) == 2
        VC.reset_state()
        assert VC.active_sessions() == ()
        assert VC.snapshot(KEY) is None

    def test_active_sessions_lists_every_live_state_in_key_order(self):
        connected(T0)
        VC.offer(
            VC.speech_key(INSTANCE, "aaa"),
            chat_id="aaa",
            is_group=False,
            peer="aaa",
            now=T0,
            cfg=cfg(),
        )
        VC.invite(
            VC.speech_key(INSTANCE, "zzz"),
            chat_id="zzz",
            is_group=False,
            peer="zzz",
            now=T0,
            cfg=cfg(),
        )
        states = {s.key: s.state for s in VC.active_sessions()}
        assert states == {
            KEY: VC.CallState.ACTIVE,
            VC.speech_key(INSTANCE, "aaa"): VC.CallState.INCOMING,
            VC.speech_key(INSTANCE, "zzz"): VC.CallState.RINGING,
        }
        assert [s.key for s in VC.active_sessions()] == sorted(states)
