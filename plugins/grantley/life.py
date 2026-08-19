"""Life document, seed library, life-beat draw, and life-rhythm signals.

Ported from ``corlinman_agent/persona/life.py`` (the life document shape,
the seed-library resolution chain, ``_mirror_placeholder_keys``, and
``compute_life_signals``) and from the scheduler builtin
``persona_life_advance.py`` (the no-LLM beat draw).

Two properties are load-bearing and are preserved deliberately:

**No LLM call.** The daily beat is drawn from the seed pack by sampling, not
by generation. That is a cost and reliability feature, not an accident: the
beat still lands when the model is down, and it costs nothing.

**Deterministic per (persona, day).** This is a *change* from the source,
which used a bare ``random.Random()``. A deterministic draw is required by
the caching design: the per-channel persona snapshot (see
:mod:`plugins.grantley.channel_binding`) must be byte-stable for the life of
a conversation, so any process that asks "what is today's beat?" — the cron
job, the gateway, a test — has to get the same answer. Seeding from
``(persona_id, date)`` gives that for free and makes the draw testable.
Callers who genuinely want fresh randomness (the model-facing
``event_seed`` inspiration draw) pass their own RNG.
"""

from __future__ import annotations

import hashlib
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

#: Bump when the on-``state_json`` layout changes incompatibly.
SCHEMA_VERSION: int = 1

#: Hard caps so a persona row never grows without bound. Ported verbatim.
MAX_DIARY_ENTRIES: int = 200
MAX_HISTORY_ENTRIES: int = 100
MAX_DIARY_CHARS: int = 4000

#: Caps on an authored / override seed library so a bad YAML can't blow up.
MAX_SEED_CATEGORIES: int = 40
MAX_SEED_ITEMS_PER_CATEGORY: int = 200
MAX_SEED_ITEM_CHARS: int = 200

#: Allowed top-level life states. Free-form ``location`` / ``activity`` let
#: the model express anything; ``state`` is constrained so a prompt or a
#: scheduler can branch on it deterministically. Ported verbatim.
ALLOWED_STATES: frozenset[str] = frozenset(
    {
        "at_academy",  # 学院/据点日常: 上课 / 训练 / 食堂 / 宿舍
        "on_mission",  # 在外执行任务
        "traveling",  # 纯旅行 / 探亲 / 散心
        "resting",  # 假期回家 / 长睡 / 病中
        "training",  # 集训 / 武试营
        "unknown",  # 模型不确定 — 提示需要 set_state
    }
)

#: Life-rhythm signal thresholds. Ported verbatim.
OUTING_STATES: frozenset[str] = frozenset({"on_mission", "traveling"})
OUTING_OVERDUE_DAYS: int = 13
SAME_STATE_STALE_DAYS: int = 6
OUTING_TOO_LONG_DAYS: int = 8

#: Generic neutral seed library — the fallback for a persona shipping no
#: bundled pack and no operator override. Deliberately bland (a scaffold,
#: not lore) so it reads as "fill this in" rather than borrowing another
#: character's world. Ported verbatim from ``life._GENERIC_SEEDS``.
GENERIC_SEEDS: dict[str, list[str]] = {
    "mission_scenario": [
        "帮人找回丢失的东西",
        "护送某人去一个地方",
        "调查一桩说不清的小事",
        "替朋友跑一趟腿",
        "处理一个临时冒出来的麻烦",
    ],
    "travel_destination": [
        "海边小镇",
        "山里的村子",
        "热闹的集市",
        "安静的旧城区",
        "没去过的远方",
    ],
    "academy_scene": [
        "日常训练",
        "食堂吃饭",
        "图书馆消磨时间",
        "走廊里闲聊",
        "屋顶上发呆",
    ],
    "companion": ["独自一人", "一个老朋友", "新认识的人", "一只跟着的小动物"],
    "tension": [
        "天气突然变了",
        "时间比想的紧",
        "遇到了熟人",
        "计划出了点岔子",
        "一切顺利得反常",
    ],
    "weather": ["晴", "阴", "小雨", "大雾", "雪", "闷热", "凉风", "夜风"],
    "mood": ["兴奋", "犯困", "心情复杂", "无聊", "警觉", "懒洋洋", "认真", "想家"],
    "duration_hint": ["半天", "一整天", "两三天", "一周左右", "看情况"],
    "season_hint": ["初春", "盛夏", "初秋", "深秋", "初冬", "雪季"],
}

