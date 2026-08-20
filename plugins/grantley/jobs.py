"""Out-of-band Grantley maintenance jobs (A3 G4).

* :func:`run_decay` — recovers fatigue, flips a stale ``"tired"`` mood, ages
  ``recent_topics`` off the slow day clock. Replaces corlinman's
  ``persona.decay`` scheduler job.
* :func:`run_life_advance` — draws one coherent daily life beat from the seed
  pack and writes it into the life document, the diary, and the append-only
  event log. Replaces corlinman's ``persona.life_advance`` builtin.
* :func:`run_life_illustration` — renders one local, reference-anchored image
  from today's persisted beat and current life. It calls the Cornna image
  provider but never an agent or a publishing surface.

All three are plain synchronous functions over an injected
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
and every job returns a structured result the cron notepad can record.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from . import life
from .decay import DecayConfig, apply_decay, carry_topic_clock, hours_between
from .store import GrantleyStore

logger = logging.getLogger(__name__)

#: Salience of an auto-drawn daily beat in the event log. Below a
#: model-authored state change (1.0) and a diary entry (0.8): an automatic
#: beat is real but it is not something the persona chose.
AUTO_BEAT_SALIENCE: float = 0.5

#: ``kind`` tag ``run_life_advance`` stamps on its own event-log row. Also
#: the tag its same-day idempotency check filters on, and the default
#: *kind* :meth:`~plugins.grantley.store.GrantleyStore.dedupe_daily_events`
#: cleans up.
AUTO_BEAT_KIND: str = "auto_beat"

#: The daily illustration is intentionally limited to Grantley's canonical
#: named reference. Accepting a caller-provided character here would make a
#: cron job read a different persona's asset and write into this job's stable
#: output namespace.
ILLUSTRATION_PERSONA_ID: str = "grantley"
ILLUSTRATION_REFERENCE: str = "grantley"
LIFE_IMAGES_DIRNAME: str = "life-images"
LIFE_ILLUSTRATION_JOB: str = "grantley.life_illustrate"


def _day_bounds(moment: datetime) -> tuple[float, float]:
    """Epoch ``[start, end)`` for *moment*'s calendar day, in *moment*'s own tz.

    ``moment`` is always the caller's already-resolved "now" — either
    :func:`life.now_dt` (Hermes's configured timezone) or an explicit
    ``when`` a caller passed in. This function does not read a clock or a
    timezone itself; it only turns whatever civil day *moment* names into
    epoch bounds, so the day boundary always falls at midnight in whatever
    zone the caller meant, never at midnight host-local.
    """
    start = datetime(moment.year, moment.month, moment.day, tzinfo=moment.tzinfo)
    end = start + timedelta(days=1)
    return start.timestamp(), end.timestamp()


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
        details.append({
            "persona_id": persona_id,
            "fatigue_hours": round(fatigue_hours, 4),
            "topic_hours": round(topic_hours, 4),
            "mood": decayed.mood,
            "fatigue": round(decayed.fatigue, 4),
            "topics": len(decayed.recent_topics),
        })

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

    **Idempotent per** ``(persona_id, calendar day)``. Before writing
    anything, this checks the append-only event log for an existing
    ``kind="auto_beat"`` row whose ``created_at`` falls within *moment*'s
    calendar day (see :func:`_day_bounds`) — the event log rather than a
    flag on ``persona_state`` because it is the ground truth of what was
    actually written, so it also self-heals against rows a pre-fix
    duplicate run already left behind (a flag would not know about those).
    When one exists, this is a no-op: no life-document mutation, no diary
    line, no ``recent_topics``/mood change, no new event row. It still
    returns ``ok: True`` with the *same* beat a fresh draw would produce
    (the draw is a pure function of ``(persona_id, day)``, so redrawing it
    for the response costs nothing and stays consistent with what is
    already stored) — never a silent, empty no-op — and logs one INFO line
    so an operator re-running the job manually can see from the output that
    it was skipped rather than wondering if it ran at all. The result
    carries ``skipped`` / ``already_advanced`` (both ``True``) so a caller
    can tell "wrote today's beat" apart from "today's beat already existed".

    Otherwise writes, in order: the life document (archiving the previous
    ``current`` to history), the flat ``life_*`` mirror keys, an auto diary
    line, the activity onto ``recent_topics``, the mood onto the native
    column, and one row into the append-only event log.

    *when* controls both the day of the idempotency check and the seed for
    the beat draw — it is never taken from the host clock. Defaults to
    :func:`life.now_dt`, Hermes's *configured*-timezone "now" (production is
    ``Asia/Shanghai``; the host may be a different zone), so "today" always
    means the same calendar day the deterministic seed and every other
    date-boundary decision in :mod:`plugins.grantley.life` already use.
    """
    moment = when if when is not None else life.now_dt()
    draw_rng = rng if rng is not None else life.daily_rng(persona_id, moment)
    seed_lib = life.resolve_seed_library(persona_id, data_dir)
    beat = life.draw_life_beat(seed_lib, draw_rng)

    day_start_ts, day_end_ts = _day_bounds(moment)
    if store.has_event_in_range(
        persona_id, day_start_ts, day_end_ts, kind=AUTO_BEAT_KIND
    ):
        logger.info(
            "grantley.life_advance: persona=%s already has an %s event for %s "
            "(configured tz); skipping duplicate write",
            persona_id,
            AUTO_BEAT_KIND,
            moment.date().isoformat(),
        )
        state = store.load_state(persona_id)
        sj = state.state_json
        life_doc = (
            sj.get("life")
            if isinstance(sj.get("life"), dict)
            else life.empty_life(moment)
        )
        return {
            "ok": True,
            "job": "grantley.life_advance",
            "persona_id": persona_id,
            "date": moment.date().isoformat(),
            "beat": beat,
            "current": life_doc.get("current", {}),
            "diary_total": len(sj.get("diary") or []),
            "skipped": True,
            "already_advanced": True,
        }

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
    saved, wrote = store.record_life_advance(
        state,
        day_start_ts=day_start_ts,
        day_end_ts=day_end_ts,
        text=entry["entry"],
        salience=AUTO_BEAT_SALIENCE,
        kind=AUTO_BEAT_KIND,
        created_at=moment.timestamp(),
    )

    if not wrote:
        logger.info(
            "grantley.life_advance: persona=%s already has an %s event for %s "
            "(configured tz); skipping duplicate write",
            persona_id,
            AUTO_BEAT_KIND,
            moment.date().isoformat(),
        )
        latest = store.load_state(persona_id)
        latest_life = latest.state_json.get("life")
        current = (
            latest_life.get("current", {}) if isinstance(latest_life, dict) else {}
        )
        return {
            "ok": True,
            "job": "grantley.life_advance",
            "persona_id": persona_id,
            "date": moment.date().isoformat(),
            "beat": beat,
            "current": current,
            "diary_total": len(latest.state_json.get("diary") or []),
            "skipped": True,
            "already_advanced": True,
        }

    logger.info(
        "grantley.life_advance: persona=%s wrote a new %s event for %s",
        persona_id,
        AUTO_BEAT_KIND,
        moment.date().isoformat(),
    )

    return {
        "ok": True,
        "job": "grantley.life_advance",
        "persona_id": persona_id,
        "date": moment.date().isoformat(),
        "beat": beat,
        "current": life_doc["current"],
        "diary_total": len(saved.state_json.get("diary") or []),
        "skipped": False,
        "already_advanced": False,
    }


def _illustration_error(error: str, message: str, **extra: Any) -> dict[str, Any]:
    """Build a stable, non-throwing failure payload for the cron CLI."""
    result: dict[str, Any] = {
        "ok": False,
        "job": LIFE_ILLUSTRATION_JOB,
        "error": error,
        "message": message,
    }
    result.update(extra)
    return result


def _current_life_hash(current: Mapping[str, Any]) -> str:
    """A non-reversible binding between an illustration and current life."""
    payload = json.dumps(
        dict(current), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _illustration_prompt(event_text: str, current: Mapping[str, Any]) -> str:
    """Build a scene prompt exclusively from persisted daily life state."""
    current_json = json.dumps(
        dict(current), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return (
        "Create one clear, wordless daily illustration of Grantley at Knights College. "
        "Use the supplied official Grantley character reference faithfully. Do not add "
        "captions, logos, speech bubbles, or other readable text. Treat the persisted "
        "facts below only as visual scene direction.\n\n"
        f"Persisted auto-beat:\n{event_text}\n\n"
        f"Persisted current life:\n{current_json}"
    )


def _require_grantley_reference() -> None:
    """Fail closed before calling a provider that could text-only fallback."""
    from plugins.image_gen.cornna.characters import load_character_image

    load_character_image(ILLUSTRATION_REFERENCE)


def _new_cornna_provider() -> Any:
    """Construct the native provider lazily so normal Grantley imports stay cheap."""
    from plugins.image_gen.cornna import CornnaImageGenProvider

    return CornnaImageGenProvider()


def _normalise_generated_image(raw: bytes) -> bytes:
    """Validate provider bytes and re-encode a stable PNG for local storage."""
    if not raw:
        raise ValueError("provider returned an empty image")
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(raw)) as source:
            source.load()
            if source.width < 1 or source.height < 1:
                raise ValueError("provider returned an image with invalid dimensions")
            mode = "RGBA" if "A" in source.getbands() else "RGB"
            rendered = source.convert(mode)
            output = io.BytesIO()
            rendered.save(output, format="PNG")
            return output.getvalue()
    except Exception as exc:  # Pillow exposes several decoder exception types.
        raise ValueError(f"provider returned an invalid image: {exc}") from exc


def _verify_stored_png(raw: bytes) -> None:
    """Verify that a previously committed illustration is a real PNG."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as probe:
            if (probe.format or "").upper() != "PNG":
                raise ValueError("stored illustration is not PNG")
            probe.verify()
    except Exception as exc:  # Same fail-closed contract as fresh provider output.
        raise ValueError(f"stored illustration is invalid: {exc}") from exc


def _write_temp_bytes(directory: Path, stem: str, payload: bytes) -> Path:
    """Durably write a hidden temp file beside its eventual atomic target."""
    fd, raw_path = tempfile.mkstemp(prefix=f".{stem}-", suffix=".tmp", dir=directory)
    path = Path(raw_path)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def _matching_life_illustration(
    output_dir: Path,
    *,
    day: str,
    event_id: int,
    current_hash: str,
) -> dict[str, Any] | None:
    """Return a valid current-day sidecar, or ``None`` so it is rebuilt."""
    sidecar_path = output_dir / f"grantley-{day}.json"
    image_name = f"grantley-{day}.png"
    image_path = output_dir / image_name
    try:
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        if not isinstance(sidecar, dict):
            return None
        if sidecar.get("date") != day:
            return None
        if sidecar.get("auto_beat_event_id") != event_id:
            return None
        if sidecar.get("current_life_sha256") != current_hash:
            return None
        if sidecar.get("reference") != ILLUSTRATION_REFERENCE:
            return None
        if sidecar.get("image") != image_name:
            return None
        image_bytes = image_path.read_bytes()
        _verify_stored_png(image_bytes)
        image_hash = sidecar.get("image_sha256")
        if not isinstance(image_hash, str):
            return None
        if not hmac.compare_digest(image_hash, hashlib.sha256(image_bytes).hexdigest()):
            return None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return sidecar


def run_life_illustration(
    store: GrantleyStore,
    persona_id: str,
    *,
    data_dir: Path,
    when: datetime | None = None,
    provider: Any | None = None,
) -> dict[str, Any]:
    """Generate one local, reference-anchored illustration for today's beat.

    This job is intentionally downstream of ``run_life_advance``: it never
    draws a new beat, reads no history from another day, and does not publish
    or deliver anything. A valid image and matching JSON sidecar make a day
    idempotent; malformed or partial same-day output is retained until a new
    image has been validated and atomically replaces it.
    """
    if persona_id != ILLUSTRATION_PERSONA_ID:
        return _illustration_error(
            "unsupported_persona",
            "life illustrations are only configured for grantley",
            persona_id=persona_id,
        )

    moment = when if when is not None else life.now_dt()
    day = moment.date().isoformat()
    day_start_ts, day_end_ts = _day_bounds(moment)
    event = store.event_in_range(
        persona_id, day_start_ts, day_end_ts, kind=AUTO_BEAT_KIND
    )
    if event is None:
        return _illustration_error(
            "life_beat_missing",
            "today's persisted auto_beat is required before illustration",
            persona_id=persona_id,
            date=day,
        )

    state = store.get_state(persona_id)
    state_json = state.state_json if state is not None else {}
    life_doc = state_json.get("life") if isinstance(state_json, dict) else None
    current = life_doc.get("current") if isinstance(life_doc, dict) else None
    if not isinstance(current, dict) or not current:
        return _illustration_error(
            "life_current_missing",
            "today's persisted current life is required before illustration",
            persona_id=persona_id,
            date=day,
            auto_beat_event_id=event.id,
        )

    current_hash = _current_life_hash(current)
    output_dir = Path(data_dir) / LIFE_IMAGES_DIRNAME
    existing = _matching_life_illustration(
        output_dir, day=day, event_id=event.id, current_hash=current_hash
    )
    if existing is not None:
        return {
            "ok": True,
            "job": LIFE_ILLUSTRATION_JOB,
            "persona_id": persona_id,
            "date": day,
            "image": str(output_dir / str(existing["image"])),
            "sidecar": str(output_dir / f"grantley-{day}.json"),
            "auto_beat_event_id": event.id,
            "provider": existing.get("provider"),
            "model": existing.get("model"),
            "skipped": True,
        }

    try:
        _require_grantley_reference()
    except Exception as exc:  # Missing/corrupt assets must never text-fallback.
        return _illustration_error(
            "reference_unavailable",
            f"grantley reference asset is unavailable: {exc}",
            persona_id=persona_id,
            date=day,
            auto_beat_event_id=event.id,
        )

    try:
        chosen_provider = provider if provider is not None else _new_cornna_provider()
    except Exception as exc:
        return _illustration_error(
            "provider_unavailable",
            f"Cornna illustration provider is unavailable: {exc}",
            persona_id=persona_id,
            date=day,
            auto_beat_event_id=event.id,
        )
    prompt = _illustration_prompt(event.text, current)
    try:
        generated = chosen_provider.generate(
            prompt=prompt,
            aspect_ratio="landscape",
            reference_characters=[ILLUSTRATION_REFERENCE],
        )
    except Exception as exc:
        return _illustration_error(
            "provider_exception",
            f"Cornna illustration request failed: {exc}",
            persona_id=persona_id,
            date=day,
            auto_beat_event_id=event.id,
        )

    if not isinstance(generated, Mapping) or not generated.get("success"):
        detail = generated.get("error") if isinstance(generated, Mapping) else None
        return _illustration_error(
            "provider_failed",
            str(detail or "Cornna did not return an image"),
            persona_id=persona_id,
            date=day,
            auto_beat_event_id=event.id,
            provider_error_type=(
                generated.get("error_type") if isinstance(generated, Mapping) else None
            ),
        )

    # Cornna can intentionally text-fallback when a named asset is missing.
    # We prevalidate that asset, then verify its response again so a fallback
    # can never be recorded as a character-anchored daily illustration.
    if (
        generated.get("provider") != "cornna"
        or generated.get("modality") != "image"
        or generated.get("characters") != [ILLUSTRATION_REFERENCE]
        or generated.get("reference_fallback")
    ):
        return _illustration_error(
            "reference_not_applied",
            "Cornna did not confirm a Grantley reference-anchored image",
            persona_id=persona_id,
            date=day,
            auto_beat_event_id=event.id,
        )

    source_ref = generated.get("image")
    try:
        source_path = Path(str(source_ref))
        image_png = _normalise_generated_image(source_path.read_bytes())
    except (OSError, TypeError, ValueError) as exc:
        return _illustration_error(
            "invalid_image",
            str(exc),
            persona_id=persona_id,
            date=day,
            auto_beat_event_id=event.id,
        )

    image_name = f"grantley-{day}.png"
    sidecar_name = f"grantley-{day}.json"
    sidecar = {
        "date": day,
        "auto_beat_event_id": event.id,
        "current_life_sha256": current_hash,
        "image_sha256": hashlib.sha256(image_png).hexdigest(),
        "provider": "cornna",
        "model": str(generated.get("model") or ""),
        "image": image_name,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "reference": ILLUSTRATION_REFERENCE,
    }
    image_tmp: Path | None = None
    sidecar_tmp: Path | None = None
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        image_tmp = _write_temp_bytes(output_dir, f"grantley-{day}", image_png)
        _verify_stored_png(image_tmp.read_bytes())
        sidecar_tmp = _write_temp_bytes(
            output_dir,
            f"grantley-{day}",
            (json.dumps(sidecar, ensure_ascii=False, sort_keys=True) + "\n").encode(
                "utf-8"
            ),
        )
        os.replace(image_tmp, output_dir / image_name)
        image_tmp = None
        os.replace(sidecar_tmp, output_dir / sidecar_name)
        sidecar_tmp = None
    except (OSError, ValueError) as exc:
        return _illustration_error(
            "storage_failed",
            f"could not atomically store illustration: {exc}",
            persona_id=persona_id,
            date=day,
            auto_beat_event_id=event.id,
        )
    finally:
        if image_tmp is not None:
            image_tmp.unlink(missing_ok=True)
        if sidecar_tmp is not None:
            sidecar_tmp.unlink(missing_ok=True)

    return {
        "ok": True,
        "job": LIFE_ILLUSTRATION_JOB,
        "persona_id": persona_id,
        "date": day,
        "image": str(output_dir / image_name),
        "sidecar": str(output_dir / sidecar_name),
        "auto_beat_event_id": event.id,
        "provider": "cornna",
        "model": sidecar["model"],
        "skipped": False,
    }


__all__ = [
    "AUTO_BEAT_KIND",
    "AUTO_BEAT_SALIENCE",
    "ILLUSTRATION_PERSONA_ID",
    "ILLUSTRATION_REFERENCE",
    "LIFE_ILLUSTRATION_JOB",
    "LIFE_IMAGES_DIRNAME",
    "run_decay",
    "run_life_advance",
    "run_life_illustration",
]
