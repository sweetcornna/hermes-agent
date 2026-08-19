"""Adapter + registration tests for the OneBot (QQ / NapCat) platform plugin.

Structural model: ``tests/gateway/test_plugin_platform_interface.py`` (the
platform-plugin contract) with ``tests/gateway/test_line_plugin.py`` as the
worked example. Everything here runs against a fake OneBot client — no
sockets, no NapCat.

Two contract details are load-bearing and get their own tests:

* ``splits_long_messages`` must be True, because this adapter chunks in
  ``send()``. With it False, ``gateway/delivery.py`` truncates at
  ``MAX_PLATFORM_OUTPUT`` (4000 chars) and long scheduled reports are
  silently clipped.
* ``SendResult.error_kind`` must come from ``SEND_ERROR_KINDS`` — it drives
  dead-target detection.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List, Optional

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageType,
    SEND_ERROR_KINDS,
    SendResult,
)

from plugins.platforms.onebot import protocol as P
from plugins.platforms.onebot import adapter as A


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeClient:
    """Stand-in for ``OneBotClient`` recording every outbound action."""

    def __init__(self, responses: Optional[List[Dict[str, Any]]] = None,
                 default: Optional[Dict[str, Any]] = None):
        self.actions: List[Any] = []
        self.fire_and_forget: List[Any] = []
        self._responses = list(responses or [])
        self._default = default or {"status": "ok", "retcode": 0,
                                    "data": {"message_id": 1001}}
        self.connected = True
        self.last_self_id = 100
        self.last_event_at_ms = 0
        self.last_status_online = True
        self.inbound_dropped_count = 0
        self.outbound_queue_depth = 0
        self.raise_on_call: Optional[BaseException] = None

    async def call_action(self, action, *, timeout=None):
        self.actions.append(action)
        if self.raise_on_call is not None:
            raise self.raise_on_call
        if self._responses:
            return self._responses.pop(0)
        return dict(self._default)

    async def send_action(self, action):
        self.actions.append(action)
        self.fire_and_forget.append(action)

    async def close(self):
        self.connected = False


def make_adapter(extra: Optional[Dict[str, Any]] = None, *,
                 client: Optional[FakeClient] = None) -> A.OneBotAdapter:
    base = {"ws_url": "ws://127.0.0.1:3001"}
    base.update(extra or {})
    ad = A.OneBotAdapter(PlatformConfig(enabled=True, extra=base))
    ad._client = client or FakeClient()
    ad._running = True
    ad._semaphore = asyncio.Semaphore(4)
    return ad


def group_event(text="hi", *, gid=183287894, uid=555, self_id=100,
                message_id=1, segments=None) -> P.MessageEvent:
    return P.MessageEvent(
        self_id=self_id, message_type=P.MessageType.GROUP, sub_type="normal",
        group_id=gid, user_id=uid, message_id=message_id,
        message=segments if segments is not None else [P.TextSegment(text=text)],
        raw_message=text, time=1_700_000_000,
        sender=P.Sender(user_id=uid, nickname="alice"),
    )


def private_event(text="hi", *, uid=2104743984, self_id=100, message_id=1) -> P.MessageEvent:
    return P.MessageEvent(
        self_id=self_id, message_type=P.MessageType.PRIVATE, sub_type="friend",
        group_id=None, user_id=uid, message_id=message_id,
        message=[P.TextSegment(text=text)], raw_message=text, time=1,
        sender=P.Sender(user_id=uid, nickname="bob"),
    )


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
# 1. Registration contract
# ---------------------------------------------------------------------------

class _FakeCtx:
    def __init__(self):
        self.kwargs: Dict[str, Any] = {}

    def register_platform(self, **kw):
        self.kwargs = kw


class TestRegister:

    def test_registers_under_the_onebot_name_not_qqbot(self):
        """The built-in ``qqbot`` platform is a different protocol entirely."""
        ctx = _FakeCtx()
        A.register(ctx)
        assert ctx.kwargs["name"] == "onebot"
        assert ctx.kwargs["name"] != "qqbot"
        assert "OneBot" in ctx.kwargs["label"]

    def test_manifest_name_resolves_to_the_directory_name(self):
        """``_platform_name_from_manifest`` strips ``-platform``; a mismatch
        makes lazy loading fail silently."""
        import yaml
        from pathlib import Path
        manifest = yaml.safe_load(
            (Path(A.__file__).parent / "plugin.yaml").read_text(encoding="utf-8"))
        assert manifest["kind"] == "platform"
        assert manifest["name"] == "onebot-platform"
        derived = manifest["name"][: -len("-platform")]
        assert derived == Path(A.__file__).parent.name == A.PLATFORM_NAME

    def test_creates_a_valid_platform_entry(self):
        from gateway.platform_registry import PlatformEntry
        ctx = _FakeCtx()
        A.register(ctx)
        entry = PlatformEntry(**ctx.kwargs)
        assert entry.name == "onebot"
        assert callable(entry.adapter_factory) and callable(entry.check_fn)

    def test_advertises_required_env(self):
        ctx = _FakeCtx()
        A.register(ctx)
        assert ctx.kwargs["required_env"] == ["ONEBOT_WS_URL"]

    def test_registers_the_registry_hooks_that_replace_core_edits(self):
        ctx = _FakeCtx()
        A.register(ctx)
        for hook in ("env_enablement_fn", "apply_yaml_config_fn",
                     "standalone_sender_fn", "cron_deliver_env_var",
                     "allowed_users_env", "allow_all_env", "platform_hint",
                     "setup_fn", "validate_config", "is_connected"):
            assert ctx.kwargs.get(hook), f"missing registry hook: {hook}"

    def test_max_message_length_matches_the_adapter(self):
        ctx = _FakeCtx()
        A.register(ctx)
        assert ctx.kwargs["max_message_length"] == A.OneBotAdapter.MAX_MESSAGE_LENGTH
        assert ctx.kwargs["max_message_length"] == 3800

    def test_factory_yields_a_onebot_adapter(self):
        ctx = _FakeCtx()
        A.register(ctx)
        ad = ctx.kwargs["adapter_factory"](
            PlatformConfig(enabled=True, extra={"ws_url": "ws://127.0.0.1:3001"}))
        assert isinstance(ad, A.OneBotAdapter)

    def test_platform_enum_resolves_without_a_core_edit(self):
        assert Platform("onebot").value == "onebot"


class TestCapabilityAttributes:

    def test_splits_long_messages_is_true(self):
        """Otherwise gateway/delivery.py truncates at MAX_PLATFORM_OUTPUT."""
        from gateway.delivery import MAX_PLATFORM_OUTPUT
        assert A.OneBotAdapter.splits_long_messages is True
        assert A.OneBotAdapter.MAX_MESSAGE_LENGTH < MAX_PLATFORM_OUTPUT

    def test_qq_renders_no_markdown(self):
        assert A.OneBotAdapter.supports_code_blocks is False
        assert A.OneBotAdapter.supports_status_text is False

    def test_all_four_abstract_methods_are_implemented(self):
        assert getattr(A.OneBotAdapter, "__abstractmethods__", frozenset()) == frozenset()
        for name in ("connect", "disconnect", "send", "get_chat_info"):
            assert getattr(A.OneBotAdapter, name) is not getattr(BasePlatformAdapter, name)


class TestCheckRequirements:

    def test_false_without_ws_url(self, monkeypatch):
        monkeypatch.delenv("ONEBOT_WS_URL", raising=False)
        assert A.check_requirements() is False

    def test_true_with_ws_url_and_websockets(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_WS_URL", "ws://127.0.0.1:3001")
        assert A.check_requirements() is True

    def test_validate_config_accepts_extra_only(self):
        assert A.validate_config(
            PlatformConfig(enabled=True, extra={"ws_url": "ws://x"})) is True
        assert A.validate_config(PlatformConfig(enabled=True, extra={})) is False


class TestEnvEnablement:

    def test_returns_none_without_ws_url(self, monkeypatch):
        monkeypatch.delenv("ONEBOT_WS_URL", raising=False)
        assert A._env_enablement() is None

    def test_seeds_ws_url_and_home_channel(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_WS_URL", "ws://127.0.0.1:3001")
        monkeypatch.setenv("ONEBOT_HOME_CHANNEL", "g183287894")
        seed = A._env_enablement()
        assert seed["ws_url"] == "ws://127.0.0.1:3001"
        assert seed["home_channel"] == {"chat_id": "g183287894", "name": "g183287894"}


class TestApplyYamlConfig:

    def test_lifts_private_keys_into_extra(self):
        extras = A._apply_yaml_config({}, {
            "ws_url": "ws://h:3001",
            "group_replies_enabled": True,
            "group_whitelist": [1, 2],
        })
        assert extras["ws_url"] == "ws://h:3001"
        assert extras["group_replies_enabled"] is True
        assert extras["group_whitelist"] == [1, 2]

    def test_does_not_re_emit_generic_keys(self):
        """Re-emitting them would clobber the core loader's precedence."""
        extras = A._apply_yaml_config({}, {
            "ws_url": "ws://h:3001",
            "extra": {"dm_policy": "open", "allow_from": ["1"], "bot_nickname": "格兰"},
        })
        assert "dm_policy" not in extras and "allow_from" not in extras
        assert extras["bot_nickname"] == "格兰"


