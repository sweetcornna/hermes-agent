"""The two-stage outbound gate: agent first, deterministic table second.

    draft ──▶ [1] agent gate ──▶ [2] machine gate ──▶ wire
              LLM judgement       policy table
              FAILS OPEN          FAILS CLOSED

What is locked here:

* **Order.** A draft the agent refuses never reaches the classifier, and never
  reaches the wire.
* **The opposite failure postures.** Stage 1 falling over (timeout, bad JSON,
  no provider) must NOT block — it hands off to stage 2. Stage 2 falling over
  must block. Getting this backwards in either direction is the bug this file
  exists to catch: two fail-closed stages mute the bot on any provider hiccup,
  two fail-open stages ship a bad sentence.
* **Stage 2 keeps the last word.** An agent that approves something the table
  refuses does not get to override it.
* **No source text in audit fields**, same contract as the machine gate.
"""

from __future__ import annotations

import asyncio
from typing import Any, List

import pytest

from plugins.platforms.onebot import adapter as A
from plugins.platforms.onebot.agent_gate import (
    AGENT_GATE_VERSION,
    AgentGateConfig,
    AgentVerdict,
    judge_outbound,
    resolve_agent_gate_config,
)
from plugins.qzone import policy as POLICY

from .test_onebot_content_gate import (  # reuse the fakes + fixtures
    BENIGN_TEXT,
    BLOCKED_TEXT,
    DM_PEER,
    GROUP,
    FakeClient,
    make_adapter,
)

#: Benign to the machine table, so any refusal in these tests is stage 1's.
AGENT_ONLY_TEXT = "那件事你懂的，我们私下说"


def _verdict(monkeypatch, verdict: AgentVerdict, spy: List[Any] | None = None):
    async def _judge(text, **kwargs):
        if spy is not None:
            spy.append((text, kwargs))
        return verdict

    monkeypatch.setattr(A, "judge_outbound", _judge)


def test_the_agent_only_fixture_is_invisible_to_the_machine_table():
    """Guard the guard: if the table ever learns this phrase, every stage-1
    test below would pass for the wrong reason."""
    cfg = POLICY.resolve_config(None)
    assert POLICY.moderate_text_all(AGENT_ONLY_TEXT, cfg).decision.allowed is True


class TestStageOneCanRefuse:
    @pytest.mark.asyncio
    async def test_an_unsafe_verdict_blocks_a_group_send(self, monkeypatch):
        _verdict(monkeypatch, AgentVerdict(safe=False, consulted=True, category="涉政"))
        client = FakeClient()
        res = await make_adapter(client=client).send(f"g{GROUP}", AGENT_ONLY_TEXT)
        assert A.is_agent_gate_blocked_send_result(res) is True
        assert res.success is False
        assert client.actions == [], "a refused draft must never reach the wire"

    @pytest.mark.asyncio
    async def test_an_unsafe_verdict_blocks_a_dm_too(self, monkeypatch):
        _verdict(monkeypatch, AgentVerdict(safe=False, consulted=True))
        client = FakeClient()
        res = await make_adapter(client=client).send(str(DM_PEER), AGENT_ONLY_TEXT)
        assert A.is_agent_gate_blocked_send_result(res) is True
        assert client.actions == []

    @pytest.mark.asyncio
    async def test_a_safe_verdict_lets_the_draft_through(self, monkeypatch):
        _verdict(monkeypatch, AgentVerdict(safe=True, consulted=True))
        client = FakeClient()
        res = await make_adapter(client=client).send(f"g{GROUP}", BENIGN_TEXT)
        assert A.is_agent_gate_blocked_send_result(res) is False
        assert res.success is True
        assert client.actions

    @pytest.mark.asyncio
    async def test_the_two_refusals_are_distinguishable(self, monkeypatch):
        """An operator must be able to tell which gate fired without reading
        prose — the two have different trust levels and different fixes."""
        _verdict(monkeypatch, AgentVerdict(safe=False, consulted=True))
        agent_blocked = await make_adapter().send(f"g{GROUP}", AGENT_ONLY_TEXT)

        _verdict(monkeypatch, AgentVerdict(safe=True, consulted=True))
        table_blocked = await make_adapter().send(f"g{GROUP}", BLOCKED_TEXT)

        assert A.is_agent_gate_blocked_send_result(agent_blocked) is True
        assert A.is_policy_blocked_send_result(agent_blocked) is False
        assert A.is_policy_blocked_send_result(table_blocked) is True
        assert A.is_agent_gate_blocked_send_result(table_blocked) is False


