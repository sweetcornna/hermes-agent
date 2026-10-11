"""Persona loading, session identity and state-delivery contracts."""

from __future__ import annotations

import pytest

from agent.system_prompt import build_system_prompt
from hermes_cli import plugins as plugin_runtime
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from plugins.grantley.persona import (
    LIVE_STATE_POINTER,
    VOLATILE_SECTION_HEADING,
    PersonaAssetError,
    describe_placeholders,
    load_persona_document,
    load_voice_document,
    render_placeholders,
    render_state_block,
    resolve_placeholder,
    split_persona_document,
)
from plugins.grantley.state import PersonaState, bucket_fatigue

def _prompt_with_registered_grantley(
    monkeypatch,
    *,
    inject_identity_section: bool,
    inject_voice_section: bool = False,
    platform: str = "onebot",
    chat_id: str = "183287894",
    chat_type: str = "group",
    return_agent: bool = False,
):
    """Exercise Grantley's real registration through prompt construction."""
    import plugins.grantley as grantley
    from run_agent import AIAgent

    monkeypatch.setattr(
        grantley,
        "load_plugin_config",
        lambda: {
            "inject_identity_section": inject_identity_section,
            "inject_voice_section": inject_voice_section,
            "channels": {
                "183287894": {"persona": "grantley", "group": True}
            },
        },
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
        platform=platform,
        chat_id=chat_id,
        chat_type=chat_type,
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        session_id="grantley-identity-section-test",
    )
    return agent if return_agent else build_system_prompt(agent)


def test_load_preserves_the_input_document(tmp_path):
    source = load_persona_document().source
    path = tmp_path / "persona.md"
    path.write_text(source, encoding="utf-8")
    assert load_persona_document(path).source == source


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
    # Every surrounding byte survives; only the volatile section is replaced.
    assert doc.volatile in doc.source
    before, after = doc.source.split(doc.volatile, 1)
    assert doc.stable == before + LIVE_STATE_POINTER + after


def test_stable_half_carries_no_placeholder():
    """The invariant the whole caching design rests on."""
    doc = load_persona_document()
    assert "{{persona." not in doc.stable
    doc.assert_cache_safe()  # must not raise


# Semantic personality/task-quality checks live in the paired 24-case
# evaluation. Substring snapshots cannot prove how a model behaves.


def test_stable_prompt_stays_within_identity_section_budget():
    assert len(load_persona_document().stable) <= 4000


@pytest.mark.parametrize("canonical_group", [False, True])
def test_identity_section_registers_the_cache_safe_current_persona_once(monkeypatch, canonical_group):
    from plugins.platforms.onebot.adapter import format_chat_id

    chat_id = format_chat_id(True, "183287894") if canonical_group else "183287894"
    prompt = _prompt_with_registered_grantley(
        monkeypatch, inject_identity_section=True, chat_id=chat_id,
    )
    stable = load_persona_document().stable

    assert stable in prompt
    assert prompt.count("## Plugin Context: grantley.identity") == 1
    assert prompt.count(stable) == 1


def test_identity_section_is_absent_when_the_opt_in_is_false(monkeypatch):
    prompt = _prompt_with_registered_grantley(
        monkeypatch, inject_identity_section=False
    )

    assert "## Plugin Context: grantley.identity" not in prompt
    assert load_persona_document().stable not in prompt


def test_asset_revision_is_adopted_only_by_new_session_identity(tmp_path, monkeypatch):
    """Real registration + SQLite restore, not a simulated prompt cache."""
    from agent.conversation_loop import _restore_or_build_system_prompt
    from hermes_state import SessionDB
    from plugins.grantley import persona

    original = load_persona_document().source
    path = tmp_path / "persona.md"
    path.write_text(original + "\nIDENTITY_REVISION_A\n", encoding="utf-8")
    monkeypatch.setattr(persona, "PROMPT_ASSET_PATH", path)
    db = SessionDB(tmp_path / "sessions.db")
    db.create_session("existing", source="onebot", model="test/model")
    db.create_session("new", source="onebot", model="test/model")
    history = [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "嗨"}]
    try:
        first = _prompt_with_registered_grantley(
            monkeypatch, inject_identity_section=True, return_agent=True,
        )
        first._session_db, first.session_id = db, "existing"
        _restore_or_build_system_prompt(first, None, [])
        saved = db.get_session("existing")["system_prompt"]
        assert "IDENTITY_REVISION_A" in saved

        path.write_text(original + "\nIDENTITY_REVISION_B\n", encoding="utf-8")
        resumed = _prompt_with_registered_grantley(
            monkeypatch, inject_identity_section=True, return_agent=True,
        )
        resumed._session_db, resumed.session_id = db, "existing"
        _restore_or_build_system_prompt(resumed, None, history)
        assert resumed._cached_system_prompt == saved
        assert db.get_session("existing")["system_prompt"] == saved

        fresh = _prompt_with_registered_grantley(
            monkeypatch, inject_identity_section=True, return_agent=True,
        )
        fresh._session_db, fresh.session_id = db, "new"
        _restore_or_build_system_prompt(fresh, None, [])
        assert "IDENTITY_REVISION_B" in fresh._cached_system_prompt
        assert "IDENTITY_REVISION_A" not in fresh._cached_system_prompt
    finally:
        db.close()


@pytest.mark.parametrize(
    ("platform", "chat_id", "chat_type"),
    [
        ("telegram", "183287894", "group"),
        ("cli", "183287894", "group"),
        ("onebot", "999999999", "group"),
    ],
)
def test_identity_section_is_absent_outside_bound_onebot_lane(
    monkeypatch,
    platform,
    chat_id,
    chat_type,
):
    prompt = _prompt_with_registered_grantley(
        monkeypatch,
        inject_identity_section=True,
        platform=platform,
        chat_id=chat_id,
        chat_type=chat_type,
    )

    assert "## Plugin Context: grantley.identity" not in prompt
    assert load_persona_document().stable not in prompt


