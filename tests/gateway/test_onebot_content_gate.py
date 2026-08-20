"""The two outbound gates on the OneBot send path: markdown, then policy.

``OneBotAdapter.send`` is the single choke point both QQ lanes reach the wire
through — the reactive reply lane and the proactive lane (``proactive.py``
calls ``adapter.send`` rather than talking to the client itself).  Two things
the source deployment does there were missing from this port:

* **markdown normalization.**  QQ renders no markdown, so an unstripped reply
  shows a literal ``**bold**`` / ``### heading`` / ```` ``` ```` fence to the
  person reading it.  The shared ``gateway.platforms.helpers.strip_markdown``
  already existed and the *other* QQ platform (``gateway/platforms/qqbot``)
  already called it; this adapter did not.

* **the Tencent content policy.**  ``plugins/qzone/policy.py`` is a byte-exact
  port of the classifier that exists to keep the QQ account from being frozen
  by Tencent's automated risk control.  The QZone tools call it at four sites;
  the OneBot outbound path called it at none.

What these tests pin:

* markdown markers do not reach the wire, on groups and DMs alike;
* benign text is untouched and still goes out;
* a refused message reaches the wire **nowhere**, and the refusal is shaped so
  that neither the retry ladder nor the dead-target registry can re-read it as
  a platform failure — asserted by calling the real ``classify_send_error``,
  not by eyeballing the wording;
* the gate **fails closed**: a classifier that explodes blocks, never passes;
* the gate is **not** group-limited — the freeze risk is account-level;
* the policy sees the *post-strip* text, and sees it **whole**, once, before
  any bubble/chunk splitting;
* the mute short-circuits before the classifier runs (this box has two cores);
* a policy refusal degrades the proactive lane into "skip this beat + warn"
  without spending any of the shared speech budget.

Everything runs against a fake client — no sockets, no NapCat, no model call;
no path here can reach a real QQ group.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

import pytest

from gateway.config import PlatformConfig
from gateway.dead_targets import DeadTargetRegistry
from gateway.platforms.base import classify_send_error
from gateway.platforms.helpers import strip_markdown

from plugins.platforms.onebot import adapter as A
from plugins.platforms.onebot import proactive as PR
from plugins.qzone import policy as POLICY


GROUP = 183287894
DM_PEER = 536132102

#: A phrase the ported rule table blocks (``fraud.account-abuse``).  Shared by
#: the QZone policy tests, so if the rule table is ever revised this constant
#: fails loudly in both suites rather than silently testing nothing.
BLOCKED_TEXT = "QQ解冻教程"
BENIGN_TEXT = "今天天气不错，一起去吃饭吗"


def test_the_blocked_fixture_is_actually_blocked():
    """Guard the guard: every refusal test below is meaningless if this drifts."""
    assert (
        POLICY.moderate_text(BLOCKED_TEXT, POLICY.resolve_config(None)).decision.allowed
        is False
    )
    assert (
        POLICY.moderate_text(BENIGN_TEXT, POLICY.resolve_config(None)).decision.allowed
        is True
    )


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


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
    base: Dict[str, Any] = {
        "ws_url": "ws://127.0.0.1:3001",
        # Unmuted, so the mute gate never masks what these tests are measuring.
        "group_replies_enabled": True,
    }
    base.update(extra or {})
    ad = A.OneBotAdapter(PlatformConfig(enabled=True, extra=base))
    ad._client = client or FakeClient()
    ad._running = True
    ad._semaphore = asyncio.Semaphore(4)
    ad._account_online = True
    ad._group_names[GROUP] = "测试群"
    ad.router.group_replies_enabled = True
    return ad


def wire_text(ad: A.OneBotAdapter) -> str:
    """Every text segment the adapter actually put on the wire, concatenated."""
    out: List[str] = []
    for action in ad._client.actions:
        for seg in getattr(action, "message", []) or []:
            text = getattr(seg, "text", None)
            if text:
                out.append(text)
    return "".join(out)


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
# 1. Markdown normalization
# ---------------------------------------------------------------------------


class TestMarkdownIsStripped:
    """QQ renders none of it, so none of it may reach the wire."""

    @pytest.mark.asyncio
    async def test_bold_markers_are_gone(self):
        ad = make_adapter()
        res = await ad.send(f"g{GROUP}", "这个 **很重要** 记住了")
        assert res.success is True
        sent = wire_text(ad)
        assert "**" not in sent
        assert "很重要" in sent

    @pytest.mark.asyncio
    async def test_heading_markers_are_gone(self):
        ad = make_adapter()
        res = await ad.send(f"g{GROUP}", "### 标题\n正文在这里")
        assert res.success is True
        sent = wire_text(ad)
        assert "###" not in sent
        assert "标题" in sent and "正文在这里" in sent

    @pytest.mark.asyncio
    async def test_every_heading_level_is_gone(self):
        ad = make_adapter()
        await ad.send(f"g{GROUP}", "# 一级\n## 二级\n### 三级\n###### 六级")
        sent = wire_text(ad)
        assert "#" not in sent
        for level in ("一级", "二级", "三级", "六级"):
            assert level in sent

    @pytest.mark.asyncio
    async def test_code_fences_are_gone(self):
        ad = make_adapter()
        res = await ad.send(f"g{GROUP}", "看这个:\n```python\nprint(1)\n```")
        assert res.success is True
        sent = wire_text(ad)
        assert "```" not in sent
        assert "print(1)" in sent

    @pytest.mark.asyncio
    async def test_inline_code_backticks_are_gone(self):
        ad = make_adapter()
        await ad.send(f"g{GROUP}", "运行 `pip install hermes` 就行")
        sent = wire_text(ad)
        assert "`" not in sent
        assert "pip install hermes" in sent

    @pytest.mark.asyncio
    async def test_link_syntax_collapses_to_its_label(self):
        ad = make_adapter()
        await ad.send(f"g{GROUP}", "见 [文档](https://example.com/docs)")
        sent = wire_text(ad)
        assert "](" not in sent and "https://example.com/docs" not in sent
        assert "文档" in sent

    @pytest.mark.asyncio
    async def test_a_dm_is_stripped_too(self):
        """The gate is on the shared path, not on the group branch."""
        ad = make_adapter()
        res = await ad.send(str(DM_PEER), "**加粗** 和 `代码`")
        assert res.success is True
        sent = wire_text(ad)
        assert "**" not in sent and "`" not in sent

    @pytest.mark.asyncio
    async def test_the_stripped_text_is_what_gets_buffered_as_ours(self):
        """The context buffer feeds the next proactive prompt.  Buffering the
        raw markdown would teach the persona to keep writing markdown."""
        ad = make_adapter()
        await ad.send(f"g{GROUP}", "**加粗**")
        buffered = A.recent_group_messages(ad.instance_id, GROUP)
        assert buffered, "the bot's own post should be in the buffer"
        assert "**" not in buffered[-1][2]

    @pytest.mark.asyncio
    async def test_plain_text_survives_untouched(self):
        ad = make_adapter()
        await ad.send(f"g{GROUP}", BENIGN_TEXT)
        assert BENIGN_TEXT in wire_text(ad)


class TestMarkdownStrippingLimits:
    """What the shared helper does NOT strip, pinned so it is not a surprise.

    ``gateway.platforms.helpers.strip_markdown`` has regexes for emphasis,
    fences, inline code, headings and links — and none for list bullets,
    blockquotes or ordered-list numbering.  These tests document the real
    behaviour rather than asserting a coverage the helper does not have.
    Widening it would change eight other callers and lives in ``gateway/``,
    so it is deliberately not done here.  See the report / follow-up.
    """

    @pytest.mark.asyncio
    async def test_dash_bullets_reach_the_wire_verbatim(self):
        ad = make_adapter()
        await ad.send(f"g{GROUP}", "- 第一项\n- 第二项")
        sent = wire_text(ad)
        assert "- 第一项" in sent, (
            "KNOWN GAP: strip_markdown has no list-bullet rule, so '- ' "
            "survives into QQ. If this assertion starts failing, the helper "
            "grew a bullet rule and this test should become the positive one."
        )

    def test_the_helper_itself_is_the_reason_why(self):
        """Pin it at the helper level too, so the gap is attributed correctly
        (the adapter wires the helper faithfully; the helper is the limit)."""
        assert strip_markdown("- 第一项") == "- 第一项"
        assert strip_markdown("> 引用") == "> 引用"
        assert strip_markdown("1. 有序") == "1. 有序"


# ---------------------------------------------------------------------------
# 2 + 3. The content gate: benign passes, blocked is refused
# ---------------------------------------------------------------------------


class TestBenignTextIsNotHarmed:
    @pytest.mark.asyncio
    async def test_ordinary_chatter_goes_out(self):
        ad = make_adapter()
        res = await ad.send(f"g{GROUP}", BENIGN_TEXT)
        assert res.success is True
        assert res.error is None
        assert ad._client.actions, "a benign message must reach the wire"

    @pytest.mark.asyncio
    async def test_a_benign_dm_goes_out(self):
        ad = make_adapter()
        res = await ad.send(str(DM_PEER), BENIGN_TEXT)
        assert res.success is True
        assert ad._client.actions

    @pytest.mark.asyncio
    async def test_a_benign_multi_bubble_reply_goes_out_whole(self):
        ad = make_adapter()
        res = await ad.send(f"g{GROUP}", f"{BENIGN_TEXT}[MSG_BREAK]另外你到了吗")
        assert res.success is True
        sent = wire_text(ad)
        assert "另外你到了吗" in sent
        assert "[MSG_BREAK]" not in sent


class TestBlockedTextIsRefused:
    @pytest.mark.asyncio
    async def test_nothing_reaches_the_wire(self):
        ad = make_adapter()
        res = await ad.send(f"g{GROUP}", BLOCKED_TEXT)
        assert res.success is False
        assert ad._client.actions == [], "a refused message must not be sent"

    @pytest.mark.asyncio
    async def test_it_is_a_failure_not_a_silent_pretend_success(self):
        res = await make_adapter().send(f"g{GROUP}", BLOCKED_TEXT)
        assert res.success is False
        assert res.message_id is None

    @pytest.mark.asyncio
    async def test_it_never_raises(self):
        res = await make_adapter().send(f"g{GROUP}", BLOCKED_TEXT)
        assert res is not None

    @pytest.mark.asyncio
    async def test_error_kind_is_unknown(self):
        res = await make_adapter().send(f"g{GROUP}", BLOCKED_TEXT)
        assert res.error_kind == "unknown"

    @pytest.mark.asyncio
    async def test_it_never_marks_the_target_permanently_dead(self):
        """``forbidden``/``not_found`` are the two kinds ``gateway.dead_targets``
        treats as permanent.  A refused *sentence* must not kill the *chat*."""
        res = await make_adapter().send(f"g{GROUP}", BLOCKED_TEXT)
        assert DeadTargetRegistry.is_dead_error_kind(res.error_kind) is False

    @pytest.mark.asyncio
    async def test_the_wording_cannot_be_reclassified(self):
        """The delivery layer also re-classifies from the error *text*.  This
        calls the real classifier rather than trusting the phrasing."""
        res = await make_adapter().send(f"g{GROUP}", BLOCKED_TEXT)
        assert classify_send_error(None, res.error or "") == "unknown"

    @pytest.mark.asyncio
    async def test_the_wording_dodges_every_substring_the_classifier_matches(self):
        """Belt and braces: enumerate the table so a future reword that happens
        to collide is caught here and not in production."""
        res = await make_adapter().send(f"g{GROUP}", BLOCKED_TEXT)
        blob = (res.error or "").lower()
        collisions = [
            needle
            for needle in (
                "message_too_long",
                "too long",
                "message is too long",
                "can't parse entities",
                "cant parse entities",
                "can't find end",
                "unsupported start tag",
                "bad request",
                "entity",
                "parse",
                "forbidden",
                "bot was blocked",
                "blocked by the user",
                "user is deactivated",
                "not enough rights",
                "have no rights",
                "not a member",
                "chat not found",
                "message to edit not found",
                "message to reply not found",
                "thread not found",
                "topic_deleted",
                "message_id_invalid",
                "flood",
                "too many requests",
                "retry after",
                "rate limit",
                "connecterror",
                "connectionerror",
                "connectionreset",
                "connectionrefused",
                "connecttimeout",
                "network",
                "broken pipe",
                "remotedisconnected",
                "eoferror",
            )
            if needle in blob
        ]
        assert collisions == [], (
            f"error text collides with classify_send_error: {collisions}"
        )

    @pytest.mark.asyncio
    async def test_it_is_not_retried_into_existence(self):
        res = await make_adapter().send(f"g{GROUP}", BLOCKED_TEXT)
        assert res.retryable is False
        assert res.retry_after is None

    @pytest.mark.asyncio
    async def test_the_marker_key_is_set(self):
        res = await make_adapter().send(f"g{GROUP}", BLOCKED_TEXT)
        assert isinstance(res.raw_response, dict)
        assert res.raw_response.get(A.POLICY_MARKER) is True
        assert res.raw_response.get("reason") == A.POLICY_REASON
        assert A.is_policy_blocked_send_result(res) is True

    @pytest.mark.asyncio
    async def test_it_is_distinguishable_from_a_mute(self):
        """Two different local refusals; a caller must be able to tell them
        apart, because only one of them is an operator toggle."""
        res = await make_adapter().send(f"g{GROUP}", BLOCKED_TEXT)
        assert A.is_policy_blocked_send_result(res) is True
        assert A.is_muted_send_result(res) is False

    @pytest.mark.asyncio
    async def test_it_is_distinguishable_from_a_real_send_failure(self):
        class _Rejecting(FakeClient):
            async def call_action(self, action, *, timeout=None):
                self.actions.append(action)
                return {"status": "failed", "retcode": 1403, "message": "no permission"}

        blocked = await make_adapter().send(f"g{GROUP}", BLOCKED_TEXT)
        failure = await make_adapter(client=_Rejecting()).send(f"g{GROUP}", BENIGN_TEXT)

        assert blocked.success is failure.success is False  # both are failures
        assert A.is_policy_blocked_send_result(blocked) is True
        assert A.is_policy_blocked_send_result(failure) is False

    @pytest.mark.asyncio
    async def test_the_three_audit_fields_are_present(self):
        res = await make_adapter().send(f"g{GROUP}", BLOCKED_TEXT)
        raw = res.raw_response
        assert raw["category_codes"] == ["fraud_gambling_account_abuse"]
        assert raw["rule_ids"] == ["fraud.account-abuse"]
        assert raw["ruleset_version"] == POLICY.RULESET_VERSION

    @pytest.mark.asyncio
    async def test_the_audit_fields_carry_no_source_text(self):
        """They get logged.  Logging the blocked sentence would defeat the
        point of refusing to transmit it."""
        res = await make_adapter().send(f"g{GROUP}", BLOCKED_TEXT)
        raw = dict(res.raw_response)
        raw.pop("group_id", None)
        assert BLOCKED_TEXT not in repr(raw)
        assert "解冻" not in repr(raw)

    @pytest.mark.asyncio
    async def test_a_refused_post_is_not_recorded_as_something_we_said(self):
        ad = make_adapter()
        await ad.send(f"g{GROUP}", BLOCKED_TEXT)
        assert A.recent_group_messages(ad.instance_id, GROUP) == []

    @pytest.mark.asyncio
    async def test_a_bad_chat_id_still_reports_the_bad_chat_id(self):
        """Parsing comes first — a typo is not a policy refusal."""
        res = await make_adapter().send("not-a-chat", BLOCKED_TEXT)
        assert res.error_kind == "not_found"
        assert A.is_policy_blocked_send_result(res) is False

    @pytest.mark.asyncio
    async def test_the_decision_wins_over_a_disconnected_link(self):
        """ "Not connected" is retryable; "refused" is a decision.  Reporting
        the transient would have the caller retry a message that must not go."""
        ad = make_adapter()
        ad._client = None
        res = await ad.send(f"g{GROUP}", BLOCKED_TEXT)
        assert A.is_policy_blocked_send_result(res) is True
        assert res.retryable is False


# ---------------------------------------------------------------------------
# 4. Fail closed
# ---------------------------------------------------------------------------


class TestClassifierFailureFailsClosed:
    """An exploding classifier must never become an open gate."""

    @pytest.mark.asyncio
    async def test_an_exploding_classifier_blocks_the_send(self, monkeypatch):
        def _boom(*_a, **_kw):
            raise RuntimeError("rule table exploded")

        monkeypatch.setattr(A.content_policy, "moderate_text", _boom)
        ad = make_adapter()
        res = await ad.send(f"g{GROUP}", BENIGN_TEXT)
        assert res.success is False, "fail-open would defeat the whole gate"
        assert ad._client.actions == []

    @pytest.mark.asyncio
    async def test_the_failure_decision_is_the_one_reported(self, monkeypatch):
        def _boom(*_a, **_kw):
            raise RuntimeError("rule table exploded")

        monkeypatch.setattr(A.content_policy, "moderate_text", _boom)
        res = await make_adapter().send(f"g{GROUP}", BENIGN_TEXT)
        assert res.raw_response["category_codes"] == ["classifier_failure"]
        assert res.raw_response["rule_ids"] == ["policy.classifier-failure"]

    @pytest.mark.asyncio
    async def test_the_failure_refusal_keeps_the_same_safe_shape(self, monkeypatch):
        """A fail-closed refusal must not mark the chat dead either — the
        classifier being broken says nothing about the peer."""

        def _boom(*_a, **_kw):
            raise RuntimeError("rule table exploded")

        monkeypatch.setattr(A.content_policy, "moderate_text", _boom)
        res = await make_adapter().send(f"g{GROUP}", BENIGN_TEXT)
        assert res.error_kind == "unknown"
        assert res.retryable is False
        assert DeadTargetRegistry.is_dead_error_kind(res.error_kind) is False
        assert classify_send_error(None, res.error or "") == "unknown"

    @pytest.mark.asyncio
    async def test_a_dm_fails_closed_too(self, monkeypatch):
        def _boom(*_a, **_kw):
            raise RuntimeError("rule table exploded")

        monkeypatch.setattr(A.content_policy, "moderate_text", _boom)
        res = await make_adapter().send(str(DM_PEER), BENIGN_TEXT)
        assert res.success is False

    def test_the_policy_module_itself_fails_closed(self):
        """The adapter's ``except`` is the second line of defence; the first is
        ``moderate_text``'s own internal guard.  Both must fail closed."""
        assert POLICY.classifier_failure_decision("anything").allowed is False


