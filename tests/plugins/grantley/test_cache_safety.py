"""The caching property this port was built around.

The claim under test: **a persona state change never mutates the cached
prefix.** It is expressed three ways, because the design has three moving
parts and any one of them regressing would silently multiply token cost
without failing anything else.

1. The stable half of the persona document is a pure function of the asset,
   so no state change can alter it.
2. The provider contributes nothing to the system prompt — ``system_prompt_block``
   stays empty, per A3 G2.
3. The volatile state travels on the *current user message's* ``api_content``
   sidecar, which is appended after the cached prefix. Verified against the
   real hermes composition path, not a mock.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from agent.memory_manager import build_memory_context_block, sanitize_context
from agent.memory_provider import MemoryProvider
from agent.turn_context import compose_user_api_content

from plugins.grantley.jobs import run_decay, run_life_advance
from plugins.grantley.memory_provider import GrantleyMemoryProvider
from plugins.grantley.persona import load_persona_document
from plugins.grantley.state import PersonaState
from plugins.grantley.store import GrantleyStore


@pytest.fixture
def provider() -> GrantleyMemoryProvider:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    prov = GrantleyMemoryProvider(connection=conn)
    prov.initialize("test-session")
    return prov


def _store(provider: GrantleyMemoryProvider) -> GrantleyStore:
    return provider._ensure_store()  # noqa: SLF001 - test reaches in deliberately


# ── 1. the stable prefix is immune to state ────────────────────────────────


def test_state_change_does_not_mutate_the_cached_prefix(provider):
    """The headline assertion."""
    before = load_persona_document().stable

    store = _store(provider)
    run_life_advance(store, "grantley")
    store.append_event("grantley", "护送商队穿越北境森林", salience=1.0)
    store.upsert_state(
        PersonaState(persona_id="grantley", mood="tired", fatigue=0.95)
    )
    run_decay(store)

    after = load_persona_document().stable
    assert after == before


def test_the_prefix_is_a_pure_function_of_the_asset(tmp_path):
    """Two independent loads are byte-identical — no clock, no state, no env."""
    a = load_persona_document()
    b = load_persona_document()
    assert a.stable == b.stable
    assert a.stable.encode("utf-8") == b.stable.encode("utf-8")


def test_no_placeholder_survives_into_the_prefix():
    stable = load_persona_document().stable
    for key in (
        "mood",
        "fatigue",
        "recent_topics",
        "life_activity",
        "life_location",
        "life_companions",
        "life_state",
        "life_story_arc",
    ):
        assert f"{{{{persona.{key}}}}}" not in stable


# ── 2. the provider adds nothing to the system prompt ──────────────────────


def test_system_prompt_block_is_empty_by_design(provider):
    """A3 G2: decaying content must never take this hook."""
    assert provider.system_prompt_block() == ""

    store = _store(provider)
    run_life_advance(store, "grantley")
    store.append_event("grantley", "屋顶上吹风", salience=1.0)
    assert provider.system_prompt_block() == ""


def test_provider_satisfies_the_memory_provider_abc(provider):
    assert isinstance(provider, MemoryProvider)
    assert provider.name == "grantley"
    assert provider.is_available() is True
    assert provider.unavailable_reason() == ""
    names = {s["function"]["name"] for s in provider.get_tool_schemas()}
    assert "persona_life_get" in names
    assert "persona_life_set_state" in names


def test_sync_turn_is_a_deliberate_no_op(provider):
    """Auto-ingesting chatter would drown the life beats in retrieval."""
    provider.sync_turn("你好", "哦，你来啦。")
    assert _store(provider).count_events("grantley") == 0


# ── 3. volatile state rides the user-message sidecar ───────────────────────


def test_prefetch_reflects_state_and_lands_in_api_content(provider):
    store = _store(provider)
    store.upsert_state(
        PersonaState(
            persona_id="grantley",
            mood="兴奋",
            fatigue=0.9,
            recent_topics=["剑术场训练"],
            state_json={"life_activity": "剑术场训练", "life_location": "骑士学院"},
        )
    )

    block = provider.prefetch("今天在干嘛")
    assert "兴奋" in block
    assert "tired" in block  # bucketed, never the raw 0.9
    assert "0.9" not in block
    assert "剑术场训练" in block

    api_content = compose_user_api_content("今天在干嘛", block, "")
    assert api_content is not None
    assert api_content.startswith("今天在干嘛")
    assert "<memory-context>" in api_content
    assert "剑术场训练" in api_content


def test_prefetch_changes_between_turns_while_the_prefix_does_not(provider):
    """Exactly the shape the design wants: sidecar varies, prefix frozen."""
    store = _store(provider)
    prefix_before = load_persona_document().stable

    store.upsert_state(PersonaState(persona_id="grantley", mood="懒洋洋"))
    first = provider.prefetch("在吗")

    store.upsert_state(PersonaState(persona_id="grantley", mood="兴奋"))
    second = provider.prefetch("在吗")

    assert first != second
    assert "懒洋洋" in first
    assert "兴奋" in second
    assert load_persona_document().stable == prefix_before


def test_prefetch_output_is_not_pre_wrapped(provider):
    """The manager wraps and warns if a provider pre-wraps; we must not."""
    store = _store(provider)
    store.upsert_state(PersonaState(persona_id="grantley", mood="认真"))
    block = provider.prefetch("在吗")
    assert "<memory-context>" not in block
    # And it survives the manager's sanitizer unchanged.
    assert sanitize_context(block) == block
    wrapped = build_memory_context_block(block)
    assert wrapped.startswith("<memory-context>")
    assert "<persona-state>" in wrapped


def test_decayed_life_events_ride_the_sidecar_not_the_prompt(provider):
    store = _store(provider)
    store.append_event("grantley", "调查山村的孩子失踪案", salience=1.0)
    block = provider.prefetch("最近怎么样")
    assert "调查山村的孩子失踪案" in block
    assert "调查山村的孩子失踪案" not in load_persona_document().stable


def test_life_nudge_rides_the_sidecar(provider):
    store = _store(provider)
    now = datetime.now(timezone.utc).astimezone()
    state = store.load_state("grantley")
    state.state_json["life"] = {
        "current": {
            "state": "at_academy",
            "since": (now.replace(year=now.year - 1)).isoformat(),
        },
        "history": [],
    }
    store.upsert_state(state)
    block = provider.prefetch("在干嘛")
    assert "生活节奏提醒" in block
    assert "生活节奏提醒" not in load_persona_document().stable


def test_prefetch_degrades_to_empty_string_never_raises():
    """A broken persona must not break the turn."""
    broken = GrantleyMemoryProvider(connection=None, data_dir=None)
    broken._store = None  # noqa: SLF001

    class _Boom:
        def __getattr__(self, _name):
            raise RuntimeError("storage is down")

    broken._ensure_store = lambda: (_ for _ in ()).throw(RuntimeError("down"))  # noqa: SLF001
    assert broken.prefetch("在吗") == ""


def test_compose_user_api_content_returns_none_without_injections():
    assert compose_user_api_content("在吗", "", "") is None
