"""The two out-of-band evolution jobs (A3 G4). Neither makes an LLM call.

* :func:`run_decay` — recovers fatigue, flips a stale ``"tired"`` mood, ages
  ``recent_topics`` off the slow day clock. Replaces corlinman's
  ``persona.decay`` scheduler job.
* :func:`run_life_advance` — draws one coherent daily life beat from the seed
  pack and writes it into the life document, the diary, and the append-only
  event log. Replaces corlinman's ``persona.life_advance`` builtin.

Both are plain synchronous functions over an injected
:class:`~plugins.grantley.store.GrantleyStore`, so they run identically from
a hermes ``no_agent`` cron job, from a test, or from
``scripts/grantley_job.py``. Neither touches ``SOUL.md`` or any system-prompt
layer — that is the pattern ``AGENTS.md`` forbids, and it is why the whole
evolution path lives out of band and takes effect on the *next* session.

Historical note that shaped this module: in corlinman's production the decay
job ran 1260 times and failed 1260 times with ``data_dir_unavailable``,
because it resolved its data directory from an app-state attribute that was
never populated. The decay mechanic was well-specified and had never once
executed. Here the store is a required argument, so "no data dir" is a
construction-time error at the callsite rather than a silent per-run failure,
and both jobs return a structured result the cron notepad can record.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import life
from .decay import DecayConfig, apply_decay, carry_topic_clock, hours_between
from .store import GrantleyStore

#: Salience of an auto-drawn daily beat in the event log. Below a
#: model-authored state change (1.0) and a diary entry (0.8): an automatic
#: beat is real but it is not something the persona chose.
AUTO_BEAT_SALIENCE: float = 0.5


def run_decay(
    store: GrantleyStore,
    *,
    persona_ids: list[str] | None = None,
    config: DecayConfig | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Apply time decay to every persona row. Returns a structured result.

    The fatigue clock is stamped to *now* on every tick. The topic clock
    advances only by whole days actually consumed
    (:func:`~plugins.grantley.decay.carry_topic_clock`), so running this
    hourly still ages exactly one topic per day rather than none.
    """
    stamp = int(time.time() * 1000) if now_ms is None else int(now_ms)
    targets = persona_ids if persona_ids is not None else store.list_personas()
    scanned = 0
    changed = 0
    details: list[dict[str, Any]] = []

    for persona_id in targets:
        state = store.get_state(persona_id)
        if state is None:
            continue
        scanned += 1
        fatigue_hours = hours_between(state.updated_at_ms, stamp)
        topic_hours = hours_between(state.topics_aged_at_ms, stamp)
        decayed = apply_decay(state, fatigue_hours, config, topic_hours)

        mutated = (
            decayed.mood != state.mood
            or decayed.fatigue != state.fatigue
            or decayed.recent_topics != state.recent_topics
        )
        decayed.updated_at_ms = stamp
        decayed.topics_aged_at_ms = carry_topic_clock(state.topics_aged_at_ms, stamp)
        store.upsert_state(decayed, now_ms=stamp)
        if mutated:
            changed += 1
        details.append(
            {
                "persona_id": persona_id,
                "fatigue_hours": round(fatigue_hours, 4),
                "topic_hours": round(topic_hours, 4),
                "mood": decayed.mood,
                "fatigue": round(decayed.fatigue, 4),
                "topics": len(decayed.recent_topics),
            }
        )

    return {
        "ok": True,
        "job": "grantley.decay",
        "rows_scanned": scanned,
        "rows_changed": changed,
        "at_ms": stamp,
        "details": details,
    }


def run_life_advance(
    store: GrantleyStore,
    persona_id: str,
    *,
    data_dir: Path | None = None,
    when: datetime | None = None,
    rng: Any = None,
) -> dict[str, Any]:
    """Draw and apply one daily life beat. **No LLM call.**

    The draw is seeded from ``(persona_id, calendar date)`` unless *rng* is
    supplied, so the same day always yields the same beat no matter which
    process asks. That determinism is not cosmetic — the per-channel persona
    snapshot in :mod:`plugins.grantley.channel_binding` reads today's beat and
    must stay byte-stable for the life of a conversation.

    Writes, in order: the life document (archiving the previous ``current``
    to history), the flat ``life_*`` mirror keys, an auto diary line, the
    activity onto ``recent_topics``, the mood onto the native column, and one
    row into the append-only event log.
    """
    moment = when if when is not None else life.now_dt()
    draw_rng = rng if rng is not None else life.daily_rng(persona_id, moment)
    seed_lib = life.resolve_seed_library(persona_id, data_dir)
    beat = life.draw_life_beat(seed_lib, draw_rng)

    state = store.load_state(persona_id)
    sj = state.state_json
    life_doc = life.advance_life(sj.get("life"), beat, when=moment)

    diary = sj.get("diary")
    if not isinstance(diary, list):
        diary = []
    entry = life.beat_diary_entry(beat, moment)
    diary = list(diary)
    diary.append(entry)

    sj["life"] = life_doc
    sj["diary"] = life.trim(diary, life.MAX_DIARY_ENTRIES)
    life.mirror_placeholder_keys(sj, life_doc)
    state.state_json = sj

    activity = str(beat.get("activity") or "").strip()
    if activity:
        state.recent_topics = [*state.recent_topics, activity]
    if beat.get("mood"):
        state.mood = str(beat["mood"])
    # 0 tells upsert_state to stamp "now"; the topic clock is left alone so a
    # life beat does not reset topic aging (only run_decay advances it).
    state.updated_at_ms = 0
    saved = store.upsert_state(state)

    store.append_event(
        persona_id,
        entry["entry"],
        salience=AUTO_BEAT_SALIENCE,
        kind="auto_beat",
        created_at=moment.timestamp(),
    )

    return {
        "ok": True,
        "job": "grantley.life_advance",
        "persona_id": persona_id,
        "date": moment.date().isoformat(),
        "beat": beat,
        "current": life_doc["current"],
        "diary_total": len(saved.state_json.get("diary") or []),
    }


__all__ = ["AUTO_BEAT_SALIENCE", "run_decay", "run_life_advance"]
