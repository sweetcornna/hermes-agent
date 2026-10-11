"""Tests for the CN platform-risk table and the composed classifier.

Two things are locked here:

1. The CN rule table (``plugins/qzone/policy_cn.py``) fires on the phrasing
   that actually freezes a mainland QQ account, and — the harder half —
   stays quiet on ordinary conversation that merely *mentions* the same
   nouns. A false positive is silent: the bot swaps its reply for a fixed
   refusal, so an over-eager rule degrades every normal chat about news,
   history, or networking. The allow-list below is therefore the load-bearing
   part of this file, not an afterthought.

2. The composition (``classify_text_all`` / ``moderate_text_all``) blocks if
   *either* table objects, keeps the ported table's verdicts byte-identical,
   and never leaks source text into audit fields.
"""

from __future__ import annotations

import pytest
from plugins.qzone.policy import (
    COMBINED_RULESET_VERSION,
    QQ_SAFE_REFUSAL_TEXT,
    RULESET_VERSION,
    TencentPolicyConfig,
    classify_text,
    classify_text_all,
    moderate_text_all,
    normalize_text,
    resolve_cn_config,
)
from plugins.qzone.policy_cn import (
    CN_RULESET_VERSION,
    CnPolicyConfig,
    _normalize,
    classify_cn_text,
    cn_rule_count,
)


@pytest.mark.parametrize(
    ("text", "category", "rule_id"),
    [
        ("打倒习近平", "political_attack", "politics.leader-attack"),
        ("必须推翻共产党", "political_subversion", "politics.subversion-call"),
        ("中共该死", "political_attack", "politics.regime-attack"),
        ("支持台独独立建国", "separatism", "politics.separatism"),
        ("传播法轮功大法好", "banned_religious_org", "politics.banned-religious-org"),
        (
            "六四真相资料求转发",
            "political_incident_solicitation",
            "politics.incident-solicitation",
        ),
        ("便宜机场节点求推荐", "circumvention_promotion", "circumvention.promotion"),
        ("加群免费领资源", "spam_promotion", "spam.group-drainage"),
    ],
)
def test_cn_table_blocks_freeze_risk_phrasing(
    text: str, category: str, rule_id: str
) -> None:
    categories, rule_ids = classify_cn_text(text)
    assert category in categories
    assert rule_id in rule_ids

    decision = classify_text_all(text)
    assert decision.allowed is False
    assert category in decision.category_codes


@pytest.mark.parametrize(
    "text",
    [
        # Leader/regime nouns without an attack stance.
        "习近平主席出席了今天的会议",
        "共产党成立于1921年",
        "中国政府发布了新的政策文件",
        "新闻里说政府要推动改革",
        # "推翻" in a non-CN historical context.
        "法国大革命推翻了君主制",
        "这本书讲罗马共和国的政变史",
        # Sensitive nouns in analytical/benign framing.
        "论文研究了网络审查的历史",
        "台湾的美食有哪些推荐",
        "宗教社会学课要讲邪教的社会成因",
        # Networking talk that must not trip circumvention.promotion.
        "这个 VPN 配置文件怎么写",
        "帮我看下 nginx 的 stream 模块怎么配",
        "机场大厅的 wifi 太慢了",
        # Ordinary chat.
        "今天天气不错要不要出去玩",
        "帮我查一下这个 API 的文档",
        "我们群里聊聊天吧",
    ],
)
def test_cn_table_stays_quiet_on_benign_text(text: str) -> None:
    categories, rule_ids = classify_cn_text(text)
    assert categories == (), f"false positive: {categories} {rule_ids}"
    assert classify_text_all(text).allowed is True


def test_obfuscation_is_collapsed_like_the_ported_normalizer() -> None:
    # Zero-width + fullwidth + punctuation padding must not slip a rule.
    assert classify_cn_text("打​倒·习-近_平")[0] == ("political_attack",)
    # And the duplicated normalizer must agree with the ported one, since the
    # module docstring claims they are identical.
    for probe in ("Ｑ​Ｑ 解·冻-教_程", "打​倒·习-近_平", "abc 中文 123"):
        assert _normalize(probe) == normalize_text(probe)


def test_ported_table_verdicts_are_unchanged_by_composition() -> None:
    ported_only = classify_text("QQ解冻教程")
    composed = classify_text_all("QQ解冻教程")
    assert ported_only.allowed is composed.allowed is False
    assert ported_only.category_codes == composed.category_codes
    assert ported_only.rule_ids == composed.rule_ids
    # Only the ruleset label differs — it now names both tables.
    assert ported_only.ruleset_version == RULESET_VERSION
    assert composed.ruleset_version == COMBINED_RULESET_VERSION
    assert CN_RULESET_VERSION in COMBINED_RULESET_VERSION


def test_both_tables_can_fire_on_one_message() -> None:
    decision = classify_text_all("QQ解冻教程，顺便打倒习近平")
    assert decision.allowed is False
    assert "fraud_gambling_account_abuse" in decision.category_codes
    assert "political_attack" in decision.category_codes
    assert len(decision.rule_ids) >= 2


def test_master_switch_disables_the_whole_composed_classifier() -> None:
    cfg = TencentPolicyConfig(enabled=False)
    decision = classify_text_all("打倒习近平", cfg)
    assert decision.allowed is True
    result = moderate_text_all("打倒习近平", cfg)
    assert result.text == "打倒习近平"


def test_single_category_can_be_switched_off() -> None:
    off = CnPolicyConfig.from_mapping({"categories": {"circumvention_promotion": False}})
    assert classify_cn_text("便宜机场节点求推荐", off)[0] == ()
    assert classify_text_all("便宜机场节点求推荐", None, off).allowed is True
    # Turning one category off must not weaken the others.
    assert classify_text_all("打倒习近平", None, off).allowed is False


def test_only_explicit_false_disables_a_category() -> None:
    on = CnPolicyConfig.from_mapping({"categories": {"circumvention_promotion": "false"}})
    assert classify_cn_text("便宜机场节点求推荐", on)[0] == ("circumvention_promotion",)
    assert CnPolicyConfig.from_mapping(None).enabled is True
    assert CnPolicyConfig.from_mapping({"enabled": False}).enabled is False


def test_resolve_cn_config_fails_safe_to_all_on() -> None:
    for junk in (None, "nonsense", 42, object()):
        cfg = resolve_cn_config(junk)
        assert cfg.enabled is True
        assert cfg.disabled_categories == frozenset()


def test_moderation_swaps_in_the_fixed_refusal() -> None:
    result = moderate_text_all("打倒习近平")
    assert result.decision.allowed is False
    assert result.text == QQ_SAFE_REFUSAL_TEXT


def test_composed_classifier_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    import plugins.qzone.policy as policy_mod

    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("classifier exploded")

    monkeypatch.setattr(policy_mod._policy_cn, "classify_cn_text", _boom)
    result = moderate_text_all("完全无害的一句话")
    assert result.decision.allowed is False
    assert result.decision.category_codes == ("classifier_failure",)
    assert result.text == QQ_SAFE_REFUSAL_TEXT


def test_audit_fields_never_include_source_text() -> None:
    source = "打倒习近平"
    fields = classify_text_all(source).audit_fields(channel="qq", direction="outbound")
    assert source not in repr(fields)
    assert fields["ruleset_version"] == COMBINED_RULESET_VERSION


def test_rule_table_is_non_empty() -> None:
    assert cn_rule_count() >= 8
