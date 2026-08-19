"""SQLite storage: persona state + the append-only life-event log (A3 G2).

Two tables in one plugin database:

``persona_state``
    One row per persona. The projection of :class:`PersonaState`, plus the
    two decay clocks. This replaces corlinman's ``agent_persona_state``
    table; the columns are the same modulo the added ``topics_aged_at_ms``.

``life_events``
    **Append-only.** ``(id, persona_id, created_at, salience, text, kind)``,
    exactly the schema A3 G2 specifies. Nothing updates or deletes a row in
    the normal path — retrieval applies decay at *read* time. That keeps the
    log auditable (you can always reconstruct what the persona "knew" on a
    given day) and makes the decay half-life a tunable rather than a
    destructive migration.

Decay formula
-------------
``weight = salience * 0.5 ** (age_days / half_life_days)``

Copied from the only decay implementation in the hermes tree,
``plugins/memory/holographic/retrieval.py`` (``_temporal_decay``, applied by
multiplying into the per-fact trust score). A half-life of ``0`` disables
decay and returns weight ``1.0``, matching that implementation's convention.

Why this and not ``MEMORY.md``: the built-in prose memory store has no
timestamps at all, so it cannot express "this mattered a lot two weeks ago
and barely matters now" — which is the entire point of a life log.

The connection is injected. In the plugin it comes from
``plugins.plugin_storage.plugin_db``, which puts the file under
``<hermes home>/plugin-data/grantley/`` so it survives plugin update and
removal; tests pass an in-memory or tmp-path connection. This module never
resolves a path itself.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .state import PersonaState, dedup_cap

#: Default half-life for life-event decay, in days. Two weeks: a mission the
#: persona ran a fortnight ago should still colour its mood at roughly half
#: strength, and be nearly gone after a month.
DEFAULT_HALF_LIFE_DAYS: float = 14.0

#: Default number of decayed events handed to the sidecar per turn. Small on
#: purpose — this rides on every user message, so it is a per-turn token cost.
DEFAULT_TOP_N: int = 5

#: Events scoring below this after decay are not worth a turn's tokens.
DEFAULT_MIN_WEIGHT: float = 0.05

_SECONDS_PER_DAY: float = 86_400.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS persona_state (
    persona_id        TEXT PRIMARY KEY,
    mood              TEXT NOT NULL DEFAULT 'neutral',
    fatigue           REAL NOT NULL DEFAULT 0.0,
    recent_topics     TEXT NOT NULL DEFAULT '[]',
    updated_at_ms     INTEGER NOT NULL DEFAULT 0,
    topics_aged_at_ms INTEGER NOT NULL DEFAULT 0,
    state_json        TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS life_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_id  TEXT NOT NULL,
    created_at  REAL NOT NULL,
    salience    REAL NOT NULL DEFAULT 1.0,
    text        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'life'
);

CREATE INDEX IF NOT EXISTS idx_life_events_persona_created
    ON life_events (persona_id, created_at DESC);
"""


@dataclass(frozen=True)
class LifeEvent:
    """One row of the append-only log, optionally carrying its decayed weight."""

    id: int
    persona_id: str
    created_at: float
    salience: float
    text: str
    kind: str
    weight: float = 1.0

    def age_days(self, now: float | None = None) -> float:
        moment = time.time() if now is None else now
        return max(0.0, (moment - self.created_at) / _SECONDS_PER_DAY)


