"""Placeholder / state rendering, and the byte-exactness of ported assets."""

from __future__ import annotations

import hashlib

import pytest

from agent.system_prompt import build_system_prompt
from hermes_cli import plugins as plugin_runtime
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
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

# Pinned after the approved canon corrections and task-first reply-policy update.
SOURCE_PROMPT_SHA256 = (
    "22ba6a368f8b4a08d60d182ce1b44a350c6560437067990373407a21b81d3687"
)

# Pinned after the approved companion-name and mentor-pool canon corrections.
SOURCE_SEEDS_SHA256 = "2d626c4f46262f59e6cdfaa8dde015a941b5c5a4f70addfb4af5b296de2be0ac"


def _sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _prompt_with_registered_grantley(
    monkeypatch, *, inject_identity_section: bool
) -> str:
    """Exercise Grantley's real registration through prompt construction."""
    import plugins.grantley as grantley
    from run_agent import AIAgent

    monkeypatch.setattr(
        grantley,
        "load_plugin_config",
        lambda: {"inject_identity_section": inject_identity_section},
    )
    manager = PluginManager()
    manager._discovered = True
    context = PluginContext(
        PluginManifest(name="grantley", key="grantley", source="bundled"),
        manager,
    )
    grantley.register(context)
    monkeypatch.setattr(plugin_runtime, "_plugin_manager", manager)
    agent = AIAgent(
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        model="test/model",
        provider="openrouter",
        platform="onebot",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        session_id="grantley-identity-section-test",
    )
    return build_system_prompt(agent)


def test_prompt_asset_is_byte_exact():
    """Catch undocumented edits to the approved persona baseline."""
    assert _sha(PROMPT_ASSET_PATH) == SOURCE_PROMPT_SHA256


def test_seed_pack_is_byte_exact():
    """Catch undocumented edits to the approved bundled life seed."""
    from plugins.grantley.life import bundled_seeds_path

    path = bundled_seeds_path("grantley")
    assert path is not None and path.is_file()
    assert _sha(path) == SOURCE_SEEDS_SHA256


def test_prompt_defers_fact_checks_to_session_visible_tools():
    """The stable persona must never name a tool the current session lacks."""
    text = PROMPT_ASSET_PATH.read_text(encoding="utf-8")
    assert "本会话可见的合适 Hermes 工具" in text
    assert "web_search" not in text


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


def test_stable_prompt_prioritizes_native_task_execution_over_persona_style():
    stable = load_persona_document().stable

    assert "用户要求达成实际目标时，先完成目标" in stable
    assert "只使用本会话可见的 Hermes 工具" in stable
    assert "工具失败时按 Hermes 既有恢复换可行路径继续" in stable
    assert "不按关键词" in stable


def test_stable_prompt_limits_only_casual_message_splitting():
    stable = load_persona_document().stable

    assert "自然选择 1–3 条 [MSG_BREAK]" in stable
    assert "短反应通常一条，普通交谈自然两三条" in stable
    assert "不固定、不轮换" in stable
    assert "复杂、专业或执行任务不受气泡数限制，完整性优先" in stable


def test_stable_prompt_has_no_conflicting_one_to_five_casual_rule():
    stable = load_persona_document().stable

    for conflicting_range in ("1–5", "1-5", "一到五"):
        assert conflicting_range not in stable


def test_stable_prompt_stays_within_identity_section_budget():
    assert len(load_persona_document().stable) <= 4000


def test_identity_section_registers_the_cache_safe_current_persona_once(monkeypatch):
    prompt = _prompt_with_registered_grantley(monkeypatch, inject_identity_section=True)
    stable = load_persona_document().stable

    assert stable in prompt
    assert prompt.count("## Plugin Context: grantley.identity") == 1
    assert prompt.count("用户要求达成实际目标时，先完成目标") == 1
    assert "自然选择 1–3 条 [MSG_BREAK]" in prompt


def test_identity_section_is_absent_when_the_opt_in_is_false(monkeypatch):
    prompt = _prompt_with_registered_grantley(
        monkeypatch, inject_identity_section=False
    )

    assert "## Plugin Context: grantley.identity" not in prompt
    assert "用户要求达成实际目标时，先完成目标" not in prompt


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
    block = render_state_block(
        doc, state, extra_lines=["- [0.90] 护送商队穿越北境森林"]
    )
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
