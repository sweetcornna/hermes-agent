"""Reply-gating tests for the OneBot (QQ) platform plugin.

These pin the semantics the port had to choose between, one test per
decision, because the wrong choice is silent in production:

* no keyword list configured ⇒ **mention-only** (not reply-to-everything);
* an @mention does **not** bypass the group whitelist;
* an @mention does **not** bypass the hard speech cap (that lives in the
  adapter, see ``test_onebot_plugin.py``) but **does** reset the cooldown;
* the live ``self_id`` on an event outranks the configured seed list;
* rate limits run AFTER the gate, so filtered messages cost nothing.
"""

from __future__ import annotations

import json
from typing import List, Optional

import pytest

from plugins.platforms.onebot.protocol import (
    AtSegment,
    MessageEvent,
    MessageType,
    Sender,
    TextSegment,
)
from plugins.platforms.onebot.rate_limit import TokenBucket
from plugins.platforms.onebot.router import (
    ChannelRouter,
    looks_like_command,
    parse_group_keywords,
)


def _group_event(raw: str, segs: List, gid: int, *, user_id: int = 200,
                 self_id: int = 100, message_id: int = 1) -> MessageEvent:
    return MessageEvent(
        self_id=self_id, message_type=MessageType.GROUP, sub_type="normal",
        group_id=gid, user_id=user_id, message_id=message_id, message=segs,
        raw_message=raw, time=1_700_000_000, sender=Sender(),
    )


def _private_event(text: str = "hi", *, self_id: int = 100, user_id: int = 77) -> MessageEvent:
    return MessageEvent(
        self_id=self_id, message_type=MessageType.PRIVATE, sub_type="friend",
        group_id=None, user_id=user_id, message_id=1,
        message=[TextSegment(text=text)], raw_message=text, time=1, sender=Sender(),
    )


def _enabled(**kw) -> ChannelRouter:
    """A router with groups switched ON — the default is off by design."""
    kw.setdefault("group_replies_enabled", True)
    kw.setdefault("self_ids", [100])
    return ChannelRouter(**kw)


# ---------------------------------------------------------------------------
# parse_group_keywords
# ---------------------------------------------------------------------------

class TestParseGroupKeywords:

    def test_parses_json_map(self):
        m = parse_group_keywords('{"123":["a","b"],"456":["c"]}')
        assert m == {"123": ["a", "b"], "456": ["c"]}

    def test_empty_input_returns_empty_map(self):
        assert parse_group_keywords("") == {}
        assert parse_group_keywords("   ") == {}

    def test_non_object_payload_raises(self):
        with pytest.raises(json.JSONDecodeError):
            parse_group_keywords("[1,2,3]")

    def test_non_array_value_raises(self):
        with pytest.raises(json.JSONDecodeError):
            parse_group_keywords('{"123": "格兰"}')

    def test_keys_and_values_coerced_to_str(self):
        m = parse_group_keywords('{"123":[456]}')
        assert m == {"123": ["456"]}


class TestLooksLikeCommand:

    @pytest.mark.parametrize("text", ["/status", "  /help me", "/new"])
    def test_command_shapes(self, text):
        assert looks_like_command(text)

    @pytest.mark.parametrize("text", ["", "hello", "/", "//x", "/1234", "a /status"])
    def test_non_command_shapes(self, text):
        assert not looks_like_command(text)


# ---------------------------------------------------------------------------
# The master switch
# ---------------------------------------------------------------------------

class TestGroupMasterSwitch:

    def test_disabled_by_default(self):
        """Off unless someone turns it on — this reproduces production."""
        assert ChannelRouter().group_replies_enabled is False

    def test_disabled_drops_everything_in_groups_including_mentions(self):
        router = ChannelRouter(group_keywords={}, self_ids=[100],
                               group_replies_enabled=False)
        assert router.dispatch(_group_event("闲聊", [TextSegment(text="闲聊")], 9999)) is None
        mention = _group_event("@bot help",
                               [AtSegment(qq="100"), TextSegment(text=" help")], 9999)
        assert router.dispatch(mention) is None
        # ...but private chat is untouched.
        assert router.dispatch(_private_event()) is not None

    def test_disabled_blocks_slash_commands_in_groups_too(self):
        router = ChannelRouter(self_ids=[100], group_replies_enabled=False)
        assert router.dispatch(_group_event("/status", [TextSegment(text="/status")], 1)) is None