def temporal_decay(
    created_at: float,
    now: float,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> float:
    """Return the decay multiplier for an event created at *created_at*.

    ``0.5 ** (age_days / half_life)`` — the formula from
    ``plugins/memory/holographic/retrieval.py``. A non-positive half-life
    disables decay (returns ``1.0``), matching that module's
    ``temporal_decay_half_life: 0`` convention. A future timestamp also
    returns ``1.0`` rather than a weight above 1.
    """
    if half_life_days <= 0:
        return 1.0
    age_days = (now - created_at) / _SECONDS_PER_DAY
    if age_days <= 0:
        return 1.0
    return 0.5 ** (age_days / half_life_days)


class GrantleyStore:
    """Persona state + life-event log over one sqlite connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- persona state ----------------------------------------------------

    def get_state(self, persona_id: str) -> PersonaState | None:
        row = self._conn.execute(
            "SELECT persona_id, mood, fatigue, recent_topics, updated_at_ms, "
            "       topics_aged_at_ms, state_json "
            "FROM persona_state WHERE persona_id = ?",
            (persona_id,),
        ).fetchone()
        if row is None:
            return None
        return PersonaState(
            persona_id=str(row["persona_id"]),
            mood=str(row["mood"]),
            fatigue=float(row["fatigue"]),
            recent_topics=_decode_json_list(row["recent_topics"]),
            updated_at_ms=int(row["updated_at_ms"]),
            topics_aged_at_ms=int(row["topics_aged_at_ms"]),
            state_json=_decode_json_dict(row["state_json"]),
        )

    def load_state(self, persona_id: str) -> PersonaState:
        """Return the stored state, or a fresh unpersisted one."""
        existing = self.get_state(persona_id)
        return existing if existing is not None else PersonaState(persona_id=persona_id)

    def upsert_state(self, state: PersonaState, now_ms: int | None = None) -> PersonaState:
        """Write *state*, dedup+capping topics and stamping the clocks.

        ``updated_at_ms == 0`` means "stamp now". ``topics_aged_at_ms == 0``
        initialises the topic clock to the same instant, so a brand-new row
        starts both clocks together instead of aging from the epoch.
        """
        stamp = int(time.time() * 1000) if now_ms is None else int(now_ms)
        topics = dedup_cap(list(state.recent_topics))
        updated = state.updated_at_ms or stamp
        aged = state.topics_aged_at_ms or stamp
        self._conn.execute(
            "INSERT INTO persona_state "
            "  (persona_id, mood, fatigue, recent_topics, updated_at_ms, "
            "   topics_aged_at_ms, state_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(persona_id) DO UPDATE SET "
            "  mood = excluded.mood, "
            "  fatigue = excluded.fatigue, "
            "  recent_topics = excluded.recent_topics, "
            "  updated_at_ms = excluded.updated_at_ms, "
            "  topics_aged_at_ms = excluded.topics_aged_at_ms, "
            "  state_json = excluded.state_json",
            (
                state.persona_id,
                state.mood,
                float(state.fatigue),
                json.dumps(topics, ensure_ascii=False),
                updated,
                aged,
                json.dumps(state.state_json, ensure_ascii=False),
            ),
        )
        self._conn.commit()
        return PersonaState(
            persona_id=state.persona_id,
            mood=state.mood,
            fatigue=state.fatigue,
            recent_topics=topics,
            updated_at_ms=updated,
            topics_aged_at_ms=aged,
            state_json=state.state_json,
        )

    def list_personas(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT persona_id FROM persona_state ORDER BY persona_id"
        ).fetchall()
        return [str(r["persona_id"]) for r in rows]

    # -- life events (append-only) ----------------------------------------

    def append_event(
        self,
        persona_id: str,
        text: str,
        *,
        salience: float = 1.0,
        kind: str = "life",
        created_at: float | None = None,
    ) -> int:
        """Append one event. Returns its rowid. Never updates an existing row."""
        body = str(text).strip()
        if not body:
            raise ValueError("life event text cannot be empty")
        stamp = time.time() if created_at is None else float(created_at)
        cur = self._conn.execute(
            "INSERT INTO life_events (persona_id, created_at, salience, text, kind) "
            "VALUES (?, ?, ?, ?, ?)",
            (persona_id, stamp, max(0.0, float(salience)), body, str(kind)),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def append_events(
        self, persona_id: str, events: Iterable[tuple[str, float, str]]
    ) -> int:
        """Bulk append ``(text, salience, kind)`` triples. Returns the count."""
        count = 0
        for text, salience, kind in events:
            self.append_event(persona_id, text, salience=salience, kind=kind)
            count += 1
        return count

    def count_events(self, persona_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM life_events WHERE persona_id = ?",
            (persona_id,),
        ).fetchone()
        return int(row["n"]) if row else 0

    def recent_events(
        self, persona_id: str, limit: int = 200, kinds: Sequence[str] | None = None
    ) -> list[LifeEvent]:
        """Newest-first raw rows, before decay. Used by retrieval and the CLI."""
        sql = "SELECT * FROM life_events WHERE persona_id = ?"
        params: list[Any] = [persona_id]
        if kinds:
            sql += " AND kind IN (%s)" % ",".join("?" for _ in kinds)
            params.extend(kinds)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(int(max(1, limit)))
        rows = self._conn.execute(sql, params).fetchall()
        return [
            LifeEvent(
                id=int(r["id"]),
                persona_id=str(r["persona_id"]),
                created_at=float(r["created_at"]),
                salience=float(r["salience"]),
                text=str(r["text"]),
                kind=str(r["kind"]),
            )
            for r in rows
        ]

    def retrieve(
        self,
        persona_id: str,
        *,
        top_n: int = DEFAULT_TOP_N,
        half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
        min_weight: float = DEFAULT_MIN_WEIGHT,
        now: float | None = None,
        scan_limit: int = 200,
        kinds: Sequence[str] | None = None,
    ) -> list[LifeEvent]:
        """Return the top-*n* events by ``salience * temporal_decay``.

        Decay is applied at read time, so changing ``half_life_days`` changes
        what surfaces without touching a single stored row.

        Ties break toward the newer event: two beats of equal weight should
        surface the one the persona lived most recently.
        """
        moment = time.time() if now is None else float(now)
        candidates = self.recent_events(persona_id, limit=scan_limit, kinds=kinds)
        scored: list[LifeEvent] = []
        for event in candidates:
            weight = event.salience * temporal_decay(
                event.created_at, moment, half_life_days
            )
            if weight < min_weight:
                continue
            scored.append(
                LifeEvent(
                    id=event.id,
                    persona_id=event.persona_id,
                    created_at=event.created_at,
                    salience=event.salience,
                    text=event.text,
                    kind=event.kind,
                    weight=weight,
                )
            )
        scored.sort(key=lambda e: (e.weight, e.created_at), reverse=True)
        return scored[: max(0, int(top_n))]

    def prune_events(
        self,
        persona_id: str,
        *,
        keep: int = 2000,
    ) -> int:
        """Drop the oldest rows beyond *keep*. Maintenance only, never on read.

        The log is append-only in the normal path; this exists so a
        multi-year deployment does not grow unbounded. Returns rows deleted.
        """
        cur = self._conn.execute(
            "DELETE FROM life_events WHERE persona_id = ? AND id NOT IN ("
            "  SELECT id FROM life_events WHERE persona_id = ? "
            "  ORDER BY created_at DESC, id DESC LIMIT ?"
            ")",
            (persona_id, persona_id, int(max(0, keep))),
        )
        self._conn.commit()
        return int(cur.rowcount or 0)


def _decode_json_list(raw: Any) -> list[str]:
    try:
        value = json.loads(str(raw) or "[]")
    except (ValueError, TypeError):
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


def _decode_json_dict(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(str(raw) or "{}")
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


__all__ = [
    "DEFAULT_HALF_LIFE_DAYS",
    "DEFAULT_MIN_WEIGHT",
    "DEFAULT_TOP_N",
    "GrantleyStore",
    "LifeEvent",
    "temporal_decay",
]
