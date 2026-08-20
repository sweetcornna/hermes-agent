"""Per-channel persona binding, on BOTH lanes (E0 task B / 00-PLAN.md §18).

``plugins/grantley/channel_binding.py`` was written (C1) as *the* integration
point for this adapter and then never called: B3's repo-wide grep found
``resolve_channel_prompt`` with zero callers, and B4 independently found
``MessageEvent.channel_prompt`` unset on both OneBot lanes — the same gap,
discovered twice.  Until it was wired,
``plugins.entries.grantley.settings.channels`` was a pure declaration: an
operator could write ``channel_owner: "2104743984"`` and nothing would read it.

B4 deliberately declined to wire only the proactive lane, on the grounds that a
persona framed one way when it answers and another way when it speaks first is
a persona with two characters.  So these tests pin all three properties
together: the binding takes effect, an absent binding degrades safely, and the
two lanes produce **the same** frame.

Everything runs against a fake client and the real (offline, file-backed) seed
pack — no sockets, no model call.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List, Optional

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import MessageEvent

from plugins.platforms.onebot import adapter as A
from plugins.platforms.onebot import persona_binding as PB
from plugins.platforms.onebot import proactive as PR
from plugins.platforms.onebot import protocol as P


GROUP = 183287894
OWNER = "2104743984"
DM_PEER = 536132102


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeClient:
    """Records every outbound action; never touches a socket."""

    def __init__(self) -> None:
        self.actions: List[Any] = []
        self.fire_and_forget: List[Any] = []
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
        self.fire_and_forget.append(action)

    async def close(self):
        self.connected = False


class FakeHandler:
    """Stand-in for the gateway message handler."""

    def __init__(self, reply: str = "嗯") -> None:
        self.reply = reply
        self.events: List[MessageEvent] = []

    async def __call__(self, event: MessageEvent):
        self.events.append(event)
        return self.reply


def make_adapter(
    extra: Optional[Dict[str, Any]] = None,
    *,
    client: Optional[FakeClient] = None,
    handler: Optional[FakeHandler] = None,
) -> A.OneBotAdapter:
    base: Dict[str, Any] = {"ws_url": "ws://127.0.0.1:3001"}
    base.update(extra or {})
    ad = A.OneBotAdapter(PlatformConfig(enabled=True, extra=base))
    ad._client = client or FakeClient()
    ad._running = True
    ad._semaphore = asyncio.Semaphore(4)
    ad._account_online = True
    ad._group_names[GROUP] = "测试群"
    ad._message_handler = handler or FakeHandler()
    return ad


def group_event(text="hi", *, gid=GROUP, uid=555, message_id=1, segments=None):
    return P.MessageEvent(
        self_id=100, message_type=P.MessageType.GROUP, sub_type="normal",
        group_id=gid, user_id=uid, message_id=message_id,
        message=segments if segments is not None else [P.TextSegment(text=text)],
        raw_message=text, time=1_700_000_000,
        sender=P.Sender(user_id=uid, nickname="alice"),
    )


def private_event(text="hi", *, uid=DM_PEER, message_id=1):
    return P.MessageEvent(
        self_id=100, message_type=P.MessageType.PRIVATE, sub_type="friend",
        group_id=None, user_id=uid, message_id=message_id,
        message=[P.TextSegment(text=text)], raw_message=text, time=1,
        sender=P.Sender(user_id=uid, nickname="bob"),
    )


CHANNELS = {
    str(GROUP): {"persona": "grantley", "channel_owner": OWNER, "group": True,
                 "name": "群聊-JLU"},
    str(DM_PEER): {"persona": "grantley", "group": False},
}


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


@pytest.fixture(autouse=True)
def _no_ambient_persona_config(monkeypatch):
    """Never let the developer's own ``config.yaml`` decide these tests.

    The fallback source is a real file read; pinning it to ``{}`` keeps the
    suite hermetic and makes every binding in here explicit.
    """
    monkeypatch.setattr(PB, "_plugin_settings", {})


@pytest.fixture(autouse=True)
def _no_stickers(monkeypatch):
    """Take the sticker menu out of every assertion in this file.

    ``channel_prompt`` composes two independent things: the per-channel
    persona frame these tests are about, and the probabilistic sticker menu
    (``test_onebot_stickers.py``).  Left at its default the menu appears on
    ~18% of calls, which would make every ``is None`` assertion below fail
    roughly one run in five — the tests would still be *testing* binding
    resolution, just unreliably.

    Declared after ``_clean_env`` so it wins: that fixture strips every
    ``ONEBOT_`` variable, and this one puts back the single value these tests
    need pinned.
    """
    monkeypatch.setenv("ONEBOT_STICKER_PROBABILITY", "0")




class TestBindingResolution:

    def test_a_bound_group_gets_the_owner_framing(self):
        prompt = PB.channel_prompt(
            {PB.EXTRA_KEY: CHANNELS}, chat_id=GROUP, is_group=True
        )
        assert prompt
        assert OWNER in prompt
        assert "群聊" in prompt

    def test_a_bound_dm_is_framed_as_a_dm(self):
        prompt = PB.channel_prompt(
            {PB.EXTRA_KEY: CHANNELS}, chat_id=DM_PEER, is_group=False
        )
        assert prompt and "私聊" in prompt
        assert OWNER not in prompt          # no owner configured for the DM

    def test_an_unbound_channel_gets_no_frame_at_all(self):
        """Not an empty string — an empty ephemeral block is still a block."""
        assert PB.channel_prompt(
            {PB.EXTRA_KEY: CHANNELS}, chat_id=999999, is_group=True
        ) is None

    def test_no_config_at_all_degrades_quietly(self):
        assert PB.channel_prompt({}, chat_id=GROUP, is_group=True) is None

    def test_a_malformed_entry_is_skipped_not_raised(self):
        prompt = PB.channel_prompt(
            {PB.EXTRA_KEY: {str(GROUP): "not-a-mapping"}},
            chat_id=GROUP, is_group=True,
        )
        assert prompt is None

    def test_a_garbage_channel_map_degrades_quietly(self):
        assert PB.channel_prompt(
            {PB.EXTRA_KEY: ["not", "a", "mapping"]}, chat_id=GROUP, is_group=True
        ) is None

    def test_the_event_wins_when_the_config_disagrees_about_group(self):
        """A ``group: false`` typo must not tell the persona it is in a DM
        while it is posting to a group."""
        typo = {str(GROUP): {"persona": "grantley", "channel_owner": OWNER,
                             "group": False}}
        prompt = PB.channel_prompt(
            {PB.EXTRA_KEY: typo}, chat_id=GROUP, is_group=True
        )
        assert prompt and "群聊" in prompt and "私聊" not in prompt

    def test_a_bound_channel_carries_the_brevity_reminder(self):
        """The bubble-count fix's upstream half: ask the model for 1-3."""
        prompt = PB.channel_prompt(
            {PB.EXTRA_KEY: CHANNELS}, chat_id=GROUP, is_group=True
        )
        assert PB._BREVITY_REMINDER in prompt

    def test_an_unbound_channel_gets_no_reminder_either(self):
        """The reminder rides the bound frame — it is not a separate, always-on
        addition (that would break the "no binding, no frame" contract the
        tests around this one pin)."""
        assert PB.channel_prompt(
            {PB.EXTRA_KEY: CHANNELS}, chat_id=999999, is_group=True
        ) is None

    def test_it_is_byte_stable_within_a_day(self):
        """``channel_binding``'s cache contract: ephemeral text may vary
        between conversations but must not vary inside one."""
        extra = {PB.EXTRA_KEY: CHANNELS}
        first = PB.channel_prompt(extra, chat_id=GROUP, is_group=True)
        PB.reset_state()
        second = PB.channel_prompt(extra, chat_id=GROUP, is_group=True)
        assert first == second

    def test_the_adapter_extra_overrides_the_plugin_settings(self, monkeypatch):
        monkeypatch.setattr(PB, "_plugin_settings", {"channels": CHANNELS})
        # Present-but-empty is meaningful: "this deployment has no bindings".
        assert PB.channel_prompt(
            {PB.EXTRA_KEY: {}}, chat_id=GROUP, is_group=True
        ) is None

    def test_it_falls_back_to_the_grantley_plugin_settings(self, monkeypatch):
        """``plugins.entries.grantley.settings.channels`` — the path C1 §4.1
        tells the operator to write, and the one that used to be inert."""
        import plugins.grantley as G

        monkeypatch.setattr(G, "load_plugin_config", lambda: {"channels": CHANNELS})
        PB.reset_state()
        prompt = PB.channel_prompt({}, chat_id=GROUP, is_group=True)
        assert prompt and OWNER in prompt

    def test_the_documented_config_path_is_the_one_that_is_read(self):
        """Guards the fallback against a silent rename on either side."""
        import plugins.grantley as G

        assert ("plugins", "entries", "grantley", "settings") in G._CONFIG_PATHS
        assert callable(G.load_plugin_config)

    def test_a_missing_persona_package_degrades_quietly(self, monkeypatch):
        monkeypatch.setattr(PB, "_CANDIDATE_PACKAGES", ("no_such_persona_pkg",))
        PB.reset_state()
        assert PB.bindings_from_extra({PB.EXTRA_KEY: CHANNELS}) == {}
        assert PB.channel_prompt(
            {PB.EXTRA_KEY: CHANNELS}, chat_id=GROUP, is_group=True
        ) is None

    def test_it_calls_the_real_channel_binding_signature(self):
        """Pinned because the module's own docstring describes a call shape
        that does not match its code; this is the code."""
        import inspect

        from plugins.grantley import channel_binding as CB

        sig = inspect.signature(CB.resolve_channel_prompt)
        assert list(sig.parameters) == ["binding", "on", "data_dir"]
        assert sig.parameters["on"].kind is inspect.Parameter.KEYWORD_ONLY
        binding_fields = set(
            inspect.signature(CB.PersonaChannelBinding).parameters
        )
        assert {"persona_id", "chat_id", "channel_owner_id", "is_group"} <= binding_fields


