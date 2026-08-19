"""Tests for the four read/comment tools.

Ported from corlinman's ``tests/test_qzone_comment.py``, including its
JS-escaped feeds3 fixture verbatim — that blob is the only offline record of
what the endpoint actually returns, so it is the closest thing to a wire
contract this port has.
"""

from __future__ import annotations

import json

import pytest

from plugins.qzone import feed, state
from plugins.qzone.client import HttpResponse, QZoneError
from plugins.qzone.feed import (
    handle_qzone_get_post,
    handle_qzone_list_feed,
    handle_qzone_list_friends,
    handle_qzone_post_comment,
    parse_feeds3,
)

_MY_UIN = "10001"
_FRIEND_UIN = "20002"
_COOKIE = f"uin=o{_MY_UIN}; skey=@Skey1; p_skey=PKEY_ABCDEFGHIJK; pt4_token=T"

# A single JS-escaped feed exactly as feeds3 ships it: the root <li> carries
# the author uin + the post tid; a nested comments-item carries one comment.
_FEED_HTML = (
    '<li class=\\"f-single nopic\\" id=\\"fct_10001_abc\\" data-tid=\\"deadbeef\\">'
    '<a class=\\"f-name q_namecard\\" target=\\"_blank\\">测试昵称<\\/a>'
    '<div class=\\"f-info\\">这是一条说说<\\/div>'
    '<span class=\\"state\\">3小时前<\\/span>'
    '<li class=\\"comments-item\\" data-tid=\\"c1\\" data-uin=\\"20002\\" '
    'data-nick=\\"好友A\\"><a class=\\"comments-name\\">好友A<\\/a>'
    '&nbsp; : 评论内容<div class=\\"comments-op\\">回复<\\/div><\\/li>'
    "<\\/li>"
)
_FRIEND_FEED_HTML = (
    '<li class=\\"f-single nopic\\" id=\\"fct_20002_zzz\\" data-tid=\\"cafebabe\\">'
    '<a class=\\"f-name q_namecard\\" target=\\"_blank\\">好友A<\\/a>'
    '<div class=\\"f-info\\">好友的说说<\\/div>'
    '<span class=\\"state\\">1小时前<\\/span><\\/li>'
)


def _feeds_body(html: str = _FEED_HTML) -> str:
    return '_Callback({"code":0,"message":"","data":{"data":"' + html + '"}});'


