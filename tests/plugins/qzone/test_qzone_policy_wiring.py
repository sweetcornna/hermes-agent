"""Proves the ported Tencent content policy (``plugins/qzone/policy.py``)
is actually wired into the four call sites, not just importable:

1. ``qzone_publish`` — outbound 说说 body (``TestOutboundPublishBodyModeration``).
2. ``qzone_publish`` — image-generation prompt (``TestImageGenerationPromptModeration``).
3. ``qzone_post_comment`` — outbound comment body (``TestCommentBodyModeration``).
4. ``qzone_list_feed`` / ``qzone_get_post`` (+ ``qzone_list_friends``) —
   inbound feed text redaction (``TestInboundFeedRedaction``).

Plus the fail-closed contract (``TestFailClosed``) and the requirement that
a policy refusal must be distinguishable from a transport failure and must
NOT poison the S17 retry ledger (``TestPolicyRefusalDoesNotPoisonRetryLedger``
— see ``plugins/qzone/state.py`` and C3 notes §2 "S17").

No network: transport and OneBot are injected callables, same pattern as
the other ``tests/plugins/qzone/*`` files.
"""

from __future__ import annotations

import json

import pytest

from plugins.qzone import feed, policy, publish, state
from plugins.qzone.client import HttpResponse, QZoneError
from plugins.qzone.feed import (
    handle_qzone_get_post,
    handle_qzone_list_feed,
    handle_qzone_list_friends,
    handle_qzone_post_comment,
)
from plugins.qzone.publish import handle_qzone_publish

# Deterministically fires the "tencent.freeze-evasion" rule (same phrase
# locked by tests/plugins/qzone/test_qzone_policy.py, thematically apt for
# a QZone-freeze-risk test corpus).
_BLOCKED_TEXT = "怎么绕过QQ风控和冻结审核"
_CLEAN_TEXT = "今天天气真好"

