"""Life-beat draw determinism/seeding, the seed pack, and the decayed log."""

from __future__ import annotations

import ast
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from plugins.grantley import life
from plugins.grantley.channel_binding import (
    PersonaChannelBinding,
    bindings_from_config,
    daily_snapshot,
    resolve_channel_prompt,
)
from plugins.grantley.jobs import run_decay, run_life_advance
from plugins.grantley.store import (
    DEFAULT_HALF_LIFE_DAYS,
    GrantleyStore,
    temporal_decay,
)

DAY = 86_400.0


@pytest.fixture
def store() -> GrantleyStore:
    return GrantleyStore(sqlite3.connect(":memory:", check_same_thread=False))


# ── seed pack ──────────────────────────────────────────────────────────────


def test_bundled_seed_pack_carries_grantleys_lore():
    lib = life.resolve_seed_library("grantley")
    assert "护送商队穿越北境森林" in lib["mission_scenario"]
    assert "艾尔戈" in lib["companion"]
    assert "奥斯卡" in lib["companion"]
    assert "剑术场训练" in lib["academy_scene"]
    assert "断崖修道院" in lib["travel_destination"]
    # Every category the source pack defines survived the port.
    assert set(lib) >= {
        "mission_scenario",
        "travel_destination",
        "academy_scene",
        "companion",
        "tension",
        "weather",
        "mood",
        "duration_hint",
        "season_hint",
    }


def test_unknown_persona_falls_back_to_generic_not_grantleys_world():
    lib = life.resolve_seed_library("someone_else")
    assert lib["companion"] == life.GENERIC_SEEDS["companion"]
    assert "艾尔戈" not in lib["companion"]


def test_seed_slug_guard_blocks_traversal():
    assert not life.valid_persona_slug("../../etc/passwd")
    assert not life.valid_persona_slug("grantley/../x")
    assert life.valid_persona_slug("grantley")


def test_operator_override_layers_over_bundled(tmp_path):
    (tmp_path / "persona_life").mkdir()
    (tmp_path / "persona_life" / "grantley.events.yaml").write_text(
        "companion:\n  - 自定义伙伴\n", encoding="utf-8"
    )
    lib = life.resolve_seed_library("grantley", tmp_path)
    assert lib["companion"] == ["自定义伙伴"]
    # A partial override replaces only the categories it names.
    assert "护送商队穿越北境森林" in lib["mission_scenario"]


# ── life-beat draw: determinism and seeding ────────────────────────────────


def test_daily_seed_is_stable_across_processes():
    """Not `hash()` — PYTHONHASHSEED would make that per-process."""
    assert life.daily_seed("grantley", "2026-08-18") == life.daily_seed(
        "grantley", "2026-08-18"
    )
    assert life.daily_seed("grantley", "2026-08-18") != life.daily_seed(
        "grantley", "2026-08-19"
    )
    assert life.daily_seed("grantley", "2026-08-18") != life.daily_seed(
        "lycaon", "2026-08-18"
    )


def test_the_same_day_always_draws_the_same_beat():
    lib = life.resolve_seed_library("grantley")
    day = datetime(2026, 8, 18, tzinfo=timezone.utc)
    first = life.draw_life_beat(lib, life.daily_rng("grantley", day))
    second = life.draw_life_beat(lib, life.daily_rng("grantley", day))
    assert first == second


def test_a_different_day_draws_a_different_beat():
    lib = life.resolve_seed_library("grantley")
    beats = {
        tuple(
            sorted(
                life.draw_life_beat(
                    lib,
                    life.daily_rng("grantley", datetime(2026, 8, d, tzinfo=timezone.utc)),
                ).items(),
                key=lambda kv: kv[0],
            )
        )[0]
        for d in range(1, 29)
    }
    # 28 days must not collapse onto a single beat.
    assert len(beats) >= 1
    distinct = {
        life.draw_life_beat(
            lib, life.daily_rng("grantley", datetime(2026, 8, d, tzinfo=timezone.utc))
        )["activity"]
        for d in range(1, 29)
    }
    assert len(distinct) > 1


