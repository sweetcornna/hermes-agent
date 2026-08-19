"""Decay math, including the topic clock decoupled from the fatigue clock."""

from __future__ import annotations

import pytest

from plugins.grantley.decay import (
    DecayConfig,
    apply_decay,
    carry_topic_clock,
    hours_between,
)
from plugins.grantley.state import PersonaState

CFG = DecayConfig()
_MS_PER_DAY = 86_400_000


def _state(**kw) -> PersonaState:
    base = {"persona_id": "grantley"}
    base.update(kw)
    return PersonaState(**base)


# ── fatigue ────────────────────────────────────────────────────────────────


def test_fatigue_recovers_at_one_tenth_per_hour():
    out = apply_decay(_state(fatigue=1.0), 5.0, CFG)
    assert out.fatigue == pytest.approx(0.5)


def test_fatigue_floors_at_zero():
    out = apply_decay(_state(fatigue=0.2), 100.0, CFG)
    assert out.fatigue == 0.0


def test_decay_does_not_mutate_the_input():
    original = _state(fatigue=0.9, mood="tired", recent_topics=["a", "b"])
    apply_decay(original, 48.0, CFG)
    assert original.fatigue == 0.9
    assert original.mood == "tired"
    assert original.recent_topics == ["a", "b"]


# ── mood flip ──────────────────────────────────────────────────────────────


def test_tired_flips_to_neutral_below_the_threshold():
    # 0.5 - 3h*0.1 = 0.2, strictly below 0.3
    out = apply_decay(_state(fatigue=0.5, mood="tired"), 3.0, CFG)
    assert out.fatigue == pytest.approx(0.2)
    assert out.mood == "neutral"


def test_tired_survives_at_exactly_the_threshold():
    """The rule is strict `<`, not `<=` — ported semantics."""
    # 0.5 - 2h*0.1 = 0.3 exactly
    out = apply_decay(_state(fatigue=0.5, mood="tired"), 2.0, CFG)
    assert out.fatigue == pytest.approx(0.3)
    assert out.mood == "tired"


def test_other_moods_are_never_rewritten():
    out = apply_decay(_state(fatigue=0.9, mood="兴奋"), 24.0, CFG)
    assert out.mood == "兴奋"
    out2 = apply_decay(_state(fatigue=0.9, mood="neutral"), 24.0, CFG)
    assert out2.mood == "neutral"


# ── topic aging (the decoupled clock) ──────────────────────────────────────


def test_topics_age_one_per_full_day_oldest_first():
    out = apply_decay(_state(recent_topics=["a", "b", "c"]), 48.0, CFG)
    assert out.recent_topics == ["c"]


def test_partial_days_do_not_age_topics():
    out = apply_decay(_state(recent_topics=["a", "b"]), 23.9, CFG)
    assert out.recent_topics == ["a", "b"]


def test_over_aging_bottoms_out_empty_not_negative():
    out = apply_decay(_state(recent_topics=["a"]), 24.0 * 50, CFG)
    assert out.recent_topics == []


def test_topic_clock_is_independent_of_the_fatigue_clock():
    """The source's stated intent: recover fatigue often, age topics slowly."""
    state = _state(fatigue=1.0, recent_topics=["a", "b", "c"])
    # One hour of fatigue time, three days of topic time.
    out = apply_decay(state, 1.0, CFG, topic_hours_elapsed=72.0)
    assert out.fatigue == pytest.approx(0.9)
    assert out.recent_topics == []


def test_topics_age_even_when_the_fatigue_clock_has_not_advanced():
    """Row just stamped (0 fatigue hours) but topic time accrued past a day."""
    state = _state(fatigue=0.5, recent_topics=["a", "b"])
    out = apply_decay(state, 0.0, CFG, topic_hours_elapsed=24.0)
    assert out.fatigue == 0.5  # untouched
    assert out.recent_topics == ["b"]


def test_both_clocks_non_positive_is_a_no_op():
    state = _state(fatigue=0.5, recent_topics=["a"])
    out = apply_decay(state, 0.0, CFG, topic_hours_elapsed=0.0)
    assert out is state


def test_omitting_the_topic_clock_collapses_to_single_clock_behaviour():
    a = apply_decay(_state(fatigue=1.0, recent_topics=["x", "y"]), 48.0, CFG)
    b = apply_decay(
        _state(fatigue=1.0, recent_topics=["x", "y"]), 48.0, CFG, topic_hours_elapsed=48.0
    )
    assert a.fatigue == b.fatigue
    assert a.recent_topics == b.recent_topics


def test_topics_decay_per_day_knob_is_honoured():
    cfg = DecayConfig(recent_topics_decay_per_day=2)
    out = apply_decay(_state(recent_topics=["a", "b", "c", "d", "e"]), 24.0, cfg)
    assert out.recent_topics == ["c", "d", "e"]


# ── the store-side clock carry (the fix over the source) ───────────────────


def test_carry_advances_only_by_whole_consumed_days():
    start = 1_000_000_000_000
    now = start + _MS_PER_DAY + 3_600_000  # 1 day 1 hour
    assert carry_topic_clock(start, now) == start + _MS_PER_DAY


def test_carry_retains_the_remainder_so_frequent_sweeps_accumulate():
    """Hourly sweeps must still age exactly one topic per day."""
    start = 1_000_000_000_000
    clock = start
    for hour in range(1, 25):
        clock = carry_topic_clock(clock, start + hour * 3_600_000)
    # After 24 hourly ticks exactly one whole day has been consumed.
    assert clock == start + _MS_PER_DAY


def test_carry_snaps_an_unset_or_future_clock_to_now():
    now = 1_000_000_000_000
    assert carry_topic_clock(0, now) == now
    assert carry_topic_clock(now + 5_000, now) == now


def test_hours_between_never_ages_a_fresh_row_from_the_epoch():
    """A naive `now - 0` would decay a brand-new row by 50+ years."""
    assert hours_between(0, 1_000_000_000_000) == 0.0


def test_hours_between_computes_forward_time_only():
    now = 1_000_000_000_000
    assert hours_between(now - 3_600_000, now) == pytest.approx(1.0)
    assert hours_between(now + 3_600_000, now) == 0.0