# --- 声音段（示范层）-------------------------------------------------------
# 这一段的存在理由是实测的：身份段里关于消息形状的散文规则没有被遵守
# （见 docs/personas/grantley-corpus-20260914/FINGERPRINT.md §A2-A4）。
# 它与身份段共用同一个车道门禁，且必须留在缓存前缀里——所以这里只断言
# 预算、门禁与缓存安全这三件结构性的事，不断言模型会怎么说话。


def test_voice_section_stays_within_its_own_budget():
    assert len(load_voice_document()) <= 4000


def test_both_sections_fit_the_aggregate_plugin_budget():
    """Per-section cap is 4000, aggregate is 8000; over-budget是整段丢弃。"""
    total = len(load_persona_document().stable) + len(load_voice_document())
    assert total <= 8000


def test_voice_asset_carries_no_placeholder():
    """它整段进缓存前缀，占位符会把实时状态烙进冻结的提示词。"""
    assert "{{persona." not in load_voice_document()


def test_voice_loader_rejects_a_placeholder_bearing_asset(tmp_path):
    bad = tmp_path / "voice.md"
    bad.write_text("# 声音\n\n心情：{{persona.mood}}\n", encoding="utf-8")
    with pytest.raises(PersonaAssetError):
        load_voice_document(bad)


@pytest.mark.parametrize("canonical_group", [False, True])
def test_voice_section_registers_once_inside_the_bound_lane(monkeypatch, canonical_group):
    from plugins.platforms.onebot.adapter import format_chat_id

    chat_id = format_chat_id(True, "183287894") if canonical_group else "183287894"
    prompt = _prompt_with_registered_grantley(
        monkeypatch,
        inject_identity_section=True,
        inject_voice_section=True,
        chat_id=chat_id,
    )
    voice = load_voice_document()

    assert voice in prompt
    assert prompt.count("## Plugin Context: grantley.voice") == 1
    assert prompt.count("## Plugin Context: grantley.identity") == 1


def test_voice_section_is_absent_when_its_own_opt_in_is_false(monkeypatch):
    """身份段开着也不能顺带把声音段带进来——两个开关互相独立。"""
    prompt = _prompt_with_registered_grantley(
        monkeypatch, inject_identity_section=True, inject_voice_section=False
    )

    assert "## Plugin Context: grantley.voice" not in prompt
    assert load_voice_document() not in prompt


@pytest.mark.parametrize(
    ("platform", "chat_id", "chat_type"),
    [
        ("telegram", "183287894", "group"),
        ("cli", "183287894", "group"),
        ("onebot", "999999999", "group"),
    ],
)
def test_voice_section_is_absent_outside_bound_onebot_lane(
    monkeypatch, platform, chat_id, chat_type
):
    prompt = _prompt_with_registered_grantley(
        monkeypatch,
        inject_identity_section=True,
        inject_voice_section=True,
        platform=platform,
        chat_id=chat_id,
        chat_type=chat_type,
    )

    assert "## Plugin Context: grantley.voice" not in prompt
    assert load_voice_document() not in prompt


def test_voice_section_is_a_pure_function_of_the_asset(monkeypatch):
    """同一份资产 -> 逐字节相同的段落，与任何运行时状态无关。"""
    first = _prompt_with_registered_grantley(
        monkeypatch, inject_identity_section=False, inject_voice_section=True
    )
    second = _prompt_with_registered_grantley(
        monkeypatch, inject_identity_section=False, inject_voice_section=True
    )
    assert first == second


def test_split_refuses_a_document_without_the_volatile_heading():
    with pytest.raises(PersonaAssetError):
        split_persona_document("# 只有身份\n\n没有实时状态段。\n")


def test_fatigue_never_renders_as_a_float():
    """Bucket labels only — the [0,1] range is an implementation detail."""
    for value, bucket, shown in (
        (0.0, "rested", "精神很好"),
        (0.14, "rested", "精神很好"),
        (0.15, "fresh", "还挺精神"),
        (0.39, "fresh", "还挺精神"),
        (0.4, "mild fatigue", "有点累"),
        (0.74, "mild fatigue", "有点累"),
        (0.75, "tired", "很累"),
        (1.0, "tired", "很累"),
    ):
        state = PersonaState(persona_id="grantley", fatigue=value)
        assert bucket_fatigue(value) == bucket
        assert resolve_placeholder(state, "fatigue") == shown


def test_fatigue_bucket_boundaries_are_inclusive_lower():
    """0.4 reads as 'mild fatigue', not 'fresh' — ported boundary semantics."""
    state = PersonaState(persona_id="grantley", fatigue=0.4)
    assert resolve_placeholder(state, "fatigue") == "有点累"


def test_machine_state_values_reach_the_model_in_chinese():
    """He repeats what he reads, so storage vocabulary never reaches the block."""
    state = PersonaState(
        persona_id="grantley",
        mood="neutral",
        state_json={"life_state": "on_mission"},
    )
    assert resolve_placeholder(state, "mood") == "平常"
    assert resolve_placeholder(state, "life_state") == "在外面办事"
    custom = PersonaState(persona_id="grantley", mood="犯困", state_json={"life_state": "其他"})
    assert resolve_placeholder(custom, "mood") == "犯困"
    assert resolve_placeholder(custom, "life_state") == "其他"


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
    assert "很累" in out
    assert "tired" not in out
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
