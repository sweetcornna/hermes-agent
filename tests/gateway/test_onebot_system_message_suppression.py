"""Hiding hermes' own system messages on the QQ channel.

A real person, in a real private chat, got ``💾 Self-improvement review: User
profile updated`` in the middle of a conversation.  That is hermes narrating
its own bookkeeping, and on QQ there is nowhere for it to land except as a chat
bubble next to everything the persona said — no thread to fold it into, no
collapsed "system" lane the way Discord has.  The mask slips.

The classification is not made here.  ``gateway/run.py``'s
``_non_conversational_metadata`` already stamps ``non_conversational: True`` on
every lifecycle/status send, at the emitter, by the code that knows what it is
sending; that marker was Discord-only and now covers this platform too.  The
adapter only reads the flag.  This matters: matching on ``💾``/``⚠️`` text was
the obvious alternative and it is the one that eventually eats a real persona
reply, because the persona writes emoji.  A persona reply is never *marked*.

What these tests pin:

* a marked message is dropped when the switch is on, and the refusal is a
  ``SendResult`` with ``error_kind == "unknown"`` — never ``forbidden`` /
  ``not_found``, which would let the delivery layer file the chat as
  permanently dead and outlive a display preference;
* the switch off means business as usual;
* an unmarked message — every persona reply — is never touched, whatever it
  says and whichever emoji it opens with;
* the two carve-outs still go out: the update prompt (``hermes update``
  *blocks* on the answer) and the session-database alarm (data is not being
  persisted);
* an unmarked dangerous-command approval request reaches the adapter like an
  ordinary user-visible message, so it can never reach this gate;
* a dropped message is written to the log, not swallowed;
* other platforms are unchanged: Telegram's metadata gains no new key.

Everything runs against a fake client — no sockets, no NapCat, no model call.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.dead_targets import DeadTargetRegistry
from gateway.platforms.base import classify_send_error

from plugins.platforms.onebot import adapter as A
from plugins.platforms.onebot import protocol as P


GROUP = 183287894
DM_PEER = 536132102

#: The exact string the user was shown, from ``agent/background_review.py``.
REVIEW_NOTICE = "💾 Self-improvement review: User profile updated"

MARKED: Dict[str, Any] = {"non_conversational": True}


class FakeClient:
    """Records every outbound action; never touches a socket."""

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


def make_adapter(
    extra: Optional[Dict[str, Any]] = None,
    *,
    client: Optional[FakeClient] = None,
) -> A.OneBotAdapter:
    base: Dict[str, Any] = {"ws_url": "ws://127.0.0.1:3001"}
    base.update(extra or {})
    ad = A.OneBotAdapter(PlatformConfig(enabled=True, extra=base))
    ad._client = client or FakeClient()
    ad._running = True
    ad._semaphore = asyncio.Semaphore(4)
    ad._account_online = True
    ad._group_names[GROUP] = "测试群"
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
# The switch
# ---------------------------------------------------------------------------


class TestTheSwitch:
    def test_it_is_on_unless_someone_turns_it_off(self):
        """QQ is the persona channel; a status bubble is a defect there."""
        assert make_adapter().suppress_system_messages is True
        assert A.system_messages_suppressed(make_adapter()) is True

    def test_env_var_turns_it_off(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_SUPPRESS_SYSTEM_MESSAGES", "false")
        assert make_adapter().suppress_system_messages is False

    def test_yaml_key_turns_it_off(self):
        ad = make_adapter({"suppress_system_messages": False})
        assert ad.suppress_system_messages is False
        assert A.system_messages_suppressed(ad) is False

    def test_it_is_registered_as_a_private_yaml_key(self):
        """Otherwise ``platforms.onebot.suppress_system_messages`` in the YAML
        would never reach ``PlatformConfig.extra``."""
        assert "suppress_system_messages" in A._PRIVATE_YAML_KEYS
        merged = A._apply_yaml_config({}, {"suppress_system_messages": False})
        assert merged["suppress_system_messages"] is False

    def test_flipping_the_live_config_hot_applies(self):
        """A config reconcile mutates ``config.extra`` in place; the gate must
        follow it without a restart, like the emergency mute does."""
        ad = make_adapter()
        assert A.system_messages_suppressed(ad) is True
        ad.config.extra["suppress_system_messages"] = "false"
        assert A.system_messages_suppressed(ad) is False


# ---------------------------------------------------------------------------
# Dropping
# ---------------------------------------------------------------------------


class TestMarkedMessagesAreDropped:
    @pytest.mark.asyncio
    async def test_the_review_notice_never_reaches_the_wire(self):
        client = FakeClient()
        ad = make_adapter(client=client)
        res = await ad.send(str(DM_PEER), REVIEW_NOTICE, metadata=dict(MARKED))
        assert res.success is False
        assert client.actions == []

    @pytest.mark.asyncio
    async def test_the_refusal_is_never_a_dead_target(self):
        """``forbidden``/``not_found`` are ``_DEAD_ERROR_KINDS``; either would
        turn a display preference into a permanently unreachable chat."""
        ad = make_adapter()
        res = await ad.send(str(DM_PEER), REVIEW_NOTICE, metadata=dict(MARKED))
        assert res.error_kind == "unknown"
        assert res.error_kind not in ("forbidden", "not_found")
        assert res.retryable is False

    @pytest.mark.asyncio
    async def test_the_real_dead_target_registry_agrees(self, tmp_path):
        """Not a claim about the constant — asked of the real registry."""
        ad = make_adapter()
        res = await ad.send(str(DM_PEER), REVIEW_NOTICE, metadata=dict(MARKED))
        registry = DeadTargetRegistry(path=tmp_path / "dead.json")
        assert registry.is_dead_error_kind(res.error_kind) is False
        assert registry.is_dead("onebot", str(DM_PEER)) is False

    @pytest.mark.asyncio
    async def test_the_wording_cannot_be_reread_as_a_platform_failure(self):
        """``classify_send_error`` must not find a retry/dead hook in the text."""
        ad = make_adapter()
        res = await ad.send(str(DM_PEER), REVIEW_NOTICE, metadata=dict(MARKED))
        assert classify_send_error(res.error) not in ("forbidden", "not_found")

    @pytest.mark.asyncio
    async def test_the_result_is_distinguishable_from_a_send_failure(self):
        ad = make_adapter()
        res = await ad.send(str(DM_PEER), REVIEW_NOTICE, metadata=dict(MARKED))
        assert A.is_suppressed_send_result(res) is True
        assert res.raw_response[A.SUPPRESSED_MARKER] is True
        # ...and not confused with the two sibling local refusals.
        assert A.is_muted_send_result(res) is False
        assert A.is_policy_blocked_send_result(res) is False

    @pytest.mark.asyncio
    async def test_groups_are_covered_too(self):
        client = FakeClient()
        ad = make_adapter({"group_replies_enabled": True}, client=client)
        res = await ad.send(f"g{GROUP}", REVIEW_NOTICE, metadata=dict(MARKED))
        assert res.success is False
        assert A.is_suppressed_send_result(res) is True
        assert client.actions == []

    @pytest.mark.asyncio
    async def test_a_dropped_post_is_not_recorded_as_something_we_said(self):
        """Otherwise the proactive lane reads its own un-sent status bubble as
        context, and as "we spoke last", which suppresses the next real post."""
        ad = make_adapter({"group_replies_enabled": True})
        await ad.send(f"g{GROUP}", REVIEW_NOTICE, metadata=dict(MARKED))
        assert A.recent_group_messages("default", GROUP) == []

    @pytest.mark.asyncio
    async def test_a_long_status_message_is_dropped_whole(self):
        """Not "the gate fires and the forward-card path leaks it anyway"."""
        client = FakeClient()
        ad = make_adapter(client=client)
        res = await ad.send(str(DM_PEER), "z" * 9000, metadata=dict(MARKED))
        assert res.success is False
        assert client.actions == []


class TestTheDropIsLogged:
    @pytest.mark.asyncio
    async def test_it_is_logged_at_info_without_replaying_the_body(self, caplog):
        ad = make_adapter()
        sensitive_body = f"{REVIEW_NOTICE} token=not-for-log"
        with caplog.at_level(logging.INFO, logger=A.logger.name):
            await ad.send(str(DM_PEER), sensitive_body, metadata=dict(MARKED))
        records = [r for r in caplog.records if A.SUPPRESSED_REASON in r.getMessage()]
        assert len(records) == 1
        record = records[0]
        assert record.levelno == logging.INFO
        message = record.getMessage()
        assert str(DM_PEER) in message
        assert REVIEW_NOTICE not in message
        assert "not-for-log" not in message

    @pytest.mark.asyncio
    async def test_nothing_is_swallowed_silently(self, caplog):
        """Every dropped message leaves exactly one trace, one per send."""
        ad = make_adapter()
        with caplog.at_level(logging.INFO, logger=A.logger.name):
            await ad.send(str(DM_PEER), "⏳ Working — 3 min", metadata=dict(MARKED))
            await ad.send(
                str(DM_PEER), "♻ Gateway restarted successfully.", metadata=dict(MARKED)
            )
        assert (
            len([r for r in caplog.records if A.SUPPRESSED_REASON in r.getMessage()])
            == 2
        )


# ---------------------------------------------------------------------------
# Not dropping
# ---------------------------------------------------------------------------


class TestTheSwitchOffChangesNothing:
    @pytest.mark.asyncio
    async def test_a_marked_message_goes_out_normally(self):
        client = FakeClient()
        ad = make_adapter({"suppress_system_messages": False}, client=client)
        res = await ad.send(str(DM_PEER), REVIEW_NOTICE, metadata=dict(MARKED))
        assert res.success is True
        assert len(client.actions) == 1
        assert isinstance(client.actions[0], P.SendPrivateMsg)

    @pytest.mark.asyncio
    async def test_nothing_is_logged_as_dropped(self, caplog):
        ad = make_adapter({"suppress_system_messages": False})
        with caplog.at_level(logging.INFO, logger=A.logger.name):
            await ad.send(str(DM_PEER), REVIEW_NOTICE, metadata=dict(MARKED))
        assert not [r for r in caplog.records if A.SUPPRESSED_REASON in r.getMessage()]


class TestConversationIsNeverTouched:
    """The failure mode of a text-shaped filter: eating a persona reply."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "text",
        [
            "在的，怎么了？",
            "💾 我把刚才那段存下来了",  # persona, same emoji as the notice
            "⚠️ 那个命令我不太敢跑",  # persona, same emoji as the alarms
            "⏳ 等我五分钟",  # persona, same emoji as the heartbeat
            "💾 Self-improvement review: User profile updated",  # verbatim!
        ],
    )
    async def test_an_unmarked_message_always_goes_out(self, text):
        client = FakeClient()
        ad = make_adapter(client=client)
        res = await ad.send(str(DM_PEER), text)
        assert res.success is True
        assert len(client.actions) == 1

    @pytest.mark.asyncio
    async def test_an_unrelated_metadata_key_is_not_a_marker(self):
        client = FakeClient()
        ad = make_adapter(client=client)
        res = await ad.send(str(DM_PEER), "嗯", metadata={"onebot_at_user_id": "555"})
        assert res.success is True
        assert len(client.actions) == 1

    @pytest.mark.asyncio
    async def test_a_falsey_marker_is_not_a_marker(self):
        client = FakeClient()
        ad = make_adapter(client=client)
        res = await ad.send(str(DM_PEER), "嗯", metadata={"non_conversational": False})
        assert res.success is True
        assert len(client.actions) == 1