_COOKIE = "uin=o10001; skey=@abcDEF; p_skey=PpKkEeYy"
_UPLOAD_OK = (
    b'frameElement.callback({"ret":0,"data":{"albumid":"a1","lloc":"l1",'
    b'"sloc":"s1","type":0,"width":800,"height":600,"url":"u"}});'
)
_PUBLISH_OK = b'{"code":0,"subcode":0,"tid":"1cbe3d3c17","feedinfo":"<li>"}'
_COMMENT_OK = b'<script>frameElement.callback({"code":0,"subcode":0});</script>'

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("QZONE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("QZONE_PERSONA_ID", "grantley")
    monkeypatch.delenv("QZONE_QQ_INSTANCE_ID", raising=False)
    return tmp_path / "state"


def _onebot(cookies=_COOKIE, user_id=10001, friends=None):
    def _call(action, params=None, **_kw):
        if action == "get_login_info":
            return {"user_id": user_id}
        if action == "get_cookies":
            return {"cookies": cookies}
        if action == "get_friend_list":
            return friends if friends is not None else []
        raise AssertionError(action)

    return _call


def _publish_transport(*, upload=_UPLOAD_OK, publish_body=_PUBLISH_OK):
    calls = []

    def _t(method, url, headers, body, timeout):
        calls.append({"url": url, "body": body})
        if "cgi_upload_image" in url:
            return HttpResponse(200, upload)
        if "emotion_cgi_publish_v6" in url:
            return HttpResponse(200, publish_body)
        raise AssertionError(f"unexpected url {url}")

    _t.calls = calls
    return _t


def _comment_transport(*, comment=_COMMENT_OK):
    calls = []

    def _t(method, url, headers, body, timeout):
        calls.append({"url": url, "body": body})
        if "emotion_cgi_re_feeds" in url:
            return HttpResponse(200, comment)
        raise AssertionError(f"unexpected url {url}")

    _t.calls = calls
    return _t


def _feed_transport(feed_html: str):
    body = '_Callback({"code":0,"message":"","data":{"data":"' + feed_html + '"}});'

    def _t(method, url, headers, body_bytes, timeout):
        if "feeds3_html_more" in url:
            return HttpResponse(200, body.encode())
        raise AssertionError(f"unexpected url {url}")

    return _t


def _run_publish(args, **kw):
    kw.setdefault("onebot_call", _onebot())
    kw.setdefault("transport", _publish_transport())
    return json.loads(handle_qzone_publish(args, **kw))


def _run_comment(args, **kw):
    kw.setdefault("onebot_call", _onebot())
    kw.setdefault("transport", _comment_transport())
    return json.loads(handle_qzone_post_comment(args, **kw))


# ---------------------------------------------------------------------------
# 1. qzone_publish — outbound 说说 body
# ---------------------------------------------------------------------------


class TestOutboundPublishBodyModeration:
    def test_blocked_text_never_reaches_transport(self):
        transport = _publish_transport()
        out = _run_publish({"text": _BLOCKED_TEXT}, transport=transport)
        assert out["code"] == "content_policy_blocked"
        assert transport.calls == [], "a refused body must never be sent"
        assert "tencent_freeze_risk" in out["category_codes"]
        assert out["rule_ids"]
        assert out["ruleset_version"] == policy.RULESET_VERSION

    def test_allowed_text_is_unaffected(self):
        out = _run_publish({"text": _CLEAN_TEXT})
        assert out["success"] is True

    def test_policy_resolver_seam_can_disable_moderation_for_tests(self):
        """Mirrors corlinman's own test pattern: policy_resolver=lambda: False
        is the only way to bypass the check, and it is a kwarg the model
        schema never exposes — see plugins/qzone/policy.py:resolve_config."""
        transport = _publish_transport()
        out = _run_publish(
            {"text": _BLOCKED_TEXT}, transport=transport, policy_resolver=lambda: False
        )
        assert out["success"] is True
        assert transport.calls, "policy disabled means the network call happens"


# ---------------------------------------------------------------------------
# 2. qzone_publish — image-generation prompt
# ---------------------------------------------------------------------------


class TestImageGenerationPromptModeration:
    def test_blocked_prompt_is_refused_even_with_clean_body_text(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            publish, "_generate_image", lambda *a, **k: called.append(1) or (b"x", "g.png")
        )
        out = _run_publish({"text": _CLEAN_TEXT, "generate": _BLOCKED_TEXT})
        assert out["code"] == "content_policy_blocked"
        assert not called, "image generation must not run once the prompt is refused"

    def test_blocked_prompt_with_no_body_text_is_also_refused(self):
        out = _run_publish({"generate": _BLOCKED_TEXT})
        assert out["code"] == "content_policy_blocked"

    def test_clean_prompt_still_hits_the_media_deny_by_default(self, monkeypatch):
        """No media classifier is wired in this port (same as corlinman's own
        qzone dispatcher — see C3 notes judgement-call log): moderate_media()
        is always called with classified_safe=None, so an allowed *prompt*
        still cannot generate an image; text-only publish proceeds instead."""
        called = []
        monkeypatch.setattr(
            publish, "_generate_image", lambda *a, **k: called.append(1) or (b"x", "g.png")
        )
        transport = _publish_transport()
        out = _run_publish(
            {"text": _CLEAN_TEXT, "generate": "一只猫"}, transport=transport
        )
        assert out["success"] is True
        assert out["images"] == 0
        assert not called, "media is denied by default, so generation is skipped"
        assert all("cgi_upload_image" not in c["url"] for c in transport.calls)


# ---------------------------------------------------------------------------
# Media deny-by-default (fail-closed on unclassified media, requirement 4)
# ---------------------------------------------------------------------------


class TestMediaDeniedByDefault:
    def test_images_are_dropped_when_text_is_present(self, tmp_path):
        path = tmp_path / "a.png"
        path.write_bytes(_PNG)
        transport = _publish_transport()
        out = _run_publish({"text": _CLEAN_TEXT, "images": [str(path)]}, transport=transport)
        assert out["success"] is True
        assert out["images"] == 0
        assert all("cgi_upload_image" not in c["url"] for c in transport.calls), (
            "unclassified media must never be uploaded"
        )

    def test_images_with_no_text_are_refused_outright(self, tmp_path):
        path = tmp_path / "a.png"
        path.write_bytes(_PNG)
        transport = _publish_transport()
        out = _run_publish({"images": [str(path)]}, transport=transport)
        assert out["code"] == "content_policy_blocked"
        assert transport.calls == []

    def test_disabling_policy_allows_images_through(self, tmp_path):
        path = tmp_path / "a.png"
        path.write_bytes(_PNG)
        transport = _publish_transport()
        out = _run_publish(
            {"text": _CLEAN_TEXT, "images": [str(path)]},
            transport=transport,
            policy_resolver=lambda: False,
        )
        assert out["success"] is True
        assert out["images"] == 1


# ---------------------------------------------------------------------------
# 3. qzone_post_comment — outbound comment body
# ---------------------------------------------------------------------------


class TestCommentBodyModeration:
    def test_blocked_comment_never_reaches_transport(self):
        transport = _comment_transport()
        out = _run_comment(
            {"owner_uin": "10001", "tid": "abc", "content": _BLOCKED_TEXT},
            transport=transport,
        )
        assert out["code"] == "content_policy_blocked"
        assert transport.calls == []

    def test_final_mention_prefixed_content_is_what_gets_checked(self):
        """The @mention is part of the body on QZone (no separate field);
        moderation must run on final_content, matching comment.py:676."""
        out = _run_comment(
            {
                "owner_uin": "10001",
                "tid": "abc",
                "content": _BLOCKED_TEXT,
                "reply_to_uin": "20002",
                "reply_to_name": "好友A",
            }
        )
        assert out["code"] == "content_policy_blocked"

    def test_allowed_comment_is_unaffected(self):
        out = _run_comment({"owner_uin": "10001", "tid": "abc", "content": _CLEAN_TEXT})
        assert out["success"] is True


# ---------------------------------------------------------------------------
# 4. Inbound feed text redaction
# ---------------------------------------------------------------------------


def _feed_html(content: str, name: str = "测试昵称", comment_content: str = "") -> str:
    comment = ""
    if comment_content:
        comment = (
            '<li class=\\"comments-item\\" data-tid=\\"c1\\" data-uin=\\"20002\\" '
            'data-nick=\\"好友A\\"><a class=\\"comments-name\\">好友A<\\/a>'
            f'&nbsp; : {comment_content}<div class=\\"comments-op\\">回复<\\/div><\\/li>'
        )
    return (
        '<li class=\\"f-single nopic\\" id=\\"fct_10001_abc\\" data-tid=\\"deadbeef\\">'
        f'<a class=\\"f-name q_namecard\\" target=\\"_blank\\">{name}<\\/a>'
        f'<div class=\\"f-info\\">{content}<\\/div>'
        '<span class=\\"state\\">3小时前<\\/span>'
        f"{comment}"
        "<\\/li>"
    )


class TestInboundFeedRedaction:
    def test_list_feed_redacts_blocked_post_content(self):
        out = json.loads(
            handle_qzone_list_feed(
                {},
                onebot_call=_onebot(),
                transport=_feed_transport(_feed_html(_BLOCKED_TEXT)),
            )
        )
        assert out["success"] is True
        assert out["feed"][0]["content"] == "[内容已按 QQ 风控策略隐藏]"
        assert out["policy_redactions"].get("tencent_freeze_risk", 0) >= 1

    def test_list_feed_redacts_blocked_comment_content(self):
        out = json.loads(
            handle_qzone_list_feed(
                {},
                onebot_call=_onebot(),
                transport=_feed_transport(
                    _feed_html(_CLEAN_TEXT, comment_content=_BLOCKED_TEXT)
                ),
            )
        )
        comment = out["feed"][0]["comments"][0]
        assert comment["content"] == "[内容已按 QQ 风控策略隐藏]"

    def test_list_feed_leaves_clean_feeds_untouched(self):
        out = json.loads(
            handle_qzone_list_feed(
                {},
                onebot_call=_onebot(),
                transport=_feed_transport(_feed_html(_CLEAN_TEXT)),
            )
        )
        assert out["feed"][0]["content"] == _CLEAN_TEXT
        assert out["policy_redactions"] == {}

    def test_get_post_redacts_the_matched_post(self):
        out = json.loads(
            handle_qzone_get_post(
                {"tid": "deadbeef"},
                onebot_call=_onebot(),
                transport=_feed_transport(_feed_html(_BLOCKED_TEXT)),
            )
        )
        assert out["found"] is True
        assert out["post"]["content"] == "[内容已按 QQ 风控策略隐藏]"
        assert out["policy_redactions"].get("tencent_freeze_risk", 0) >= 1

    def test_list_friends_redacts_blocked_nickname(self):
        out = json.loads(
            handle_qzone_list_friends(
                {},
                onebot_call=_onebot(
                    friends=[{"user_id": 20002, "nickname": _BLOCKED_TEXT, "remark": ""}]
                ),
            )
        )
        assert out["friends"][0]["nickname"] == "[内容已按 QQ 风控策略隐藏]"


# ---------------------------------------------------------------------------
# Fail-closed: an internal classifier error must never allow content through
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_publish_text_moderation_exception_is_refused(self, monkeypatch):
        monkeypatch.setattr(
            policy, "moderate_text", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        transport = _publish_transport()
        out = _run_publish({"text": _CLEAN_TEXT}, transport=transport)
        assert out["code"] == "content_policy_blocked"
        assert transport.calls == []

    def test_publish_media_moderation_exception_is_refused(self, tmp_path, monkeypatch):
        path = tmp_path / "a.png"
        path.write_bytes(_PNG)
        monkeypatch.setattr(
            policy, "moderate_media", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        transport = _publish_transport()
        out = _run_publish({"text": _CLEAN_TEXT, "images": [str(path)]}, transport=transport)
        assert out["code"] == "content_policy_blocked"
        assert transport.calls == []

    def test_comment_moderation_exception_is_refused(self, monkeypatch):
        monkeypatch.setattr(
            policy, "moderate_text", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        transport = _comment_transport()
        out = _run_comment(
            {"owner_uin": "10001", "tid": "abc", "content": _CLEAN_TEXT}, transport=transport
        )
        assert out["code"] == "content_policy_blocked"
        assert transport.calls == []

    def test_redaction_exception_redacts_rather_than_leaking(self, monkeypatch):
        """_redact_feeds catches internally (feed.py, ported from
        comment.py:411-414) — a classifier crash must still redact the
        field, not pass the original text through."""
        monkeypatch.setattr(
            policy, "moderate_text", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        out = json.loads(
            handle_qzone_list_feed(
                {},
                onebot_call=_onebot(),
                transport=_feed_transport(_feed_html(_CLEAN_TEXT)),
            )
        )
        assert out["feed"][0]["content"] == "[内容已按 QQ 风控策略隐藏]"


# ---------------------------------------------------------------------------
# A policy refusal must be distinguishable from a transport failure and
# must NOT poison the S17 retry ledger — this is the requirement the task
# calls out explicitly (see plugins/qzone/state.py's unknown-is-terminal
# write-outcome semantics).
# ---------------------------------------------------------------------------


class TestPolicyRefusalDoesNotPoisonRetryLedger:
    def test_publish_refusal_writes_nothing_to_the_post_log(self):
        _run_publish({"text": _BLOCKED_TEXT})
        assert state.post_log_entries() == []

    def test_publish_refusal_code_is_not_the_unknown_family(self):
        out = _run_publish({"text": _BLOCKED_TEXT})
        assert out["code"] not in (
            "qzone_publish_unknown",
            "qzone_publish_unknown_pending",
        )
        assert out["code"] == "content_policy_blocked"

    def test_retry_after_a_publish_refusal_is_free(self):
        """A blocked attempt must not trip the unknown-publish guard — that
        guard exists for transport failures, not policy refusals."""
        _run_publish({"text": _BLOCKED_TEXT})
        assert state.unknown_publish_guard(_BLOCKED_TEXT) is None
        # And once policy is bypassed for the test, the identical text can
        # still be published — nothing about the earlier refusal blocks it.
        out = _run_publish({"text": _BLOCKED_TEXT}, policy_resolver=lambda: False)
        assert out["success"] is True

    def test_comment_refusal_writes_nothing_to_the_seen_ledger(self):
        _run_comment(
            {
                "owner_uin": "10001",
                "tid": "abc",
                "content": _BLOCKED_TEXT,
                "reply_to_comment_id": "cid1",
            }
        )
        assert (
            state.is_recorded_comment(
                owner_uin="10001", tid="abc", identity="id:cid1", actor_uin="10001"
            )
            is False
        )

    def test_comment_refusal_code_is_not_the_unknown_family(self):
        out = _run_comment({"owner_uin": "10001", "tid": "abc", "content": _BLOCKED_TEXT})
        assert out["code"] not in ("qzone_comment_unknown", "qzone_unparseable")
        assert out["code"] == "content_policy_blocked"

    def test_retry_after_a_comment_refusal_is_free(self):
        _run_comment(
            {
                "owner_uin": "10001",
                "tid": "abc",
                "content": _BLOCKED_TEXT,
                "reply_to_comment_id": "cid1",
            }
        )
        out = _run_comment(
            {
                "owner_uin": "10001",
                "tid": "abc",
                "content": _BLOCKED_TEXT,
                "reply_to_comment_id": "cid1",
            },
            policy_resolver=lambda: False,
        )
        assert out["success"] is True