class TestOrdering:
    @pytest.mark.asyncio
    async def test_stage_one_runs_before_stage_two(self, monkeypatch):
        """A draft stage 1 refuses is never handed to the classifier."""
        seen: List[str] = []
        real = POLICY.moderate_text_all

        def _spy(text, *a, **k):
            seen.append(text)
            return real(text, *a, **k)

        monkeypatch.setattr(A.content_policy, "moderate_text_all", _spy)
        _verdict(monkeypatch, AgentVerdict(safe=False, consulted=True))

        await make_adapter().send(f"g{GROUP}", AGENT_ONLY_TEXT)
        assert seen == [], "stage 2 must not run once stage 1 has refused"

    @pytest.mark.asyncio
    async def test_stage_one_sees_the_post_strip_body(self, monkeypatch):
        """Markdown is stripped before either gate, so both judge what QQ will
        actually render — not the source with syntax QQ does not display."""
        spy: List[Any] = []
        _verdict(monkeypatch, AgentVerdict(safe=True, consulted=True), spy=spy)
        await make_adapter().send(f"g{GROUP}", "**加粗** 和 `代码`")
        assert spy, "stage 1 was not consulted"
        text = spy[0][0]
        assert "**" not in text and "`" not in text

    @pytest.mark.asyncio
    async def test_stage_one_is_told_group_versus_dm(self, monkeypatch):
        spy: List[Any] = []
        _verdict(monkeypatch, AgentVerdict(safe=True, consulted=True), spy=spy)
        await make_adapter().send(f"g{GROUP}", BENIGN_TEXT)
        await make_adapter().send(str(DM_PEER), BENIGN_TEXT)
        assert spy[0][1]["is_group"] is True
        assert spy[1][1]["is_group"] is False


class TestStageTwoKeepsTheLastWord:
    @pytest.mark.asyncio
    async def test_an_approving_agent_cannot_override_the_table(self, monkeypatch):
        """The whole reason the deterministic table is still there."""
        _verdict(monkeypatch, AgentVerdict(safe=True, consulted=True))
        client = FakeClient()
        res = await make_adapter(client=client).send(f"g{GROUP}", BLOCKED_TEXT)
        assert A.is_policy_blocked_send_result(res) is True
        assert client.actions == []

    @pytest.mark.asyncio
    async def test_a_political_draft_is_still_caught_when_the_agent_approves(
        self, monkeypatch
    ):
        _verdict(monkeypatch, AgentVerdict(safe=True, consulted=True))
        res = await make_adapter().send(f"g{GROUP}", "打倒习近平")
        assert A.is_policy_blocked_send_result(res) is True
        assert res.raw_response["rule_ids"] == ["politics.leader-attack"]


