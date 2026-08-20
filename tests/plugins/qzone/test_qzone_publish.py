"""Tests for ``qzone_publish``.

The pure wire-format cases are ported from the older hermes
``tests/tools/test_qzone_tool.py`` (which locks both success shapes of
``emotion_cgi_publish_v6``); the idempotency cases come from corlinman's
effect protocol, re-expressed against this port's on-disk equivalent.

No network: the transport seam is a callable that routes on URL.
"""

from __future__ import annotations

import json
import urllib.parse

import pytest

from plugins.qzone import publish, state
from plugins.qzone.client import HttpResponse, QZoneError
from plugins.qzone.publish import (
    _build_publish_form,
    _build_richval,
    _build_upload_form,
    _extract_pic_info,
    _parse_publish_response,
    _parse_upload_response,
    _read_image_file,
    handle_qzone_publish,
)

_COOKIE = "uin=o10001; skey=@abcDEF; p_skey=PpKkEeYy"
_UPLOAD_OK = b'frameElement.callback({"ret":0,"data":{"albumid":"a1","lloc":"l1","sloc":"s1","type":0,"width":800,"height":600,"url":"u"}});'
_PUBLISH_OK = b'{"code":0,"subcode":0,"tid":"1cbe3d3c17","feedinfo":"<li>"}'

# A 1x1 PNG — small, real, and the right magic bytes.
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


def _onebot(cookies=_COOKIE, user_id=10001):
    def _call(action, params=None, **_kw):
        if action == "get_login_info":
            return {"user_id": user_id}
        if action == "get_cookies":
            return {"cookies": cookies}
        raise AssertionError(action)

    return _call


def _transport(*, upload=_UPLOAD_OK, publish_body=_PUBLISH_OK, raise_on=None):
    """Route by URL; ``raise_on`` makes that host's call die in transport."""
    calls = []

    def _t(method, url, headers, body, timeout):
        calls.append({"url": url, "body": body, "headers": dict(headers)})
        if raise_on and raise_on in url:
            raise QZoneError("connection reset by peer", "qzone_request_failed")
        if "cgi_upload_image" in url:
            return HttpResponse(200, upload)
        if "emotion_cgi_publish_v6" in url:
            return HttpResponse(200, publish_body)
        raise AssertionError(f"unexpected url {url}")

    _t.calls = calls
    return _t


def _run(args, **kw):
    kw.setdefault("onebot_call", _onebot())
    kw.setdefault("transport", _transport())
    return json.loads(handle_qzone_publish(args, **kw))


# ---------------------------------------------------------------------------
# Generated images — character references
# ---------------------------------------------------------------------------


