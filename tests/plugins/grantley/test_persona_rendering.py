"""Placeholder / state rendering, and the byte-exactness of ported assets."""

from __future__ import annotations

import hashlib

import pytest

from plugins.grantley.persona import (
    LIVE_STATE_POINTER,
    PROMPT_ASSET_PATH,
    VOLATILE_SECTION_HEADING,
    PersonaAssetError,
    describe_placeholders,
    load_persona_document,
    render_placeholders,
    render_state_block,
    resolve_placeholder,
    split_persona_document,
)
from plugins.grantley.state import PersonaState

# sha256 of corlinman's
# python/packages/corlinman-server/src/corlinman_server/persona/default_grantley.md
# (the repo copy — the one that says `web_search`, not `WebSearch`).
SOURCE_PROMPT_SHA256 = "db2efde6b62902dc11fd7e151c2518ef01cec848eee2f5c8012fcfcd5881ec25"

# sha256 of corlinman's
# python/packages/corlinman-agent/src/corlinman_agent/persona/life_seeds/grantley.yaml
SOURCE_SEEDS_SHA256 = "ce280754ed9e0e8cdb88d559b9685cca7956178587b284b0f35d496c07fb26fb"


def _sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_prompt_asset_is_byte_exact():
    """The character content must not drift. Ever."""
    assert _sha(PROMPT_ASSET_PATH) == SOURCE_PROMPT_SHA256


def test_seed_pack_is_byte_exact():
    from plugins.grantley.life import bundled_seeds_path

    path = bundled_seeds_path("grantley")
    assert path is not None and path.is_file()
    assert _sha(path) == SOURCE_SEEDS_SHA256


def test_prompt_uses_web_search_wire_name():
    """The repo copy's later fix, not production's stale `WebSearch`."""
    text = PROMPT_ASSET_PATH.read_text(encoding="utf-8")
    assert "web_search" in text
    assert "WebSearch" not in text


def test_split_extracts_exactly_the_volatile_section():
    doc = load_persona_document()

    assert doc.volatile.startswith(VOLATILE_SECTION_HEADING)
    assert set(doc.placeholder_keys()) == {
        "mood",
        "fatigue",
        "recent_topics",
        "life_activity",
        "life_location",
        "life_companions",
        "life_state",
        "life_story_arc",
    }
    # The stable half keeps every other section verbatim.
    for heading in ("## 角色扮演规则", "## 回答工作流", "## 心智模型", "## 诚实边界"):
        assert heading in doc.stable
    # And the pointer replaced the volatile block.
    assert LIVE_STATE_POINTER in doc.stable


def test_stable_half_carries_no_placeholder():
    """The invariant the whole caching design rests on."""
    doc = load_persona_document()
    assert "{{persona." not in doc.stable
    doc.assert_cache_safe()  # must not raise


def test_split_refuses_a_document_without_the_volatile_heading():
    with pytest.raises(PersonaAssetError):
        split_persona_document("# 只有身份\n\n没有实时状态段。\n")


def test_fatigue_never_renders_as_a_float():
    """Bucket labels only — the [0,1] range is an implementation detail."""
    for value, expected in (
        (0.0, "rested"),
        (0.14, "rested"),
        (0.15, "fresh"),
        (0.39, "fresh"),
        (0.4, "mild fatigue"),
        (0.74, "mild fatigue"),
        (0.75, "tired"),
        (1.0, "tired"),
    ):
        state = PersonaState(persona_id="grantley", fatigue=value)
        assert resolve_placeholder(state, "fatigue") == expected


def test_fatigue_bucket_boundaries_are_inclusive_lower():
    """0.4 reads as 'mild fatigue', not 'fresh' — ported boundary semantics."""
    state = PersonaState(persona_id="grantley", fatigue=0.4)
    assert resolve_placeholder(state, "fatigue") == "mild fatigue"


def test_recent_topics_render_newest_first_capped_at_five():
    state = PersonaState(
        persona_id="grantley",
        recent_topics=["a", "b", "c", "d", "e", "f", "g"],
    )
    assert resolve_placeholder(state, "recent_topics") == "g, f, e, d, c"


def test_empty_recent_topics_render_as_empty_string():
    state = PersonaState(persona_id="grantley")
    assert resolve_placeholder(state, "recent_topics") == ""


def test_unknown_placeholder_resolves_empty_not_raises():
    state = PersonaState(persona_id="grantley")
    assert resolve_placeholder(state, "definitely_not_a_key") == ""


def test_life_star_keys_resolve_from_state_json():
    state = PersonaState(
        persona_id="grantley",
        state_json={
            "life_activity": "剑术场训练",
            "life_location": "骑士学院",
            "life_companions": "艾尔戈, 奥斯卡",
            "life_state": "at_academy",
            "life_story_arc": "护送商队任务",
        },
    )
    rendered = describe_placeholders(state)
    assert rendered["life_activity"] == "剑术场训练"
    assert rendered["life_companions"] == "艾尔戈, 奥斯卡"
    assert rendered["life_story_arc"] == "护送商队任务"


def test_render_placeholders_substitutes_every_key():
    doc = load_persona_document()
    state = PersonaState(
        persona_id="grantley",
        mood="兴奋",
        fatigue=0.8,
        recent_topics=["训练"],
        state_json={"life_activity": "剑术场训练"},
    )
    out = render_placeholders(doc.volatile, state)
    assert "{{" not in out
    assert "兴奋" in out
    assert "tired" in out
    assert "剑术场训练" in out


def test_state_block_is_tagged_and_carries_extras():
    doc = load_persona_document()
    state = PersonaState(persona_id="grantley", mood="痞痞的")
    block = render_state_block(doc, state, extra_lines=["- [0.90] 护送商队穿越北境森林"])
    assert block.startswith("<persona-state>")
    assert block.endswith("</persona-state>")
    assert "痞痞的" in block
    assert "护送商队穿越北境森林" in block
    # Must NOT pre-wrap in the manager's own fence — it strips and warns.
    assert "<memory-context>" not in block


def test_state_block_omits_empty_extras():
    doc = load_persona_document()
    state = PersonaState(persona_id="grantley")
    block = render_state_block(doc, state, extra_lines=["", "   "])
    assert block.count("\n\n") >= 1
    assert block.strip().endswith("</persona-state>")