#: Companion sentinel meaning "no companion" — a drawn ``独自一人`` yields an
#: empty companion list rather than a companion literally named "alone".
SOLO_COMPANION: str = "独自一人"

#: Activity-source category → life-state bucket. The source
#: (``persona_life_advance._draw_life_beat._PRIORITY``) treated this tuple as a
#: strict priority list; we draw uniformly among the non-empty categories
#: instead. See :func:`draw_life_beat` for why (decision D18).
_BEAT_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("academy_scene", "at_academy"),
    ("mission_scenario", "on_mission"),
    ("travel_destination", "traveling"),
)

#: Inspiration-draw pools per ``kind``. Ported verbatim from
#: ``dispatch_persona_life_event_seed``.
EVENT_SEED_POOLS: dict[str, dict[str, str]] = {
    "mission": {
        "scenario": "mission_scenario",
        "companion": "companion",
        "tension": "tension",
        "weather": "weather",
        "duration_hint": "duration_hint",
        "season_hint": "season_hint",
        "mood": "mood",
    },
    "travel": {
        "destination": "travel_destination",
        "companion": "companion",
        "tension": "tension",
        "weather": "weather",
        "duration_hint": "duration_hint",
        "season_hint": "season_hint",
        "mood": "mood",
    },
    "academy": {
        "scene": "academy_scene",
        "companion": "companion",
        "weather": "weather",
        "mood": "mood",
    },
}


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def now_dt() -> datetime:
    """Local-time-aware "now" — the anchor for :func:`compute_life_signals`."""
    return datetime.now(timezone.utc).astimezone()


def now_iso(when: datetime | None = None) -> str:
    """Timezone-aware ISO timestamp the model can parse and format."""
    moment = when if when is not None else now_dt()
    return moment.isoformat(timespec="seconds")


def parse_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 string into a datetime, or ``None``.

    Tolerates non-strings / blanks / malformed values so a corrupt ``since``
    or history ``ts`` degrades to "signal omitted" rather than raising —
    this whole surface is best-effort.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except (ValueError, TypeError):
        return None


def delta_days(now: datetime, then: datetime) -> float | None:
    """Fractional days from *then* to *now*; ``None`` on any error.

    Normalises a naive/aware mismatch by dropping tzinfo from both so the
    subtraction cannot raise (stored stamps are aware, but a hand-authored
    or migrated document might not be).
    """
    try:
        if (now.tzinfo is None) != (then.tzinfo is None):
            now = now.replace(tzinfo=None)
            then = then.replace(tzinfo=None)
        delta = now - then
    except (TypeError, ValueError, OverflowError):
        return None
    return delta.total_seconds() / 86400.0


def trim(items: list[Any], cap: int) -> list[Any]:
    """Cap a list to its last *cap* entries (cheap FIFO trim)."""
    return items[-cap:] if len(items) > cap else items


# ---------------------------------------------------------------------------
# Life document
# ---------------------------------------------------------------------------


def empty_life(when: datetime | None = None) -> dict[str, Any]:
    """A freshly-initialised life document for ``state_json["life"]``."""
    return {
        "schema_version": SCHEMA_VERSION,
        "current": {
            "state": "at_academy",
            "location": "",
            "activity": "日常",
            "companions": [],
            "mood": "",
            "weather": "",
            "since": now_iso(when),
            "until_estimate": None,
            "story_arc": None,
        },
        "history": [],
    }