class TestTheCarveOuts:
    """Marked, but delivered anyway — silence would cost more than the bubble."""

    UPDATE_PROMPT = (
        "⚕ **Update needs your input:**\n\n"
        "Overwrite local changes? (default: no)\n\n"
        "Reply `/approve` (yes) or `/deny` (no), or type your answer directly."
    )
    DB_UNAVAILABLE = (
        "⚠️ Session database unavailable — messages may not be persisted. "
        "Run `hermes doctor` for diagnostics."
    )
    DB_CORRUPT = (
        "⚠️ Session database corruption detected. Messages may not be "
        "persisted. Recovery options:\n1. Run `hermes doctor --fix`"
    )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("attr", ["UPDATE_PROMPT", "DB_UNAVAILABLE", "DB_CORRUPT"])
    async def test_it_is_delivered_with_the_switch_on(self, attr):
        client = FakeClient()
        ad = make_adapter(client=client)
        res = await ad.send(str(DM_PEER), getattr(self, attr), metadata=dict(MARKED))
        assert res.success is True
        assert client.actions

    @pytest.mark.asyncio
    async def test_the_update_prompt_is_the_only_form_qq_can_receive(self):
        """``_watch_update_progress`` prefers ``send_update_prompt`` and only
        falls back to this text when the adapter class has no such method.
        This adapter has none, so the fallback is the whole channel — dropping
        it strands ``hermes update`` on a question nobody was shown."""
        assert getattr(A.OneBotAdapter, "send_update_prompt", None) is None

    @pytest.mark.asyncio
    async def test_the_carve_out_is_not_logged_as_dropped(self, caplog):
        ad = make_adapter()
        with caplog.at_level(logging.INFO, logger=A.logger.name):
            await ad.send(str(DM_PEER), self.UPDATE_PROMPT, metadata=dict(MARKED))
        assert not [r for r in caplog.records if A.SUPPRESSED_REASON in r.getMessage()]

    def test_the_carve_out_does_not_widen_into_a_text_filter(self):
        """The patterns are anchored; a status bubble that merely mentions
        either subject stays hidden."""
        assert (
            A.is_suppressible_system_message(
                MARKED, "💾 Self-improvement review: session database notes updated"
            )
            is True
        )
        assert (
            A.is_suppressible_system_message(MARKED, "✅ Hermes update finished.")
            is True
        )