def test_an_injected_rng_makes_the_draw_fully_reproducible():
    lib = life.resolve_seed_library("grantley")
    a = life.draw_life_beat(lib, random.Random(1234))
    b = life.draw_life_beat(lib, random.Random(1234))
    assert a == b


def test_beat_uses_grantleys_own_pools():
    """Whatever category is drawn, the cues come from Grantley's lore."""
    lib = life.resolve_seed_library("grantley")
    source_pool = {
        "at_academy": "academy_scene",
        "on_mission": "mission_scenario",
        "traveling": "mission_scenario",  # travel re-draws the activity
    }
    for seed in range(20):
        beat = life.draw_life_beat(lib, random.Random(seed))
        assert beat["activity"] in lib[source_pool[beat["life_state"]]]
        assert beat["mood"] in lib["mood"]
        assert beat["weather"] in lib["weather"]
        if beat["life_state"] == "traveling":
            assert beat["location"] in lib["travel_destination"]
        else:
            assert beat["location"] == ""


def test_grantley_can_actually_leave_the_academy():
    """Regression guard for D18: the source could never draw `traveling`.

    ``grantley.yaml`` populates academy_scene AND mission_scenario, so under
    the source's strict-priority draw the ten travel destinations were dead
    data and this set would be ``{"at_academy"}`` forever.
    """
    lib = life.resolve_seed_library("grantley")
    states = {life.draw_life_beat(lib, random.Random(s))["life_state"] for s in range(60)}
    assert states == {"at_academy", "on_mission", "traveling"}


def test_solo_companion_yields_an_empty_companion_list():
    lib = {"academy_scene": ["训练"], "companion": [life.SOLO_COMPANION]}
    beat = life.draw_life_beat(lib, random.Random(0))
    assert beat["companions"] == []


def test_both_categories_are_reachable_when_both_pools_exist():
    """D18: the category draw is uniform, not a strict priority walk."""
    lib = {"travel_destination": ["海港小镇"], "mission_scenario": ["替朋友跑腿"]}
    states = {life.draw_life_beat(lib, random.Random(s))["life_state"] for s in range(30)}
    assert states == {"traveling", "on_mission"}


def test_travel_draw_redraws_activity_away_from_the_destination():
    """The re-draw branch that was unreachable in the source implementation."""
    lib = {"travel_destination": ["海港小镇"], "mission_scenario": ["替朋友跑腿"]}
    beats = [life.draw_life_beat(lib, random.Random(s)) for s in range(30)]
    travelling = [b for b in beats if b["life_state"] == "traveling"]
    assert travelling, "the travel branch must be reachable at all"
    for beat in travelling:
        assert beat["location"] == "海港小镇"
        # Re-drawn from the mission pool, NOT left equal to the destination.
        assert beat["activity"] == "替朋友跑腿"


def test_travel_only_pack_leaves_activity_as_the_destination():
    """Degenerate case: nothing to re-draw from, so activity == location."""
    lib = {"travel_destination": ["海港小镇"]}
    beat = life.draw_life_beat(lib, random.Random(0))
    assert beat["life_state"] == "traveling"
    assert beat["location"] == "海港小镇"
    assert beat["activity"] == "海港小镇"


def test_empty_seed_library_degrades_to_the_generic_default():
    beat = life.draw_life_beat({}, random.Random(0))
    assert beat == {
        "life_state": "at_academy",
        "location": "",
        "activity": "日常",
        "companions": [],
        "weather": "",
        "mood": "",
    }


#: Import roots that would give a module the ability to reach a model.
#: ``agent`` and ``model_tools`` are hermes' own inference surfaces; the rest
#: are provider SDKs and the HTTP clients one would reach a model through.
_MODEL_CAPABLE_ROOTS = frozenset(
    {
        "agent",
        "anthropic",
        "cli",
        "google",
        "httpx",
        "litellm",
        "mistralai",
        "model_tools",
        "openai",
        "providers",
        "requests",
        "run_agent",
        "urllib",
    }
)


def _absolute_import_roots(path: Path) -> set[str]:
    """Top-level packages *path* imports absolutely (relative imports excluded)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _relative_import_targets(path: Path) -> set[str]:
    """Sibling modules *path* imports relatively (``from .x import y``)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level > 0:
            if node.module:
                targets.add(node.module.split(".")[0])
            else:  # from . import life, decay
                targets.update(alias.name for alias in node.names)
    return targets


