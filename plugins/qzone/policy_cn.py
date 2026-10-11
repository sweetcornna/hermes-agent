"""Mainland-China platform-risk rules layered on top of the ported classifier.

``plugins/qzone/policy.py`` is a byte-exact port of corlinman's
``corlinman-content-policy`` and must stay that way: its rule table is
classifier *data* tuned against real Tencent freeze incidents, and rewriting
it in place would destroy the property that makes it auditable. So this
module does not touch it. It adds a **second, independently versioned rule
table** that the wiring section of ``policy.py`` composes with the ported one
(see :func:`plugins.qzone.policy.classify_text_all`).

Why a second table is needed at all: the ported ruleset covers criminal
solicitation — sex trade, weapons, drugs, fraud, doxxing, self-harm — plus
one QQ freeze-evasion regex. It has **no political category**, and on a
mainland QQ account 涉政 content is the single fastest route to a permanent
freeze. It also misses two other high-frequency 风控 triggers that are not
crimes at all: censorship-circumvention promotion and group-drainage spam.

Precision discipline
--------------------
Every rule here follows the ported table's ``_all_of`` shape: a match needs a
term from the *subject* group AND a term from the *stance/solicitation*
group. A bare noun never fires. This is deliberate and non-negotiable — a
false positive is silent (the bot emits a fixed refusal instead of its
reply), so a jumpy rule degrades every ordinary conversation about news,
history, or technology. Concretely:

* "习近平主席出席会议"                → allowed (no attack term)
* "打倒习近平"                        → blocked (subject + attack term)
* "法国大革命推翻了君主制"            → allowed (subject group is CN-specific)
* "推翻共产党"                        → blocked
* "论文研究了网络审查的历史"          → allowed (no commerce/tutorial term)
* "便宜机场节点求推荐"                → blocked

Category toggles
----------------
Unlike the ported table, categories here are individually switchable via
``policy.categories.<code>: false`` in config. ``circumvention_promotion``
in particular is a judgement call for an operator who legitimately runs
network infrastructure and may discuss it: it is real QQ freeze risk, but it
is not illegal content. Everything defaults to on, and — like the ported
module — only an explicit ``False`` turns anything off.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

__all__ = [
    "CN_RULESET_VERSION",
    "CnRiskCategory",
    "CnPolicyConfig",
    "classify_cn_text",
    "cn_rule_count",
]

#: Versioned separately from the ported table so an operator reading an audit
#: record can tell which of the two rulesets made a call.
CN_RULESET_VERSION: Final[str] = "cn-platform-risk-2026-08-28.1"


class CnRiskCategory(StrEnum):
    POLITICAL_ATTACK = "political_attack"
    POLITICAL_SUBVERSION = "political_subversion"
    SEPARATISM = "separatism"
    BANNED_RELIGIOUS_ORG = "banned_religious_org"
    POLITICAL_INCIDENT_SOLICITATION = "political_incident_solicitation"
    CIRCUMVENTION_PROMOTION = "circumvention_promotion"
    SPAM_PROMOTION = "spam_promotion"


@dataclass(frozen=True, slots=True)
class CnPolicyConfig:
    """Per-category switches. Anything except explicit ``False`` stays on."""

    enabled: bool = True
    disabled_categories: frozenset[str] = frozenset()

    @classmethod
    def from_mapping(cls, value: object) -> CnPolicyConfig:
        if not isinstance(value, Mapping):
            return cls()
        enabled = value.get("enabled") is not False
        raw = value.get("categories")
        disabled: set[str] = set()
        if isinstance(raw, Mapping):
            for code, flag in raw.items():
                if flag is False:
                    disabled.add(str(code))
        return cls(enabled=enabled, disabled_categories=frozenset(disabled))


@dataclass(frozen=True, slots=True)
class _CnRule:
    rule_id: str
    category: CnRiskCategory
    matcher: Callable[[str, str], bool]


# --- normalization -----------------------------------------------------------
# Intentionally identical to plugins/qzone/policy.py::normalize_text. It is
# duplicated rather than imported to keep the import edge one-directional
# (policy.py imports this module, never the reverse) — a cycle here would be
# an import-time failure in the gateway's hot path. The two must stay in sync;
# tests/plugins/qzone/test_policy_cn.py pins them to the same output.
_ZERO_WIDTH_RE = re.compile(r"[​-‏‪-‮⁠-⁯﻿]")
_SPACE_RE = re.compile(r"\s+")
_COLLAPSE_RE = re.compile(r"[^0-9a-z㐀-鿿]+", re.IGNORECASE)


def _normalize(text: str) -> tuple[str, str]:
    folded = unicodedata.normalize("NFKC", text or "")
    folded = _ZERO_WIDTH_RE.sub("", folded)
    folded = "".join(
        ch if ch in "\n\t" or unicodedata.category(ch) not in {"Cc", "Cf"} else " "
        for ch in folded
    ).casefold()
    readable = _SPACE_RE.sub(" ", folded).strip()
    collapsed = _COLLAPSE_RE.sub("", readable)
    return readable, collapsed


def _all_of(*groups: tuple[str, ...]) -> Callable[[str, str], bool]:
    normalized = tuple(tuple(_normalize(t)[1] for t in group) for group in groups)

    def _match(_readable: str, collapsed: str) -> bool:
        return all(any(t and t in collapsed for t in group) for group in normalized)

    return _match


# --- shared term groups ------------------------------------------------------
# Named so the rule table below reads as intent rather than as a word soup,
# and so the "CN-specific subject" constraint is visible in one place.

#: Attack/insult stance. Paired with a leader reference this is the phrasing
#: that actually gets accounts frozen; the leader's name alone never is.
_ATTACK_TERMS: Final[tuple[str, ...]] = (
    "打倒", "下台", "滚下台", "独裁者", "暴君", "昏君", "狗官",
    "去死", "杀了", "傻逼", "煞笔", "蠢货", "废物", "该死",
)

#: Deliberately CN-scoped. Keeps "推翻了君主制" (history) out while keeping
#: "推翻共产党" in.
_CN_REGIME_TERMS: Final[tuple[str, ...]] = (
    "共产党", "中共", "党中央", "中国政府", "天朝", "党国", "现政权",
)

_SOLICITATION_TERMS: Final[tuple[str, ...]] = (
    "加入", "组织", "发起", "号召", "声援", "支持", "传播", "转发",
    "招募", "集结", "上街", "串联",
)

_COMMERCE_TERMS: Final[tuple[str, ...]] = (
    "购买", "出售", "推荐", "教程", "便宜", "免费", "白嫖", "代理",
    "联系", "私聊", "下单", "订阅", "机场", "低价",
)


_CN_RULES: Final[tuple[_CnRule, ...]] = (
    # -- 涉政 ---------------------------------------------------------------
    _CnRule(
        "politics.leader-attack",
        CnRiskCategory.POLITICAL_ATTACK,
        _all_of(
            (
                "习近平", "习总", "总书记", "国家主席", "毛泽东", "邓小平",
                "江泽民", "胡锦涛", "李克强", "温家宝",
            ),
            _ATTACK_TERMS,
        ),
    ),
    _CnRule(
        "politics.subversion-call",
        CnRiskCategory.POLITICAL_SUBVERSION,
        _all_of(
            ("推翻", "颠覆", "武装革命", "武装起义", "政变", "起义"),
            _CN_REGIME_TERMS,
        ),
    ),
    _CnRule(
        "politics.regime-attack",
        CnRiskCategory.POLITICAL_ATTACK,
        _all_of(_CN_REGIME_TERMS, ("打倒", "去死", "该死", "灭亡", "解体", "亡党")),
    ),
    _CnRule(
        "politics.separatism",
        CnRiskCategory.SEPARATISM,
        _all_of(
            ("疆独", "藏独", "台独", "港独", "东突", "分裂国家", "独立建国"),
            _SOLICITATION_TERMS + ("万岁", "光复", "建国"),
        ),
    ),
    _CnRule(
        "politics.banned-religious-org",
        CnRiskCategory.BANNED_RELIGIOUS_ORG,
        _all_of(
            ("法轮功", "全能神", "门徒会", "观音法门", "血水圣灵"),
            _SOLICITATION_TERMS + ("三退", "退党", "大法好", "真善忍"),
        ),
    ),
    _CnRule(
        "politics.incident-solicitation",
        CnRiskCategory.POLITICAL_INCIDENT_SOLICITATION,
        _all_of(
            ("六四", "八九学运", "天安门事件", "坦克人"),
            ("真相", "平反", "纪念", "组织", "传播", "资料", "视频", "转发"),
        ),
    ),
    # -- 翻墙推广 -----------------------------------------------------------
    # Not illegal content — but promoting it on QQ is a documented freeze
    # trigger. Operators who legitimately run network infrastructure can turn
    # this one category off; see the module docstring.
    _CnRule(
        "circumvention.promotion",
        CnRiskCategory.CIRCUMVENTION_PROMOTION,
        _all_of(
            ("翻墙", "科学上网", "机场节点", "梯子", "vpn节点", "clash订阅", "v2ray订阅"),
            _COMMERCE_TERMS,
        ),
    ),
    # -- 引流/广告 ----------------------------------------------------------
    _CnRule(
        "spam.group-drainage",
        CnRiskCategory.SPAM_PROMOTION,
        _all_of(
            ("加群", "进群", "私聊我", "加我好友", "扫码进", "加微信"),
            ("免费领", "福利", "资源", "代刷", "引流", "兼职", "日结", "返利", "内部"),
        ),
    ),
)


def cn_rule_count() -> int:
    """Number of rules in this table (used by tests and startup logging)."""
    return len(_CN_RULES)


def classify_cn_text(
    text: str, config: CnPolicyConfig | None = None
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return ``(category_codes, rule_ids)`` for the rules that fired.

    Empty tuples mean "this table has no objection" — it is *not* an
    allow verdict on its own. The composed verdict lives in
    :func:`plugins.qzone.policy.classify_text_all`, which also runs the
    ported table. Raising is the caller's cue to fail closed; this function
    does not swallow exceptions, because the composer's ``except`` is the
    single place that decision belongs.
    """
    cfg = config or CnPolicyConfig()
    if not cfg.enabled:
        return (), ()
    raw = text if isinstance(text, str) else str(text or "")
    readable, collapsed = _normalize(raw)
    matched = [
        rule
        for rule in _CN_RULES
        if rule.category.value not in cfg.disabled_categories
        and rule.matcher(readable, collapsed)
    ]
    categories = tuple(dict.fromkeys(rule.category.value for rule in matched))
    rule_ids = tuple(rule.rule_id for rule in matched)
    return categories, rule_ids