class TestApprovalRequestsAreNotEvenMarked:
    """The category whose loss would actually block the operator.

    ``_approval_notify_sync`` passes ``ctx._status_thread_metadata`` straight to
    ``send`` and never routes it through ``_non_conversational_metadata``, so an
    approval request carries no marker and this gate cannot see it.  That is a
    structural guarantee rather than a carve-out, and it is worth pinning here:
    if someone later wraps that call site, this test fails and the carve-out
    table gets a third entry instead of the operator getting a stuck agent.
    """

    @pytest.mark.asyncio
    async def test_an_unmarked_approval_request_goes_out(self):
        client = FakeClient()
        ad = make_adapter(client=client)
        res = await ad.send(
            str(DM_PEER),
            "⚠️ **Dangerous command requires approval:**\n```\nrm -rf /tmp/x\n```",
            metadata={},
        )
        assert res.success is True
        assert client.actions


class TestTheRoutingLogicIsUntouched:
    """The gate sits ABOVE the card/bubble split, and must stay invisible to it.

    ``send()`` routes the whole reply once: either LENGTH over
    ``forward_threshold`` or bubble COUNT over ``max_bubbles_per_reply`` folds
    it into a single merged-forward card. The suppression check runs before
    ``parse_chat_id``, so it cannot reorder, re-measure, or short-circuit any
    of that; these tests pin that an unmarked message still takes its normal
    route and that a marked one is stopped before the router ever runs.
    """

    @pytest.mark.asyncio
    async def test_a_long_unmarked_reply_still_becomes_one_card(self):
        client = FakeClient()
        ad = make_adapter(client=client)
        res = await ad.send(str(DM_PEER), "长" * 1500)
        assert res.success is True
        assert len(client.actions) == 1
        assert isinstance(client.actions[0], P.SendPrivateForwardMsg)

    @pytest.mark.asyncio
    async def test_too_many_unmarked_bubbles_still_become_one_card(self):
        client = FakeClient()
        ad = make_adapter(client=client)
        body = "[MSG_BREAK]".join(["一", "二", "三", "四", "五"])
        res = await ad.send(str(DM_PEER), body)
        assert res.success is True
        assert len(client.actions) == 1
        assert isinstance(client.actions[0], P.SendPrivateForwardMsg)

    @pytest.mark.asyncio
    async def test_short_and_few_unmarked_bubbles_still_go_out_as_bubbles(self):
        client = FakeClient()
        ad = make_adapter(client=client)
        res = await ad.send(str(DM_PEER), "一[MSG_BREAK]二[MSG_BREAK]三")
        assert res.success is True
        assert len(client.actions) == 3
        assert all(isinstance(a, P.SendPrivateMsg) for a in client.actions)

    @pytest.mark.asyncio
    async def test_the_forward_threshold_is_still_read_from_config(self):
        """``forward_threshold: 0`` disables folding; the same 1500-char body
        that becomes a card above must go out as plain bubbles here.  A
        regression would mean the gate shadowed the routing config."""
        client = FakeClient()
        ad = make_adapter({"forward_threshold": 0}, client=client)
        res = await ad.send(str(DM_PEER), "长" * 1500)
        assert res.success is True
        assert client.actions
        assert all(isinstance(a, P.SendPrivateMsg) for a in client.actions)
        assert not any(isinstance(a, P.SendPrivateForwardMsg) for a in client.actions)

    @pytest.mark.asyncio
    async def test_a_marked_message_never_reaches_the_router_at_all(self):
        """Not "the card path also happens to refuse it" — it stops earlier."""
        client = FakeClient()
        ad = make_adapter(client=client)
        calls: List[Any] = []

        async def _spy(*args, **kwargs):
            calls.append((args, kwargs))
            return True, None

        ad._deliver_forward = _spy
        res = await ad.send(str(DM_PEER), "长" * 1500, metadata=dict(MARKED))
        assert res.success is False
        assert calls == []
        assert client.actions == []

    @pytest.mark.asyncio
    async def test_the_gate_runs_before_the_mute_so_it_raises_no_alarm(self, caplog):
        """A hidden heartbeat aimed at a muted group must not log a mute
        warning — the operator would read it as the bot trying to talk."""
        ad = make_adapter()  # group_replies_enabled defaults False
        with caplog.at_level(logging.DEBUG, logger=A.logger.name):
            res = await ad.send(f"g{GROUP}", REVIEW_NOTICE, metadata=dict(MARKED))
        assert A.is_suppressed_send_result(res) is True
        assert not [r for r in caplog.records if A.MUTED_REASON in r.getMessage()]

    @pytest.mark.asyncio
    async def test_media_paths_keep_their_own_mute_gate(self):
        """``_send_attachment`` does not go through ``send()``; the new gate
        must not have been mistaken for a replacement of its mute check."""
        ad = make_adapter()  # muted
        res = await ad._send_attachment(f"g{GROUP}", "/tmp/nope.png", None)
        assert A.is_muted_send_result(res) is True