def test_life_advance_makes_no_llm_call(store):
    """Structural guarantee: nothing reachable from jobs.py can call a model.

    Checked by parsing the import graph rather than grepping the source, so a
    docstring that merely *mentions* an LLM cannot trip it and a real import
    cannot hide behind an alias.
    """
    import plugins.grantley.jobs as jobs_mod

    package_dir = Path(jobs_mod.__file__).parent
    seen: set[str] = set()
    queue = [Path(jobs_mod.__file__)]
    reachable_roots: set[str] = set()

    while queue:
        module_path = queue.pop()
        if module_path.name in seen:
            continue
        seen.add(module_path.name)
        reachable_roots |= _absolute_import_roots(module_path)
        for sibling in _relative_import_targets(module_path):
            candidate = package_dir / f"{sibling}.py"
            if candidate.is_file():
                queue.append(candidate)

    # The whole reachable graph, not just jobs.py itself.
    assert seen == {"jobs.py", "life.py", "decay.py", "store.py", "state.py"}
    offenders = reachable_roots & _MODEL_CAPABLE_ROOTS
    assert not offenders, f"life-advance graph can reach a model via {sorted(offenders)}"

    result = run_life_advance(store, "grantley")
    assert result["ok"] is True
    assert result["beat"]["activity"]


# ── life signals ───────────────────────────────────────────────────────────


def test_go_out_nudge_after_the_overdue_threshold():
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    doc = {
        "current": {
            "state": "at_academy",
            "since": (now - timedelta(days=20)).isoformat(),
        },
        "history": [],
    }
    signals = life.compute_life_signals(doc, now)
    assert signals["life_nudge"]["kind"] == "go_out"
    assert signals["life_nudge"]["level"] == "high"


def test_wrap_outing_beats_change_scene_when_out_too_long():
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    doc = {
        "current": {
            "state": "on_mission",
            "since": (now - timedelta(days=9)).isoformat(),
        },
        "history": [],
    }
    signals = life.compute_life_signals(doc, now)
    assert signals["days_since_last_outing"] == 0
    assert signals["life_nudge"]["kind"] == "wrap_outing"


def test_change_scene_for_generic_staleness():
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    doc = {
        "current": {
            "state": "at_academy",
            "since": (now - timedelta(days=7)).isoformat(),
        },
        "history": [
            {
                "ts": (now - timedelta(days=7)).isoformat(),
                "from": {"state": "on_mission"},
                "to": {"state": "at_academy"},
            }
        ],
    }
    signals = life.compute_life_signals(doc, now)
    assert signals["life_nudge"]["kind"] == "change_scene"


def test_signals_are_total_on_garbage_input():
    assert life.compute_life_signals(None, life.now_dt()) == {}
    assert life.compute_life_signals({"current": "nope"}, life.now_dt()) == {}
    assert life.compute_life_signals(
        {"current": {"since": "not-a-date"}, "history": "nope"}, life.now_dt()
    ) == {}


# ── append-only event log + decay ──────────────────────────────────────────


def test_decay_formula_matches_the_holographic_half_life():
    now = 1_000_000.0
    assert temporal_decay(now, now, 14.0) == pytest.approx(1.0)
    assert temporal_decay(now - 14 * DAY, now, 14.0) == pytest.approx(0.5)
    assert temporal_decay(now - 28 * DAY, now, 14.0) == pytest.approx(0.25)


def test_zero_half_life_disables_decay():
    now = 1_000_000.0
    assert temporal_decay(now - 365 * DAY, now, 0.0) == 1.0


def test_future_timestamps_never_exceed_weight_one():
    now = 1_000_000.0
    assert temporal_decay(now + 10 * DAY, now, 14.0) == 1.0


def test_retrieval_ranks_recent_over_stale(store):
    now = 1_000_000.0
    store.append_event("grantley", "很久以前的事", salience=1.0, created_at=now - 60 * DAY)
    store.append_event("grantley", "昨天的事", salience=1.0, created_at=now - 1 * DAY)
    out = store.retrieve("grantley", now=now, top_n=2)
    assert [e.text for e in out] == ["昨天的事", "很久以前的事"]
    assert out[0].weight > out[1].weight