class TestStageOneFailsOpen:
    """Every one of these must reach stage 2, not block.

    ``consulted=False`` is the honest signal: it means "this gate did not
    answer", not "this gate approved". The adapter only blocks on
    ``consulted and not safe``.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("cause", ["timeout", "call_failed", "unparseable"])
    async def test_a_fallthrough_verdict_does_not_block(self, monkeypatch, cause):
        _verdict(
            monkeypatch,
            AgentVerdict(safe=True, consulted=False, fallthrough_cause=cause),
        )
        client = FakeClient()
        res = await make_adapter(client=client).send(f"g{GROUP}", BENIGN_TEXT)
        assert res.success is True
        assert client.actions, "a fall-through must not silence the bot"

    @pytest.mark.asyncio
    async def test_a_fallthrough_still_lands_on_stage_two(self, monkeypatch):
        """The point of failing open: the deterministic gate is still there."""
        _verdict(monkeypatch, AgentVerdict(safe=True, consulted=False))
        res = await make_adapter().send(f"g{GROUP}", BLOCKED_TEXT)
        assert A.is_policy_blocked_send_result(res) is True

    @pytest.mark.asyncio
    async def test_an_exploding_judge_does_not_block(self, monkeypatch):
        async def _boom(_text, **_kw):
            raise RuntimeError("judge exploded")

        monkeypatch.setattr(A, "judge_outbound", _boom)
        with pytest.raises(RuntimeError):
            # Documenting the contract precisely: judge_outbound itself never
            # raises (see the unit tests below), so the adapter does not wrap
            # it. A raising stub therefore propagates — this test exists so
            # that if someone later makes judge_outbound raise for real, the
            # missing try//except is loud rather than a production 500.
            await make_adapter().send(f"g{GROUP}", BENIGN_TEXT)


class TestJudgeOutboundNeverRaises:
    """``judge_outbound`` swallows everything and reports the cause."""

    @pytest.mark.asyncio
    async def test_a_timeout_falls_through(self, monkeypatch):
        async def _slow(**_kw):
            await asyncio.sleep(10)

        monkeypatch.setattr(
            "agent.auxiliary_client.async_call_llm", _slow, raising=False
        )
        v = await judge_outbound("你好", config=AgentGateConfig(timeout_secs=0.05))
        assert v.consulted is False and v.safe is True
        assert v.fallthrough_cause == "timeout"

    @pytest.mark.asyncio
    async def test_a_provider_error_falls_through(self, monkeypatch):
        async def _boom(**_kw):
            raise RuntimeError("429 rate limited")

        monkeypatch.setattr(
            "agent.auxiliary_client.async_call_llm", _boom, raising=False
        )
        v = await judge_outbound("你好")
        assert v.consulted is False
        assert v.fallthrough_cause == "call_failed"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "answer",
        [
            "",
            "I think it is fine",
            '{"safe": "true"}',  # a truthy STRING must not read as approval
            '{"reason": "no verdict key"}',
            "{not json at all",
        ],
    )
    async def test_an_unusable_answer_falls_through(self, monkeypatch, answer):
        async def _reply(**_kw):
            return answer

        monkeypatch.setattr(
            "agent.auxiliary_client.async_call_llm", _reply, raising=False
        )
        monkeypatch.setattr(
            "agent.auxiliary_client.extract_content_or_reasoning",
            lambda r: r,
            raising=False,
        )
        v = await judge_outbound("你好")
        assert v.consulted is False
        assert v.fallthrough_cause == "unparseable"

    @pytest.mark.asyncio
    async def test_a_well_formed_answer_is_used(self, monkeypatch):
        async def _reply(**_kw):
            return '{"safe": false, "reason": "涉政攻击", "category": "political"}'

        monkeypatch.setattr(
            "agent.auxiliary_client.async_call_llm", _reply, raising=False
        )
        monkeypatch.setattr(
            "agent.auxiliary_client.extract_content_or_reasoning",
            lambda r: r,
            raising=False,
        )
        v = await judge_outbound("某段文字")
        assert v.consulted is True and v.safe is False
        assert v.category == "political"

    @pytest.mark.asyncio
    async def test_empty_text_is_not_sent_to_the_model(self, monkeypatch):
        called = []

        async def _reply(**_kw):
            called.append(1)
            return '{"safe": true}'

        monkeypatch.setattr(
            "agent.auxiliary_client.async_call_llm", _reply, raising=False
        )
        v = await judge_outbound("   ")
        assert v.consulted is False and v.fallthrough_cause == "empty"
        assert not called

    @pytest.mark.asyncio
    async def test_disabled_gate_does_not_call_the_model(self, monkeypatch):
        called = []

        async def _reply(**_kw):
            called.append(1)
            return '{"safe": true}'

        monkeypatch.setattr(
            "agent.auxiliary_client.async_call_llm", _reply, raising=False
        )
        v = await judge_outbound("你好", config=AgentGateConfig(enabled=False))
        assert v.consulted is False and v.fallthrough_cause == "disabled"
        assert not called


class TestCaching:
    @pytest.mark.asyncio
    async def test_a_decided_verdict_is_reused(self, monkeypatch):
        calls = []

        async def _reply(**_kw):
            calls.append(1)
            return '{"safe": false, "reason": "x", "category": "c"}'

        monkeypatch.setattr(
            "agent.auxiliary_client.async_call_llm", _reply, raising=False
        )
        monkeypatch.setattr(
            "agent.auxiliary_client.extract_content_or_reasoning",
            lambda r: r,
            raising=False,
        )
        text = "缓存用的一段独特文字 %s" % AGENT_GATE_VERSION
        first = await judge_outbound(text)
        second = await judge_outbound(text)
        assert first.safe is second.safe is False
        assert len(calls) == 1, "the second judgement must come from the cache"

    @pytest.mark.asyncio
    async def test_a_fallthrough_is_never_cached(self, monkeypatch):
        """Caching a transient outage would pin it into every later send."""
        calls = []

        async def _boom(**_kw):
            calls.append(1)
            raise RuntimeError("transient")

        monkeypatch.setattr(
            "agent.auxiliary_client.async_call_llm", _boom, raising=False
        )
        text = "不该被缓存的一段文字 %s" % AGENT_GATE_VERSION
        await judge_outbound(text)
        await judge_outbound(text)
        assert len(calls) == 2


class TestConfig:
    def test_only_explicit_false_disables_the_gate(self):
        assert resolve_agent_gate_config({"enabled": False}).enabled is False
        assert resolve_agent_gate_config({"enabled": "false"}).enabled is True
        assert resolve_agent_gate_config(None).enabled is True
        assert resolve_agent_gate_config("nonsense").enabled is True

    def test_malformed_numbers_fall_back_to_defaults(self):
        cfg = resolve_agent_gate_config(
            {"timeout_secs": "soon", "max_tokens": None, "cache_size": -5}
        )
        assert cfg.timeout_secs > 0 and cfg.max_tokens > 0 and cfg.cache_size > 0


class TestAuditFields:
    @pytest.mark.asyncio
    async def test_they_carry_no_source_text(self, monkeypatch):
        secret = "这段文字绝不能出现在日志里"
        _verdict(
            monkeypatch,
            AgentVerdict(safe=False, consulted=True, reason="r", category="c"),
        )
        res = await make_adapter().send(f"g{GROUP}", secret)
        assert secret not in repr(res.raw_response)

    @pytest.mark.asyncio
    async def test_they_name_the_gate_and_its_version(self, monkeypatch):
        _verdict(monkeypatch, AgentVerdict(safe=False, consulted=True, category="c"))
        res = await make_adapter().send(f"g{GROUP}", AGENT_ONLY_TEXT)
        raw = res.raw_response
        assert raw["gate"] == "agent"
        assert raw["gate_version"] == AGENT_GATE_VERSION
        assert raw["reason"] == A.AGENT_GATE_REASON
