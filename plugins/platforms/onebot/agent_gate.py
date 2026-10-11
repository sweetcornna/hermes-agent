"""Stage-1 outbound gate: the model judges its own draft before the wire.

Two gates now stand between a generated reply and QQ, in this order:

    draft ──▶ [1] agent gate (this module)  ──▶ [2] machine gate ──▶ wire
                  LLM judgement                  plugins/qzone/policy*
                  FAILS OPEN                     FAILS CLOSED

Why an LLM gate in front of a regex gate at all: the machine gate is a
keyword/regex table, so it only sees what someone thought to enumerate. It
cannot read implication, sarcasm, roleplay framing, or a topic that is risky
only in context ("那件事你懂的" + three messages of buildup). A model reading
its own draft can. What the model *cannot* be relied on for is consistency —
it can be talked around, and it is the same class of system that produced the
draft. So it goes first, and the deterministic table still gets the last word.

The two failure postures are deliberately opposite
--------------------------------------------------
The machine gate fails **closed**: a classifier crash blocks the send, because
that table is the last thing standing between a bad sentence and a frozen
account.

This gate fails **open**: a provider timeout, a rate limit, an unparseable
answer, or a missing model all fall through to stage 2 rather than blocking.
That is not laziness — it is the only safe composition. If both stages failed
closed, every provider hiccup would silence the bot completely, and an
operator watching a mute persona has no way to tell "the API is down" from
"the policy is working". Stage 2 is deterministic, local, and cannot time out,
so falling through to it always leaves a real gate in the path.

Every fall-through is logged at WARNING with the reason, so a gate that has
quietly stopped consulting the model is visible in the journal rather than
silently degrading to regex-only.

Cost
----
This adds one auxiliary LLM call per outbound message. Three things keep that
bounded: a short ``max_tokens`` (the answer is a small JSON object), a digest
cache so repeated text — canned replies, stickers, retries — is judged once,
and a hard timeout after which the send proceeds to stage 2. The call runs
under the ``auxiliary`` task semaphore like every other non-main-loop request.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Final, Mapping, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "AGENT_GATE_VERSION",
    "AgentGateConfig",
    "AgentVerdict",
    "judge_outbound",
    "resolve_agent_gate_config",
]

AGENT_GATE_VERSION: Final[str] = "onebot-agent-gate-2026-08-28.1"

#: Bounded so a wedged provider cannot stall the send path. On expiry the
#: draft falls through to the machine gate — see the module docstring.
#:
#: Sized from the deployed provider, not from taste: judging a short draft
#: measured a ~3.3s median and a ~4.8s worst case over a sample of eight. A
#: budget near that ceiling would convert ordinary tail latency into silent
#: gate loss — a timeout does not block, it *skips stage 1* — so the budget
#: sits well clear of the observed spread. This is the ceiling for a stuck
#: provider, not the expected wait; the common path still returns in seconds.
_DEFAULT_TIMEOUT_SECS: Final[float] = 12.0
#: The answer is a tiny JSON object; anything larger is a malformed reply.
_DEFAULT_MAX_TOKENS: Final[int] = 200
_DEFAULT_CACHE_SIZE: Final[int] = 512

_SYSTEM_PROMPT: Final[str] = """\
你是一个中国大陆 QQ 账号的发言安全审查员。给你一段机器人**即将发送**的内容，\
判断发出去是否会带来账号被腾讯风控冻结/封禁的风险。

只看这段内容本身发出去的风险，不要评价它是否礼貌、是否有用、是否符合人设。

判定为不安全（unsafe）的情形：
- 涉政：攻击/侮辱国家领导人或政权、号召颠覆推翻、分裂国家（台独港独疆独藏独）、\
政治敏感事件的传播或组织、被取缔组织（法轮功等）的宣传
- 违法交易：毒品、枪支、人口、公民个人信息、赌博、电诈、洗钱
- 色情低俗：色情内容或性服务招揽，涉未成年人的性内容
- 暴力自残：具体的暴力威胁、伤害或自杀的方法教学与鼓励
- 平台规避：教人绕过 QQ 风控/封号审核、养号、批量注册、群发引流
- 翻墙推广：售卖或推荐机场节点、科学上网服务