def test_high_salience_can_outrank_recency(store):
    now = 1_000_000.0
    store.append_event("grantley", "琐事", salience=0.1, created_at=now)
    store.append_event(
        "grantley", "重要的事", salience=1.0, created_at=now - DEFAULT_HALF_LIFE_DAYS * DAY
    )
    out = store.retrieve("grantley", now=now, top_n=2)
    assert out[0].text == "重要的事"


def test_events_below_min_weight_are_dropped(store):
    now = 1_000_000.0
    store.append_event("grantley", "老掉牙", salience=1.0, created_at=now - 365 * DAY)
    assert store.retrieve("grantley", now=now, min_weight=0.05) == []
    # But the row is still there — the log is append-only, decay is read-time.
    assert store.count_events("grantley") == 1


def test_changing_half_life_changes_recall_without_touching_rows(store):
    now = 1_000_000.0
    store.append_event("grantley", "一个月前", salience=1.0, created_at=now - 30 * DAY)
    assert store.retrieve("grantley", now=now, half_life_days=1.0, min_weight=0.05) == []
    assert len(store.retrieve("grantley", now=now, half_life_days=365.0)) == 1
    assert store.count_events("grantley") == 1


def test_events_are_scoped_per_persona(store):
    store.append_event("grantley", "格兰的事")
    store.append_event("lycaon", "另一个角色的事")
    assert [e.text for e in store.retrieve("grantley")] == ["格兰的事"]


def test_append_rejects_empty_text(store):
    with pytest.raises(ValueError):
        store.append_event("grantley", "   ")


# ── the jobs ───────────────────────────────────────────────────────────────


def test_life_advance_writes_life_diary_topics_and_event(store):
    result = run_life_advance(store, "grantley")
    state = store.get_state("grantley")

    assert state is not None
    life_doc = state.state_json["life"]
    assert life_doc["current"]["activity"] == result["beat"]["activity"]
    # Flat mirror keys are what the placeholder layer reads.
    assert state.state_json["life_activity"] == result["beat"]["activity"]
    assert state.state_json["life_state"] == result["beat"]["life_state"]
    assert len(state.state_json["diary"]) == 1
    assert state.state_json["diary"][0]["tag"] == "auto_advance"
    assert result["beat"]["activity"] in state.recent_topics
    assert store.count_events("grantley") == 1


def test_life_advance_preserves_the_story_arc(store):
    st = store.load_state("grantley")
    st.state_json["life"] = life.empty_life()
    st.state_json["life"]["current"]["story_arc"] = "护送商队任务"
    store.upsert_state(st)

    run_life_advance(store, "grantley")
    after = store.get_state("grantley")
    assert after.state_json["life"]["current"]["story_arc"] == "护送商队任务"


def test_life_advance_archives_the_previous_current_to_history(store):
    """Two advances from a fresh row archive twice: default→day1, day1→day2."""
    day1 = datetime(2026, 8, 18, tzinfo=timezone.utc)
    day2 = datetime(2026, 8, 19, tzinfo=timezone.utc)
    first = run_life_advance(store, "grantley", when=day1)
    second = run_life_advance(store, "grantley", when=day2)

    history = store.get_state("grantley").state_json["life"]["history"]
    assert len(history) == 2
    assert [h["reason"] for h in history] == ["auto_daily_advance"] * 2

    # The first entry archives the freshly-initialised default state...
    assert history[0]["from"]["activity"] == "日常"
    assert history[0]["to"]["activity"] == first["beat"]["activity"]
    # ...and the second archives exactly what the first advance produced.
    assert history[1]["from"] == first["current"]
    assert history[1]["to"]["activity"] == second["beat"]["activity"]


def test_repeating_an_identical_beat_does_not_pad_history(store):
    """Same day → same seeded beat → nothing changed → nothing archived."""
    day = datetime(2026, 8, 18, tzinfo=timezone.utc)
    first = run_life_advance(store, "grantley", when=day)
    second = run_life_advance(store, "grantley", when=day)
    assert first["beat"] == second["beat"]

    history = store.get_state("grantley").state_json["life"]["history"]
    # Only the initial default→beat transition, not a second identical one.
    assert len(history) == 1