# ---------------------------------------------------------------------------
# 2. Pure helpers
# ---------------------------------------------------------------------------

class TestChatIds:

    @pytest.mark.parametrize(("raw", "expected"), [
        ("g183287894", (True, 183287894)),
        ("183287894", (False, 183287894)),
        ("group:12345", (True, 12345)),
        ("private:777", (False, 777)),
        ("user:777", (False, 777)),
        ("  g42  ", (True, 42)),
    ])
    def test_parse(self, raw, expected):
        assert A.parse_chat_id(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", "abc", "gg1", "g", None, "12a"])
    def test_rejects_garbage(self, raw):
        with pytest.raises(ValueError):
            A.parse_chat_id(raw)

    def test_canonical_form_has_no_colon(self):
        """DeliveryTarget.parse splits on ':' — a colon would misroute."""
        assert ":" not in A.format_chat_id(True, 183287894)
        assert A.format_chat_id(True, 1) == "g1"
        assert A.format_chat_id(False, 1) == "1"

    def test_round_trip(self):
        for is_group, target in ((True, 12345), (False, 999)):
            assert A.parse_chat_id(A.format_chat_id(is_group, target)) == (is_group, target)


class TestWhitelistParsing:

    def test_unset_means_no_whitelist(self):
        assert A.parse_whitelist(None) is None

    def test_empty_string_means_block_every_group(self):
        assert A.parse_whitelist("") == frozenset()

    def test_empty_list_means_block_every_group(self):
        assert A.parse_whitelist([]) == frozenset()

    def test_production_five_group_list(self):
        raw = "1082225370,183287894,894800697,149881991,667528618"
        assert A.parse_whitelist(raw) == frozenset(
            {"1082225370", "183287894", "894800697", "149881991", "667528618"})

    def test_accepts_a_yaml_int_list(self):
        assert A.parse_whitelist([1082225370, 183287894]) == frozenset(
            {"1082225370", "183287894"})


class TestIdListParsing:

    def test_parses_comma_and_semicolon(self):
        assert A.parse_id_list("1, 2;3") == [1, 2, 3]

    def test_ignores_non_numeric(self):
        assert A.parse_id_list("1,oops,3") == [1, 3]

    def test_empty(self):
        assert A.parse_id_list(None) == [] and A.parse_id_list("") == []


class TestChunking:

    def test_short_text_is_untouched(self):
        assert A.chunk_text("hello") == ["hello"]

    def test_long_text_is_chunked_with_counters(self):
        chunks = A.chunk_text("x" * 9000, 3800)
        assert len(chunks) > 1
        assert chunks[0].startswith("(1/")
        assert all(len(c) <= 3800 for c in chunks)

    def test_no_characters_are_lost(self):
        body = "\n\n".join(f"paragraph {i} " + "y" * 200 for i in range(60))
        rebuilt = "".join(
            c.split("\n", 1)[1] for c in A.chunk_text(body, 1000)
        ).replace("\n", "").replace(" ", "")
        assert rebuilt == body.replace("\n", "").replace(" ", "")

    def test_prefers_paragraph_boundaries(self):
        body = ("a" * 1500) + "\n\n" + ("b" * 1500)
        chunks = A.chunk_text(body, 2000)
        assert chunks[0].rstrip().endswith("a")
        assert chunks[1].split("\n", 1)[1].startswith("b")

    def test_cjk_sentence_boundary(self):
        body = ("啊" * 900) + "。" + ("哦" * 900)
        chunks = A.chunk_text(body, 1000)
        assert chunks[0].rstrip().endswith("。")

    def test_split_bubbles(self):
        assert A.split_bubbles("one[MSG_BREAK]two") == ["one", "two"]
        assert A.split_bubbles("  solo  ") == ["solo"]
        assert A.split_bubbles("") == []
        assert A.split_bubbles("a[MSG_BREAK]   [MSG_BREAK]b") == ["a", "b"]


class TestErrorClassification:

    @pytest.mark.parametrize(("retcode", "message", "expected"), [
        (1401, "", "forbidden"),
        (1403, "", "forbidden"),
        (1404, "", "not_found"),
        (10003, "", "bad_format"),
        (1200, "群不存在", "not_found"),
        (1200, "您已被禁言", "forbidden"),
        (1200, "发送过于频繁", "rate_limited"),
        (1200, "消息过长", "too_long"),
        (1200, "connection closed", "transient"),
        (1200, "", "unknown"),
    ])
    def test_maps_onto_the_shared_vocabulary(self, retcode, message, expected):
        assert A.classify_onebot_error(retcode, message) == expected

    def test_every_result_is_a_valid_send_error_kind(self):
        for retcode in (0, 100, 1200, 1401, 1403, 1404, 10003, 10004, 99999):
            for msg in ("", "boom", "群不存在", "rate limit exceeded", "not a member"):
                assert A.classify_onebot_error(retcode, msg) in SEND_ERROR_KINDS

    def test_shared_classifier_wins_over_the_retcode_table(self):
        # An explicit "rate limit" phrase must not be masked by the retcode.
        assert A.classify_onebot_error(1403, "rate limit exceeded") == "rate_limited"


class TestMediaHelpers:

    @pytest.mark.parametrize(("name", "kind"), [
        ("a.png", "image"), ("a.jpg", "image"),
        ("a.mp3", "audio"), ("a.silk", "audio"), ("a.amr", "audio"),
        ("a.mp4", "video"),
        ("a.pdf", "document"), ("a.bin", "document"), ("noext", "document"),
    ])
    def test_guess_media_kind(self, name, kind):
        assert A.guess_media_kind(f"/tmp/{name}") == kind

    def test_base64_ceiling_is_eight_mib(self):
        """Cut from the sources' 30 MiB: base64 inflates ~4/3 and the frame
        is buffered in RAM at both ends on a 2 GB host."""
        assert A.BASE64_MAX_BYTES == 8 * 1024 * 1024

    def test_encodes_a_small_file(self, tmp_path):
        p = tmp_path / "x.png"
        p.write_bytes(b"hello")
        assert A.encode_base64_file(str(p)) == "base64://aGVsbG8="

    def test_returns_none_above_the_ceiling(self, tmp_path):
        p = tmp_path / "big.bin"
        p.write_bytes(b"0" * 2048)
        assert A.encode_base64_file(str(p), max_bytes=1024) is None

    def test_returns_none_for_a_missing_file(self):
        assert A.encode_base64_file("/nonexistent/nope.png") is None


# ---------------------------------------------------------------------------
# 3. Adapter construction / config
# ---------------------------------------------------------------------------

class TestAdapterConfig:

    def test_group_replies_default_to_off(self):
        """Reproduces the production posture: DMs only until switched on."""
        ad = make_adapter()
        assert ad.group_replies_enabled is False
        assert ad.router.group_replies_enabled is False

    def test_master_switch_is_one_line(self):
        ad = make_adapter({"group_replies_enabled": True})
        assert ad.router.group_replies_enabled is True

    def test_env_configures_the_production_shape(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_WS_URL", "ws://127.0.0.1:3001")
        monkeypatch.setenv("ONEBOT_GROUP_REPLIES_ENABLED", "true")
        monkeypatch.setenv(
            "ONEBOT_GROUP_WHITELIST",
            "1082225370,183287894,894800697,149881991,667528618")
        monkeypatch.setenv(
            "ONEBOT_GROUP_KEYWORDS",
            '{"1082225370":["格兰"],"183287894":["格兰"],"894800697":["格兰"],'
            '"149881991":["格兰"],"667528618":["格兰"]}')
        monkeypatch.setenv("ONEBOT_GROUP_RATE_LIMIT_WINDOW_MINUTES", "3")
        monkeypatch.setenv("ONEBOT_GROUP_RATE_LIMIT_MAX_MESSAGES", "5")
        ad = A.OneBotAdapter(PlatformConfig(enabled=True, extra={}))
        assert ad.ws_url == "ws://127.0.0.1:3001"
        assert ad.group_replies_enabled is True
        assert len(ad.group_whitelist) == 5
        assert ad.group_keywords["183287894"] == ["格兰"]
        assert ad.group_reply_policy == "mention_or_keyword"
        assert (ad.group_window_secs, ad.group_window_max) == (180.0, 5)

    def test_yaml_extra_beats_env(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_WS_URL", "ws://from-env:3001")
        ad = A.OneBotAdapter(PlatformConfig(enabled=True,
                                            extra={"ws_url": "ws://from-yaml:3001"}))
        assert ad.ws_url == "ws://from-yaml:3001"

    def test_malformed_keyword_json_degrades_to_mention_only(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_GROUP_KEYWORDS", "{not json")
        ad = make_adapter()
        assert ad.group_keywords == {}

    def test_unset_whitelist_is_none_not_empty(self, monkeypatch):
        monkeypatch.delenv("ONEBOT_GROUP_WHITELIST", raising=False)
        assert make_adapter().group_whitelist is None

    def test_empty_whitelist_env_blocks_everything(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_GROUP_WHITELIST", "")
        assert make_adapter().group_whitelist == frozenset()

    def test_concurrency_default_suits_a_small_host(self):
        assert make_adapter().max_concurrency == A.DEFAULT_MAX_CONCURRENCY == 2

    def test_self_id_prefers_the_live_value(self):
        ad = make_adapter({"self_ids": "999"})
        assert ad.self_id == 100  # FakeClient reports the live account
        ad._client = None
        assert ad.self_id == 999

    def test_health_snapshot_separates_link_from_account(self):
        ad = make_adapter()
        snap = ad.health_snapshot()
        assert snap["link_online"] is True
        assert snap["account_online"] is None  # not probed yet
        assert snap["platform"] == "onebot"


# ---------------------------------------------------------------------------
# 4. Outbound
# ---------------------------------------------------------------------------

class TestSend:

    @pytest.mark.asyncio
    async def test_private_send_is_a_plain_text_message(self):
        client = FakeClient()
        ad = make_adapter(client=client)
        res = await ad.send("2104743984", "hello")
        assert res.success is True and res.message_id == "1001"
        assert len(client.actions) == 1
        action = client.actions[0]
        assert isinstance(action, P.SendPrivateMsg) and action.user_id == 2104743984
        assert action.message == [P.TextSegment(text="hello")]

    @pytest.mark.asyncio
    async def test_group_reply_mentions_only_the_first_chunk(self):
        """N pings for one reply is what QQ's anti-spam reacts to."""
        client = FakeClient()
        # forward folding off so the long body actually chunks; group replies
        # on because ``send()`` now obeys the mute (D44).
        ad = make_adapter({"forward_threshold": 0, "group_replies_enabled": True},
                          client=client)
        await ad.send("g183287894", "y" * 9000,
                      metadata={"onebot_at_user_id": "555"})
        sends = [a for a in client.actions if isinstance(a, P.SendGroupMsg)]
        assert len(sends) > 1
        assert isinstance(sends[0].message[0], P.AtSegment)
        assert sends[0].message[0].qq == "555"
        for later in sends[1:]:
            assert not any(isinstance(s, P.AtSegment) for s in later.message)

    @pytest.mark.asyncio
    async def test_reply_segment_only_on_the_first_chunk(self):
        client = FakeClient()
        ad = make_adapter({"forward_threshold": 0}, client=client)
        await ad.send("2104743984", "y" * 9000, reply_to="42")
        sends = [a for a in client.actions if isinstance(a, P.SendPrivateMsg)]
        assert isinstance(sends[0].message[0], P.ReplySegment)
        for later in sends[1:]:
            assert not any(isinstance(s, P.ReplySegment) for s in later.message)

    @pytest.mark.asyncio
    async def test_mention_can_be_disabled(self):
        client = FakeClient()
        ad = make_adapter({"reply_with_mention": False,
                           "group_replies_enabled": True}, client=client)
        await ad.send("g1", "hi", metadata={"onebot_at_user_id": "555"})
        assert not any(isinstance(s, P.AtSegment)
                       for s in client.actions[0].message)

    @pytest.mark.asyncio
    async def test_bubble_marker_produces_separate_messages(self):
        client = FakeClient()
        ad = make_adapter(client=client)
        await ad.send("777", "first[MSG_BREAK]second")
        texts = [a.message[-1].text for a in client.actions]
        assert texts == ["first", "second"]

    @pytest.mark.asyncio
    async def test_long_bubble_folds_into_a_forward_card(self):
        client = FakeClient()
        ad = make_adapter(client=client)
        await ad.send("2104743984", "z" * 1500)
        assert any(isinstance(a, P.SendPrivateForwardMsg) for a in client.actions)

    @pytest.mark.asyncio
    async def test_group_forward_card_is_preceded_by_a_lead_line(self):
        """A card cannot carry an @mention, so the ping goes out first."""
        client = FakeClient()
        ad = make_adapter({"group_replies_enabled": True}, client=client)
        await ad.send("g183287894", "z" * 1500,
                      metadata={"onebot_at_user_id": "555"})
        assert isinstance(client.actions[0], P.SendGroupMsg)
        assert isinstance(client.actions[0].message[0], P.AtSegment)
        assert A.FORWARD_LEAD_TEXT in client.actions[0].message[-1].text
        assert isinstance(client.actions[1], P.SendGroupForwardMsg)

    @pytest.mark.asyncio
    async def test_forward_nodes_carry_the_body(self):
        client = FakeClient()
        ad = make_adapter(client=client)
        await ad.send("777", "z" * 1500)
        card = [a for a in client.actions if isinstance(a, P.SendPrivateForwardMsg)][0]
        assert card.messages[0].content[0].text == "z" * 1500

    @pytest.mark.asyncio
    async def test_rejected_forward_card_falls_back_to_chunks(self):
        """Content is never lost because a card was refused."""
        client = FakeClient(responses=[
            {"status": "failed", "retcode": 1200, "message": "forward unsupported"},
            {"status": "ok", "retcode": 0, "data": {"message_id": 5}},
        ])
        ad = make_adapter(client=client)
        res = await ad.send("777", "z" * 1500)
        assert res.success is True
        assert any(isinstance(a, P.SendPrivateMsg) for a in client.actions)

    @pytest.mark.asyncio
    async def test_forward_can_be_disabled(self):
        client = FakeClient()
        ad = make_adapter({"forward_threshold": 0}, client=client)
        await ad.send("777", "z" * 1500)
        assert not any(isinstance(a, P.SendPrivateForwardMsg) for a in client.actions)

    @pytest.mark.asyncio
    async def test_continuation_message_ids_are_reported(self):
        client = FakeClient(default={"status": "ok", "retcode": 0,
                                     "data": {"message_id": 7}})
        ad = make_adapter({"forward_threshold": 0}, client=client)
        res = await ad.send("777", "y" * 9000)
        assert res.success and res.message_id == "7"
        assert len(res.continuation_message_ids) >= 1


class TestSendFailures:

    @pytest.mark.asyncio
    async def test_rejected_send_reports_a_classified_error_kind(self):
        client = FakeClient(responses=[
            {"status": "failed", "retcode": 1403, "message": "no permission"}])
        ad = make_adapter({"group_replies_enabled": True}, client=client)
        res = await ad.send("g1", "hi")
        assert res.success is False
        assert res.error_kind == "forbidden"
        assert res.error_kind in SEND_ERROR_KINDS
        assert "no permission" in res.error

    @pytest.mark.asyncio
    async def test_dead_group_is_reported_as_not_found(self):
        client = FakeClient(responses=[
            {"status": "failed", "retcode": 1200, "message": "群不存在"}])
        ad = make_adapter({"group_replies_enabled": True}, client=client)
        res = await ad.send("g404", "hi")
        assert res.success is False and res.error_kind == "not_found"

    @pytest.mark.asyncio
    async def test_bad_chat_id_never_reaches_the_wire(self):
        client = FakeClient()
        ad = make_adapter(client=client)
        res = await ad.send("not-a-chat", "hi")
        assert res.success is False and res.error_kind == "not_found"
        assert client.actions == []

    @pytest.mark.asyncio
    async def test_disconnected_adapter_reports_a_retryable_transient(self):
        ad = make_adapter()
        ad._client = None
        res = await ad.send("777", "hi")
        assert res.success is False
        assert res.error_kind == "transient" and res.retryable is True

    @pytest.mark.asyncio
    async def test_missing_ack_is_optimistic_rather_than_a_false_failure(self):
        """A backend that never echoes must not look like a delivery failure."""
        client = FakeClient()
        client.raise_on_call = asyncio.TimeoutError()
        ad = make_adapter(client=client)
        res = await ad.send("777", "hi")
        assert res.success is True and res.message_id is None

    @pytest.mark.asyncio
    async def test_fire_and_forget_mode_skips_the_ack(self):
        client = FakeClient()
        ad = make_adapter({"wait_for_send_ack": False}, client=client)
        res = await ad.send("777", "hi")
        assert res.success is True
        assert len(client.fire_and_forget) == 1

    @pytest.mark.asyncio
    async def test_send_never_raises_transport_errors_at_the_gateway(self):
        """The channel degrades to a SendResult; only tools see exceptions."""
        from plugins.platforms.onebot.client import OneBotTransportError
        client = FakeClient()
        client.raise_on_call = OneBotTransportError("socket gone")
        ad = make_adapter(client=client)
        res = await ad.send("777", "hi")
        assert res.success is False and res.retryable is True


class TestOutboundMedia:

    @pytest.mark.asyncio
    async def test_image_is_sent_inline_as_base64(self, tmp_path):
        p = tmp_path / "pic.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        client = FakeClient()
        ad = make_adapter(client=client)
        res = await ad.send_image_file("777", str(p))
        assert res.success is True
        seg = client.actions[-1].message[-1]
        assert isinstance(seg, P.ImageSegment)
        assert seg.file.startswith("base64://")

    @pytest.mark.asyncio
    async def test_voice_is_sent_inline_as_a_record_segment(self, tmp_path):
        p = tmp_path / "clip.silk"
        p.write_bytes(b"SILK" + b"0" * 32)
        client = FakeClient()
        ad = make_adapter(client=client)
        await ad.send_voice("777", str(p))
        seg = client.actions[-1].message[-1]
        assert isinstance(seg, P.RecordSegment) and seg.file.startswith("base64://")

    @pytest.mark.asyncio
    async def test_document_goes_to_the_file_area(self, tmp_path):
        p = tmp_path / "report.pdf"
        p.write_bytes(b"%PDF-1.4" + b"0" * 32)
        client = FakeClient()
        ad = make_adapter({"group_replies_enabled": True}, client=client)
        await ad.send_document("g42", str(p))
        action = client.actions[-1]
        assert isinstance(action, P.UploadGroupFile)
        assert action.name == "report.pdf" and action.file.startswith("base64://")

    @pytest.mark.asyncio
    async def test_oversized_file_falls_back_to_a_literal_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(A, "BASE64_MAX_BYTES", 16)
        p = tmp_path / "big.pdf"
        p.write_bytes(b"0" * 1024)
        client = FakeClient()
        ad = make_adapter(client=client)
        await ad.send_document("777", str(p))
        assert client.actions[-1].file == str(p)

    @pytest.mark.asyncio
    async def test_remote_image_url_is_handed_to_the_backend(self):
        client = FakeClient()
        ad = make_adapter(client=client)
        await ad.send_image("777", "https://cdn.example/pic.png")
        seg = client.actions[-1].message[-1]
        assert isinstance(seg, P.ImageSegment) and seg.url == "https://cdn.example/pic.png"

    @pytest.mark.asyncio
    async def test_unsafe_attachment_path_is_refused(self):
        client = FakeClient()
        ad = make_adapter(client=client)
        res = await ad.send_document("777", "/nonexistent/secret.pdf")
        assert res.success is False and res.error_kind == "forbidden"
        assert client.actions == []


class TestTypingIndicator:

    @pytest.mark.asyncio
    async def test_dm_typing_sends_the_napcat_extension(self):
        client = FakeClient()
        ad = make_adapter(client=client)
        await ad.send_typing("2104743984")
        assert isinstance(client.actions[0], P.SetInputStatus)
        assert client.actions[0].event_type == 1
        await ad.stop_typing("2104743984")
        assert client.actions[1].event_type == 0

    @pytest.mark.asyncio
    async def test_group_typing_is_a_noop(self):
        """QQ groups render no typing state — do not waste a frame."""
        client = FakeClient()
        ad = make_adapter(client=client)
        await ad.send_typing("g183287894")
        assert client.actions == []

    @pytest.mark.asyncio
    async def test_typing_failure_is_swallowed(self):
        class Boom(FakeClient):
            async def send_action(self, action):
                raise RuntimeError("unsupported action")
        ad = make_adapter(client=Boom())
        await ad.send_typing("777")  # must not raise


# ---------------------------------------------------------------------------
# 5. Inbound dispatch
# ---------------------------------------------------------------------------

class _Recorder:
    def __init__(self):
        self.events: List[Any] = []

    async def __call__(self, event):
        self.events.append(event)


def instrument(ad: A.OneBotAdapter) -> _Recorder:
    rec = _Recorder()
    ad.handle_message = rec  # type: ignore[assignment]
    return rec


class TestInboundDispatch:

    @pytest.mark.asyncio
    async def test_private_message_becomes_a_hermes_event(self):
        ad = make_adapter()
        rec = instrument(ad)
        await ad._on_message_event(private_event("你好"))
        await asyncio.sleep(0.05)
        assert len(rec.events) == 1
        ev = rec.events[0]
        assert ev.text == "你好"
        assert ev.source.chat_type == "dm"
        assert ev.source.chat_id == "2104743984"
        assert ev.user_name == "bob"
        assert ev.metadata["onebot_at_user_id"] == "2104743984"
        assert ev.message_type == MessageType.TEXT

    @pytest.mark.asyncio
    async def test_group_message_is_muted_by_default(self):
        ad = make_adapter()
        rec = instrument(ad)
        await ad._on_message_event(group_event(
            "@bot 在吗", segments=[P.AtSegment(qq="100"), P.TextSegment(text=" 在吗")]))
        await asyncio.sleep(0.05)
        assert rec.events == []

    @pytest.mark.asyncio
    async def test_group_mention_dispatches_once_enabled(self):
        ad = make_adapter({"group_replies_enabled": True})
        rec = instrument(ad)
        await ad._on_message_event(group_event(
            "", segments=[P.AtSegment(qq="100"), P.TextSegment(text=" 在吗")]))
        await asyncio.sleep(0.05)
        assert len(rec.events) == 1
        ev = rec.events[0]
        assert ev.source.chat_type == "group"
        assert ev.source.chat_id == "g183287894"
        # The bot's own @ is stripped from the agent-facing text.
        assert "@100" not in ev.text and "在吗" in ev.text

    @pytest.mark.asyncio
    async def test_self_sent_echo_is_ignored(self):
        """Backends with reportSelfMessage would otherwise loop forever."""
        ad = make_adapter({"group_replies_enabled": True})
        rec = instrument(ad)
        await ad._on_message_event(group_event("mine", uid=100, self_id=100))
        await asyncio.sleep(0.05)
        assert rec.events == []

    @pytest.mark.asyncio
    async def test_duplicate_message_ids_are_dropped(self):
        ad = make_adapter()
        rec = instrument(ad)
        await ad._on_message_event(private_event("hi", message_id=9))
        await ad._on_message_event(private_event("hi", message_id=9))
        await asyncio.sleep(0.05)
        assert len(rec.events) == 1

    @pytest.mark.asyncio
    async def test_reply_segment_becomes_reply_to_message_id(self):
        ad = make_adapter()
        rec = instrument(ad)
        ev = private_event("answer")
        ev.message = [P.ReplySegment(id="4321"), P.TextSegment(text="answer")]
        await ad._on_message_event(ev)
        await asyncio.sleep(0.05)
        assert rec.events[0].reply_to_message_id == "4321"

    @pytest.mark.asyncio
    async def test_group_chatter_feeds_the_context_buffer_before_the_gate(self):
        """The persona should see the whole room, not just what it answered."""
        ad = make_adapter()  # groups muted
        instrument(ad)
        await ad._on_message_event(group_event("闲聊一句"))
        await asyncio.sleep(0.05)
        buf = A.recent_group_messages("default", 183287894)
        assert len(buf) == 1
        assert buf[0][1] == "alice" and "闲聊一句" in buf[0][2] and buf[0][3] is False

    @pytest.mark.asyncio
    async def test_outbound_group_messages_are_recorded_as_self(self):
        client = FakeClient()
        ad = make_adapter({"group_replies_enabled": True}, client=client)
        await ad.send("g183287894", "我的回复")
        buf = A.recent_group_messages("default", 183287894)
        assert buf and buf[-1][3] is True and buf[-1][2] == "我的回复"


class TestGroupSpeechCap:
    """A hard budget shared with any future proactive speaking."""

    @pytest.mark.asyncio
    async def test_cap_drops_over_budget_even_for_mentions(self):
        ad = make_adapter({
            "group_replies_enabled": True,
            "group_rate_limit_window_minutes": 3,
            "group_rate_limit_max_messages": 2,
        })
        rec = instrument(ad)
        for i in range(4):
            await ad._on_message_event(group_event(
                "", message_id=i, uid=1000 + i,
                segments=[P.AtSegment(qq="100"), P.TextSegment(text=f" 在吗{i}")]))
        await asyncio.sleep(0.05)
        assert len(rec.events) == 2

    @pytest.mark.asyncio
    async def test_cap_is_disabled_by_default(self):
        ad = make_adapter({"group_replies_enabled": True})
        rec = instrument(ad)
        for i in range(4):
            await ad._on_message_event(group_event(
                "", message_id=i, uid=1000 + i,
                segments=[P.AtSegment(qq="100"), P.TextSegment(text=f" 在吗{i}")]))
        await asyncio.sleep(0.05)
        assert len(rec.events) == 4

    @pytest.mark.asyncio
    async def test_slash_commands_are_never_locked_out_by_the_cap(self):
        """Operator tooling must survive a chatty group."""
        ad = make_adapter({
            "group_replies_enabled": True,
            "group_rate_limit_window_minutes": 3,
            "group_rate_limit_max_messages": 1,
        })
        rec = instrument(ad)
        await ad._on_message_event(group_event(
            "", message_id=1, segments=[P.AtSegment(qq="100"), P.TextSegment(text=" hi")]))
        for i in range(3):
            await ad._on_message_event(group_event("/status", message_id=10 + i))
        await asyncio.sleep(0.05)
        assert len(rec.events) == 4

    @pytest.mark.asyncio
    async def test_cap_is_per_group(self):
        ad = make_adapter({
            "group_replies_enabled": True,
            "group_rate_limit_window_minutes": 3,
            "group_rate_limit_max_messages": 1,
        })
        rec = instrument(ad)
        for gid in (111, 222):
            for i in range(2):
                await ad._on_message_event(group_event(
                    "", gid=gid, message_id=gid + i,
                    segments=[P.AtSegment(qq="100"), P.TextSegment(text=" hi")]))
        await asyncio.sleep(0.05)
        assert len(rec.events) == 2

    def test_budget_is_shared_through_a_module_level_api(self):
        """A proactive job must spend from the same budget, not a second one."""
        assert A.group_speech_allowed("default", 42, 180.0, 1) is True
        assert A.group_speech_allowed("default", 42, 180.0, 1) is False
        assert A.group_speech_allowed("default", 42, 180.0, 1, record=False) is False


# ---------------------------------------------------------------------------
# 6. Chat info / directory
# ---------------------------------------------------------------------------

class TestChatInfo:

    @pytest.mark.asyncio
    async def test_group_info(self):
        client = FakeClient(responses=[{"status": "ok", "retcode": 0, "data": {
            "group_id": 183287894, "group_name": "格兰的群", "member_count": 42}}])
        ad = make_adapter(client=client)
        info = await ad.get_chat_info("g183287894")
        assert info == {"name": "格兰的群", "type": "group",
                        "id": "g183287894", "member_count": 42}

    @pytest.mark.asyncio
    async def test_dm_info(self):
        client = FakeClient(responses=[{"status": "ok", "retcode": 0,
                                        "data": {"nickname": "bob"}}])
        ad = make_adapter(client=client)
        info = await ad.get_chat_info("2104743984")
        assert info["name"] == "bob" and info["type"] == "dm"

    @pytest.mark.asyncio
    async def test_falls_back_when_the_lookup_fails(self):
        client = FakeClient()
        client.raise_on_call = RuntimeError("nope")
        ad = make_adapter(client=client)
        info = await ad.get_chat_info("g1")
        assert info["type"] == "group" and info["id"] == "g1"

    @pytest.mark.asyncio
    async def test_bad_chat_id_still_returns_a_dict(self):
        ad = make_adapter()
        assert (await ad.get_chat_info("garbage"))["name"] == "garbage"


class TestListChannels:

    @pytest.mark.asyncio
    async def test_lists_groups_and_friends(self):
        client = FakeClient(responses=[
            {"status": "ok", "retcode": 0,
             "data": [{"group_id": 1, "group_name": "G1"}]},
            {"status": "ok", "retcode": 0,
             "data": [{"user_id": 2, "nickname": "N", "remark": "R"}]},
        ])
        ad = make_adapter(client=client)
        channels = await ad.list_channels()
        assert {"id": "g1", "name": "G1", "type": "group"} in channels
        assert {"id": "2", "name": "R", "type": "dm"} in channels

    @pytest.mark.asyncio
    async def test_returns_none_when_the_link_is_down(self):
        """None keeps the directory's existing entries; [] would wipe them."""
        ad = make_adapter()
        ad._client = None
        assert await ad.list_channels() is None


# ---------------------------------------------------------------------------
# 7. Standalone (out-of-process) delivery
# ---------------------------------------------------------------------------

class TestStandaloneSend:

    @pytest.mark.asyncio
    async def test_requires_a_ws_url(self, monkeypatch):
        monkeypatch.delenv("ONEBOT_WS_URL", raising=False)
        res = await A._standalone_send(
            PlatformConfig(enabled=True, extra={}), "777", "hi")
        assert "error" in res

    @pytest.mark.asyncio
    async def test_rejects_a_bad_chat_id(self):
        res = await A._standalone_send(
            PlatformConfig(enabled=True, extra={"ws_url": "ws://127.0.0.1:1"}),
            "nope", "hi")
        assert "error" in res

    @pytest.mark.asyncio
    async def test_sends_chunked_text_over_a_throwaway_connection(self):
        import json as _json
        from contextlib import asynccontextmanager
        from websockets.asyncio.server import serve

        received: List[dict] = []

        async def handler(ws):
            try:
                async for raw in ws:
                    received.append(_json.loads(raw))
            except Exception:
                pass

        server = await serve(handler, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            res = await A._standalone_send(
                PlatformConfig(enabled=True,
                               extra={"ws_url": f"ws://127.0.0.1:{port}",
                                      "group_replies_enabled": True}),
                "g183287894", "y" * 9000)
        finally:
            server.close()
            await server.wait_closed()
        assert res.get("success") is True
        assert len(received) > 1  # chunked
        assert received[0]["action"] == "send_group_msg"
        assert received[0]["params"]["group_id"] == 183287894