# ---------------------------------------------------------------------------
# Whitelist — a hard gate
# ---------------------------------------------------------------------------

class TestWhitelist:

    def test_whitelist_blocks_even_mentions(self):
        router = _enabled(group_whitelist=frozenset({"111"}))
        blocked = _group_event("@bot help",
                               [AtSegment(qq="100"), TextSegment(text=" help")], 9999)
        assert router.dispatch(blocked) is None
        allowed = _group_event("@bot help",
                               [AtSegment(qq="100"), TextSegment(text=" help")], 111)
        assert router.dispatch(allowed) is not None

    def test_empty_whitelist_blocks_all_groups(self):
        """Set-but-empty is 'nobody', not 'everybody'."""
        router = _enabled(group_whitelist=frozenset())
        ev = _group_event("@bot hi", [AtSegment(qq="100"), TextSegment(text=" hi")], 42)
        assert router.dispatch(ev) is None

    def test_none_whitelist_allows_every_group(self):
        router = _enabled(group_whitelist=None, group_reply_policy="all")
        assert router.dispatch(_group_event("hi", [TextSegment(text="hi")], 42)) is not None

    def test_whitelist_blocks_slash_commands(self):
        router = _enabled(group_whitelist=frozenset({"111"}))
        ev = _group_event("/status", [TextSegment(text="/status")], 9999)
        assert router.dispatch(ev) is None


# ---------------------------------------------------------------------------
# Reply policy
# ---------------------------------------------------------------------------

class TestReplyPolicy:

    def test_default_policy_is_mention_only_without_keywords(self):
        router = _enabled(group_keywords={})
        assert router.dispatch(_group_event("闲聊", [TextSegment(text="闲聊")], 9999)) is None
        mention = _group_event("@bot 在吗",
                               [AtSegment(qq="100"), TextSegment(text=" 在吗")], 9999)
        assert router.dispatch(mention) is not None

    def test_default_policy_honours_explicit_keywords(self):
        router = _enabled(group_keywords={"9999": ["格兰"]})
        assert router.dispatch(_group_event("格兰在吗", [TextSegment(text="格兰在吗")], 9999)) is not None
        assert router.dispatch(_group_event("吃了吗", [TextSegment(text="吃了吗")], 9999)) is None

    def test_legacy_all_policy_replies_without_keywords(self):
        router = _enabled(group_keywords={}, group_reply_policy="all")
        assert router.dispatch(_group_event("随便", [TextSegment(text="随便")], 9999)) is not None

    def test_legacy_all_policy_still_honours_configured_keywords(self):
        router = _enabled(group_keywords={"9999": ["格兰"]}, group_reply_policy="all")
        assert router.dispatch(_group_event("格兰", [TextSegment(text="格兰")], 9999)) is not None
        assert router.dispatch(_group_event("nope", [TextSegment(text="nope")], 9999)) is None

    def test_keyword_match_is_case_insensitive(self):
        router = _enabled(group_keywords={"123": ["格兰", "Aemeath"]})
        assert router.dispatch(_group_event("hey AEMEATH are you there", [], 123)) is not None
        assert router.dispatch(_group_event("irrelevant chatter", [], 123)) is None

    def test_mention_bypasses_keyword_filter(self):
        router = _enabled(group_keywords={"123": ["never_matches"]})
        req = router.dispatch(_group_event(
            "[CQ:at,qq=100] help", [AtSegment(qq="100"), TextSegment(text=" help")], 123))
        assert req is not None and req.mentioned is True

    def test_slash_command_counts_as_an_explicit_summons(self):
        """Without this, '/status' in a group is eaten by the keyword gate."""
        router = _enabled(group_keywords={"123": ["never_matches"]})
        req = router.dispatch(_group_event("/status", [TextSegment(text="/status")], 123))
        assert req is not None and req.is_command is True