def coerce_companions(value: Any) -> list[str]:
    """Normalise a companions value into a list of non-empty strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def repair_life(raw: Any, when: datetime | None = None) -> dict[str, Any]:
    """Return a structurally-valid life document from possibly-corrupt input."""
    base = empty_life(when)
    if not isinstance(raw, dict):
        return base
    life = dict(raw)
    if not isinstance(life.get("current"), dict):
        life["current"] = base["current"]
    if not isinstance(life.get("history"), list):
        life["history"] = []
    life.setdefault("schema_version", SCHEMA_VERSION)
    return life


def mirror_placeholder_keys(state_json: dict[str, Any], life: dict[str, Any]) -> None:
    """Mirror the salient ``life["current"]`` fields onto flat ``life_*`` keys.

    This is the load-bearing link to the placeholder layer: the resolver
    reads top-level ``state_json`` keys, so the prompt can interpolate
    ``{{persona.life_state}}`` / ``{{persona.life_location}}`` /
    ``{{persona.life_activity}}`` / ``{{persona.life_companions}}`` /
    ``{{persona.life_story_arc}}`` without the model calling a tool.

    ``mood`` rides the native column instead, and ``activity`` feeds
    ``recent_topics`` — both already first-class placeholder keys.
    """
    raw_current = life.get("current")
    current: dict[str, Any] = raw_current if isinstance(raw_current, dict) else {}
    companions = current.get("companions")
    companions_str = (
        ", ".join(str(c) for c in companions) if isinstance(companions, list) else ""
    )
    state_json["life_state"] = str(current.get("state") or "")
    state_json["life_location"] = str(current.get("location") or "")
    state_json["life_activity"] = str(current.get("activity") or "")
    state_json["life_companions"] = companions_str
    state_json["life_story_arc"] = str(current.get("story_arc") or "")


# ---------------------------------------------------------------------------
# Seed library
# ---------------------------------------------------------------------------


def valid_persona_slug(persona_id: str) -> bool:
    """True iff *persona_id* is a safe filename slug.

    Blocks path traversal (``..``, ``/``, ``\\``) and any non-ascii char,
    because the id is interpolated into a filename in both the bundled and
    the override lookup. Mirrors the source rule: stripping ``_``/``-`` must
    leave a non-empty ascii-alphanumeric run.
    """
    if not persona_id:
        return False
    stripped = persona_id.replace("_", "").replace("-", "")
    return bool(stripped) and stripped.isascii() and stripped.isalnum()


def coerce_seed_mapping(loaded: Any) -> dict[str, list[str]] | None:
    """Clean a parsed YAML mapping into ``{category: [cue, ...]}``."""
    if not isinstance(loaded, Mapping):
        return None
    out: dict[str, list[str]] = {}
    for key, value in loaded.items():
        if isinstance(value, list) and value:
            items = [
                str(item).strip()[:MAX_SEED_ITEM_CHARS]
                for item in value[:MAX_SEED_ITEMS_PER_CATEGORY]
                if str(item).strip()
            ]
            if items:
                out[str(key)] = items
        if len(out) >= MAX_SEED_CATEGORIES:
            break
    return out or None


def _read_yaml_mapping(path: Path) -> dict[str, list[str]] | None:
    """Parse a seed YAML file, returning ``None`` on any problem.

    ``yaml`` is a base hermes dependency (``pyyaml==6.0.3`` in
    ``pyproject.toml``), so this adds no install footprint. The import is
    local so a broken PyYAML cannot take the plugin down at import time.
    """
    try:
        import yaml  # noqa: PLC0415 - local import keeps module import cheap
    except ImportError:  # pragma: no cover - pyyaml is a base dependency
        return None
    try:
        text = path.read_text(encoding="utf-8")
        return coerce_seed_mapping(yaml.safe_load(text))
    except Exception:  # noqa: BLE001 - OSError / yaml.YAMLError / anything
        return None


def bundled_seeds_path(persona_id: str) -> Path | None:
    """Path to the bundled seed pack for *persona_id*, if the slug is safe."""
    if not valid_persona_slug(persona_id):
        return None
    return Path(__file__).resolve().parent / "assets" / "life_seeds" / f"{persona_id}.yaml"


def override_seeds_path(persona_id: str, data_dir: Path | None) -> Path | None:
    """Path to the operator override ``<data_dir>/persona_life/<id>.events.yaml``."""
    if data_dir is None or not valid_persona_slug(persona_id):
        return None
    return Path(data_dir) / "persona_life" / f"{persona_id}.events.yaml"


def resolve_seed_library(
    persona_id: str | None, data_dir: Path | None = None
) -> dict[str, list[str]]:
    """Resolve the active seed library: generic ← bundled pack ← override.

    Each layer that resolves is merged *over* the generic base, so a partial
    override replaces only the categories it names.
    """
    merged: dict[str, list[str]] = {k: list(v) for k, v in GENERIC_SEEDS.items()}
    pid = (persona_id or "").strip()
    if not pid:
        return merged

    bundled_path = bundled_seeds_path(pid)
    if bundled_path is not None and bundled_path.is_file():
        bundled = _read_yaml_mapping(bundled_path)
        if bundled:
            merged.update(bundled)

    override_path = override_seeds_path(pid, data_dir)
    if override_path is not None and override_path.is_file():
        override = _read_yaml_mapping(override_path)
        if override:
            merged.update(override)

    return merged


# ---------------------------------------------------------------------------
# Life-beat draw (no LLM)
# ---------------------------------------------------------------------------


def daily_seed(persona_id: str, day: str) -> int:
    """Derive a stable integer seed from ``(persona_id, day)``.

    BLAKE2b rather than :func:`hash` because ``PYTHONHASHSEED`` randomises
    ``hash()`` per process — the whole point here is that two processes
    agree on the same day's beat.
    """
    digest = hashlib.blake2b(
        f"{persona_id}\x00{day}".encode("utf-8"), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big")


def daily_rng(persona_id: str, when: datetime | None = None) -> random.Random:
    """Return the RNG for *persona_id*'s beat on *when*'s calendar day."""
    moment = when if when is not None else now_dt()
    return random.Random(daily_seed(persona_id, moment.date().isoformat()))


def draw_life_beat(
    seed_lib: Mapping[str, list[str]],
    rng: random.Random,
) -> dict[str, Any]:
    """Draw a single coherent life beat from *seed_lib*. No LLM call.

    The activity source is drawn **uniformly at random among the non-empty**
    categories:

    * ``academy_scene``      → life state ``"at_academy"``
    * ``mission_scenario``   → life state ``"on_mission"``
    * ``travel_destination`` → life state ``"traveling"``
    * nothing populated      → generic ``"at_academy"`` / ``"日常"``

    Companion, weather and mood are drawn from the *same* library instance,
    so the persona's own lore (Grantley's named companions, his 骑士学院
    weather) is used rather than the generic fallback.

    Deliberate deviation from the source (decision **D18**)
    -------------------------------------------------------
    ``persona_life_advance._draw_life_beat`` walked the three categories as a
    **strict priority list** and ``break``-ed on the first non-empty one. That
    has two coupled consequences, and they are mutually exclusive:

    * ``travel_destination`` is last, so it is only ever selected when
      ``academy_scene`` *and* ``mission_scenario`` are both empty;
    * but the "re-draw the activity from the mission/academy pool" branch
      inside the travel case requires one of those two pools to be non-empty.

    So the re-draw branch was unreachable dead code, and because
    ``grantley.yaml`` populates both ``academy_scene`` and
    ``mission_scenario``, the ``traveling`` state could never be drawn at
    all — the pack's ten travel destinations were dead data and the character
    could never leave the academy on an automatic beat.

    We fix it rather than preserve it. ``persona.life_advance`` was never
    enabled in corlinman production (``[persona.life_advance] enabled =
    false``, zero recorded runs), so there is no live behaviour to stay
    compatible with and the change carries no regression risk. Keeping a
    plainly-unintended dead branch would permanently cost the character an
    entire class of life scene.

    *rng* is required rather than defaulted: a caller that wants today's
    canonical beat passes :func:`daily_rng`, and a caller that wants fresh
    randomness passes its own. Making it explicit is what stopped the
    source's implicit ``random.Random()`` from silently producing a
    different "today" in every process.
    """
    activity = "日常"
    life_state = "at_academy"
    location = ""

    candidates = [
        (category, bucket)
        for category, bucket in _BEAT_CATEGORIES
        if seed_lib.get(category)
    ]
    if candidates:
        category, bucket = rng.choice(candidates)
        activity = rng.choice(list(seed_lib[category]))
        life_state = bucket
        if category == "travel_destination":
            # For travel the category *is* the location pool; re-draw the
            # activity from a scene pool so "what he's doing" and "where he
            # is" are not the same string. Reachable only because the
            # category choice above is no longer strictly ordered.
            location = activity
            for alt_cat in ("mission_scenario", "academy_scene"):
                alt = list(seed_lib.get(alt_cat) or [])
                if alt:
                    activity = rng.choice(alt)
                    break

    companions_pool = list(seed_lib.get("companion") or [])
    companion = rng.choice(companions_pool) if companions_pool else ""
    companions = [companion] if companion and companion != SOLO_COMPANION else []

    weather_pool = list(seed_lib.get("weather") or [])
    weather = rng.choice(weather_pool) if weather_pool else ""

    mood_pool = list(seed_lib.get("mood") or [])
    mood = rng.choice(mood_pool) if mood_pool else ""

    return {
        "life_state": life_state,
        "location": location,
        "activity": activity,
        "companions": companions,
        "weather": weather,
        "mood": mood,
    }


def draw_event_seed(
    seed_lib: Mapping[str, list[str]],
    kind: str,
    rng: random.Random,
) -> dict[str, str]:
    """Draw a themed inspiration set — keyword cues, not a finished story.

    ``kind`` is one of ``mission`` / ``travel`` / ``academy`` / ``freeform``.
    Unknown kinds raise :class:`ValueError` so the tool layer can turn it
    into a structured error.
    """
    normalised = (kind or "mission").strip().lower()
    if normalised == "freeform":
        pool = {key: key for key in seed_lib}
    else:
        pool = EVENT_SEED_POOLS.get(normalised, {})
        if not pool:
            raise ValueError(
                "'kind' must be one of: mission, travel, academy, freeform "
                f"(got {normalised!r})."
            )
    draw: dict[str, str] = {}
    for out_key, lib_key in pool.items():
        choices = list(seed_lib.get(lib_key) or GENERIC_SEEDS.get(lib_key) or [])
        if choices:
            draw[out_key] = rng.choice(choices)
    return draw


def beat_to_current(
    beat: Mapping[str, Any],
    previous: Mapping[str, Any] | None,
    when: datetime | None = None,
) -> dict[str, Any]:
    """Turn a drawn beat into a life ``current`` node, preserving the arc."""
    prev = dict(previous or {})
    return {
        "state": beat["life_state"],
        "location": beat["location"],
        "activity": beat["activity"],
        "companions": list(beat["companions"]),
        "mood": beat["mood"],
        "weather": beat["weather"],
        "since": now_iso(when),
        "until_estimate": None,
        # The story arc is the one field a random daily beat must not
        # clobber — it is the thread the model has been developing.
        "story_arc": prev.get("story_arc"),
    }


def advance_life(
    life: Any,
    beat: Mapping[str, Any],
    *,
    reason: str = "auto_daily_advance",
    when: datetime | None = None,
) -> dict[str, Any]:
    """Apply *beat* to *life*, archiving the previous ``current`` to history.

    Returns a new life document. A transition is only recorded when one of
    ``state`` / ``activity`` / ``location`` actually changed, so a repeated
    identical beat does not pad history.
    """
    doc = repair_life(life, when)
    old_current = dict(doc.get("current") or {})
    history: list[Any] = list(doc.get("history") or [])
    new_current = beat_to_current(beat, old_current, when)

    diff_keys = [
        key
        for key in ("state", "activity", "location")
        if new_current.get(key) != old_current.get(key, "")
    ]
    if diff_keys and old_current:
        history.append(
            {
                "ts": now_iso(when),
                "from": old_current,
                "to": new_current,
                "reason": reason,
            }
        )

    doc["current"] = new_current
    doc["history"] = trim(history, MAX_HISTORY_ENTRIES)
    doc["schema_version"] = SCHEMA_VERSION
    return doc


def beat_diary_entry(
    beat: Mapping[str, Any], when: datetime | None = None
) -> dict[str, Any]:
    """Render the one-line auto diary entry for a drawn beat. Ported verbatim."""
    companions = list(beat.get("companions") or [])
    companion_note = f"同行: {companions[0]}" if companions else SOLO_COMPANION
    location = str(beat.get("location") or "")
    weather = str(beat.get("weather") or "")
    return {
        "ts": now_iso(when),
        "entry": (
            f"[每日自动] {beat.get('activity', '')}"
            + (f" @ {location}" if location else "")
            + f" — {companion_note}"
            + (f" ({weather})" if weather else "")
        ),
        "tag": "auto_advance",
        "mood": str(beat.get("mood") or ""),
        "location": location,
    }


# ---------------------------------------------------------------------------
# Life-rhythm signals (pure — no IO, never raises)
# ---------------------------------------------------------------------------


def _entry_involves_outing(entry: Mapping[str, Any]) -> bool:
    """True iff a history transition entered OR left an outing state.

    A transition's ``ts`` is the moment it happened; the most recent such ts
    marks the last time the persona was "out" (leaving an outing is always
    later than entering it, so ``max(ts)`` is the came-back time).
    """
    for side in ("from", "to"):
        node = entry.get(side)
        if isinstance(node, Mapping):
            st = node.get("state")
            if isinstance(st, str) and st.strip() in OUTING_STATES:
                return True
    return False


def days_since_last_outing(
    *,
    state: str,
    history: Iterable[Any],
    since_dt: datetime | None,
    now: datetime,
) -> int | None:
    """Days since the persona was last "out", or ``None`` when undecidable.

    * currently in an outing state → ``0``;
    * else the smallest gap to any outing transition in history;
    * else an anchor = the OLDEST known timestamp — i.e. how long we have
      tracked this persona while it never went out, so a persona that simply
      never uses outing states still trips the ``go_out`` nudge once tracked
      long enough. Reads a little loose ("N days since an outing that never
      happened") but drives the right nudge and never a wrong one;
    * else (no usable timestamps at all) → ``None``.
    """
    entries = list(history)
    if state in OUTING_STATES:
        return 0

    gaps: list[float] = []
    for entry in entries:
        if not isinstance(entry, Mapping) or not _entry_involves_outing(entry):
            continue
        ts = parse_ts(entry.get("ts"))
        if ts is None:
            continue
        d = delta_days(now, ts)
        if d is not None:
            gaps.append(max(0.0, d))
    if gaps:
        return int(min(gaps))

    anchors: list[float] = []
    if since_dt is not None:
        d = delta_days(now, since_dt)
        if d is not None:
            anchors.append(d)
    for entry in entries:
        if isinstance(entry, Mapping):
            ts = parse_ts(entry.get("ts"))
            if ts is not None:
                d = delta_days(now, ts)
                if d is not None:
                    anchors.append(d)
    if anchors:
        return max(0, int(max(anchors)))
    return None


def pick_nudge(
    *,
    state: str,
    days_in_current_state: int | None,
    days_since_outing: int | None,
) -> dict[str, str] | None:
    """Choose at most one life-rhythm nudge. Ported verbatim.

    Priority: a HIGH ``go_out`` covers everything; among the MEDIUM pair,
    ``wrap_outing`` (been out too long — concrete) beats ``change_scene``
    (generic staleness). ``suggested_action`` points the model at the
    ``persona_life_*`` tools so it actually advances its life rather than
    merely noting the nudge.
    """
    if days_since_outing is not None and days_since_outing >= OUTING_OVERDUE_DAYS:
        return {
            "level": "high",
            "kind": "go_out",
            "message": (
                f"你已经 {days_since_outing} 天没有外出了，"
                "生活有点停滞——该出门走走、给自己找点新鲜事了。"
            ),
            "suggested_action": (
                "先调用 persona_life_event_seed（kind=mission 或 travel）抽一个灵感，"
                "再用 persona_life_set_state 把状态切到 on_mission / traveling，"
                "让生活真正动起来。"
            ),
        }
    if (
        state in OUTING_STATES
        and days_in_current_state is not None
        and days_in_current_state >= OUTING_TOO_LONG_DAYS
    ):
        return {
            "level": "medium",
            "kind": "wrap_outing",
            "message": (
                f"你已经在外奔波 {days_in_current_state} 天了，"
                "是时候把这趟行程收个尾、回到日常了。"
            ),
            "suggested_action": (
                "用 persona_life_set_state 把状态切回 at_academy / resting 收尾；"
                "也可以先用 persona_life_diary_add 记下这趟的收获。"
            ),
        }
    if (
        days_in_current_state is not None
        and days_in_current_state >= SAME_STATE_STALE_DAYS
    ):
        return {
            "level": "medium",
            "kind": "change_scene",
            "message": (
                f"你已经保持同一种状态 {days_in_current_state} 天了，"
                "节奏有点单调——换个场景会更有生活感。"
            ),
            "suggested_action": (
                "用 persona_life_set_state 换一种状态或地点（例如去 training / resting，"
                "或换个 location）；需要灵感可以先调用 persona_life_event_seed。"
            ),
        }
    return None


def compute_life_signals(life: Any, now: datetime) -> dict[str, Any]:
    """Derive life-rhythm signals from a life document. PURE and total.

    May contain ``days_in_current_state``, ``days_since_last_outing``, and
    ``life_nudge``. Any field whose backing timestamp is missing or
    malformed is simply omitted; a non-dict / empty *life* yields ``{}``.
    Never raises, never does I/O — the caller supplies *now*.
    """
    signals: dict[str, Any] = {}
    if not isinstance(life, Mapping):
        return signals
    raw_current = life.get("current")
    current: Mapping[str, Any] = raw_current if isinstance(raw_current, Mapping) else {}
    raw_history = life.get("history")
    history: list[Any] = list(raw_history) if isinstance(raw_history, list) else []
    state = str(current.get("state") or "").strip()

    since_dt = parse_ts(current.get("since"))
    days_in_current_state: int | None = None
    if since_dt is not None:
        d = delta_days(now, since_dt)
        if d is not None:
            days_in_current_state = max(0, int(d))
            signals["days_in_current_state"] = days_in_current_state

    outing = days_since_last_outing(
        state=state, history=history, since_dt=since_dt, now=now
    )
    if outing is not None:
        signals["days_since_last_outing"] = outing

    nudge = pick_nudge(
        state=state,
        days_in_current_state=days_in_current_state,
        days_since_outing=outing,
    )
    if nudge is not None:
        signals["life_nudge"] = nudge
    return signals


__all__ = [
    "ALLOWED_STATES",
    "EVENT_SEED_POOLS",
    "GENERIC_SEEDS",
    "MAX_DIARY_CHARS",
    "MAX_DIARY_ENTRIES",
    "MAX_HISTORY_ENTRIES",
    "OUTING_OVERDUE_DAYS",
    "OUTING_STATES",
    "OUTING_TOO_LONG_DAYS",
    "SAME_STATE_STALE_DAYS",
    "SCHEMA_VERSION",
    "SOLO_COMPANION",
    "advance_life",
    "beat_diary_entry",
    "beat_to_current",
    "bundled_seeds_path",
    "coerce_companions",
    "coerce_seed_mapping",
    "compute_life_signals",
    "daily_rng",
    "daily_seed",
    "days_since_last_outing",
    "delta_days",
    "draw_event_seed",
    "draw_life_beat",
    "empty_life",
    "mirror_placeholder_keys",
    "now_dt",
    "now_iso",
    "override_seeds_path",
    "parse_ts",
    "pick_nudge",
    "repair_life",
    "resolve_seed_library",
    "trim",
    "valid_persona_slug",
]