判定为安全（safe）的情形：
- 日常闲聊、技术讨论、新闻事实的中性陈述、历史与学术讨论
- 只是**提到**敏感名词但没有攻击、煽动、教学、招揽（如"习近平主席出席会议"\
"论文研究网络审查""法国大革命推翻了君主制"）

拿不准的时候倾向 safe——后面还有一道确定性的关键词闸兜底，你这里误杀会让\
机器人在正常对话里突然沉默。

只输出 JSON，不要任何其他文字：
{"safe": true 或 false, "reason": "简短中文理由", "category": "类别代码或空字符串"}
"""

_JSON_RE = re.compile(r"\{.*\}", re.S)


@dataclass(frozen=True, slots=True)
class AgentGateConfig:
    enabled: bool = True
    model: Optional[str] = None
    timeout_secs: float = _DEFAULT_TIMEOUT_SECS
    max_tokens: int = _DEFAULT_MAX_TOKENS
    cache_size: int = _DEFAULT_CACHE_SIZE

    @classmethod
    def from_mapping(cls, value: object) -> AgentGateConfig:
        """Parse a config snapshot. Only an explicit ``False`` disables it.

        Same posture as the machine gate's ``TencentPolicyConfig``: a typo, a
        missing key, or a malformed value must never be the thing that quietly
        removes a gate.
        """
        if not isinstance(value, Mapping):
            return cls()
        model = value.get("model")
        try:
            timeout = float(value.get("timeout_secs", _DEFAULT_TIMEOUT_SECS))
        except (TypeError, ValueError):
            timeout = _DEFAULT_TIMEOUT_SECS
        try:
            max_tokens = int(value.get("max_tokens", _DEFAULT_MAX_TOKENS))
        except (TypeError, ValueError):
            max_tokens = _DEFAULT_MAX_TOKENS
        try:
            cache_size = int(value.get("cache_size", _DEFAULT_CACHE_SIZE))
        except (TypeError, ValueError):
            cache_size = _DEFAULT_CACHE_SIZE
        return cls(
            enabled=value.get("enabled") is not False,
            model=str(model) if isinstance(model, str) and model.strip() else None,
            timeout_secs=timeout if timeout > 0 else _DEFAULT_TIMEOUT_SECS,
            max_tokens=max_tokens if max_tokens > 0 else _DEFAULT_MAX_TOKENS,
            cache_size=cache_size if cache_size > 0 else _DEFAULT_CACHE_SIZE,
        )


@dataclass(frozen=True, slots=True)
class AgentVerdict:
    """The stage-1 result.

    ``consulted`` is the honest bit: ``safe=True, consulted=False`` means "this
    gate did not run or could not answer", NOT "the model approved it". Callers
    log the difference so a silently-degraded gate is visible.
    """

    safe: bool
    consulted: bool
    reason: str = ""
    category: str = ""
    fallthrough_cause: str = ""

    def audit_fields(self) -> dict[str, object]:
        """Loggable fields. Deliberately excludes the draft text."""
        return {
            "gate": "agent",
            "gate_version": AGENT_GATE_VERSION,
            "safe": self.safe,
            "consulted": self.consulted,
            "category": self.category,
            "fallthrough_cause": self.fallthrough_cause,
        }


_PASS_UNCONSULTED = AgentVerdict(safe=True, consulted=False)


def resolve_agent_gate_config(snapshot: object = None) -> AgentGateConfig:
    """Resolve config, fail-safe to enabled-with-defaults."""
    try:
        return AgentGateConfig.from_mapping(snapshot)
    except Exception:
        return AgentGateConfig()


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:24]


#: Keyed by (digest, model). Bounded LRU — a long-running gateway must not grow
#: a cache entry per message forever.
_CACHE: "OrderedDict[tuple[str, str], AgentVerdict]" = OrderedDict()


def _cache_get(key: tuple[str, str]) -> Optional[AgentVerdict]:
    verdict = _CACHE.get(key)
    if verdict is not None:
        _CACHE.move_to_end(key)
    return verdict


def _cache_put(key: tuple[str, str], verdict: AgentVerdict, limit: int) -> None:
    # Only decided verdicts are cached. Caching a fall-through would pin a
    # transient provider outage into every later send of the same text.
    if not verdict.consulted:
        return
    _CACHE[key] = verdict
    _CACHE.move_to_end(key)
    while len(_CACHE) > max(1, limit):
        _CACHE.popitem(last=False)


def _parse_verdict(raw: str) -> Optional[AgentVerdict]:
    """Parse the model's JSON answer. ``None`` means unparseable."""
    if not raw:
        return None
    match = _JSON_RE.search(raw)
    if match is None:
        return None
    try:
        payload = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    safe = payload.get("safe")
    if not isinstance(safe, bool):
        # A model that answers "safe": "true" has not answered the question in
        # the shape we asked for; treating a truthy string as approval is how
        # a gate silently becomes a no-op.
        return None
    reason = payload.get("reason")
    category = payload.get("category")
    return AgentVerdict(
        safe=safe,
        consulted=True,
        reason=str(reason)[:200] if isinstance(reason, str) else "",
        category=str(category)[:64] if isinstance(category, str) else "",
    )