class TestGenerateImageCharacterReferences:
    @pytest.fixture
    def _generation_stubs(self, tmp_path, monkeypatch):
        from hermes_cli import config
        from plugins.image_gen import cornna
        from tools import image_generation_tool

        image_path = tmp_path / "generated.png"
        image_path.write_bytes(_PNG)
        calls = []

        monkeypatch.setattr(config, "load_config", lambda: {})
        monkeypatch.setattr(cornna, "available_characters", lambda: [])
        monkeypatch.setattr(
            image_generation_tool, "check_image_generation_requirements", lambda: True
        )

        def _generate(args):
            calls.append(args)
            return {"image": str(image_path)}

        monkeypatch.setattr(image_generation_tool, "_handle_image_generate", _generate)
        return calls, config, cornna, monkeypatch

    def test_reference_characters_empty_uses_plain_generation(self, _generation_stubs):
        calls, config, _cornna, monkeypatch = _generation_stubs
        monkeypatch.setattr(
            config, "load_config", lambda: {"qzone": {"reference_characters": []}}
        )

        image, filename = publish._generate_image("配图", "square")

        assert image == _PNG
        assert filename == "generated.png"
        assert calls == [{"prompt": "配图", "aspect_ratio": "square"}]

    def test_reference_characters_missing_on_disk_uses_plain_generation(
        self, _generation_stubs, caplog
    ):
        calls, config, cornna, monkeypatch = _generation_stubs
        monkeypatch.setattr(
            config, "load_config", lambda: {"qzone": {"reference_characters": ["grantley"]}}
        )
        monkeypatch.setattr(cornna, "available_characters", lambda: [])

        with caplog.at_level("INFO"):
            publish._generate_image("配图", "square")

        assert calls == [{"prompt": "配图", "aspect_ratio": "square"}]
        assert "grantley" in caplog.text

    def test_reference_characters_partially_available_are_passed(
        self, _generation_stubs, caplog
    ):
        calls, config, cornna, monkeypatch = _generation_stubs
        monkeypatch.setattr(
            config,
            "load_config",
            lambda: {"qzone": {"reference_characters": ["grantley", "bating"]}},
        )
        monkeypatch.setattr(cornna, "available_characters", lambda: ["grantley"])

        with caplog.at_level("INFO"):
            publish._generate_image("配图", "square")

        assert calls == [{
            "prompt": "配图",
            "aspect_ratio": "square",
            "reference_image_urls": ["character:grantley"],
        }]
        assert "bating" in caplog.text

    def test_reference_characters_over_recommended_are_truncated(
        self, _generation_stubs, caplog
    ):
        calls, config, cornna, monkeypatch = _generation_stubs
        configured = ["algo", "grantley", "bating", "paul"]
        monkeypatch.setattr(
            config, "load_config", lambda: {"qzone": {"reference_characters": configured}}
        )
        monkeypatch.setattr(cornna, "available_characters", lambda: configured)

        publish._generate_image("配图", "square")

        assert calls == [{
            "prompt": "配图",
            "aspect_ratio": "square",
            "reference_image_urls": ["character:algo", "character:grantley", "character:bating"],
        }]
        assert "too many reference images" in caplog.text

    def test_reference_generation_failure_retries_without_references(
        self, _generation_stubs, caplog
    ):
        calls, config, cornna, monkeypatch = _generation_stubs
        monkeypatch.setattr(
            config, "load_config", lambda: {"qzone": {"reference_characters": ["grantley"]}}
        )
        monkeypatch.setattr(cornna, "available_characters", lambda: ["grantley"])

        from tools import image_generation_tool

        plain_generate = image_generation_tool._handle_image_generate

        def _retrying_generate(args):
            if args.get("reference_image_urls"):
                calls.append(args)
                raise RuntimeError("reference image disappeared")
            return plain_generate(args)

        monkeypatch.setattr(image_generation_tool, "_handle_image_generate", _retrying_generate)

        with caplog.at_level("WARNING"):
            image, filename = publish._generate_image("配图", "square")

        assert image == _PNG
        assert filename == "generated.png"
        assert calls == [
            {
                "prompt": "配图",
                "aspect_ratio": "square",
                "reference_image_urls": ["character:grantley"],
            },
            {"prompt": "配图", "aspect_ratio": "square"},
        ]
        assert "retrying without references" in caplog.text

    def test_plain_generation_failure_after_reference_retry_propagates(
        self, _generation_stubs
    ):
        calls, config, cornna, monkeypatch = _generation_stubs
        monkeypatch.setattr(
            config, "load_config", lambda: {"qzone": {"reference_characters": ["grantley"]}}
        )
        monkeypatch.setattr(cornna, "available_characters", lambda: ["grantley"])

        from tools import image_generation_tool

        def _generate(args):
            calls.append(args)
            raise RuntimeError("backend unavailable")

        monkeypatch.setattr(image_generation_tool, "_handle_image_generate", _generate)

        with pytest.raises(RuntimeError, match="backend unavailable"):
            publish._generate_image("配图", "square")

        assert calls == [
            {
                "prompt": "配图",
                "aspect_ratio": "square",
                "reference_image_urls": ["character:grantley"],
            },
            {"prompt": "配图", "aspect_ratio": "square"},
        ]

    def test_non_local_terminal_backend_skips_character_references(
        self, _generation_stubs, caplog
    ):
        calls, config, cornna, monkeypatch = _generation_stubs
        monkeypatch.setenv("TERMINAL_ENV", "ssh")
        monkeypatch.setattr(
            config, "load_config", lambda: {"qzone": {"reference_characters": ["grantley"]}}
        )
        monkeypatch.setattr(cornna, "available_characters", lambda: ["grantley"])

        with caplog.at_level("INFO"):
            publish._generate_image("配图", "square")

        assert calls == [{"prompt": "配图", "aspect_ratio": "square"}]
        assert "does not support character: references" in caplog.text


