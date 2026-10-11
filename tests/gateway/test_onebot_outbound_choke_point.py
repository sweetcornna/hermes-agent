"""Every outbound path runs BOTH gates — the drift regression.

The bug this file exists to prevent already happened once. Five distinct code
paths can put a string on QQ from this plugin:

    1. ``OneBotAdapter.send``                  — ordinary replies
    2. ``_send_attachment`` (inline branch)    — image/voice captions
    3. ``send_image`` (remote-URL branch)      — captions on remote images
    4. ``_standalone_send``                    — cron delivery, no gateway
    5. ``voice.speak`` / ``speak_ai_character``— spoken script + caption

Each carried its own copy of the content gate. When the stage-1 agent gate was
added it was added to (1) only, and nobody noticed that (2)–(5) had silently
become the weaker, keyword-only check. Nothing failed; the gate just got worse
on four paths out of five.

That is the characteristic failure of a duplicated gate: not deletion, but
**improvement of one copy**. So the audit now lives in
``adapter.guard_outbound_text`` and every path calls it, and this file asserts
that from the outside — by feeding each path a draft that only the *agent*
stage rejects. A path that still runs its own keyword-only check passes that
draft straight through and fails here.

Everything runs against a fake client and a stubbed judge: no sockets, no
NapCat, no model call.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.onebot import adapter as A
from plugins.platforms.onebot import voice as V
from plugins.platforms.onebot.agent_gate import AgentVerdict
from plugins.qzone import policy as POLICY

GROUP = 183287894
DM_PEER = 536132102

#: Blocked by the deterministic table (``fraud.account-abuse``).
TABLE_BLOCKED = "QQ解冻教程"
#: Benign to the table. Only a model can object to it — which is exactly what
#: makes it able to tell "this path runs stage 1" from "this path does not".
AGENT_BLOCKED = "那件事你懂的，我们私下说，别在群里提"
BENIGN = "今天天气不错，一起去吃饭吗"


def test_the_fixtures_are_what_this_file_claims():
    """Guard the guard: every assertion below is vacuous if these drift."""
    cfg = POLICY.resolve_config(None)
    assert POLICY.moderate_text_all(TABLE_BLOCKED, cfg).decision.allowed is False
    # The whole point of AGENT_BLOCKED is that stage 2 does NOT catch it.
    assert POLICY.moderate_text_all(AGENT_BLOCKED, cfg).decision.allowed is True
    assert POLICY.moderate_text_all(BENIGN, cfg).decision.allowed is True


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeClient:
    def __init__(self) -> None:
        self.actions: List[Any] = []
        self.connected = True
        self.last_self_id = 100
        self.last_event_at_ms = 0
        self.last_status_online = True
        self.inbound_dropped_count = 0
        self.outbound_queue_depth = 0

    async def call_action(self, action, *, timeout=None):
        self.actions.append(action)
        return {"status": "ok", "retcode": 0, "data": {"message_id": 1001}}

    async def send_action(self, action):
        self.actions.append(action)

    async def close(self):
        self.connected = False


def make_adapter(extra: Optional[Dict[str, Any]] = None) -> A.OneBotAdapter:
    base: Dict[str, Any] = {
        "ws_url": "ws://127.0.0.1:3001",
        "group_replies_enabled": True,
    }
    base.update(extra or {})
    ad = A.OneBotAdapter(PlatformConfig(enabled=True, extra=base))
    ad._client = FakeClient()
    ad._running = True
    ad._semaphore = asyncio.Semaphore(4)
    ad._account_online = True
    ad._group_names[GROUP] = "测试群"
    ad.router.group_replies_enabled = True
    return ad


@pytest.fixture(autouse=True)
def _clean_module_state():
    A._reset_module_state()
    yield
    A._reset_module_state()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("ONEBOT_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def picky_judge(monkeypatch):
    """A stage-1 judge that refuses exactly ``AGENT_BLOCKED``.

    Deliberately narrow: it approves everything else, so a path that fails a
    test below failed because it never *consulted* the gate, not because the
    stub is refusing indiscriminately.
    """
    seen: List[str] = []

    async def _judge(text, **_kw):
        seen.append(text)
        if AGENT_BLOCKED in (text or ""):
            return AgentVerdict(
                safe=False, consulted=True, reason="stub", category="test"
            )
        return AgentVerdict(safe=True, consulted=True)

    monkeypatch.setattr(A, "judge_outbound", _judge)
    return seen


@pytest.fixture
def png(tmp_path: Path) -> str:
    """A real 1x1 PNG on disk — the attachment paths stat and read the file."""
    raw = bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080600000"
        "01f15c4890000000a49444154789c6300010000050001"
        "0d0a2db40000000049454e44ae426082"
    )
    target = tmp_path / "probe.png"
    target.write_bytes(raw)
    return str(target)


# ---------------------------------------------------------------------------
# Stage 1 reaches every path
# ---------------------------------------------------------------------------


class TestTheAgentGateCoversEveryOutboundPath:
    """A draft only the model objects to must die on all five paths."""

    @pytest.mark.asyncio
    async def test_send(self, picky_judge):
        ad = make_adapter()
        res = await ad.send(f"g{GROUP}", AGENT_BLOCKED)
        assert res.success is False
        assert A.is_agent_gate_blocked_send_result(res) is True
        assert ad._client.actions == []

    @pytest.mark.asyncio
    async def test_inline_attachment_caption(self, picky_judge, png):
        ad = make_adapter()
        res = await ad.send_image_file(f"g{GROUP}", png, caption=AGENT_BLOCKED)
        assert res.success is False
        assert A.is_agent_gate_blocked_send_result(res) is True
        assert ad._client.actions == []

    @pytest.mark.asyncio
    async def test_remote_image_caption(self, picky_judge):
        ad = make_adapter()
        res = await ad.send_image(
            f"g{GROUP}", "https://example.invalid/a.png", caption=AGENT_BLOCKED
        )
        assert res.success is False
        assert A.is_agent_gate_blocked_send_result(res) is True
        assert ad._client.actions == []

    @pytest.mark.asyncio
    async def test_standalone_send(self, picky_judge):
        pconfig = PlatformConfig(
            enabled=True,
            # Unmuted: the mute short-circuits before the gates, so leaving it
            # off would make this test pass for the wrong reason.
            extra={
                "ws_url": "ws://127.0.0.1:3001",
                "group_replies_enabled": True,
            },
        )
        out = await A._standalone_send(pconfig, f"g{GROUP}", AGENT_BLOCKED)
        # Refused before the socket is opened — a websockets import error or a
        # connection failure would produce a different key entirely.
        assert out.get(A.AGENT_GATE_MARKER) is True

    @pytest.mark.asyncio
    async def test_voice_lane(self, picky_judge, monkeypatch):
        ad = make_adapter()
        outcome = await V.speak(ad, f"g{GROUP}", AGENT_BLOCKED)
        assert outcome.spoke is False
        assert ad._client.actions == []
        raw = outcome.failure.raw_response if outcome.failure else None
        assert isinstance(raw, dict) and raw.get(A.AGENT_GATE_MARKER) is True

    @pytest.mark.asyncio
    async def test_voice_caption_is_audited_too(self, picky_judge):
        """The script and the caption are two messages; both are read."""
        ad = make_adapter()
        outcome = await V.speak(ad, f"g{GROUP}", BENIGN, caption=AGENT_BLOCKED)
        assert outcome.spoke is False
        assert ad._client.actions == []


# ---------------------------------------------------------------------------
# Stage 2 still reaches every path
# ---------------------------------------------------------------------------


class TestTheMachineGateStillCoversEveryOutboundPath:
    """Adding stage 1 must not have displaced the deterministic table."""

    @pytest.mark.asyncio
    async def test_send(self):
        ad = make_adapter()
        res = await ad.send(f"g{GROUP}", TABLE_BLOCKED)
        assert A.is_policy_blocked_send_result(res) is True
        assert ad._client.actions == []

    @pytest.mark.asyncio
    async def test_inline_attachment_caption(self, png):
        ad = make_adapter()
        res = await ad.send_image_file(f"g{GROUP}", png, caption=TABLE_BLOCKED)
        assert A.is_policy_blocked_send_result(res) is True
        assert ad._client.actions == []

    @pytest.mark.asyncio
    async def test_remote_image_caption(self):
        ad = make_adapter()
        res = await ad.send_image(
            f"g{GROUP}", "https://example.invalid/a.png", caption=TABLE_BLOCKED
        )
        assert A.is_policy_blocked_send_result(res) is True
        assert ad._client.actions == []

    @pytest.mark.asyncio
    async def test_standalone_send(self):
        pconfig = PlatformConfig(
            enabled=True,
            # Unmuted: the mute short-circuits before the gates, so leaving it
            # off would make this test pass for the wrong reason.
            extra={
                "ws_url": "ws://127.0.0.1:3001",
                "group_replies_enabled": True,
            },
        )
        out = await A._standalone_send(pconfig, f"g{GROUP}", TABLE_BLOCKED)
        assert out.get(A.POLICY_MARKER) is True

    @pytest.mark.asyncio
    async def test_voice_lane(self):
        ad = make_adapter()
        outcome = await V.speak(ad, f"g{GROUP}", TABLE_BLOCKED)
        assert outcome.spoke is False
        assert ad._client.actions == []


# ---------------------------------------------------------------------------
# The composition itself
# ---------------------------------------------------------------------------


class TestTheTwoStagesCompose:
    @pytest.mark.asyncio
    async def test_stage_one_runs_first(self, monkeypatch):
        """Text both stages reject is attributed to the agent gate.

        The operator asked for "先 agent 闸后机器闸" specifically, and the
        marker is the only externally visible proof of the order.
        """

        async def _refuse(_text, **_kw):
            return AgentVerdict(safe=False, consulted=True, category="test")

        monkeypatch.setattr(A, "judge_outbound", _refuse)
        ad = make_adapter()
        res = await ad.send(f"g{GROUP}", TABLE_BLOCKED)
        assert A.is_agent_gate_blocked_send_result(res) is True
        assert A.is_policy_blocked_send_result(res) is False

    @pytest.mark.asyncio
    async def test_stage_one_fails_open_into_stage_two(self, monkeypatch):
        """A dead judge must not mute the bot — but must not open the gate."""

        async def _explode(_text, **_kw):
            raise RuntimeError("provider down")

        # judge_outbound swallows its own errors; this asserts the swallow is
        # real rather than trusting it.
        monkeypatch.setattr(
            "plugins.platforms.onebot.agent_gate.async_call_llm", _explode, raising=False
        )

        async def _fallthrough(_text, **_kw):
            return AgentVerdict(
                safe=True, consulted=False, fallthrough_cause="call_failed"
            )

        monkeypatch.setattr(A, "judge_outbound", _fallthrough)
        ad = make_adapter()

        # Benign text still goes out: the gate failed open, not closed.
        assert (await ad.send(f"g{GROUP}", BENIGN)).success is True
        # ...and stage 2 still refuses what it always refused.
        res = await ad.send(f"g{GROUP}", TABLE_BLOCKED)
        assert A.is_policy_blocked_send_result(res) is True

    @pytest.mark.asyncio
    async def test_an_unconsulted_pass_is_not_an_approval(self, monkeypatch):
        """``safe=True, consulted=False`` must never be read as "model said ok"."""

        async def _unconsulted(_text, **_kw):
            return AgentVerdict(
                safe=False, consulted=False, fallthrough_cause="timeout"
            )

        monkeypatch.setattr(A, "judge_outbound", _unconsulted)
        ad = make_adapter()
        # safe=False but consulted=False: the gate did not run, so it must fall
        # through rather than block. Blocking here would mean a provider outage
        # silences the bot.
        assert (await ad.send(f"g{GROUP}", BENIGN)).success is True

    @pytest.mark.asyncio
    async def test_stage_two_fails_closed(self, monkeypatch):
        def _boom(*_a, **_k):
            raise RuntimeError("classifier exploded")

        monkeypatch.setattr(A.content_policy, "moderate_text_all", _boom)
        ad = make_adapter()
        res = await ad.send(f"g{GROUP}", BENIGN)
        assert A.is_policy_blocked_send_result(res) is True
        assert ad._client.actions == []

    @pytest.mark.asyncio
    async def test_every_string_is_audited_not_just_the_first(self, picky_judge):
        """A refusal in the second argument still refuses."""
        refusal = await A.guard_outbound_text(
            BENIGN, AGENT_BLOCKED, target=GROUP, is_group=True
        )
        assert refusal is not None
        assert A.is_agent_gate_blocked_send_result(refusal) is True

    @pytest.mark.asyncio
    async def test_empty_and_none_strings_are_skipped(self, picky_judge):
        """Nothing to audit means nothing to refuse — and no model call."""
        assert (
            await A.guard_outbound_text(
                None, "", "   ", target=GROUP, is_group=True
            )
            is None
        )
        assert picky_judge == []

    @pytest.mark.asyncio
    async def test_the_gate_is_not_group_only(self, picky_judge):
        """The freeze risk is account-level; a DM is not a safe harbour.

        Uses the bare-number DM chat id, not ``u<id>``: the latter fails to
        parse, which would make this test pass on a routing error instead of on
        the gate actually firing.
        """
        ad = make_adapter()
        res = await ad.send(str(DM_PEER), AGENT_BLOCKED)
        assert res.success is False
        assert A.is_agent_gate_blocked_send_result(res) is True
        assert ad._client.actions == []
