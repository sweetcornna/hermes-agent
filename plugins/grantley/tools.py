"""``persona_life_*`` model-facing tools.

Ported from ``corlinman_agent/persona/life.py``'s six dispatchers. Wire names
are preserved exactly (``persona_life_get``, ``persona_life_set_state``,
``persona_life_diary_add``, ``persona_life_event_seed``,
``persona_life_set_seeds``, ``persona_life_get_seeds``) so any prompt, skill
or cron job written against corlinman keeps working.

They are exposed through :meth:`MemoryProvider.get_tool_schemas` /
:meth:`MemoryProvider.handle_tool_call` rather than ``ctx.register_tool``.
That is not a stylistic choice: a memory provider is loaded through
``plugins/memory/__init__.py``'s exclusive activation path, whose
``_ProviderCollector`` is not the general plugin manager — the provider's own
tool surface is the supported way for it to expose tools.

Every handler returns a **JSON string** (``AGENTS.md``: "all handlers must
return JSON strings") and never raises: a persona tool failing must degrade
the character, not kill the turn.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, Mapping

from . import life
from .state import PersonaState
from .store import GrantleyStore

PERSONA_LIFE_GET_TOOL = "persona_life_get"
PERSONA_LIFE_SET_STATE_TOOL = "persona_life_set_state"
PERSONA_LIFE_DIARY_ADD_TOOL = "persona_life_diary_add"
PERSONA_LIFE_EVENT_SEED_TOOL = "persona_life_event_seed"
PERSONA_LIFE_SET_SEEDS_TOOL = "persona_life_set_seeds"
PERSONA_LIFE_GET_SEEDS_TOOL = "persona_life_get_seeds"

PERSONA_LIFE_TOOLS: frozenset[str] = frozenset(
    {
        PERSONA_LIFE_GET_TOOL,
        PERSONA_LIFE_SET_STATE_TOOL,
        PERSONA_LIFE_DIARY_ADD_TOOL,
        PERSONA_LIFE_EVENT_SEED_TOOL,
        PERSONA_LIFE_SET_SEEDS_TOOL,
        PERSONA_LIFE_GET_SEEDS_TOOL,
    }
)


def _ok(payload: Mapping[str, Any]) -> str:
    return json.dumps(dict(payload), ensure_ascii=False)


def _err(code: str, message: str) -> str:
    """Render a failure envelope in the canonical shape. Ported verbatim."""
    return json.dumps(
        {"ok": False, "error": code, "message": message}, ensure_ascii=False
    )


def _load(store: GrantleyStore, persona_id: str) -> tuple[PersonaState, dict, list]:
    """Return ``(state, life_doc, diary)``, repairing malformed blobs."""
    state = store.load_state(persona_id)
    sj = state.state_json
    life_doc = life.repair_life(sj.get("life"))
    diary = sj.get("diary")
    if not isinstance(diary, list):
        diary = []
    return state, life_doc, list(diary)


def _save(
    store: GrantleyStore,
    state: PersonaState,
    life_doc: dict,
    diary: list,
    *,
    mood: str | None = None,
    push_topic: str | None = None,
) -> PersonaState:
    """Read-merge-upsert, mirroring the life onto the placeholder keys.

    ``mood`` is "explicit-provided" rather than "inherit-on-empty": ``None``
    leaves the column alone, an explicit ``""`` clears it. Ported verbatim —
    it is what stops a no-mood ``set_state`` from clobbering a mood the decay
    job manages.
    """
    sj = state.state_json
    sj["life"] = life_doc
    sj["diary"] = life.trim(list(diary), life.MAX_DIARY_ENTRIES)
    life.mirror_placeholder_keys(sj, life_doc)
    state.state_json = sj
    if mood is not None:
        state.mood = mood
    if push_topic and push_topic.strip():
        state.recent_topics = [*state.recent_topics, push_topic.strip()]
    # 0 tells upsert_state to stamp "now".
    state.updated_at_ms = 0
    return store.upsert_state(state)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def persona_life_get(
    args: Mapping[str, Any], *, store: GrantleyStore, persona_id: str
) -> str:
    try:
        tail = int(args.get("diary_tail") or 5)
    except (TypeError, ValueError):
        tail = 5
    tail = max(0, min(tail, 50))
    try:
        _state, life_doc, diary = _load(store, persona_id)
    except Exception as exc:  # noqa: BLE001 - a tool must never raise
        return _err("persona_life_get_failed", str(exc))

    history = life_doc.get("history") if isinstance(life_doc.get("history"), list) else []
    try:
        signals = life.compute_life_signals(life_doc, life.now_dt())
    except Exception:  # noqa: BLE001 - pure by contract, guarded anyway
        signals = {}
    return _ok(
        {
            "ok": True,
            "persona_id": persona_id,
            "current": life_doc.get("current", {}),
            "diary_tail": diary[-tail:] if tail else [],
            "history_tail": list(history)[-3:],
            "diary_total": len(diary),
            "signals": signals,
            "now": life.now_iso(),
        }
    )


def persona_life_set_state(
    args: Mapping[str, Any], *, store: GrantleyStore, persona_id: str
) -> str:
    new_state = str(args.get("state") or "").strip().lower()
    if new_state not in life.ALLOWED_STATES:
        return _err(
            "invalid_args",
            f"'state' must be one of {sorted(life.ALLOWED_STATES)} "
            f"(got {new_state!r}).",
        )
    try:
        state, life_doc, diary = _load(store, persona_id)
    except Exception as exc:  # noqa: BLE001
        return _err("persona_life_set_state_failed", str(exc))

    current: dict[str, Any] = dict(life_doc.get("current") or {})
    location_val = str(args.get("location") or current.get("location") or "").strip()
    activity_val = str(args.get("activity") or current.get("activity") or "").strip()
    weather_val = str(args.get("weather") or current.get("weather") or "").strip()
    raw_mood = args.get("mood")
    mood_arg = raw_mood.strip() if isinstance(raw_mood, str) else None
    mood_display = mood_arg if mood_arg is not None else str(current.get("mood") or "")

    incoming: dict[str, Any] = {
        "state": new_state,
        "location": location_val,
        "activity": activity_val,
        "companions": life.coerce_companions(
            args.get("companions", current.get("companions"))
        ),
        "mood": mood_display,
        "weather": weather_val,
        "since": life.now_iso(),
        "until_estimate": args.get("until_estimate") or None,
        "story_arc": (
            args.get("story_arc")
            if args.get("story_arc") is not None
            else current.get("story_arc")
        ),
    }
    reason = str(args.get("reason") or "").strip()
    diff_keys = [
        k
        for k in ("state", "location", "activity")
        if incoming[k] != current.get(k, "")
    ]
    history = list(life_doc.get("history") or [])
    if diff_keys and current:
        history.append(
            {"ts": life.now_iso(), "from": current, "to": incoming, "reason": reason}
        )
    life_doc["current"] = incoming
    life_doc["history"] = life.trim(history, life.MAX_HISTORY_ENTRIES)

    try:
        _save(
            store,
            state,
            life_doc,
            diary,
            mood=mood_arg,
            push_topic=activity_val or None,
        )
        # A model-authored state change is a life event worth remembering,
        # and it is the highest-salience kind: the persona chose it.
        if diff_keys:
            summary = f"{new_state}: {activity_val}" + (
                f" @ {location_val}" if location_val else ""
            )
            store.append_event(persona_id, summary, salience=1.0, kind="state_change")
    except Exception as exc:  # noqa: BLE001
        return _err("persona_life_set_state_failed", str(exc))

    return _ok(
        {
            "ok": True,
            "persona_id": persona_id,
            "current": incoming,
            "changed": diff_keys,
            "reason": reason or None,
        }
    )


def persona_life_diary_add(
    args: Mapping[str, Any], *, store: GrantleyStore, persona_id: str
) -> str:
    entry = str(args.get("entry") or "").strip()
    if not entry:
        return _err("invalid_args", "'entry' is required and cannot be empty.")
    if len(entry) > life.MAX_DIARY_CHARS:
        return _err(
            "invalid_args",
            f"'entry' must be under {life.MAX_DIARY_CHARS} characters.",
        )
    try:
        state, life_doc, diary = _load(store, persona_id)
    except Exception as exc:  # noqa: BLE001
        return _err("persona_life_diary_add_failed", str(exc))

    current = life_doc.get("current") or {}
    rec = {
        "ts": life.now_iso(),
        "entry": entry,
        "tag": str(args.get("tag") or "").strip().lower() or "thoughts",
        "mood": str(args.get("mood") or "").strip(),
        "location": str(args.get("location") or current.get("location") or "").strip(),
    }
    diary.append(rec)
    try:
        _save(store, state, life_doc, diary)
        store.append_event(persona_id, entry, salience=0.8, kind="diary")
    except Exception as exc:  # noqa: BLE001
        return _err("persona_life_diary_add_failed", str(exc))

    return _ok(
        {
            "ok": True,
            "persona_id": persona_id,
            "saved": rec,
            "diary_total": min(len(diary), life.MAX_DIARY_ENTRIES),
        }
    )


def persona_life_event_seed(
    args: Mapping[str, Any],
    *,
    persona_id: str,
    data_dir: Path | None = None,
    rng: random.Random | None = None,
) -> str:
    """Inspiration draw. Deliberately **not** day-seeded.

    Unlike the daily beat, this is the model asking for a fresh idea mid-turn;
    returning the same cues all day would defeat the point. Callers may still
    inject an RNG for tests.
    """
    kind = str(args.get("kind") or "mission").strip().lower()
    library = life.resolve_seed_library(persona_id, data_dir)
    try:
        draw = life.draw_event_seed(library, kind, rng or random.Random())
    except ValueError as exc:
        return _err("invalid_args", str(exc))
    return _ok(
        {
            "ok": True,
            "kind": kind,
            "persona_id": persona_id,
            "seed": draw,
            "note": (
                "这些只是灵感种子, 自己决定要不要用、怎么用. "
                "可以全用, 也可以全扔掉自己想."
            ),
        }
    )


def persona_life_get_seeds(
    args: Mapping[str, Any], *, data_dir: Path | None = None
) -> str:
    pid = str(args.get("persona_id") or "").strip()
    if not life.valid_persona_slug(pid):
        return _err("invalid_args", f"invalid persona_id {pid!r}.")
    override = life.override_seeds_path(pid, data_dir)
    return _ok(
        {
            "ok": True,
            "persona_id": pid,
            "seeds": life.resolve_seed_library(pid, data_dir),
            "has_override": bool(override is not None and override.is_file()),
        }
    )


def persona_life_set_seeds(
    args: Mapping[str, Any], *, data_dir: Path | None = None
) -> str:
    """Write an operator/author override seed file.

    Writes ``<data_dir>/persona_life/<persona_id>.events.yaml``. The bundled
    pack is never touched — Grantley's lore is irreplaceable and lives in
    version control, so authoring only ever produces an override layer on top.
    """
    pid = str(args.get("persona_id") or "").strip()
    if not life.valid_persona_slug(pid):
        return _err("invalid_args", f"invalid persona_id {pid!r}.")
    if data_dir is None:
        return _err("unavailable", "no writable data dir for seed overrides.")
    raw = args.get("seeds")
    cleaned = life.coerce_seed_mapping(raw)
    if not cleaned:
        return _err("invalid_args", "'seeds' must be a non-empty category mapping.")

    path = life.override_seeds_path(pid, data_dir)
    if path is None:
        return _err("invalid_args", f"invalid persona_id {pid!r}.")
    merged = cleaned
    if bool(args.get("merge")) and path.is_file():
        existing = life.resolve_seed_library(pid, data_dir)
        merged = {**existing, **cleaned}

    try:
        import yaml  # noqa: PLC0415 - base dependency, imported lazily

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            yaml.safe_dump(merged, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        tmp.replace(path)
    except Exception as exc:  # noqa: BLE001
        return _err("write_failed", str(exc))

    return _ok(
        {
            "ok": True,
            "persona_id": pid,
            "path": str(path),
            "categories": sorted(merged),
        }
    )


# ---------------------------------------------------------------------------
# Schemas — ported verbatim from ``life.py``'s ``*_tool_schema()`` functions
# ---------------------------------------------------------------------------


def tool_schemas() -> list[dict[str, Any]]:
    """Return every ``persona_life_*`` schema, OpenAI function shape."""
    return [
        {
            "type": "function",
            "function": {
                "name": PERSONA_LIFE_GET_TOOL,
                "description": (
                    "Read your current life-state (where you are, what you're "
                    "doing, who's with you, since when, expected return) plus "
                    "the tail of your private diary. Call this at the start of "
                    "a session when it's unclear where you 'are' in your "
                    "ongoing life."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "diary_tail": {
                            "type": "integer",
                            "description": "How many recent diary entries to return (0-50, default 5).",
                            "minimum": 0,
                            "maximum": 50,
                        },
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": PERSONA_LIFE_SET_STATE_TOOL,
                "description": (
                    "Update your life-state — call when you leave for a "
                    "mission, return from one, start travelling, etc. "
                    "Unprovided fields are inherited from the current state; "
                    "the previous state is archived to history with an optional "
                    "'reason'. 'mood' is mirrored onto your persona mood."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "state": {
                            "type": "string",
                            "enum": sorted(life.ALLOWED_STATES),
                            "description": (
                                "High-level state bucket. at_academy = 据点/学院日常; "
                                "on_mission = 出任务在外; traveling = 旅行散心; "
                                "resting = 假期/休养; training = 集训."
                            ),
                        },
                        "location": {
                            "type": "string",
                            "description": "Free-form place name (eg 北境森林, 海港小镇).",
                        },
                        "activity": {
                            "type": "string",
                            "description": "Free-form description of what you're doing right now.",
                        },
                        "companions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Names of people with you (empty list = alone).",
                        },
                        "mood": {"type": "string", "description": "Free-form mood word."},
                        "weather": {
                            "type": "string",
                            "description": "Optional weather note.",
                        },
                        "until_estimate": {
                            "type": "string",
                            "description": (
                                "Your own estimate of when this state ends — ISO "
                                "datetime or natural language ('三天后'). Pure note."
                            ),
                        },
                        "story_arc": {
                            "type": "string",
                            "description": (
                                "Optional short name for the ongoing arc "
                                "(eg '护送商队任务'). Pass empty string to clear."
                            ),
                        },
                        "reason": {
                            "type": "string",
                            "description": "One-line note on why the state changed.",
                        },
                    },
                    "required": ["state"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": PERSONA_LIFE_DIARY_ADD_TOOL,
                "description": (
                    "Append a PRIVATE diary entry — what you're actually "
                    "thinking and wouldn't post publicly: missions in "
                    "progress, decisions, regrets, feelings."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "entry": {
                            "type": "string",
                            "description": f"The diary text (under {life.MAX_DIARY_CHARS} chars).",
                        },
                        "tag": {
                            "type": "string",
                            "description": "Short tag: training, mission, travel, thoughts, dream, regret, …",
                        },
                        "mood": {
                            "type": "string",
                            "description": "Mood at the time of writing.",
                        },
                        "location": {
                            "type": "string",
                            "description": "Where you wrote this (defaults to current location).",
                        },
                    },
                    "required": ["entry"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": PERSONA_LIFE_EVENT_SEED_TOOL,
                "description": (
                    "Pull a random themed inspiration draw. Returns keyword "
                    "cues (scenario, location, companion, tension, weather, "
                    "mood, …) — NOT a finished story. Use them as a prompt to "
                    "yourself and write the actual story / mission in your own "
                    "voice; ignore any cue you dislike."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": ["mission", "travel", "academy", "freeform"],
                            "description": (
                                "mission = 出任务种子; travel = 旅行种子; "
                                "academy = 据点/日常种子; freeform = 各类全抽."
                            ),
                        },
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": PERSONA_LIFE_GET_SEEDS_TOOL,
                "description": (
                    "Read a persona's effective life-event seed library "
                    "(generic ← bundled pack ← operator override). Returns "
                    "``has_override`` so you know whether a custom file exists."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "persona_id": {
                            "type": "string",
                            "description": "Persona id to read the seed library for.",
                        },
                    },
                    "required": ["persona_id"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": PERSONA_LIFE_SET_SEEDS_TOOL,
                "description": (
                    "Author (write) a persona's life-event seed library — the "
                    "lore the persona 'lives' inside, drawn at random by "
                    "persona_life_event_seed. Each category maps to a list of "
                    "SHORT keyword cues (a few words each), not sentences. "
                    "Standard categories: mission_scenario, travel_destination, "
                    "academy_scene, companion, tension, weather, mood, "
                    "duration_hint, season_hint — any custom category is also "
                    "allowed (freeform draws them all). Writes an override "
                    "layer; the bundled pack is never modified."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "persona_id": {
                            "type": "string",
                            "description": "Target persona id.",
                        },
                        "seeds": {
                            "type": "object",
                            "description": (
                                "Mapping of category → list of short keyword "
                                'strings, e.g. {"companion": ["华生"], '
                                '"mission_scenario": ["调查离奇命案"]}.'
                            ),
                        },
                        "merge": {
                            "type": "boolean",
                            "description": (
                                "When true, layer the given categories over the "
                                "persona's existing seed file instead of "
                                "replacing it. Default false (replace)."
                            ),
                        },
                    },
                    "required": ["persona_id", "seeds"],
                    "additionalProperties": False,
                },
            },
        },
    ]


def dispatch(
    tool_name: str,
    args: Mapping[str, Any],
    *,
    store: GrantleyStore,
    persona_id: str,
    data_dir: Path | None = None,
) -> str:
    """Route a tool call. Unknown names produce a JSON error, never a raise."""
    if tool_name == PERSONA_LIFE_GET_TOOL:
        return persona_life_get(args, store=store, persona_id=persona_id)
    if tool_name == PERSONA_LIFE_SET_STATE_TOOL:
        return persona_life_set_state(args, store=store, persona_id=persona_id)
    if tool_name == PERSONA_LIFE_DIARY_ADD_TOOL:
        return persona_life_diary_add(args, store=store, persona_id=persona_id)
    if tool_name == PERSONA_LIFE_EVENT_SEED_TOOL:
        return persona_life_event_seed(args, persona_id=persona_id, data_dir=data_dir)
    if tool_name == PERSONA_LIFE_GET_SEEDS_TOOL:
        return persona_life_get_seeds(args, data_dir=data_dir)
    if tool_name == PERSONA_LIFE_SET_SEEDS_TOOL:
        return persona_life_set_seeds(args, data_dir=data_dir)
    return _err("unknown_tool", f"{tool_name!r} is not a persona_life tool.")


__all__ = [
    "PERSONA_LIFE_DIARY_ADD_TOOL",
    "PERSONA_LIFE_EVENT_SEED_TOOL",
    "PERSONA_LIFE_GET_SEEDS_TOOL",
    "PERSONA_LIFE_GET_TOOL",
    "PERSONA_LIFE_SET_SEEDS_TOOL",
    "PERSONA_LIFE_SET_STATE_TOOL",
    "PERSONA_LIFE_TOOLS",
    "dispatch",
    "tool_schemas",
]
