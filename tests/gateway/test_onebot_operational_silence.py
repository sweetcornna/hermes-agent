"""Nothing of hermes' own reaches QQ — including the parts that arrive unflagged.

``suppress_system_messages`` already hid everything the gateway *stamps* as
non-conversational. It did not hide the operational replies the gateway
**returns as the turn's answer** — "Hermes is at the active session limit",
"⏳ Another turn is still running on this session", the provider-error family.
Those never pass through ``_non_conversational_metadata``, carry no flag, and
so reached the chat looking like a persona reply. The switch read as "system
messages are hidden" while system messages were visibly not hidden.

Closing that means a text test, which is normally the wrong tool: a persona
that writes emoji eventually collides with an emoji-shaped filter. Two
conditions together make it safe, and the second is the load-bearing one —
**this persona speaks Chinese**, so a message that opens with a warning glyph
and contains not one CJK character is hermes talking about itself.

The false-positive half of this file is therefore the important half. If these
rules ever start eating real replies, the bot goes quiet in a way nobody gets
an alert about.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List, Optional

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.onebot import adapter as A

GROUP = 183287894
DM_PEER = 536132102


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


# ---------------------------------------------------------------------------
# Suppressed: hermes talking about itself
# ---------------------------------------------------------------------------

OPERATIONAL = [
    "Hermes is at the active session limit (3/3). Try again when another session finishes.",
    "⏳ Another turn is still running on this session. To protect the transcript, "
    "this message was not processed.",
    "⚠️ Provider authentication failed. Check the configured credentials; raw "
    "provider details are in the gateway logs.",
    "⚠️ The model provider rejected the request.",
    "⚠️ Session too large for the model's context window.",
    "⚠️ Processing completed but no response was generated.",
    "⏳ Gateway is restarting and is not accepting another turn right now.",
    "⚕ Update needs your input",
    "⚠️ Session database is unavailable; messages may not be persisted",
    "❌ Steer failed: boom",
    # gateway/run.py::_gateway_provider_error_reply, verbatim. It reached QQ
    # three times on 2026-10-04 while the provider was cooling down, once per
    # 1200 s retry cycle, because ⏱ was missing from the glyph set.
    "⏱️ The model provider is rate-limiting requests. Please wait a moment and try again.",
]


class TestOperationalTextIsSilenced:
    @pytest.mark.parametrize("text", OPERATIONAL)
    def test_it_is_recognised(self, text: str):
        assert A.is_operational_reply(text) is True

    @pytest.mark.parametrize("text", OPERATIONAL)
    @pytest.mark.asyncio
    async def test_it_never_reaches_the_wire(self, text: str):
        ad = make_adapter()
        res = await ad.send(f"g{GROUP}", text)
        assert A.is_suppressed_send_result(res) is True
        assert ad._client.actions == []

    @pytest.mark.asyncio
    async def test_dms_are_silenced_too(self):
        """The operator's DM is the one place these used to be most visible."""
        ad = make_adapter()
        res = await ad.send(str(DM_PEER), OPERATIONAL[0])
        assert A.is_suppressed_send_result(res) is True
        assert ad._client.actions == []

    @pytest.mark.asyncio
    async def test_the_two_old_carve_outs_are_gone(self):
        """'全部静默' means the update prompt and the DB alarm go too."""
        assert A._ALWAYS_DELIVER_PATTERNS == ()
        ad = make_adapter()
        for text in ("⚕ Update needs your input", "⚠️ Session database is gone"):
            res = await ad.send(str(DM_PEER), text)
            assert A.is_suppressed_send_result(res) is True
        assert ad._client.actions == []


# ---------------------------------------------------------------------------
# NOT suppressed: the persona doing its job
# ---------------------------------------------------------------------------

PERSONA = [
    "今天挥剑五百次，我胳膊快废了。",
    "⚠️ 注意，那边的台阶有点滑，你走慢点。",
    "⏳ 等我一下，马上就好。",
    "哈？你说什么，我没听清。",
    "行吧，那就这么定了。别赖账。",
    # A real error the persona is relaying, in its own voice.
    "⚠️ 工具报错了：connection refused，我再试一次。",
    # Code is ASCII but carries no operational glyph.
    "def hello():\n    return 'world'",
    "OK",
]


class TestPersonaRepliesAreUntouched:
    """The load-bearing half: a filter that eats real replies is worse than
    the leak it fixed, because nothing alerts on a bot that went quiet."""

    @pytest.mark.parametrize("text", PERSONA)
    def test_it_is_not_operational(self, text: str):
        assert A.is_operational_reply(text) is False

    @pytest.mark.parametrize("text", PERSONA)
    @pytest.mark.asyncio
    async def test_it_still_goes_out(self, text: str):
        ad = make_adapter()
        res = await ad.send(f"g{GROUP}", text)
        assert res.success is True
        assert ad._client.actions != []

    def test_a_glyph_alone_is_not_enough(self):
        """CJK present ⇒ the persona wrote it, whatever it starts with."""
        assert A.is_operational_reply("⚠️ 会话太长了，我重新开一段。") is False

    def test_english_alone_is_not_enough(self):
        """No glyph ⇒ not operational, even in pure English."""
        assert A.is_operational_reply("Another turn is running, probably.") is False
        assert A.is_operational_reply("Session too large for the window") is False

    def test_empty_is_not_operational(self):
        for probe in ("", "   ", None):
            assert A.is_operational_reply(probe) is False


class TestApprovalRequestsSurvive:
    """The one exception, and the reason it exists.

    An approval request BLOCKS the turn that raised it. Hiding it does not make
    the channel quieter — it deadlocks the agent on a question nobody was shown,
    with no timeout and no alert. Silence is the goal; a stuck bot is not
    silence. The ``hermes update`` prompt blocks the same way and is
    deliberately NOT excepted: it only appears when an operator runs an update
    and is therefore already watching, whereas an approval can be triggered by
    ordinary agent work at any moment.
    """

    APPROVALS = [
        "⚠️ **Dangerous command requires approval:**\n```\nrm -rf /tmp/x\n```",
        "🔒 Approval required for this operation",
        "⚠️ **Smart DENY — owner override for one operation:**",
    ]

    @pytest.mark.parametrize("text", APPROVALS)
    def test_it_is_not_treated_as_operational(self, text: str):
        assert A.is_operational_reply(text) is False

    @pytest.mark.parametrize("text", APPROVALS)
    @pytest.mark.asyncio
    async def test_it_still_reaches_the_operator(self, text: str):
        ad = make_adapter()
        res = await ad.send(str(DM_PEER), text)
        assert res.success is True
        assert ad._client.actions != []


class TestTheSwitchStillGoverns:
    @pytest.mark.asyncio
    async def test_turning_suppression_off_delivers_everything(self):
        """The operator can still get the old behaviour back."""
        ad = make_adapter({"suppress_system_messages": False})
        res = await ad.send(f"g{GROUP}", OPERATIONAL[0])
        assert res.success is True
        assert ad._client.actions != []