class TestBindingOnBothLanes:

    @pytest.mark.asyncio
    async def test_the_reply_lane_sets_the_channel_prompt(self):
        ad = make_adapter({"group_replies_enabled": True, PB.EXTRA_KEY: CHANNELS})
        event = await ad._build_message_event(group_event("在吗"), "在吗")
        assert event.channel_prompt and OWNER in event.channel_prompt

    @pytest.mark.asyncio
    async def test_the_reply_lane_frames_a_dm_too(self):
        ad = make_adapter({PB.EXTRA_KEY: CHANNELS})
        event = await ad._build_message_event(private_event("在吗"), "在吗")
        assert event.channel_prompt and "私聊" in event.channel_prompt

    @pytest.mark.asyncio
    async def test_an_unbound_channel_leaves_it_unset(self):
        ad = make_adapter({"group_replies_enabled": True, PB.EXTRA_KEY: CHANNELS})
        event = await ad._build_message_event(group_event("hi", gid=424242), "hi")
        assert event.channel_prompt is None

    @pytest.mark.asyncio
    async def test_the_proactive_lane_sets_the_channel_prompt(self):
        handler = FakeHandler()
        ad = make_adapter(
            {"group_replies_enabled": True, PB.EXTRA_KEY: CHANNELS},
            handler=handler,
        )
        await PR.generate(ad, str(GROUP), "说点什么")
        assert handler.events
        assert handler.events[0].channel_prompt
        assert OWNER in handler.events[0].channel_prompt

    @pytest.mark.asyncio
    async def test_both_lanes_frame_the_channel_identically(self):
        """The whole reason this was wired in one change rather than two: a
        persona framed one way when it answers and another way when it speaks
        first is a persona with two characters."""
        handler = FakeHandler()
        ad = make_adapter(
            {"group_replies_enabled": True, PB.EXTRA_KEY: CHANNELS},
            handler=handler,
        )
        inbound = await ad._build_message_event(group_event("在吗"), "在吗")
        await PR.generate(ad, str(GROUP), "说点什么")
        proactive = handler.events[0]
        assert inbound.channel_prompt == proactive.channel_prompt
        assert inbound.channel_prompt is not None

    @pytest.mark.asyncio
    async def test_both_lanes_degrade_together(self):
        handler = FakeHandler()
        ad = make_adapter({"group_replies_enabled": True}, handler=handler)
        inbound = await ad._build_message_event(group_event("在吗"), "在吗")
        await PR.generate(ad, str(GROUP), "说点什么")
        assert inbound.channel_prompt is None
        assert handler.events[0].channel_prompt is None

    @pytest.mark.asyncio
    async def test_a_broken_resolver_costs_neither_lane_its_message(self,
                                                                   monkeypatch):
        """"A persona is decorative; chat must keep working when it breaks.\""""
        def _boom(*_a, **_kw):
            raise RuntimeError("seed pack on fire")

        monkeypatch.setattr(PB, "bindings_from_extra", _boom)
        handler = FakeHandler()
        ad = make_adapter(
            {"group_replies_enabled": True, PB.EXTRA_KEY: CHANNELS},
            handler=handler,
        )
        with pytest.raises(RuntimeError):
            PB.bindings_from_extra({})          # the fake really does raise
        # …and neither lane propagates it.
        inbound = await ad._build_message_event(group_event("在吗"), "在吗")
        assert inbound.channel_prompt is None
        assert await PR.generate(ad, str(GROUP), "说点什么") == handler.reply