def _comment_body(code: int = 0, subcode: int = 0) -> bytes:
    payload = json.dumps({"code": code, "subcode": subcode, "message": "ok"})
    return f"<script>frameElement.callback({payload});</script>".encode()


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("QZONE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("QZONE_PERSONA_ID", "grantley")
    monkeypatch.delenv("QZONE_QQ_INSTANCE_ID", raising=False)
    return tmp_path / "state"


def _onebot(cookies=_COOKIE, friends=None, fail_login=False):
    def _call(action, params=None, **_kw):
        if action == "get_login_info":
            if fail_login:
                raise RuntimeError("OneBot action 'get_login_info' failed: offline")
            return {"user_id": int(_MY_UIN)}
        if action == "get_cookies":
            return {"cookies": cookies}
        if action == "get_friend_list":
            return friends if friends is not None else []
        raise AssertionError(action)

    return _call


def _transport(*, feeds=None, comment=None, status=200, raise_on=None):
    calls = []

    def _t(method, url, headers, body, timeout):
        calls.append({"method": method, "url": url, "body": body})
        if raise_on and raise_on in url:
            raise QZoneError("connection reset by peer", "qzone_request_failed")
        if "feeds3_html_more" in url:
            return HttpResponse(status, (feeds if feeds is not None else _feeds_body()).encode())
        if "emotion_cgi_re_feeds" in url:
            return HttpResponse(status, comment if comment is not None else _comment_body())
        raise AssertionError(f"unexpected url {url}")

    _t.calls = calls
    return _t


def _run(handler, args, **kw):
    kw.setdefault("onebot_call", _onebot())
    kw.setdefault("transport", _transport())
    return json.loads(handler(args, **kw))


# ---------------------------------------------------------------------------
# Pure parser
# ---------------------------------------------------------------------------


class TestParseFeeds3:
    def test_extracts_feed_and_comment(self):
        feeds = parse_feeds3(_feeds_body())
        assert len(feeds) == 1
        item = feeds[0]
        assert item["uin"] == _MY_UIN
        assert item["tid"] == "deadbeef"
        assert item["name"] == "测试昵称"
        assert item["content"] == "这是一条说说"
        assert item["time"] == "3小时前"
        assert item["comments"] == [
            {"id": "c1", "uin": _FRIEND_UIN, "name": "好友A", "content": "评论内容"}
        ]

    def test_multiple_feeds_are_split(self):
        feeds = parse_feeds3(_feeds_body(_FEED_HTML + _FRIEND_FEED_HTML))
        assert [f["tid"] for f in feeds] == ["deadbeef", "cafebabe"]
        assert [f["uin"] for f in feeds] == [_MY_UIN, _FRIEND_UIN]

    def test_feed_without_a_tid_is_skipped(self):
        html = '<li class=\\"f-single\\" id=\\"fct_10001_x\\">no tid here<\\/li>'
        assert parse_feeds3(_feeds_body(html)) == []

    def test_unparseable_markup_yields_empty_not_an_error(self):
        assert parse_feeds3("totally different markup") == []


# ---------------------------------------------------------------------------
# qzone_list_feed
# ---------------------------------------------------------------------------


class TestListFeed:
    def test_happy_path(self):
        out = _run(handle_qzone_list_feed, {})
        assert out["success"] is True
        assert out["my_uin"] == _MY_UIN
        assert out["returned"] == 1
        assert out["feed"][0]["tid"] == "deadbeef"

    def test_owner_filter_excludes_others(self):
        transport = _transport(feeds=_feeds_body(_FEED_HTML + _FRIEND_FEED_HTML))
        out = _run(handle_qzone_list_feed, {"owner_uin": _FRIEND_UIN}, transport=transport)
        assert out["returned"] == 1
        assert out["feed"][0]["uin"] == _FRIEND_UIN
        assert out["filter_owner_uin"] == _FRIEND_UIN

    def test_bad_owner_uin_rejected(self):
        out = _run(handle_qzone_list_feed, {"owner_uin": "not-a-qq"})
        assert out["code"] == "invalid_args"

    def test_non_numeric_num_rejected(self):
        assert _run(handle_qzone_list_feed, {"num": "many"})["code"] == "invalid_args"

    def test_num_is_clamped(self):
        transport = _transport()
        _run(handle_qzone_list_feed, {"num": 9999}, transport=transport)
        assert "count=40" in transport.calls[0]["url"]

    def test_owner_filter_overfetches(self):
        transport = _transport()
        _run(handle_qzone_list_feed, {"num": 2, "owner_uin": _FRIEND_UIN},
             transport=transport)
        assert "count=20" in transport.calls[0]["url"]

    def test_login_failure_envelope(self):
        out = _run(handle_qzone_list_feed, {}, onebot_call=_onebot(fail_login=True))
        assert out["code"] == "onebot_unavailable"

    def test_stale_cookie_envelope(self):
        out = _run(handle_qzone_list_feed, {}, onebot_call=_onebot(cookies="uin=o1"))
        assert out["code"] == "qzone_cookie_stale"

    def test_qzone_error_code_is_extracted_without_the_prose(self):
        body = '_Callback({"code":-10000,"message":"使用人数过多"});'
        out = _run(handle_qzone_list_feed, {}, transport=_transport(feeds=body))
        assert out["code"] == "qzone_read_failed"
        assert "code=-10000" in out["error"]
        assert "使用人数过多" not in out["error"]

    def test_http_error_does_not_echo_the_response_body(self):
        marker = "PRIVATE_QZONE_RESPONSE"
        out = _run(
            handle_qzone_list_feed, {},
            transport=_transport(feeds=marker, status=502),
        )
        assert out["code"] == "qzone_read_failed"
        assert marker not in out["error"]
        assert "HTTP 502" in out["error"]

    def test_transport_failure_envelope(self):
        out = _run(handle_qzone_list_feed, {}, transport=_transport(raise_on="feeds3"))
        assert out["code"] == "qzone_request_failed"

    def test_request_carries_gtk_and_the_cookie_jar(self):
        transport = _transport()
        _run(handle_qzone_list_feed, {}, transport=transport)
        assert "g_tk=" in transport.calls[0]["url"]
        assert "outputhtmlfeed=1" in transport.calls[0]["url"]


# ---------------------------------------------------------------------------
# qzone_get_post
# ---------------------------------------------------------------------------


class TestGetPost:
    def test_found(self):
        out = _run(handle_qzone_get_post, {"tid": "deadbeef"})
        assert out["found"] is True
        assert out["post"]["tid"] == "deadbeef"
        assert out["searched"] == 1

    def test_missing(self):
        out = _run(handle_qzone_get_post, {"tid": "0000"})
        assert out["found"] is False
        assert out["known_post"] is None

    def test_requires_tid(self):
        assert _run(handle_qzone_get_post, {"tid": " "})["code"] == "invalid_args"

    def test_always_fetches_the_full_window(self):
        transport = _transport()
        _run(handle_qzone_get_post, {"tid": "x"}, transport=transport)
        assert "count=40" in transport.calls[0]["url"]

    def test_older_own_post_is_labelled_but_still_not_found(self):
        """R13 stays observable: found=false, with a hint about why."""
        state.record_publish(
            persona_id=None, text="an older post", tid="rolledout",
            qzone_url="https://user.qzone.qq.com/10001/mood/rolledout",
            outcome=state.OUTCOME_SENT,
        )
        out = _run(handle_qzone_get_post, {"tid": "rolledout"})
        assert out["found"] is False
        assert out["known_post"]["source"] == "post_log"
        assert out["known_post"]["text"] == "an older post"

    def test_read_failure_envelope(self):
        out = _run(handle_qzone_get_post, {"tid": "x"},
                   transport=_transport(feeds='_Callback({"code":-3000});'))
        assert out["code"] == "qzone_read_failed"


# ---------------------------------------------------------------------------
# qzone_post_comment
# ---------------------------------------------------------------------------


class TestPostCommentValidation:
    def test_requires_content(self):
        out = _run(handle_qzone_post_comment, {"owner_uin": _MY_UIN, "tid": "t"})
        assert out["code"] == "invalid_args"

    def test_requires_tid(self):
        out = _run(handle_qzone_post_comment, {"owner_uin": _MY_UIN, "content": "hi"})
        assert out["code"] == "invalid_args"

    def test_requires_owner_uin(self):
        out = _run(handle_qzone_post_comment, {"tid": "deadbeef", "content": "hi"})
        assert out["code"] == "invalid_args"

    def test_owner_uin_must_be_numeric(self):
        out = _run(handle_qzone_post_comment,
                   {"owner_uin": "abc", "tid": "t", "content": "hi"})
        assert out["code"] == "invalid_args"

    def test_reply_to_uin_must_be_numeric(self):
        out = _run(handle_qzone_post_comment,
                   {"owner_uin": _MY_UIN, "tid": "t", "content": "hi",
                    "reply_to_uin": "abc"})
        assert out["code"] == "invalid_args"

    def test_content_length_capped(self):
        out = _run(handle_qzone_post_comment,
                   {"owner_uin": _MY_UIN, "tid": "t", "content": "x" * 1501})
        assert out["code"] == "invalid_args"

    def test_reply_identity_fields_length_capped(self):
        out = _run(handle_qzone_post_comment,
                   {"owner_uin": _MY_UIN, "tid": "t", "content": "hi",
                    "reply_to_comment_id": "x" * 300})
        assert out["code"] == "invalid_args"


class TestPostCommentWire:
    def test_top_level_comment(self):
        transport = _transport()
        out = _run(handle_qzone_post_comment,
                   {"owner_uin": _MY_UIN, "tid": "deadbeef", "content": "不错"},
                   transport=transport)
        assert out["success"] is True
        assert out["is_reply"] is False
        assert out["content_sent"] == "不错"
        assert "emotion_cgi_re_feeds" in transport.calls[0]["url"]

    def test_form_shape(self):
        import urllib.parse

        transport = _transport()
        _run(handle_qzone_post_comment,
             {"owner_uin": _FRIEND_UIN, "tid": "cafebabe", "content": "hi"},
             transport=transport)
        form = urllib.parse.parse_qs(transport.calls[0]["body"].decode())
        assert form["topicId"] == [f"{_FRIEND_UIN}_cafebabe__1"]
        assert form["feedsType"] == ["100"]
        assert form["format"] == ["fs"]
        assert form["hostUin"] == [_FRIEND_UIN]
        assert form["uin"] == [_MY_UIN]
        assert "targetUin" not in form

    def test_reply_prepends_the_mention_and_sets_target_uin(self):
        import urllib.parse

        transport = _transport()
        out = _run(
            handle_qzone_post_comment,
            {"owner_uin": _MY_UIN, "tid": "deadbeef", "content": "谢谢",
             "reply_to_uin": _FRIEND_UIN, "reply_to_name": "好友A"},
            transport=transport,
        )
        assert out["is_reply"] is True
        assert out["content_sent"].startswith(f"@{{uin:{_FRIEND_UIN},nick:好友A,who:1}}")
        form = urllib.parse.parse_qs(transport.calls[0]["body"].decode())
        assert form["targetUin"] == [_FRIEND_UIN]

    def test_mention_is_not_doubled(self):
        content = f"@{{uin:{_FRIEND_UIN},nick:好友A,who:1}} 已经带了"
        out = _run(
            handle_qzone_post_comment,
            {"owner_uin": _MY_UIN, "tid": "deadbeef", "content": content,
             "reply_to_uin": _FRIEND_UIN, "reply_to_name": "好友A"},
        )
        assert out["content_sent"] == content

    def test_rejected_by_qzone(self):
        out = _run(handle_qzone_post_comment,
                   {"owner_uin": _MY_UIN, "tid": "deadbeef", "content": "x"},
                   transport=_transport(comment=_comment_body(code=-1)))
        assert out["code"] == "qzone_rejected"
        assert out["qzone_code"] == -1

    def test_nonzero_subcode_is_a_rejection(self):
        out = _run(handle_qzone_post_comment,
                   {"owner_uin": _MY_UIN, "tid": "deadbeef", "content": "x"},
                   transport=_transport(comment=_comment_body(subcode=-4001)))
        assert out["code"] == "qzone_rejected"

    def test_stale_cookie_envelope(self):
        out = _run(handle_qzone_post_comment,
                   {"owner_uin": _MY_UIN, "tid": "t", "content": "x"},
                   onebot_call=_onebot(cookies="uin=o1"))
        assert out["code"] == "qzone_cookie_stale"


# ---------------------------------------------------------------------------
# S17 — comment idempotency
# ---------------------------------------------------------------------------


class TestCommentIdempotency:
    def _reply(self, **over):
        args = {"owner_uin": _MY_UIN, "tid": "deadbeef", "content": "谢谢",
                "reply_to_uin": _FRIEND_UIN, "reply_to_name": "好友A",
                "reply_to_comment_id": "c1"}
        args.update(over)
        return args

    def test_success_records_the_identity(self, _isolated_state):
        _run(handle_qzone_post_comment, self._reply())
        payload = json.loads(
            (_isolated_state / "qzone_seen_comments" / "grantley.json").read_text()
        )
        assert payload["version"] == 2
        assert payload["seen"]["deadbeef"][0].startswith("id:c1:")

    def test_second_identical_reply_is_refused_without_a_request(self):
        _run(handle_qzone_post_comment, self._reply())
        transport = _transport()
        out = _run(handle_qzone_post_comment, self._reply(), transport=transport)
        assert out["code"] == "qzone_comment_duplicate"
        assert transport.calls == []

    def test_a_different_comment_on_the_same_post_still_goes_out(self):
        _run(handle_qzone_post_comment, self._reply())
        out = _run(handle_qzone_post_comment,
                   self._reply(reply_to_comment_id="c2", content="也谢谢"))
        assert out["success"] is True

    def test_dedup_can_be_overridden(self):
        _run(handle_qzone_post_comment, self._reply())
        out = _run(handle_qzone_post_comment, self._reply(dedup=False))
        assert out["success"] is True

    def test_transport_failure_marks_the_ledger(self):
        """S17: unknown, not failed — the comment may already be public."""
        out = _run(handle_qzone_post_comment, self._reply(),
                   transport=_transport(raise_on="re_feeds"))
        assert out["code"] == "qzone_comment_unknown"
        assert state.is_recorded_comment(
            owner_uin=_MY_UIN, tid="deadbeef", identity="id:c1", actor_uin=_MY_UIN
        )

    def test_retry_after_an_unknown_is_refused(self):
        _run(handle_qzone_post_comment, self._reply(),
             transport=_transport(raise_on="re_feeds"))
        transport = _transport()
        out = _run(handle_qzone_post_comment, self._reply(), transport=transport)
        assert out["code"] == "qzone_comment_duplicate"
        assert transport.calls == []

    def test_unparseable_response_counts_as_unknown(self):
        out = _run(handle_qzone_post_comment, self._reply(),
                   transport=_transport(comment=b"<html>nope</html>"))
        assert out["code"] == "qzone_unparseable"
        assert state.is_recorded_comment(
            owner_uin=_MY_UIN, tid="deadbeef", identity="id:c1", actor_uin=_MY_UIN
        )

    def test_qzone_rejection_leaves_the_ledger_clean(self):
        """An explicit refusal is definitive, so a corrected retry is allowed."""
        _run(handle_qzone_post_comment, self._reply(),
             transport=_transport(comment=_comment_body(code=-1)))
        assert not state.is_recorded_comment(
            owner_uin=_MY_UIN, tid="deadbeef", identity="id:c1", actor_uin=_MY_UIN
        )
        assert _run(handle_qzone_post_comment, self._reply())["success"] is True

    def test_content_digest_identity_when_no_comment_id(self, _isolated_state):
        _run(handle_qzone_post_comment,
             self._reply(reply_to_comment_id="", reply_to_comment_content="原评论"))
        payload = json.loads(
            (_isolated_state / "qzone_seen_comments" / "grantley.json").read_text()
        )
        assert payload["seen"]["deadbeef"][0].startswith("sha256:")

    def test_friend_post_uses_the_friend_ledger(self, _isolated_state):
        _run(handle_qzone_post_comment,
             {"owner_uin": _FRIEND_UIN, "tid": "cafebabe", "content": "写得好"})
        payload = json.loads(
            (_isolated_state / "qzone_friend_comments" / "grantley.json").read_text()
        )
        assert payload == {"version": 1, "seen": [f"{_FRIEND_UIN}:cafebabe"]}
        assert not (_isolated_state / "qzone_seen_comments").exists()

    def test_second_comment_on_a_friend_post_is_refused(self):
        _run(handle_qzone_post_comment,
             {"owner_uin": _FRIEND_UIN, "tid": "cafebabe", "content": "写得好"})
        out = _run(handle_qzone_post_comment,
                   {"owner_uin": _FRIEND_UIN, "tid": "cafebabe", "content": "再夸一句"})
        assert out["code"] == "qzone_comment_duplicate"

    def test_persona_argument_routes_to_its_own_ledger(self, _isolated_state):
        _run(handle_qzone_post_comment, self._reply(persona_id="other"))
        assert (_isolated_state / "qzone_seen_comments" / "other.json").is_file()


# ---------------------------------------------------------------------------
# qzone_list_friends
# ---------------------------------------------------------------------------


class TestListFriends:
    _FRIENDS = [
        {"user_id": 20002, "nickname": "好友A", "remark": "战友"},
        {"user_id": 30003, "nickname": "Bob", "remark": ""},
    ]

    def test_lists_everyone(self):
        out = json.loads(
            handle_qzone_list_friends({}, onebot_call=_onebot(friends=self._FRIENDS))
        )
        assert out["success"] is True
        assert out["total"] == 2
        assert out["friends"][0] == {"uin": "20002", "nickname": "好友A", "remark": "战友"}

    def test_filter_is_case_insensitive(self):
        out = json.loads(
            handle_qzone_list_friends(
                {"filter": "bob"}, onebot_call=_onebot(friends=self._FRIENDS)
            )
        )
        assert out["total"] == 1
        assert out["friends"][0]["uin"] == "30003"

    def test_filter_matches_remark_and_uin(self):
        for needle, uin in (("战友", "20002"), ("30003", "30003")):
            out = json.loads(
                handle_qzone_list_friends(
                    {"filter": needle}, onebot_call=_onebot(friends=self._FRIENDS)
                )
            )
            assert out["friends"][0]["uin"] == uin

    def test_empty_list(self):
        out = json.loads(handle_qzone_list_friends({}, onebot_call=_onebot(friends=[])))
        assert out["total"] == 0
        assert out["friends"] == []

    def test_limit_is_clamped(self):
        out = json.loads(
            handle_qzone_list_friends(
                {"limit": 1}, onebot_call=_onebot(friends=self._FRIENDS)
            )
        )
        assert out["total"] == 2
        assert out["returned"] == 1

    def test_never_touches_qzone(self):
        transport = _transport()
        handle_qzone_list_friends(
            {}, onebot_call=_onebot(friends=self._FRIENDS), transport=transport
        )
        assert transport.calls == []

    def test_onebot_failure_envelope(self):
        def _call(*_a, **_kw):
            raise RuntimeError("OneBot action 'get_friend_list' failed: offline")

        out = json.loads(handle_qzone_list_friends({}, onebot_call=_call))
        assert out["code"] == "onebot_failed"

    def test_unexpected_shape_is_an_error(self):
        out = json.loads(
            handle_qzone_list_friends({}, onebot_call=lambda *a, **k: {"oops": 1})
        )
        assert out["code"] == "onebot_failed"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


def test_schemas_are_hermes_shaped():
    schemas = [
        feed.QZONE_LIST_FEED_SCHEMA,
        feed.QZONE_GET_POST_SCHEMA,
        feed.QZONE_POST_COMMENT_SCHEMA,
        feed.QZONE_LIST_FRIENDS_SCHEMA,
    ]
    assert {s["name"] for s in schemas} == {
        "qzone_list_feed", "qzone_get_post", "qzone_post_comment", "qzone_list_friends"
    }
    for schema in schemas:
        assert schema["parameters"]["type"] == "object"
        assert isinstance(schema["parameters"]["properties"], dict)
        assert isinstance(schema["description"], str) and schema["description"]