# ---------------------------------------------------------------------------
# The gateway side of the one-line change
# ---------------------------------------------------------------------------


class TestOtherPlatformsAreUnchanged:
    """The marker is stamped for exactly two platforms, and no others."""

    def test_onebot_is_marked(self):
        from gateway.run import _non_conversational_metadata

        # ``Platform("onebot")`` rather than ``Platform.ONEBOT``: plugin
        # platforms are dynamic pseudo-members that ``_missing_`` only
        # materialises on first lookup, so the attribute form exists only
        # after someone has already asked for it.  The lookup form is what
        # production code uses and is order-independent here.
        assert _non_conversational_metadata(
            {"thread_id": "7"}, platform=Platform("onebot")
        ) == {"thread_id": "7", "non_conversational": True}
        assert _non_conversational_metadata(None, platform="onebot") == {
            "non_conversational": True
        }

    def test_discord_still_is(self):
        from gateway.run import _non_conversational_metadata

        assert _non_conversational_metadata(
            {"thread_id": "7"}, platform=Platform.DISCORD
        ) == {"thread_id": "7", "non_conversational": True}

    @pytest.mark.parametrize(
        "platform",
        [
            Platform.TELEGRAM,
            Platform.SLACK,
            Platform.MATRIX,
            Platform.EMAIL,
            Platform.QQBOT,
            Platform.RELAY,
            Platform.LOCAL,
        ],
    )
    def test_every_other_platform_gets_its_metadata_back_untouched(self, platform):
        from gateway.run import _non_conversational_metadata

        original = {"thread_id": "7"}
        result = _non_conversational_metadata(original, platform=platform)
        assert result is original
        assert "non_conversational" not in result
        assert _non_conversational_metadata(None, platform=platform) is None

    @pytest.mark.asyncio
    async def test_a_telegram_adapter_sees_no_new_key(self):
        """The end-to-end form of the above: what actually reaches ``send``."""
        from gateway.run import _non_conversational_metadata

        seen: List[Any] = []

        class FakeTelegramAdapter:
            async def send(self, chat_id, content, metadata=None):
                seen.append(metadata)
                return None

        meta = _non_conversational_metadata(
            {"thread_id": "777"}, platform=Platform.TELEGRAM
        )
        await FakeTelegramAdapter().send("parent-42", "♻️ Gateway online", metadata=meta)
        assert seen == [{"thread_id": "777"}]