# ---------------------------------------------------------------------------
# Pure wire format
# ---------------------------------------------------------------------------


class TestBuildPublishForm:
    def test_text_goes_into_con(self):
        assert _build_publish_form("今天天气真好", "10001")["con"] == "今天天气真好"

    def test_hostuin_is_stringified(self):
        assert _build_publish_form("hi", 10001)["hostuin"] == "10001"

    def test_qzreferrer_includes_uin(self):
        form = _build_publish_form("hi", "10001")
        assert form["qzreferrer"] == "https://user.qzone.qq.com/10001"

    def test_format_is_json(self):
        assert _build_publish_form("hi", "10001")["format"] == "json"

    def test_no_images_keeps_richtype_empty(self):
        form = _build_publish_form("hi", "10001")
        assert form["richtype"] == ""
        assert form["richval"] == ""

    def test_with_images_sets_richtype_and_richval(self):
        pics = [{"albumid": "a1", "lloc": "l1", "sloc": "s1", "type": 0,
                 "width": 800, "height": 600}]
        form = _build_publish_form("hi", "10001", pics)
        assert form["richtype"] == "1"
        assert "a1" in form["richval"]


class TestBuildRichval:
    def test_single_image_segment(self):
        out = _build_richval([{"albumid": "a1", "lloc": "l1", "sloc": "s1",
                               "type": 0, "width": 800, "height": 600}])
        assert out == ",a1,l1,s1,0,600,800,,600,800"

    def test_multiple_images_tab_joined(self):
        pics = [
            {"albumid": "a1", "lloc": "l1", "sloc": "s1", "type": 0,
             "width": 1, "height": 2},
            {"albumid": "a2", "lloc": "l2", "sloc": "s2", "type": 0,
             "width": 3, "height": 4},
        ]
        out = _build_richval(pics)
        assert out.count("\t") == 1
        assert out.split("\t")[1] == ",a2,l2,s2,0,4,3,,4,3"

    def test_empty_list(self):
        assert _build_richval([]) == ""


class TestBuildUploadForm:
    def test_picfile_carries_base64(self):
        form = _build_upload_form("QUJD", "a.png", "10001", "s", "p", 42)
        assert form["picfile"] == "QUJD"
        assert form["base64"] == "1"

    def test_uin_fields_populated(self):
        form = _build_upload_form("x", "a.png", "10001", "sk", "pk", 42)
        assert form["uin"] == form["p_uin"] == form["zzpaneluin"] == "10001"
        assert form["skey"] == "sk"
        assert form["p_skey"] == "pk"

    def test_url_includes_gtk(self):
        assert "g_tk=42" in _build_upload_form("x", "a.png", "1", "", "", 42)["url"]

    def test_album_is_the_shuoshuo_album(self):
        form = _build_upload_form("x", "a.png", "1", "", "", 1)
        assert form["albumtype"] == "7"
        assert form["refer"] == "shuoshuo"


