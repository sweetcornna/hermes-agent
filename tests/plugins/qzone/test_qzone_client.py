"""Tests for the shared QZone primitives: auth, transport, parsing.

Ports the ``TestComputeGtk`` / ``TestExtractCookieValue`` cases from the
older hermes ``tests/tools/test_qzone_tool.py`` and the ``_unescape_hex`` /
``_parse_callback_json`` cases from corlinman's ``test_qzone_comment.py``.
No network: the transport seam is a plain callable.
"""

from __future__ import annotations

import urllib.error

import pytest

from plugins.qzone import client
from plugins.qzone.client import (
    HttpResponse,
    QZoneError,
    compute_gtk,
    extract_cookie_value,
    extract_json_object,
    parse_callback_json,
    qzone_auth,
    qzone_get,
    qzone_post,
    strip_html_lite,
    unescape_js,
)

_COOKIE = "uin=o0010001; skey=@abcDEF; p_skey=PpKkEeYy; pt4_token=tok123"


def _recording_transport(response: HttpResponse):
    """A transport that records its call and returns a canned response."""
    calls = []

    def _transport(method, url, headers, body, timeout):
        calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout": timeout,
            }
        )
        return response

    _transport.calls = calls
    return _transport


# ---------------------------------------------------------------------------
# g_tk
# ---------------------------------------------------------------------------


class TestComputeGtk:
    def test_empty_skey_is_seed(self):
        assert compute_gtk("") == 5381

    def test_known_value(self):
        # Locked-in regression value for the standard QZone hash.
        assert compute_gtk("test") == 2090756197

    def test_result_fits_31_bits(self):
        for skey in ("p_skey_abc", "ZZZZZZZZZZ", "@#$%^&*()"):
            assert 0 <= compute_gtk(skey) <= 0x7FFFFFFF

    def test_deterministic(self):
        assert compute_gtk("abc123") == compute_gtk("abc123")


# ---------------------------------------------------------------------------
# Cookie parsing
# ---------------------------------------------------------------------------


class TestExtractCookieValue:
    def test_extracts_middle_key(self):
        assert extract_cookie_value(_COOKIE, "skey") == "@abcDEF"

    def test_extracts_p_skey(self):
        assert extract_cookie_value(_COOKIE, "p_skey") == "PpKkEeYy"

    def test_extracts_first_key(self):
        assert extract_cookie_value(_COOKIE, "uin") == "o0010001"

    def test_missing_key_returns_none(self):
        assert extract_cookie_value(_COOKIE, "nope") is None

    def test_empty_string_returns_none(self):
        assert extract_cookie_value("", "p_skey") is None

    def test_value_containing_equals_sign(self):
        assert extract_cookie_value("token=a=b=c; x=1", "token") == "a=b=c"


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


class TestJsonExtraction:
    def test_strips_jsonp_shim(self):
        assert extract_json_object(b'_Callback({"ret":0,"tid":"x"});') == {
            "ret": 0,
            "tid": "x",
        }

    def test_accepts_str_and_bytes(self):
        assert extract_json_object('{"a":1}') == extract_json_object(b'{"a":1}')

    def test_html_error_page_is_unparseable(self):
        assert extract_json_object(b"<html>403 Forbidden</html>") is None

    def test_empty_is_unparseable(self):
        assert extract_json_object(b"") is None


class TestParseCallbackJson:
    def test_extracts_payload(self):
        body = '<script>frameElement.callback({"code":0,"subcode":0});</script>'
        assert parse_callback_json(body) == {"code": 0, "subcode": 0}

    def test_garbage_returns_none(self):
        assert parse_callback_json("garbage no callback") is None

    def test_anchors_past_the_try_block(self):
        # A naive ``{.*}`` search matches the wrapper's try{...} instead of
        # the payload; anchoring on ``callback(`` is what avoids that.
        body = (
            "<script>try{document.domain='qq.com';}catch(e){}"
            'frameElement.callback({"code":0,"subcode":0,"message":"ok"});</script>'
        )
        assert parse_callback_json(body) == {"code": 0, "subcode": 0, "message": "ok"}


class TestUnescapeJs:
    def test_decodes_js_escapes(self):
        assert unescape_js(r"a\/b") == "a/b"
        assert unescape_js(r"x\x41y") == "xAy"
        assert unescape_js(r"<\/div>") == "</div>"
        assert unescape_js("中") == "中"

    def test_decodes_unicode_escape(self):
        assert unescape_js(r"中") == "中"

    def test_two_char_escapes_are_handled(self):
        # Decoding only \xNN leaves ``<\/div>`` mangled and every downstream
        # regex silently misses. This is the case that locks that in.
        assert "</div>" in unescape_js(r'<div class=\"f-info\">hi<\/div>')