# ---------------------------------------------------------------------------
# 5. Not group-limited
# ---------------------------------------------------------------------------


class TestTheGateIsNotGroupLimited:
    """The mute is group-scoped; this is not.  A Tencent freeze is applied to
    the *account*, and risk control does not care which surface the sentence
    was typed into."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("chat_id", [f"g{GROUP}", str(DM_PEER)])
    async def test_blocked_text_is_refused_on_both_surfaces(self, chat_id):
        ad = make_adapter()
        res = await ad.send(chat_id, BLOCKED_TEXT)
        assert res.success is False
        assert A.is_policy_blocked_send_result(res) is True
        assert ad._client.actions == []

    @pytest.mark.asyncio
    async def test_the_dm_refusal_names_the_user_not_a_group(self):
        res = await make_adapter().send(str(DM_PEER), BLOCKED_TEXT)
        assert res.raw_response.get("user_id") == DM_PEER
        assert "group_id" not in res.raw_response

    @pytest.mark.asyncio
    async def test_a_dm_is_refused_even_while_groups_are_muted(self):
        """The two gates are independent: muting groups must not accidentally
        disable, or accidentally stand in for, the content gate on DMs."""
        ad = make_adapter({"group_replies_enabled": False})
        ad.router.group_replies_enabled = False
        res = await ad.send(str(DM_PEER), BLOCKED_TEXT)
        assert A.is_policy_blocked_send_result(res) is True


# ---------------------------------------------------------------------------
# 6. Gate ordering and what the classifier actually sees
# ---------------------------------------------------------------------------


class _SpyPolicy:
    """Wraps ``moderate_text`` and records every body it was handed."""

    def __init__(self):
        self.calls: List[str] = []

    def install(self, monkeypatch):
        real = POLICY.moderate_text

        def _spy(text, config=None):
            self.calls.append(text)
            return real(text, config)

        monkeypatch.setattr(A.content_policy, "moderate_text", _spy)
        return self


class TestGateOrdering:
    @pytest.mark.asyncio
    async def test_the_mute_short_circuits_before_the_classifier_runs(
        self, monkeypatch
    ):
        """Two cores.  A muted group must not pay for a classification whose
        result is discarded anyway."""
        spy = _SpyPolicy().install(monkeypatch)
        ad = make_adapter({"group_replies_enabled": False})
        ad.router.group_replies_enabled = False
        res = await ad.send(f"g{GROUP}", BENIGN_TEXT)
        assert A.is_muted_send_result(res) is True
        assert spy.calls == [], "the classifier ran behind a closed mute gate"

    @pytest.mark.asyncio
    async def test_the_classifier_sees_the_post_strip_text(self, monkeypatch):
        """Auditing the pre-strip body would audit something nobody receives."""
        spy = _SpyPolicy().install(monkeypatch)
        raw = "### 标题\n这个 **很重要**,见 [文档](https://example.com)"
        await make_adapter().send(f"g{GROUP}", raw)
        assert spy.calls == [strip_markdown(raw)]
        assert "**" not in spy.calls[0] and "###" not in spy.calls[0]

    @pytest.mark.asyncio
    async def test_the_classifier_sees_the_whole_body_once(self, monkeypatch):
        """Not once per bubble.  A phrase the table catches can straddle a
        ``[MSG_BREAK]``, and per-bubble auditing would pass both halves."""
        spy = _SpyPolicy().install(monkeypatch)
        body = f"{BENIGN_TEXT}[MSG_BREAK]第二段[MSG_BREAK]第三段"
        await make_adapter().send(f"g{GROUP}", body)
        assert len(spy.calls) == 1, f"expected one whole-body call, got {spy.calls}"
        assert "第二段" in spy.calls[0] and "第三段" in spy.calls[0]

    @pytest.mark.asyncio
    async def test_the_classifier_sees_the_whole_body_not_the_chunks(self, monkeypatch):
        """Same argument at the chunk boundary: ``chunk_text`` cuts on length,
        which has nothing to do with meaning."""
        spy = _SpyPolicy().install(monkeypatch)
        body = "啊" * (A.MAX_MESSAGE_LENGTH + 500)
        ad = make_adapter({"forward_threshold": 0})
        res = await ad.send(f"g{GROUP}", body)
        assert res.success is True
        assert len(spy.calls) == 1
        assert len(spy.calls[0]) == len(body)
        assert len(ad._client.actions) > 1, "this body should have been chunked"

    @pytest.mark.asyncio
    async def test_the_gate_runs_before_anything_is_split(self, monkeypatch):
        """Ordering, observed from the other side: on a refusal, no bubble and
        no chunk was ever built into an action."""
        ad = make_adapter()
        body = f"{BLOCKED_TEXT}[MSG_BREAK]" + "啊" * (A.MAX_MESSAGE_LENGTH + 10)
        res = await ad.send(f"g{GROUP}", body)
        assert res.success is False
        assert ad._client.actions == []

    def test_production_call_sites_pass_no_resolver(self):
        """``resolve_config`` only ever disables on an explicit ``False`` from
        an injected resolver.  Production must not inject one — same as the
        four QZone call sites."""
        assert POLICY.resolve_config(None).enabled is True


# ---------------------------------------------------------------------------
# 7. The proactive lane degrades correctly
# ---------------------------------------------------------------------------


def _proactive_cfg() -> PR.ProactiveConfig:
    return PR.ProactiveConfig(
        groups=(str(GROUP),),
        min_gap_minutes=0.0,
        max_gap_minutes=0.0,
        daily_max=5,
        active_start_hour=0,
        active_end_hour=24,
        prompt="say something",
        probability=1.0,
    )


async def _run_one_beat(monkeypatch, adapter, text: str, caplog):
    """Drive ``proactive_loop`` through exactly one beat that tries to post."""
    cfg = _proactive_cfg()
    cancel = asyncio.Event()

    async def _no_sleep(_cancel, _secs):
        return _cancel.is_set()

    async def _generate(_adapter, _group, _prompt):
        cancel.set()  # this beat completes, then the loop exits
        return text

    monkeypatch.setattr(PR, "live_config", lambda _ad: cfg)
    monkeypatch.setattr(PR, "sleep_or_cancel", _no_sleep)
    monkeypatch.setattr(PR, "generate", _generate)
    monkeypatch.setattr(PR, "now_parts", lambda _tz: ("2026-08-19", 12))

    with caplog.at_level(logging.WARNING, logger="plugins.platforms.onebot.proactive"):
        await asyncio.wait_for(PR.proactive_loop(adapter, cancel, cfg), timeout=10)
    return cfg


class TestProactiveLaneDegradesToASkippedBeat:
    """``proactive.py`` already checks ``result.success`` and ``continue``s.
    These tests verify that link actually holds end to end rather than
    assuming it."""

    @pytest.mark.asyncio
    async def test_a_refused_post_never_reaches_the_wire(self, monkeypatch, caplog):
        ad = make_adapter()
        await _run_one_beat(monkeypatch, ad, BLOCKED_TEXT, caplog)
        assert ad._client.actions == []

    @pytest.mark.asyncio
    async def test_it_logs_a_warning_and_keeps_living(self, monkeypatch, caplog):
        ad = make_adapter()
        await _run_one_beat(monkeypatch, ad, BLOCKED_TEXT, caplog)
        assert any(
            "proactive send rejected" in rec.getMessage() for rec in caplog.records
        ), [rec.getMessage() for rec in caplog.records]

    @pytest.mark.asyncio
    async def test_the_refused_beat_spends_no_daily_budget(self, monkeypatch, caplog):
        ad = make_adapter()
        await _run_one_beat(monkeypatch, ad, BLOCKED_TEXT, caplog)
        key = A.speech_key(ad.instance_id, str(GROUP))
        assert PR.sent_today(key, "2026-08-19") == 0

    @pytest.mark.asyncio
    async def test_the_refused_beat_spends_no_rate_quota(self, monkeypatch, caplog):
        """``_GROUP_SPEECH.record`` sits *after* the success check.  A refusal
        that consumed quota would let one bad sentence silence the hour."""
        ad = make_adapter()
        await _run_one_beat(monkeypatch, ad, BLOCKED_TEXT, caplog)
        key = A.speech_key(ad.instance_id, str(GROUP))
        assert list(A._GROUP_SPEECH._events.get(key, [])) == []

    @pytest.mark.asyncio
    async def test_the_refused_beat_does_not_move_the_last_post_clock(
        self, monkeypatch, caplog
    ):
        ad = make_adapter()
        await _run_one_beat(monkeypatch, ad, BLOCKED_TEXT, caplog)
        key = A.speech_key(ad.instance_id, str(GROUP))
        assert key not in PR._LAST_POST_MONO

    @pytest.mark.asyncio
    async def test_a_successful_beat_does_spend_the_budget(self, monkeypatch, caplog):
        """The control: the assertions above must be measuring the refusal,
        not a loop that never got as far as posting."""
        ad = make_adapter()
        await _run_one_beat(monkeypatch, ad, BENIGN_TEXT, caplog)
        key = A.speech_key(ad.instance_id, str(GROUP))
        assert ad._client.actions, "the benign beat should have posted"
        assert PR.sent_today(key, "2026-08-19") == 1
        assert list(A._GROUP_SPEECH._events.get(key, [])) != []
        assert key in PR._LAST_POST_MONO

    @pytest.mark.asyncio
    async def test_a_proactive_post_is_markdown_stripped_too(self, monkeypatch, caplog):
        """The proactive lane reaches the wire through ``send``, so it inherits
        the same shaping — this pins that it really does."""
        ad = make_adapter()
        await _run_one_beat(monkeypatch, ad, "### 今天\n**很热**", caplog)
        sent = wire_text(ad)
        assert sent, "the beat should have posted"
        assert "###" not in sent and "**" not in sent