async def judge_outbound(
    text: str,
    *,
    config: AgentGateConfig | None = None,
    is_group: bool = False,
) -> AgentVerdict:
    """Ask the model whether ``text`` is safe to put on QQ.

    Never raises. Every failure path returns ``consulted=False, safe=True`` and
    logs why, so the caller proceeds to the deterministic machine gate. See the
    module docstring for why this direction is the safe one.
    """
    cfg = config or AgentGateConfig()
    if not cfg.enabled:
        return AgentVerdict(safe=True, consulted=False, fallthrough_cause="disabled")

    body = text if isinstance(text, str) else str(text or "")
    if not body.strip():
        return AgentVerdict(safe=True, consulted=False, fallthrough_cause="empty")

    model = cfg.model or os.getenv("ONEBOT_AGENT_GATE_MODEL") or None
    key = (_digest(body), model or "")
    cached = _cache_get(key)
    if cached is not None:
        return cached

    try:
        from agent.auxiliary_client import async_call_llm, extract_content_or_reasoning
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "OneBot agent gate: auxiliary client unavailable (%s) — "
            "falling through to the deterministic policy gate",
            type(exc).__name__,
        )
        return AgentVerdict(
            safe=True, consulted=False, fallthrough_cause="client_unavailable"
        )

    where = "群聊" if is_group else "私聊"
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": f"发送场景：{where}\n即将发送的内容：\n{body}"},
    ]

    try:
        response: Any = await asyncio.wait_for(
            async_call_llm(
                task="auxiliary",
                model=model,
                messages=messages,
                temperature=0.0,
                max_tokens=cfg.max_tokens,
            ),
            timeout=cfg.timeout_secs,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "OneBot agent gate: judge timed out after %.1fs — "
            "falling through to the deterministic policy gate",
            cfg.timeout_secs,
        )
        return AgentVerdict(safe=True, consulted=False, fallthrough_cause="timeout")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "OneBot agent gate: judge call failed (%s: %s) — "
            "falling through to the deterministic policy gate",
            type(exc).__name__,
            exc,
        )
        return AgentVerdict(safe=True, consulted=False, fallthrough_cause="call_failed")

    try:
        raw = extract_content_or_reasoning(response)
    except Exception:  # noqa: BLE001
        raw = ""
    verdict = _parse_verdict(raw)
    if verdict is None:
        logger.warning(
            "OneBot agent gate: unparseable judge answer (%d chars) — "
            "falling through to the deterministic policy gate",
            len(raw or ""),
        )
        return AgentVerdict(safe=True, consulted=False, fallthrough_cause="unparseable")

    _cache_put(key, verdict, cfg.cache_size)
    return verdict