class TestParsePublishResponse:
    def test_success_with_tid(self):
        result = _parse_publish_response(b'{"ret":0,"tid":"feedtid123"}')
        assert result["ok"] is True
        assert result["tid"] == "feedtid123"

    def test_success_t1_tid_fallback(self):
        assert _parse_publish_response(b'{"ret":0,"t1_tid":"alt456"}')["tid"] == "alt456"

    def test_success_str_input(self):
        assert _parse_publish_response('{"ret":0,"tid":"x"}')["ok"] is True

    def test_jsonp_callback_wrapper_is_stripped(self):
        result = _parse_publish_response(b'_Callback({"ret":0,"tid":"wrapped789"});')
        assert result["tid"] == "wrapped789"

    def test_error_ret_nonzero(self):
        result = _parse_publish_response(b'{"ret":-3000,"msg":"need verify"}')
        assert result["ok"] is False
        assert result["code"] == -3000
        assert "need verify" in result["error"]

    def test_error_nonzero_subcode(self):
        assert _parse_publish_response(b'{"ret":0,"subcode":-4001,"msg":"bad"}')["ok"] is False

    def test_unparseable_response(self):
        result = _parse_publish_response(b"<html>403 Forbidden</html>")
        assert result["ok"] is False
        assert "unparseable" in result["error"]

    def test_empty_response(self):
        assert _parse_publish_response(b"")["ok"] is False

    def test_success_with_code_field(self):
        # Verified live upstream: the CGI returns `code` with no `ret`.
        result = _parse_publish_response(_PUBLISH_OK)
        assert result["ok"] is True
        assert result["tid"] == "1cbe3d3c17"

    def test_error_code_nonzero(self):
        result = _parse_publish_response(
            '{"code":-10000,"message":"使用人数过多"}'.encode()
        )
        assert result["ok"] is False
        assert result["code"] == -10000

    def test_code_success_with_bad_subcode(self):
        assert _parse_publish_response(b'{"code":0,"subcode":-4001}')["ok"] is False


class TestParseUploadResponse:
    def test_success_extracts_pic(self):
        result = _parse_upload_response(_UPLOAD_OK)
        assert result["ok"] is True
        assert result["pic"]["albumid"] == "a1"

    def test_error_ret_nonzero(self):
        assert _parse_upload_response(b'{"ret":-1}')["ok"] is False

    def test_unparseable(self):
        assert _parse_upload_response(b"nope")["ok"] is False


class TestExtractPicInfo:
    def test_direct_fields(self):
        info = _extract_pic_info({"albumid": "a", "lloc": "l", "sloc": "s",
                                  "type": 1, "width": 10, "height": 20, "url": "u"})
        assert info == {"albumid": "a", "lloc": "l", "sloc": "s", "type": 1,
                        "width": 10, "height": 20, "url": "u"}

    def test_photoid_fallback_for_lloc_sloc(self):
        info = _extract_pic_info({"photoid": "pid"})
        assert info["lloc"] == info["sloc"] == "pid"

    def test_pre_fallback_for_url(self):
        assert _extract_pic_info({"pre": "p"})["url"] == "p"


class TestReadImageFile:
    def test_reads_valid_image(self, tmp_path):
        path = tmp_path / "a.png"
        path.write_bytes(_PNG)
        data, name = _read_image_file(str(path))
        assert data == _PNG
        assert name == "a.png"

    def test_missing_file(self, tmp_path):
        with pytest.raises(ValueError, match="file not found"):
            _read_image_file(str(tmp_path / "nope.png"))

    def test_unsupported_extension(self, tmp_path):
        path = tmp_path / "a.txt"
        path.write_bytes(b"x")
        with pytest.raises(ValueError, match="unsupported image type"):
            _read_image_file(str(path))

    def test_empty_file(self, tmp_path):
        path = tmp_path / "a.png"
        path.write_bytes(b"")
        with pytest.raises(ValueError, match="empty"):
            _read_image_file(str(path))

    def test_oversized_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(publish, "_MAX_IMAGE_BYTES", 4)
        path = tmp_path / "a.png"
        path.write_bytes(_PNG)
        with pytest.raises(ValueError, match="too large"):
            _read_image_file(str(path))

    def test_ceiling_is_the_adapter_ceiling(self):
        """Bounded for a 1.9 GB host — see the PORT NOTE in publish.py."""
        assert publish._MAX_IMAGE_BYTES == 8 * 1024 * 1024


# ---------------------------------------------------------------------------
# Handler — validation
# ---------------------------------------------------------------------------