class TestCooldown:

    def test_cooldown_gates_keyword_replies_but_not_mentions(self):
        router = _enabled(group_keywords={"9999": ["天气"]},
                          group_reply_cooldown_secs=3600.0)
        assert router.dispatch(_group_event("天气咋样", [TextSegment(text="天气咋样")], 9999)) is not None
        assert router.dispatch(_group_event("又问天气", [TextSegment(text="又问天气")], 9999)) is None
        mention = _group_event("@bot 天气",
                               [AtSegment(qq="100"), TextSegment(text=" 天气")], 9999)
        assert router.dispatch(mention) is not None

    def test_mention_resets_the_cooldown_clock(self):
        """An @ is answered AND restarts the quiet period."""
        router = _enabled(group_keywords={"1": ["kw"]}, group_reply_cooldown_secs=3600.0)
        mention = _group_event("@bot", [AtSegment(qq="100"), TextSegment(text=" kw")], 1)
        assert router.dispatch(mention) is not None
        # The keyword reply that follows is inside the window the @ reset.
        assert router.dispatch(_group_event("kw again", [TextSegment(text="kw again")], 1)) is None

    def test_cooldown_is_per_group(self):
        router = _enabled(group_keywords={"1": ["kw"], "2": ["kw"]},
                          group_reply_cooldown_secs=3600.0)
        assert router.dispatch(_group_event("kw", [TextSegment(text="kw")], 1)) is not None
        assert router.dispatch(_group_event("kw", [TextSegment(text="kw")], 2)) is not None


# ---------------------------------------------------------------------------
# self_id resolution
# ---------------------------------------------------------------------------

class TestSelfIdResolution:

    def test_event_self_id_is_authoritative_and_does_not_mutate_config(self):
        router = _enabled(group_keywords={"123": ["never_matches"]}, self_ids=[])
        req = router.dispatch(_group_event(
            "[CQ:at,qq=999] help",
            [AtSegment(qq="999"), TextSegment(text=" help")], 123, self_id=999))
        assert req is not None and req.mentioned is True
        assert router.self_ids == []

    def test_event_self_id_replaces_a_stale_seed(self):
        router = _enabled(group_keywords={"9999": ["never_matches"]}, self_ids=[100])
        stale = _group_event("[CQ:at,qq=100] help",
                             [AtSegment(qq="100"), TextSegment(text=" help")],
                             9999, self_id=200)
        assert router.dispatch(stale) is None
        current = _group_event("[CQ:at,qq=200] help",
                               [AtSegment(qq="200"), TextSegment(text=" help")],
                               9999, self_id=200)
        assert router.dispatch(current) is not None

    def test_zero_self_id_falls_back_to_configured_seed(self):
        router = _enabled(group_keywords={"123": ["never_matches"]}, self_ids=[100])
        req = router.dispatch(_group_event(
            "[CQ:at,qq=100] help",
            [AtSegment(qq="100"), TextSegment(text=" help")], 123, self_id=0))
        assert req is not None and req.mentioned is True


# ---------------------------------------------------------------------------
# Basics
# ---------------------------------------------------------------------------

