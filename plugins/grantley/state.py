"""Persona runtime state — the in-memory projection of one ``persona_state`` row.

Ported from ``corlinman_persona/state.py`` (dataclass + cap) and
``corlinman_persona/placeholders.py`` (fatigue bucketing + topic formatting).
Both are pure: no I/O, no clock reads. :mod:`plugins.grantley.store` owns
persistence, :mod:`plugins.grantley.decay` owns the time math.

Two deliberate deviations from the source, both documented in
``docs/migration-corlinman/C1-grantley-port-notes.md``:

* ``topics_aged_at_ms`` is a *second* clock. The source decoupled the
  topic-aging clock from the fatigue clock inside ``apply_decay`` but had
  nowhere to persist it, so every caller collapsed back to a single clock.
  Carrying it on the row is what makes the decoupling actually reachable.
* ``fatigue`` is clamped into ``[0.0, 1.0]`` on construction. The source
  clamped only at render time, which let a bad write persist an
  out-of-range float that every later decay tick had to walk back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Hard cap on ``recent_topics`` length. Enforced at every write site rather
#: than trusting callers (source: ``corlinman_persona/state.py``).
RECENT_TOPICS_CAP: int = 20

#: Number of topics surfaced to the prompt. The store retains up to
#: :data:`RECENT_TOPICS_CAP`; the prompt only ever sees the freshest few so
#: token usage stays bounded (source: ``placeholders.RECENT_TOPICS_VISIBLE``).
RECENT_TOPICS_VISIBLE: int = 5

#: Fatigue bucket boundaries — categorical so the prompt is never shaped by
#: a float that is only meaningful internally. Inclusive lower bounds, so
#: ``0.4`` itself reads as ``"mild fatigue"`` and not ``"fresh"``.
#: Ported verbatim from ``placeholders._FATIGUE_BUCKETS``.
FATIGUE_BUCKETS: tuple[tuple[float, str], ...] = (
    (0.75, "tired"),
    (0.4, "mild fatigue"),
    (0.15, "fresh"),
    (0.0, "rested"),
)


def bucket_fatigue(value: float) -> str:
    """Return the categorical label for *value*.

    Out-of-range values clamp to the nearest bucket; this never raises
    because it runs during prompt expansion, where a noisy error is worse
    than a slightly-wrong label.

    **A raw float must never reach the prompt.** That is the whole point of
    this function — see the C1 notes.
    """
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "rested"
    if numeric != numeric:  # NaN
        return "rested"
    clamped = max(0.0, min(1.0, numeric))
    for threshold, label in FATIGUE_BUCKETS:
        if clamped >= threshold:
            return label
    # Unreachable while the table covers >= 0.0; keeps the type checker happy.
    return "rested"


def format_topics(topics: list[str], limit: int = RECENT_TOPICS_VISIBLE) -> str:
    """Comma-join the freshest *limit* topics, newest-first.

    Topics are stored oldest-first (``push`` appends to the tail); we take
    the tail and reverse it so the prompt reads newest-first. An empty list
    produces an empty string — never ``"[]"`` or ``"None"``.
    """
    if not topics:
        return ""
    if limit <= 0:
        return ""
    tail = list(topics[-limit:])
    return ", ".join(str(t) for t in reversed(tail))


def dedup_cap(topics: list[str]) -> list[str]:
    """Keep the last occurrence of each topic, then trim to the cap.

    Ported from ``corlinman_persona/store._dedup_cap``: a repeated topic
    moves to the newest position rather than occupying two slots.
    """
    ordered: dict[str, None] = {}
    for topic in topics:
        text = str(topic).strip()
        if not text:
            continue
        ordered.pop(text, None)
        ordered[text] = None
    deduped = list(ordered)
    if len(deduped) > RECENT_TOPICS_CAP:
        deduped = deduped[-RECENT_TOPICS_CAP:]
    return deduped


@dataclass
class PersonaState:
    """A single persona's persisted runtime state.

    Attributes
    ----------
    persona_id
        Stable persona slug (``"grantley"``).
    mood
        Free-form mood label. :mod:`plugins.grantley.decay` treats a small
        set of well-known values (``"tired"``, ``"neutral"``) specially but
        does not constrain the vocabulary.
    fatigue
        ``[0.0, 1.0]`` — 0 is well-rested, 1 is maxed-out tired. Recovers
        over time via :func:`plugins.grantley.decay.apply_decay`. Only ever
        reaches a prompt through :func:`bucket_fatigue`.
    recent_topics
        Most-recent-last list, capped at :data:`RECENT_TOPICS_CAP`.
    updated_at_ms
        Unix milliseconds — the **fatigue** clock. The decay job reads this
        to compute elapsed hours.
    topics_aged_at_ms
        Unix milliseconds — the **topic-aging** clock, advanced only by whole
        days actually consumed. See the module docstring.
    state_json
        Free-form extension dict. Carries ``life`` / ``diary`` and the flat
        ``life_*`` mirror keys the placeholder layer reads.
    """

    persona_id: str
    mood: str = "neutral"
    fatigue: float = 0.0
    recent_topics: list[str] = field(default_factory=list)
    updated_at_ms: int = 0
    topics_aged_at_ms: int = 0
    state_json: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            self.fatigue = max(0.0, min(1.0, float(self.fatigue)))
        except (TypeError, ValueError):
            self.fatigue = 0.0
        self.recent_topics = dedup_cap(list(self.recent_topics or []))
        if not isinstance(self.state_json, dict):
            self.state_json = {}


__all__ = [
    "FATIGUE_BUCKETS",
    "RECENT_TOPICS_CAP",
    "RECENT_TOPICS_VISIBLE",
    "PersonaState",
    "bucket_fatigue",
    "dedup_cap",
    "format_topics",
]