class TestHandlerValidation:
    def test_no_args_rejected(self):
        out = _run({})
        assert out["code"] == "invalid_args"

    def test_empty_text_no_images_rejected(self):
        out = _run({"text": "   "})
        assert out["code"] == "invalid_args"

    def test_non_list_images_rejected(self):
        assert _run({"text": "hi", "images": 5})["code"] == "invalid_args"

    def test_non_string_generate_rejected(self):
        assert _run({"text": "hi", "generate": {"prompt": "x"}})["code"] == "invalid_args"

    def test_too_many_images_rejected(self):
        # Content policy denies unclassified media by default (see
        # TestContentPolicyPublish), so this structural check needs the
        # policy seam disabled to reach the too_many_images branch —
        # mirrors corlinman's own test_qzone_publish.py pattern.
        out = _run(
            {"text": "hi", "images": [f"{i}.png" for i in range(10)]},
            policy_resolver=lambda: False,
        )
        assert out["code"] == "too_many_images"

    def test_bad_image_path_fails_before_any_network(self, tmp_path):
        transport = _transport()
        out = json.loads(
            handle_qzone_publish(
                {"text": "hi", "images": [str(tmp_path / "nope.png")]},
                onebot_call=_onebot(),
                transport=transport,
                policy_resolver=lambda: False,
            )
        )
        assert out["code"] == "image_not_found"
        assert transport.calls == []


# ---------------------------------------------------------------------------
# Handler — auth failures
# ---------------------------------------------------------------------------


class TestHandlerAuth:
    def test_missing_p_skey_is_stale_cookie(self):
        out = _run({"text": "hi"}, onebot_call=_onebot(cookies="uin=o1; skey=@x"))
        assert out["code"] == "qzone_cookie_stale"

    def test_unreachable_onebot(self):
        def _call(*_a, **_kw):
            raise RuntimeError("Cannot reach OneBot")

        assert _run({"text": "hi"}, onebot_call=_call)["code"] == "onebot_unavailable"


# ---------------------------------------------------------------------------
# Handler — happy paths
# ---------------------------------------------------------------------------


class TestHandlerPublish:
    def test_text_only_post(self):
        transport = _transport()
        out = _run({"text": "本大爷回来了"}, transport=transport)
        assert out["success"] is True
        assert out["tid"] == "1cbe3d3c17"
        assert out["uin"] == "10001"
        assert out["images"] == 0
        assert out["qzone_url"] == "https://user.qzone.qq.com/10001/mood/1cbe3d3c17"
        assert len(transport.calls) == 1

    def test_publish_targets_the_h5_host(self):
        """Disagreement S14 — corlinman's production host, not the legacy one."""
        transport = _transport()
        _run({"text": "hi"}, transport=transport)
        url = transport.calls[0]["url"]
        assert url.startswith("https://h5.qzone.qq.com/proxy/domain/taotao.qzone.qq.com")
        assert "g_tk=" in url

    def test_form_body_carries_the_text(self):
        transport = _transport()
        _run({"text": "你好"}, transport=transport)
        form = urllib.parse.parse_qs(transport.calls[0]["body"].decode())
        assert form["con"] == ["你好"]
        assert form["hostuin"] == ["10001"]

    def test_image_post_uploads_then_publishes(self, tmp_path):
        path = tmp_path / "a.png"
        path.write_bytes(_PNG)
        transport = _transport()
        out = _run(
            {"text": "配图", "images": [str(path)]},
            transport=transport,
            policy_resolver=lambda: False,
        )
        assert out["success"] is True
        assert out["images"] == 1
        assert "cgi_upload_image" in transport.calls[0]["url"]
        assert "emotion_cgi_publish_v6" in transport.calls[1]["url"]
        form = urllib.parse.parse_qs(transport.calls[1]["body"].decode())
        assert form["richtype"] == ["1"]
        assert "a1" in form["richval"][0]

    def test_single_image_path_as_string(self, tmp_path):
        path = tmp_path / "a.png"
        path.write_bytes(_PNG)
        assert (
            _run(
                {"text": "x", "images": str(path)}, policy_resolver=lambda: False
            )["images"]
            == 1
        )

    def test_upload_failure_is_surfaced_and_nothing_is_logged(self, tmp_path):
        path = tmp_path / "a.png"
        path.write_bytes(_PNG)
        out = _run(
            {"text": "x", "images": [str(path)]},
            transport=_transport(upload=b'{"ret":-1}'),
            policy_resolver=lambda: False,
        )
        assert out["code"] == "image_upload_failed"
        assert state.post_log_entries() == []

    def test_qzone_rejection_is_surfaced(self):
        out = _run({"text": "x"}, transport=_transport(publish_body=b'{"ret":-3000,"msg":"verify"}'))
        assert out["code"] == "qzone_rejected"
        assert out["qzone_code"] == -3000

    def test_unparseable_publish_response_is_a_rejection(self):
        out = _run({"text": "x"}, transport=_transport(publish_body=b"<html>403</html>"))
        assert out["code"] == "qzone_rejected"