class TestStripHtmlLite:
    def test_br_becomes_newline(self):
        assert strip_html_lite("a<br/>b") == "a\nb"

    def test_tags_removed_and_entities_decoded(self):
        assert strip_html_lite("<b>x</b>&amp;y") == "x&y"

    def test_empty_is_empty(self):
        assert strip_html_lite("") == ""


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class TestTransport:
    def test_post_sends_urlencoded_body_and_cookie(self):
        transport = _recording_transport(HttpResponse(200, b"ok"))
        body = qzone_post(
            "https://example.test/cgi", {"con": "你好"}, _COOKIE, "10001", 5,
            transport=transport,
        )
        assert body == b"ok"
        call = transport.calls[0]
        assert call["method"] == "POST"
        assert call["headers"]["Cookie"] == _COOKIE
        assert call["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
        assert call["headers"]["Referer"] == "https://user.qzone.qq.com/10001"
        assert b"con=" in call["body"]

    def test_get_appends_query_params(self):
        transport = _recording_transport(HttpResponse(200, b"body"))
        text = qzone_get(
            "https://example.test/feeds", {"g_tk": "42"}, _COOKIE, "10001", 5,
            transport=transport,
        )
        assert text == "body"
        assert "g_tk=42" in transport.calls[0]["url"]

    def test_http_error_status_raises_without_echoing_body(self):
        marker = "PRIVATE_QZONE_RESPONSE"
        transport = _recording_transport(HttpResponse(502, marker.encode()))
        with pytest.raises(QZoneError) as excinfo:
            qzone_post("https://example.test/cgi", {}, _COOKIE, "10001", 5,
                       transport=transport)
        assert marker not in str(excinfo.value)
        assert "HTTP 502" in str(excinfo.value)

    def test_get_http_error_uses_read_failure_code(self):
        transport = _recording_transport(HttpResponse(500, b"secret"))
        with pytest.raises(QZoneError) as excinfo:
            qzone_get("https://example.test/feeds", {}, _COOKIE, "10001", 5,
                      transport=transport)
        assert excinfo.value.code == "qzone_read_failed"

    def test_default_transport_discards_http_error_body(self, monkeypatch):
        """A Tencent error page must never reach the caller."""

        class _Err(urllib.error.HTTPError):
            def __init__(self):
                super().__init__("https://x", 403, "Forbidden", {}, None)

            def read(self):
                return b"<html>RISK CONTROL INTERSTITIAL</html>"

        def _boom(*_a, **_kw):
            raise _Err()

        monkeypatch.setattr(client.urllib.request, "urlopen", _boom)
        response = client.default_transport("GET", "https://x", {}, None, 5)
        assert response.status == 403
        assert response.body == b""

    def test_default_transport_url_error_becomes_qzone_error(self, monkeypatch):
        def _boom(*_a, **_kw):
            raise urllib.error.URLError("no route to host")

        monkeypatch.setattr(client.urllib.request, "urlopen", _boom)
        with pytest.raises(QZoneError) as excinfo:
            client.default_transport("GET", "https://x", {}, None, 5)
        assert excinfo.value.code == "qzone_request_failed"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def _fake_onebot(cookies=_COOKIE, user_id=10001):
    def _call(action, params=None, **_kw):
        if action == "get_login_info":
            return {"user_id": user_id, "nickname": "Me"}
        if action == "get_cookies":
            assert params == {"domain": "user.qzone.qq.com"}
            return {"cookies": cookies}
        raise AssertionError(f"unexpected action {action}")

    return _call


class TestQzoneAuth:
    def test_happy_path_derives_gtk_from_p_skey(self):
        auth = qzone_auth(_fake_onebot())
        assert auth.uin == "10001"
        assert auth.skey == "@abcDEF"
        assert auth.p_skey == "PpKkEeYy"
        assert auth.gtk == compute_gtk("PpKkEeYy")

    def test_missing_p_skey_is_stale_cookie(self):
        with pytest.raises(QZoneError) as excinfo:
            qzone_auth(_fake_onebot(cookies="uin=o1; skey=@x"))
        assert excinfo.value.code == "qzone_cookie_stale"

    def test_empty_cookie_string_is_stale_cookie(self):
        with pytest.raises(QZoneError) as excinfo:
            qzone_auth(_fake_onebot(cookies=""))
        assert excinfo.value.code == "qzone_cookie_stale"

    def test_absent_skey_degrades_to_empty_string(self):
        auth = qzone_auth(_fake_onebot(cookies="p_skey=PK"))
        assert auth.skey == ""
        assert auth.p_skey == "PK"

    def test_missing_user_id_is_onebot_failure(self):
        def _call(action, params=None, **_kw):
            if action == "get_login_info":
                return {}
            return {"cookies": _COOKIE}

        with pytest.raises(QZoneError) as excinfo:
            qzone_auth(_call)
        assert excinfo.value.code == "onebot_failed"

    def test_unreachable_backend_is_onebot_unavailable(self):
        def _call(*_a, **_kw):
            raise RuntimeError("Cannot reach OneBot at ws://x")

        with pytest.raises(QZoneError) as excinfo:
            qzone_auth(_call)
        assert excinfo.value.code == "onebot_unavailable"

    def test_prefers_self_id_observed_by_the_live_adapter(self, monkeypatch):
        """Disagreement S11: the adapter's observed self_id wins over a probe."""

        class _Client:
            last_self_id = 987654321

        import plugins.platforms.onebot.client as live

        monkeypatch.setattr(live, "get_live_client", lambda: (_Client(), object()))

        def _call(action, params=None, **_kw):
            assert action != "get_login_info", "should not probe when self_id is known"
            return {"cookies": _COOKIE}

        assert qzone_auth(_call).uin == "987654321"

    def test_falls_back_to_get_login_info_without_an_adapter(self, monkeypatch):
        import plugins.platforms.onebot.client as live

        monkeypatch.setattr(live, "get_live_client", lambda: (None, None))
        assert qzone_auth(_fake_onebot()).uin == "10001"