def test_decay_job_advances_both_clocks(store):
    from plugins.grantley.state import PersonaState

    base_ms = 1_700_000_000_000
    store.upsert_state(
        PersonaState(
            persona_id="grantley",
            mood="tired",
            fatigue=1.0,
            recent_topics=["a", "b", "c"],
            updated_at_ms=base_ms,
            topics_aged_at_ms=base_ms,
        )
    )
    later = base_ms + 48 * 3_600_000  # 2 days
    result = run_decay(store, now_ms=later)

    assert result["ok"] and result["rows_changed"] == 1
    after = store.get_state("grantley")
    assert after.fatigue == 0.0
    assert after.mood == "neutral"
    assert after.recent_topics == ["c"]
    assert after.updated_at_ms == later
    assert after.topics_aged_at_ms == base_ms + 2 * 86_400_000


def test_hourly_decay_still_ages_exactly_one_topic_per_day(store):
    """The failure the source design had: a frequent sweep aging nothing."""
    from plugins.grantley.state import PersonaState

    base_ms = 1_700_000_000_000
    store.upsert_state(
        PersonaState(
            persona_id="grantley",
            fatigue=1.0,
            recent_topics=["a", "b", "c"],
            updated_at_ms=base_ms,
            topics_aged_at_ms=base_ms,
        )
    )
    for hour in range(1, 25):
        run_decay(store, now_ms=base_ms + hour * 3_600_000)
    assert store.get_state("grantley").recent_topics == ["b", "c"]


def test_decay_on_an_empty_store_is_a_clean_no_op(store):
    result = run_decay(store)
    assert result == {
        "ok": True,
        "job": "grantley.decay",
        "rows_scanned": 0,
        "rows_changed": 0,
        "at_ms": result["at_ms"],
        "details": [],
    }


# ── per-channel binding (G3) ───────────────────────────────────────────────


def test_channel_prompt_is_byte_stable_within_a_day():
    """The ephemeral prompt must not vary inside one conversation."""
    binding = PersonaChannelBinding(
        chat_id="183287894", channel_owner_id="2104743984", is_group=True
    )
    day = datetime(2026, 8, 18, 3, 0, tzinfo=timezone.utc)
    later_same_day = datetime(2026, 8, 18, 22, 45, tzinfo=timezone.utc)
    assert resolve_channel_prompt(binding, on=day) == resolve_channel_prompt(
        binding, on=later_same_day
    )


def test_channel_prompt_names_the_channel_owner():
    binding = PersonaChannelBinding(
        chat_id="183287894", channel_owner_id="2104743984", is_group=True
    )
    text = resolve_channel_prompt(binding, on=datetime(2026, 8, 18, tzinfo=timezone.utc))
    assert "2104743984" in text
    assert "群聊" in text


def test_dm_binding_reads_as_a_direct_message():
    binding = PersonaChannelBinding(chat_id="536132102", is_group=False)
    text = resolve_channel_prompt(binding, on=datetime(2026, 8, 18, tzinfo=timezone.utc))
    assert "私聊" in text
    assert "群主" not in text


def test_snapshot_contains_no_decaying_value():
    """Fatigue / mood / recent_topics must never reach the ephemeral layer."""
    binding = PersonaChannelBinding(chat_id="1", channel_owner_id="2")
    snap = daily_snapshot(binding, on=datetime(2026, 8, 18, tzinfo=timezone.utc))
    assert "fatigue" not in snap
    assert "mood" not in snap
    assert "recent_topics" not in snap


def test_bindings_from_config_parses_and_skips_junk():
    parsed = bindings_from_config(
        {
            "183287894": {"channel_owner": "2104743984", "group": True, "name": "群"},
            "536132102": {"group": False},
            "bad": "not-a-mapping",
        }
    )
    assert parsed["183287894"].channel_owner_id == "2104743984"
    assert parsed["183287894"].is_group is True
    assert parsed["536132102"].channel_owner_id is None
    assert "bad" not in parsed


def test_resolve_channel_prompt_tolerates_a_missing_binding():
    assert resolve_channel_prompt(None) is None