# ---------------------------------------------------------------------------
# S17 — outcome recording and the double-post guard
# ---------------------------------------------------------------------------


class TestPublishIdempotency:
    def test_success_is_logged_as_sent(self):
        _run({"text": "hello", "job": "hermes.qzone_daily"})
        entry = state.post_log_entries()[-1]
        assert entry["outcome"] == "sent"
        assert entry["tid"] == "1cbe3d3c17"
        assert entry["job"] == "hermes.qzone_daily"
        assert entry["text"] == "hello"

    def test_transport_failure_is_logged_as_unknown_not_failed(self):
        """The core of S17: the post may be live, so it is never 'failed'."""
        out = _run({"text": "hello"}, transport=_transport(raise_on="publish_v6"))
        assert out["code"] == "qzone_publish_unknown"
        entry = state.post_log_entries()[-1]
        assert entry["outcome"] == "unknown"
        assert entry["tid"] is None

    def test_qzone_rejection_is_not_logged_at_all(self):
        """An explicit refusal means it is definitely not public — retry is safe."""
        _run({"text": "hello"}, transport=_transport(publish_body=b'{"ret":-4001}'))
        assert state.post_log_entries() == []

    def test_retry_after_unknown_is_refused(self):
        _run({"text": "hello"}, transport=_transport(raise_on="publish_v6"))
        transport = _transport()
        out = _run({"text": "hello"}, transport=transport)
        assert out["code"] == "qzone_publish_unknown_pending"
        assert transport.calls == [], "must not touch the network on a blocked retry"

    def test_different_text_after_unknown_still_publishes(self):
        _run({"text": "hello"}, transport=_transport(raise_on="publish_v6"))
        assert _run({"text": "a different post"})["success"] is True

    def test_retry_is_allowed_once_the_guard_expires(self, monkeypatch):
        _run({"text": "hello"}, transport=_transport(raise_on="publish_v6"))
        monkeypatch.setattr(state, "UNKNOWN_PUBLISH_GUARD_SECS", 0)
        assert _run({"text": "hello"})["success"] is True

    def test_unknown_body_still_feeds_the_anti_repeat_corpus(self):
        _run({"text": "去图书馆吹凉风"}, transport=_transport(raise_on="publish_v6"))
        assert [e["text"] for e in state.post_log_entries()] == ["去图书馆吹凉风"]

    def test_persona_argument_routes_to_its_own_log(self, _isolated_state):
        _run({"text": "hi", "persona_id": "other"})
        assert (_isolated_state / "qzone_post_log" / "other.json").is_file()
        assert not (_isolated_state / "qzone_post_log" / "grantley.json").exists()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_schema_is_hermes_shaped():
    schema = publish.QZONE_PUBLISH_SCHEMA
    assert schema["name"] == "qzone_publish"
    assert schema["parameters"]["type"] == "object"
    assert schema["parameters"]["required"] == []
    assert "text" in schema["parameters"]["properties"]
