"""Pure-function persona decay.

Ported from ``corlinman_persona/decay.py``. Given a :class:`PersonaState`
plus a wall-clock delta, it returns a *new* state. No I/O, no randomness,
no clock reads — the caller supplies elapsed time, so tests drive it with a
synthetic clock and the cron job drives it with the real one.

We do not decay the ``mood`` string itself — it is a categorical label.
Instead ``fatigue`` recovers and a single threshold rule flips a ``"tired"``
mood back to ``"neutral"`` once the persona is rested.

Two clocks, on purpose
----------------------
``hours_elapsed`` drives fatigue recovery (and therefore the mood flip).
``topic_hours_elapsed`` drives ``recent_topics`` aging. They are decoupled
so a high-frequency sweep can recover fatigue every tick while topics still
age off a slower, cumulative *day* clock. When ``topic_hours_elapsed`` is
``None`` topic aging falls back to ``hours_elapsed`` — identical to
single-clock behaviour.

:func:`carry_topic_clock` is the piece the source design was missing: the
pure function knew about two clocks, but the store only ever stamped one, so
in practice a sweep more frequent than 24h could never accumulate a whole
day and topics never aged. It computes how far the topic clock is allowed to
advance — whole consumed days only, remainder retained.
"""

from __future__ import annotations

import math
from dataclasses import replace

from .state import PersonaState

#: Milliseconds in one hour / one day — used by :func:`carry_topic_clock`.
_MS_PER_HOUR: int = 3_600_000
_MS_PER_DAY: int = 86_400_000


class DecayConfig:
    """Tunables for :func:`apply_decay`.

    Defaults are the production values from corlinman's
    ``docs/design/phase3-roadmap.md`` §6 ``[persona]``, carried over
    unchanged. Frozen-by-convention: treat instances as immutable.
    """

    __slots__ = (
        "fatigue_recovery_per_hour",
        "tired_to_neutral_below",
        "recent_topics_decay_per_day",
        "mood_decay_per_hour",
    )

    def __init__(
        self,
        fatigue_recovery_per_hour: float = 0.1,
        tired_to_neutral_below: float = 0.3,
        recent_topics_decay_per_day: int = 1,
        mood_decay_per_hour: float = 0.05,
    ) -> None:
        #: Recovery rate for fatigue, per hour of elapsed wall time.
        self.fatigue_recovery_per_hour = float(fatigue_recovery_per_hour)
        #: Threshold below which a ``"tired"`` mood auto-flips to ``"neutral"``.
        #: Intentionally generous (0.3) so the persona recovers before fatigue
        #: hits 0 — mood is a coarser signal than the float.
        self.tired_to_neutral_below = float(tired_to_neutral_below)
        #: Number of ``recent_topics`` dropped per full day elapsed.
        self.recent_topics_decay_per_day = int(recent_topics_decay_per_day)
        #: Reserved for future use (mood numerics). Carried so a job config
        #: can already name it without a schema bump later.
        self.mood_decay_per_hour = float(mood_decay_per_hour)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            "DecayConfig("
            f"fatigue_recovery_per_hour={self.fatigue_recovery_per_hour!r}, "
            f"tired_to_neutral_below={self.tired_to_neutral_below!r}, "
            f"recent_topics_decay_per_day={self.recent_topics_decay_per_day!r}, "
            f"mood_decay_per_hour={self.mood_decay_per_hour!r})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, DecayConfig):
            return NotImplemented
        return all(
            getattr(self, slot) == getattr(other, slot) for slot in self.__slots__
        )


DEFAULT_DECAY_CONFIG = DecayConfig()


def apply_decay(
    state: PersonaState,
    hours_elapsed: float,
    config: DecayConfig | None = None,
    topic_hours_elapsed: float | None = None,
) -> PersonaState:
    """Return a new :class:`PersonaState` with decay applied.

    Pure function — does not mutate the input. Rules (ported verbatim from
    ``corlinman_persona.decay.apply_decay``):

    * ``fatigue``: ``max(0.0, fatigue - hours_elapsed * recovery_per_hour)``,
      applied only when ``hours_elapsed > 0``.
    * ``mood``: if it was ``"tired"`` and the **new** fatigue dropped strictly
      below :attr:`DecayConfig.tired_to_neutral_below`, flip to ``"neutral"``.
      Every other mood label is left alone.
    * ``recent_topics``: drop ``floor(topic_hours / 24) * per_day`` of the
      *oldest* entries, where ``topic_hours`` is ``topic_hours_elapsed`` when
      supplied else ``hours_elapsed``. Clamped at the list length, so an
      over-aged state bottoms out empty rather than going negative.
    * timestamps are left to the store layer; this function invents none.

    Negative or zero elapsed time is a no-op **only when both clocks are
    non-positive** — with a decoupled topic clock, fatigue time can be 0
    (row just stamped) while topic time has accrued past a day, and topics
    must still age in that case.
    """
    cfg = config if config is not None else DEFAULT_DECAY_CONFIG
    topic_hours = hours_elapsed if topic_hours_elapsed is None else topic_hours_elapsed
    if hours_elapsed <= 0 and topic_hours <= 0:
        return state

    new_fatigue = (
        max(0.0, state.fatigue - hours_elapsed * cfg.fatigue_recovery_per_hour)
        if hours_elapsed > 0
        else state.fatigue
    )

    new_mood = state.mood
    if state.mood == "tired" and new_fatigue < cfg.tired_to_neutral_below:
        new_mood = "neutral"

    days_elapsed = math.floor(topic_hours / 24.0) if topic_hours > 0 else 0
    drop_count = days_elapsed * cfg.recent_topics_decay_per_day
    if drop_count <= 0:
        new_topics = list(state.recent_topics)
    elif drop_count >= len(state.recent_topics):
        new_topics = []
    else:
        # Oldest entries live at the head (pushes append to the tail), so
        # slicing from ``drop_count:`` ages them out from the front.
        new_topics = list(state.recent_topics[drop_count:])

    return replace(
        state,
        mood=new_mood,
        fatigue=new_fatigue,
        recent_topics=new_topics,
    )


def carry_topic_clock(topics_aged_at_ms: int, now_ms: int) -> int:
    """Return the new topic-clock stamp after a decay tick.

    The topic clock advances by **whole consumed days only**; the remainder
    is retained so frequent sweeps accumulate toward the next day boundary
    instead of resetting it. This is what makes the source's two-clock
    design reachable in practice — see the module docstring.

    A clock in the future (or unset, i.e. ``0``) snaps to *now*, which is the
    safe direction: it delays aging rather than dropping topics spuriously.
    """
    if topics_aged_at_ms <= 0 or topics_aged_at_ms > now_ms:
        return now_ms
    whole_days = (now_ms - topics_aged_at_ms) // _MS_PER_DAY
    if whole_days <= 0:
        return topics_aged_at_ms
    return topics_aged_at_ms + whole_days * _MS_PER_DAY


def hours_between(then_ms: int, now_ms: int) -> float:
    """Elapsed hours between two millisecond stamps, floored at 0.

    An unset (``0``) or future ``then_ms`` yields ``0.0`` so a fresh row
    does not decay by "the entire Unix epoch" on its first tick — the exact
    failure mode a naive ``now - 0`` would produce.
    """
    if then_ms <= 0 or now_ms <= then_ms:
        return 0.0
    return (now_ms - then_ms) / float(_MS_PER_HOUR)


__all__ = [
    "DEFAULT_DECAY_CONFIG",
    "DecayConfig",
    "apply_decay",
    "carry_topic_clock",
    "hours_between",
]