class TestBasics:

    def test_private_message_always_dispatches(self):
        router = _enabled(group_keywords={}, group_replies_enabled=False)
        req = router.dispatch(_private_event("hi", user_id=77))
        assert req is not None
        assert req.binding.channel == "onebot"
        assert req.binding.thread == "77" and req.binding.sender == "77"
        assert req.binding.is_group is False

    def test_group_binding_fields(self):
        router = _enabled(group_reply_policy="all")
        req = router.dispatch(_group_event("hi", [TextSegment(text="hi")], 321, user_id=200))
        assert req is not None
        assert (req.binding.account, req.binding.thread, req.binding.sender) == ("100", "321", "200")
        assert req.binding.is_group is True

    def test_empty_group_message_drops(self):
        router = _enabled(group_reply_policy="all")
        assert router.dispatch(_group_event("", [], 123)) is None

    def test_session_key_stable_across_events(self):
        router = _enabled(group_reply_policy="all")
        r1 = router.dispatch(_group_event("一号", [TextSegment(text="一号")], 321))
        r2 = router.dispatch(_group_event("二号", [TextSegment(text="二号")], 321))
        assert r1 is not None and r2 is not None
        assert r1.session_key == r2.session_key

    def test_raw_message_is_preferred_for_routing_text(self):
        router = _enabled(group_reply_policy="all")
        req = router.dispatch(_group_event("RAW", [TextSegment(text="SEGMENTS")], 1))
        assert req is not None and req.content == "RAW"

    def test_segments_used_when_raw_message_absent(self):
        router = _enabled(group_reply_policy="all")
        req = router.dispatch(_group_event("", [TextSegment(text="SEGMENTS")], 1))
        assert req is not None and req.content == "SEGMENTS"

    def test_group_id_none_drops(self):
        router = _enabled(group_reply_policy="all")
        ev = _group_event("hi", [TextSegment(text="hi")], 1)
        ev.group_id = None
        assert router.dispatch(ev) is None


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def _count_hook():
    calls: List[tuple] = []
    return calls, lambda channel, reason: calls.append((channel, reason))


class TestRateLimitDispatch:

    def test_drops_when_group_over_limit(self):
        calls, hook = _count_hook()
        router = _enabled(group_reply_policy="all").with_rate_limits(
            TokenBucket.per_minute(1), None).with_rate_limit_hook(hook)
        assert router.dispatch(_group_event("m1", [TextSegment(text="m1")], 555)) is not None
        assert router.dispatch(_group_event("m2", [TextSegment(text="m2")], 555)) is None
        assert calls == [("onebot", "group")]

    def test_drops_when_sender_over_limit(self):
        calls, hook = _count_hook()
        router = _enabled(group_reply_policy="all").with_rate_limits(
            None, TokenBucket.per_minute(1)).with_rate_limit_hook(hook)
        assert router.dispatch(_group_event("hi", [TextSegment(text="hi")], 777)) is not None
        assert router.dispatch(_group_event("hi2", [TextSegment(text="hi2")], 777)) is None
        assert calls == [("onebot", "sender")]

    def test_rate_limit_buckets_do_not_cross_groups(self):
        router = _enabled(group_reply_policy="all").with_rate_limits(
            TokenBucket.per_minute(1), None)
        assert router.dispatch(_group_event("m", [TextSegment(text="m")], 1)) is not None
        assert router.dispatch(_group_event("m", [TextSegment(text="m")], 1)) is None
        assert router.dispatch(_group_event("m", [TextSegment(text="m")], 2)) is not None

    def test_filtered_messages_do_not_consume_tokens(self):
        """Order matters: the gate runs first, so noise costs no budget."""
        bucket = TokenBucket.per_minute(1)
        router = _enabled(group_keywords={"5": ["格兰"]}).with_rate_limits(bucket, None)
        for _ in range(10):
            assert router.dispatch(_group_event("noise", [TextSegment(text="noise")], 5)) is None
        # The single token is still there for the message that matters.
        assert router.dispatch(_group_event("格兰", [TextSegment(text="格兰")], 5)) is not None

    def test_rate_limit_hook_failure_never_blocks_dispatch(self):
        def boom(channel, reason):
            raise RuntimeError("observability is not load-bearing")
        router = _enabled(group_reply_policy="all").with_rate_limits(
            TokenBucket.per_minute(1), None).with_rate_limit_hook(boom)
        assert router.dispatch(_group_event("m1", [TextSegment(text="m1")], 1)) is not None
        assert router.dispatch(_group_event("m2", [TextSegment(text="m2")], 1)) is None