# ---------------------------------------------------------------------------
# The actual leak: TurnRunner._status_callback_sync, the real emitter behind
# every agent._emit_status("lifecycle"/"warn", ...) call — memory-recall
# indicators (agent/memory_manager.py's describe_recall()), compression/retry
# chatter, idle status, and friends.  A real QQ chat once got a bare
# "- recalled 1 memory" bubble mid-persona because THIS call site built
# ``ctx._status_thread_metadata`` straight from the thread lookup and never
# ran it through ``_non_conversational_metadata`` before handing it to
# ``_send_or_update_status_coro`` — unlike the sibling ``_progress_metadata``
# path, which already did. The fix wraps the metadata at the emitter, not the
# OneBot gate above (which was already correct and already tested).
# ---------------------------------------------------------------------------


def _status_callback_runner(adapter, platform):
    """A TurnRunner wired to ``adapter`` through the real _status_callback_sync.

    ``_loop_for_step`` is a plain sentinel object: the test's
    ``safe_schedule_threadsafe`` monkeypatch never touches it, only records
    the coroutine it was asked to schedule, so the test can await that
    coroutine directly instead of racing a background event loop.
    """
    from gateway.run import TurnRunner
    from gateway.session import SessionSource
    from gateway.turn_context import TurnContext

    class _StubGatewayRunner:
        def _adapter_for_source(self, source):
            return adapter

    ctx = TurnContext(
        source=SessionSource(
            platform=platform, chat_id=str(DM_PEER), chat_type="private"
        ),
        _run_still_current=lambda: True,
        _status_adapter=adapter,
        _status_chat_id=str(DM_PEER),
        _status_thread_metadata=None,
        _loop_for_step=object(),
    )
    return TurnRunner(_StubGatewayRunner(), ctx)


