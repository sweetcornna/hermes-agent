"""The pre-judge that decides whether an unprompted QQ group post goes out.

The judge's model call is replaced by a fake ``async_call_llm``; nothing here
reaches a provider or a QQ group.  What matters is the direction of every
failure — the gate exists to keep the bot from talking too much, so anything
short of a clear ``{"speak": true}`` keeps it quiet.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from agent import auxiliary_client
from plugins.platforms.onebot import adapter as A
from plugins.platforms.onebot import proactive as PR
from plugins.platforms.onebot import proactive_judge as PJ

from tests.gateway.test_onebot_proactive import (
    GROUP,
    FakeHandler,
    freeze_clock,
    make_adapter,
    one_beat,
    sent_texts,
)


def _response(text: str) -> Any:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text, reasoning=None))]
    )


@pytest.fixture
def llm(monkeypatch) -> Dict[str, Any]:
    """Fake judge model: answers ``state['answer']`` and records each call."""
    state: Dict[str, Any] = {"answer": '{"speak": false, "reason": "没口子"}', "calls": []}

    async def _call(task=None, **kwargs):  # noqa: ANN001
        state["calls"].append({"task": task, **kwargs})
        answer = state["answer"]
        if isinstance(answer, BaseException):
            raise answer
        return _response(answer)

    monkeypatch.setattr(auxiliary_client, "async_call_llm", _call)
    return state


@pytest.fixture(autouse=True)
def _reset_state():
    A._reset_module_state()
    PR.set_context_provider(None)
    yield
    A._reset_module_state()
    PR.set_context_provider(None)


LINES = ["[12:00] 小明: 有人知道 Python 怎么读 sqlite 吗", "[12:01] 小红: 不知道"]


async def _judge(**over) -> PJ.JudgeVerdict:
    kwargs = dict(self_label=PR.SELF_LABEL, now_label="2026-10-03 12:02", idle_minutes=1.0)
    kwargs.update(over)
    lines = kwargs.pop("lines", LINES)
    return await PJ.judge(lines, **kwargs)


class TestVerdict:
    @pytest.mark.asyncio
    async def test_speak_true_lets_the_post_through(self, llm):
        llm["answer"] = '{"speak": true, "reason": "有人在问没人答"}'
        verdict = await _judge()
        assert verdict.speak is True and verdict.consulted is True
        assert verdict.reason == "有人在问没人答"

    @pytest.mark.asyncio
    async def test_speak_false_holds_it(self, llm):
        verdict = await _judge()
        assert verdict.speak is False and verdict.consulted is True

    @pytest.mark.asyncio
    async def test_routes_through_the_named_auxiliary_task(self, llm):
        await _judge()
        call = llm["calls"][0]
        assert call["task"] == PJ.TASK_KEY == "onebot_proactive_judge"
        system, user = call["messages"]
        assert PR.SELF_LABEL in system["content"]
        assert "不适合开口" in system["content"]
        assert "他是谁" not in system["content"]
        assert "2026-10-03 12:02" in user["content"]
        assert "1 分钟前" in user["content"]
        assert LINES[0] in user["content"] and LINES[1] in user["content"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "answer",
        [
            "好的我觉得可以说",
            '{"speak": "true", "reason": "字符串不算"}',
            '{"reason": "缺字段"}',
            "",
        ],
    )
    async def test_answers_in_the_wrong_shape_stay_quiet(self, llm, answer):
        llm["answer"] = answer
        verdict = await _judge()
        assert verdict.speak is False
        assert verdict.cause == "unparseable"

    @pytest.mark.asyncio
    async def test_json_wrapped_in_prose_is_still_read(self, llm):
        llm["answer"] = '判断如下：\n{"speak": true, "reason": "能接"}\n'
        assert (await _judge()).speak is True

    @pytest.mark.asyncio
    async def test_reasoning_is_never_read_as_the_answer(self, monkeypatch):
        """Thinking that mulls over ``{"speak": true}`` is not a verdict."""

        async def _call(task=None, **kwargs):  # noqa: ANN001
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="", reasoning='也许 {"speak": true, "reason": "?"}'
                        )
                    )
                ]
            )

        monkeypatch.setattr(auxiliary_client, "async_call_llm", _call)
        verdict = await _judge()
        assert verdict.speak is False and verdict.cause == "unparseable"

    @pytest.mark.asyncio
    async def test_a_failing_call_stays_quiet(self, llm):
        llm["answer"] = RuntimeError("502")
        verdict = await _judge()
        assert verdict.speak is False and verdict.cause == "call_failed"

    @pytest.mark.asyncio
    async def test_a_slow_call_stays_quiet(self, monkeypatch):
        async def _slow(task=None, **kwargs):  # noqa: ANN001
            await asyncio.sleep(5)

        monkeypatch.setattr(auxiliary_client, "async_call_llm", _slow)
        verdict = await _judge(settings=PJ.JudgeSettings(timeout_seconds=0.05))
        assert verdict.speak is False and verdict.cause == "timeout"

    @pytest.mark.asyncio
    async def test_an_empty_room_is_not_judged_and_stays_quiet(self, llm):
        verdict = await _judge(lines=[])
        assert verdict.speak is False and verdict.cause == "no_context"
        assert llm["calls"] == []

    @pytest.mark.asyncio
    async def test_disabled_steps_aside_without_a_call(self, llm):
        verdict = await _judge(settings=PJ.JudgeSettings(enabled=False))
        assert verdict.speak is True and verdict.cause == "disabled"
        assert llm["calls"] == []


class TestPersona:
    @pytest.mark.asyncio
    async def test_persona_reaches_the_judge(self, llm):
        settings = PJ.resolve_settings({"proactive_judge_persona": "  爱运动的虎兽人  "})
        assert settings.persona == "爱运动的虎兽人"
        await _judge(settings=settings)
        system = llm["calls"][0]["messages"][0]["content"]
        assert "他是谁：爱运动的虎兽人" in system


class TestSettings:
    def test_defaults(self):
        cfg = PJ.resolve_settings({})
        assert cfg == PJ.JudgeSettings()
        assert cfg.enabled is True

    def test_parsing(self):
        cfg = PJ.resolve_settings(
            {
                "proactive_judge_enabled": "off",
                "proactive_judge_timeout_seconds": "30",
                "proactive_judge_max_tokens": 10,
            }
        )
        assert cfg.enabled is False
        assert cfg.timeout_seconds == 30.0
        assert cfg.max_tokens == 256

    def test_bad_values_fall_back(self):
        cfg = PJ.resolve_settings(
            {"proactive_judge_timeout_seconds": "nan", "proactive_judge_max_tokens": "x"}
        )
        assert cfg.timeout_seconds == PJ.DEFAULT_TIMEOUT_SECONDS
        assert cfg.max_tokens == PJ.DEFAULT_MAX_TOKENS

    def test_yaml_keys_are_accepted(self):
        extras = A._apply_yaml_config(
            {}, {"proactive_judge_enabled": False, "proactive_judge_timeout_seconds": 20}
        )
        assert extras["proactive_judge_enabled"] is False
        assert extras["proactive_judge_timeout_seconds"] == 20

    def test_plugin_registers_the_auxiliary_task(self):
        from plugins.platforms import onebot

        seen: List[tuple] = []

        class Ctx:
            def register_platform(self, *args, **kwargs):  # noqa: ANN002, ANN003
                pass

            def register_auxiliary_task(self, key, **kwargs):  # noqa: ANN001, ANN003
                seen.append((key, kwargs))

        onebot.register(Ctx())
        assert seen and seen[0][0] == "onebot_proactive_judge"
        assert seen[0][1]["defaults"]["provider"] == "auto"


class TestLoop:
    @pytest.mark.asyncio
    async def test_judge_no_means_the_persona_is_never_asked(self, monkeypatch, llm):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        handler = FakeHandler("不该出现")
        ad = make_adapter({"proactive_enabled": True}, handler=handler)
        A.record_group_message("default", GROUP, "小明", "今天好热", False)
        await PR.proactive_loop(ad, asyncio.Event(), PR.live_config(ad))
        assert len(llm["calls"]) == 1
        assert handler.calls == 0
        assert sent_texts(ad) == []
        assert PR.sent_today(A.speech_key("default", GROUP), "2026-08-19") == 0

    @pytest.mark.asyncio
    async def test_judge_yes_hands_over_to_the_persona(self, monkeypatch, llm):
        llm["answer"] = json.dumps({"speak": True, "reason": "能接"})
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        handler = FakeHandler("用 sqlite3 模块就行")
        ad = make_adapter({"proactive_enabled": True}, handler=handler)
        A.record_group_message("default", GROUP, "小明", "有人知道怎么读 sqlite 吗", False)
        await PR.proactive_loop(ad, asyncio.Event(), PR.live_config(ad))
        assert handler.calls == 1
        assert sent_texts(ad) == ["用 sqlite3 模块就行"]

    @pytest.mark.asyncio
    async def test_judge_reads_the_whole_buffer_even_with_a_small_context(
        self, monkeypatch, llm
    ):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        ad = make_adapter({"proactive_enabled": True, "proactive_context_messages": 0})
        for i in range(5):
            A.record_group_message("default", GROUP, f"u{i}", f"第{i}句", False)
        await PR.proactive_loop(ad, asyncio.Event(), PR.live_config(ad))
        user = llm["calls"][0]["messages"][1]["content"]
        assert all(f"第{i}句" in user for i in range(5))

    @pytest.mark.asyncio
    async def test_an_empty_buffer_holds_the_post_without_a_model_call(
        self, monkeypatch, llm
    ):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        handler = FakeHandler("不该出现")
        ad = make_adapter({"proactive_enabled": True}, handler=handler)
        await PR.proactive_loop(ad, asyncio.Event(), PR.live_config(ad))
        assert llm["calls"] == [] and handler.calls == 0

    @pytest.mark.asyncio
    async def test_judge_disabled_restores_the_old_ladder(self, monkeypatch, llm):
        one_beat(monkeypatch)
        freeze_clock(monkeypatch)
        handler = FakeHandler("大家下午好呀")
        ad = make_adapter(
            {"proactive_enabled": True, "proactive_judge_enabled": False}, handler=handler
        )
        await PR.proactive_loop(ad, asyncio.Event(), PR.live_config(ad))
        assert llm["calls"] == []
        assert sent_texts(ad) == ["大家下午好呀"]