class TestTheStatusCallbackEmitterItself:
    """gateway.run.TurnRunner._status_callback_sync, exercised for real."""

    def _capture_scheduled_coro(self, monkeypatch):
        import gateway.run as gateway_run

        captured: Dict[str, Any] = {}

        def _fake_schedule(coro, loop, **kwargs):
            captured["coro"] = coro
            return None  # _status_callback_sync early-returns; we await ourselves

        monkeypatch.setattr(gateway_run, "safe_schedule_threadsafe", _fake_schedule)
        return captured

    def test_recall_indicator_is_suppressed_end_to_end_on_onebot(self, monkeypatch):
        """The exact regression: describe_recall()'s output, through the real
        emitter, into the real OneBot adapter, must never reach the wire."""
        client = FakeClient()
        ad = make_adapter(client=client)
        captured = self._capture_scheduled_coro(monkeypatch)

        runner = _status_callback_runner(ad, Platform("onebot"))
        runner._status_callback_sync("lifecycle", "🧠 Hindsight — recalled 1 memory")

        assert "coro" in captured
        result = asyncio.run(captured["coro"])
        assert A.is_suppressed_send_result(result) is True
        assert client.actions == []

    def test_warn_status_is_also_suppressed_on_onebot(self, monkeypatch):
        """_emit_warning routes through the same event_type-agnostic emitter."""
        client = FakeClient()
        ad = make_adapter(client=client)
        captured = self._capture_scheduled_coro(monkeypatch)

        runner = _status_callback_runner(ad, Platform("onebot"))
        runner._status_callback_sync(
            "warn",
            "⚠ Memory provider degraded, continuing without recall context this turn.",
        )

        result = asyncio.run(captured["coro"])
        assert A.is_suppressed_send_result(result) is True
        assert client.actions == []

    def test_recall_indicator_still_reaches_discord(self, monkeypatch):
        """Discord shows this line by design (docstring: 'let the user SEE
        memory was used'). The marker only steers history reconstruction —
        plugins/platforms/discord/adapter.py's own suite
        (test_discord_send_does_not_cache_nonconversational_status_as_history_boundary)
        pins that a non_conversational send still goes out."""
        captured = self._capture_scheduled_coro(monkeypatch)

        sent: List[Dict[str, Any]] = []

        class FakeDiscordAdapter:
            async def send(self, chat_id, content, metadata=None):
                sent.append({
                    "chat_id": chat_id,
                    "content": content,
                    "metadata": metadata,
                })
                return SimpleNamespaceResult(success=True)

        ad = FakeDiscordAdapter()
        runner = _status_callback_runner(ad, Platform.DISCORD)
        runner._status_callback_sync("lifecycle", "🧠 Hindsight — recalled 1 memory")

        asyncio.run(captured["coro"])
        assert len(sent) == 1
        assert sent[0]["content"] == "🧠 Hindsight — recalled 1 memory"
        assert sent[0]["metadata"] == {"non_conversational": True}

    def test_recall_indicator_still_reaches_telegram_untouched(self, monkeypatch):
        """Telegram is not in the (discord, onebot) marking set at all — the
        send must go out with metadata unchanged, exactly as before this fix."""
        captured = self._capture_scheduled_coro(monkeypatch)

        sent: List[Dict[str, Any]] = []

        class FakeTelegramAdapter:
            async def send(self, chat_id, content, metadata=None):
                sent.append({
                    "chat_id": chat_id,
                    "content": content,
                    "metadata": metadata,
                })
                return SimpleNamespaceResult(success=True)

        ad = FakeTelegramAdapter()
        runner = _status_callback_runner(ad, Platform.TELEGRAM)
        runner._status_callback_sync("lifecycle", "🧠 Hindsight — recalled 1 memory")

        asyncio.run(captured["coro"])
        assert len(sent) == 1
        assert sent[0]["content"] == "🧠 Hindsight — recalled 1 memory"
        assert sent[0]["metadata"] is None


class SimpleNamespaceResult:
    """Minimal stand-in for gateway.platforms.base.SendResult's success flag."""

    def __init__(self, success: bool) -> None:
        self.success = success
