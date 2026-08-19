"""Tests for the shared synchronous OneBot client used by QQ-backed tools.

This is the surface the Qzone tool family builds on: a tool handler is
synchronous, so it needs ``onebot_call("get_cookies", {...})`` to work from
a plain function, whether or not a gateway is running in the process.

Exercised with a mocked ``urlopen`` and a fake WebSocket — no network.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from tools.onebot_client import (
    onebot_access_token,
    onebot_base_url,
    onebot_call,
    onebot_configured,
)


class _FakeHTTPResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeWS:
    """A fake OneBot WebSocket that answers our echo, optionally after noise."""

    def __init__(self, data=None, status="ok", retcode=0, message=None, lead_events=0):
        self._data = data
        self._status = status
        self._retcode = retcode
        self._message = message
        self._lead_events = lead_events
        self.sent = []
        self._answered = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, payload):
        self.sent.append(payload)

    async def recv(self):
        if self._lead_events > 0:
            self._lead_events -= 1
            return json.dumps({"post_type": "meta_event",
                               "meta_event_type": "heartbeat"})
        if self._answered:  # the round trip should already have returned
            await asyncio.sleep(3600)
        self._answered = True
        echo = json.loads(self.sent[-1])["echo"]
        reply = {"status": self._status, "retcode": self._retcode, "echo": echo}
        if self._message is not None:
            reply["message"] = self._message
        if self._status != "failed" and self._data is not None:
            reply["data"] = self._data
        return json.dumps(reply)


def _fake_connect(fake_ws):
    def _connect(uri, **kwargs):
        _connect.last_uri = uri
        _connect.last_kwargs = kwargs
        return fake_ws
    _connect.last_uri = None
    _connect.last_kwargs = {}
    return _connect


@pytest.fixture(autouse=True)
def _no_live_adapter(monkeypatch):
    """Default: no gateway in-process, so the direct transports are used."""
    monkeypatch.setattr(
        "plugins.platforms.onebot.client.get_live_client", lambda: (None, None))


class TestConfig:

    def test_base_url_strips_trailing_slash(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_HTTP_URL", "http://127.0.0.1:3000/")
        assert onebot_base_url() == "http://127.0.0.1:3000"

    def test_base_url_strips_whitespace(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_HTTP_URL", "  http://host:3000  ")
        assert onebot_base_url() == "http://host:3000"

    def test_base_url_empty_when_unset(self, monkeypatch):
        monkeypatch.delenv("ONEBOT_HTTP_URL", raising=False)
        monkeypatch.delenv("ONEBOT_WS_URL", raising=False)
        assert onebot_base_url() == ""

    def test_base_url_falls_back_to_ws_url(self, monkeypatch):
        monkeypatch.delenv("ONEBOT_HTTP_URL", raising=False)
        monkeypatch.setenv("ONEBOT_WS_URL", "ws://127.0.0.1:3001")
        assert onebot_base_url() == "ws://127.0.0.1:3001"

    def test_http_url_wins_over_ws_url(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_HTTP_URL", "http://127.0.0.1:3000")
        monkeypatch.setenv("ONEBOT_WS_URL", "ws://127.0.0.1:3001")
        assert onebot_base_url() == "http://127.0.0.1:3000"

    def test_access_token_strips_whitespace(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_ACCESS_TOKEN", "  tok123  ")
        assert onebot_access_token() == "tok123"

    def test_access_token_empty_when_unset(self, monkeypatch):
        monkeypatch.delenv("ONEBOT_ACCESS_TOKEN", raising=False)
        assert onebot_access_token() == ""


class TestConfigured:
    """``onebot_configured`` gates the tools out of the model schema."""

    def test_configured_when_http_url_set(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_HTTP_URL", "http://127.0.0.1:3000")
        assert onebot_configured() is True

    def test_configured_when_only_ws_url_set(self, monkeypatch):
        monkeypatch.delenv("ONEBOT_HTTP_URL", raising=False)
        monkeypatch.setenv("ONEBOT_WS_URL", "ws://127.0.0.1:3001")
        assert onebot_configured() is True

    def test_not_configured_when_unset(self, monkeypatch):
        monkeypatch.delenv("ONEBOT_HTTP_URL", raising=False)
        monkeypatch.delenv("ONEBOT_WS_URL", raising=False)
        assert onebot_configured() is False

    def test_not_configured_when_blank(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_HTTP_URL", "   ")
        monkeypatch.delenv("ONEBOT_WS_URL", raising=False)
        assert onebot_configured() is False


class TestHttpTransport:

    def test_raises_when_url_unconfigured(self, monkeypatch):
        monkeypatch.delenv("ONEBOT_HTTP_URL", raising=False)
        monkeypatch.delenv("ONEBOT_WS_URL", raising=False)
        with pytest.raises(RuntimeError, match="ONEBOT_WS_URL"):
            onebot_call("get_login_info")

    def test_returns_data_on_success(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_HTTP_URL", "http://127.0.0.1:3000")
        body = json.dumps({"status": "ok", "retcode": 0,
                           "data": {"message_id": 7}}).encode()
        with patch("urllib.request.urlopen", return_value=_FakeHTTPResponse(body)):
            assert onebot_call("send_msg", {"message": []}) == {"message_id": 7}

    def test_raises_on_failed_status(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_HTTP_URL", "http://127.0.0.1:3000")
        body = json.dumps({"status": "failed", "retcode": 1404,
                           "message": "no login"}).encode()
        with patch("urllib.request.urlopen", return_value=_FakeHTTPResponse(body)):
            with pytest.raises(RuntimeError, match="no login"):
                onebot_call("send_msg")

    def test_raises_on_missing_data(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_HTTP_URL", "http://127.0.0.1:3000")
        body = json.dumps({"status": "ok", "retcode": 0}).encode()
        with patch("urllib.request.urlopen", return_value=_FakeHTTPResponse(body)):
            with pytest.raises(RuntimeError, match="no data"):
                onebot_call("send_msg")

    def test_raises_on_non_json(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_HTTP_URL", "http://127.0.0.1:3000")
        with patch("urllib.request.urlopen",
                   return_value=_FakeHTTPResponse(b"<html>502</html>")):
            with pytest.raises(RuntimeError, match="non-JSON"):
                onebot_call("send_msg")

    def test_auth_header_sent_when_token_configured(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_HTTP_URL", "http://127.0.0.1:3000")
        monkeypatch.setenv("ONEBOT_ACCESS_TOKEN", "secret")
        body = json.dumps({"status": "ok", "data": {"ok": 1}}).encode()
        captured = {}

        def _fake_urlopen(req, timeout=None):
            captured["auth"] = req.headers.get("Authorization")
            return _FakeHTTPResponse(body)

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            onebot_call("get_status")
        assert captured["auth"] == "Bearer secret"

    def test_http_error_does_not_echo_the_response_body(self, monkeypatch):
        """A risk-control interstitial must not land in the model's context."""
        import urllib.error
        monkeypatch.setenv("ONEBOT_HTTP_URL", "http://127.0.0.1:3000")

        class _Err(urllib.error.HTTPError):
            def __init__(self):
                super().__init__("http://x", 403, "Forbidden", {}, None)

            def read(self):
                return b"<html>SECRET RISK PAGE</html>"

        with patch("urllib.request.urlopen", side_effect=_Err()):
            with pytest.raises(RuntimeError) as exc:
                onebot_call("send_msg")
        assert "403" in str(exc.value)
        assert "SECRET RISK PAGE" not in str(exc.value)

    def test_no_retry_on_failure(self, monkeypatch):
        """Tools never silently retry — a resend could double-post."""
        monkeypatch.setenv("ONEBOT_HTTP_URL", "http://127.0.0.1:3000")
        calls = {"n": 0}

        def _boom(req, timeout=None):
            calls["n"] += 1
            raise OSError("down")

        import urllib.error
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("down")):
            with pytest.raises(RuntimeError, match="Cannot reach OneBot"):
                onebot_call("send_msg")


class TestWebSocketTransport:

    def test_returns_data_on_success(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_HTTP_URL", "ws://127.0.0.1:3001")
        monkeypatch.delenv("ONEBOT_ACCESS_TOKEN", raising=False)
        fake = _FakeWS(data={"user_id": 42}, lead_events=3)
        with patch("websockets.connect", _fake_connect(fake)):
            assert onebot_call("get_login_info") == {"user_id": 42}
        assert json.loads(fake.sent[-1])["action"] == "get_login_info"

    def test_uses_ws_url_fallback(self, monkeypatch):
        monkeypatch.delenv("ONEBOT_HTTP_URL", raising=False)
        monkeypatch.setenv("ONEBOT_WS_URL", "ws://127.0.0.1:3001")
        monkeypatch.delenv("ONEBOT_ACCESS_TOKEN", raising=False)
        fake = _FakeWS(data={"message_id": 9})
        with patch("websockets.connect", _fake_connect(fake)):
            assert onebot_call("send_msg", {"message": []}) == {"message_id": 9}

    def test_raises_on_failed_status(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_HTTP_URL", "ws://127.0.0.1:3001")
        fake = _FakeWS(status="failed", retcode=1400, message="bad request")
        with patch("websockets.connect", _fake_connect(fake)):
            with pytest.raises(RuntimeError, match="bad request"):
                onebot_call("send_msg")

    def test_raises_on_missing_data(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_HTTP_URL", "ws://127.0.0.1:3001")
        fake = _FakeWS(data=None)
        with patch("websockets.connect", _fake_connect(fake)):
            with pytest.raises(RuntimeError, match="no data"):
                onebot_call("get_login_info")

    def test_token_goes_in_both_the_uri_and_the_handshake_header(self, monkeypatch):
        """OneBot allows either; sending both survives a header-rewriting proxy."""
        monkeypatch.setenv("ONEBOT_HTTP_URL", "ws://127.0.0.1:3001")
        monkeypatch.setenv("ONEBOT_ACCESS_TOKEN", "s3cr3t")
        fake = _FakeWS(data={"ok": 1})
        connect = _fake_connect(fake)
        with patch("websockets.connect", connect):
            onebot_call("get_status")
        assert "access_token=s3cr3t" in connect.last_uri
        assert ("Authorization", "Bearer s3cr3t") in connect.last_kwargs["additional_headers"]

    def test_no_token_no_query_and_no_header(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_HTTP_URL", "ws://127.0.0.1:3001")
        monkeypatch.delenv("ONEBOT_ACCESS_TOKEN", raising=False)
        fake = _FakeWS(data={"ok": 1})
        connect = _fake_connect(fake)
        with patch("websockets.connect", connect):
            onebot_call("get_status")
        assert "access_token" not in connect.last_uri
        assert not connect.last_kwargs["additional_headers"]

    def test_qzone_cookie_borrow_shape(self, monkeypatch):
        """The exact call the Qzone tools will make."""
        monkeypatch.setenv("ONEBOT_WS_URL", "ws://127.0.0.1:3001")
        monkeypatch.delenv("ONEBOT_HTTP_URL", raising=False)
        fake = _FakeWS(data={"cookies": "p_skey=abc; skey=def"})
        with patch("websockets.connect", _fake_connect(fake)):
            data = onebot_call("get_cookies", {"domain": "user.qzone.qq.com"})
        assert data["cookies"].startswith("p_skey=")
        sent = json.loads(fake.sent[-1])
        assert sent["params"] == {"domain": "user.qzone.qq.com"}


class TestLiveAdapterReuse:
    """Prefer the running gateway's connection over opening a second one."""

    def test_uses_the_live_client_when_one_is_registered(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_WS_URL", "ws://127.0.0.1:3001")
        seen = {}

        class _Client:
            async def call_action(self, action, timeout=None):
                seen["action"] = action.action
                seen["params"] = action.params
                return {"status": "ok", "retcode": 0, "data": {"user_id": 12345}}

        loop = asyncio.new_event_loop()
        import threading
        t = threading.Thread(target=loop.run_forever, daemon=True)
        t.start()
        try:
            monkeypatch.setattr(
                "plugins.platforms.onebot.client.get_live_client",
                lambda: (_Client(), loop))
            # If this fell through to the direct transport it would try to
            # open a real socket to 127.0.0.1:3001 and fail.
            data = onebot_call("get_login_info")
        finally:
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=5)
            loop.close()
        assert data == {"user_id": 12345}
        assert seen["action"] == "get_login_info"

    def test_falls_back_when_the_live_call_fails(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_WS_URL", "ws://127.0.0.1:3001")

        class _Client:
            async def call_action(self, action, timeout=None):
                raise RuntimeError("adapter is unhappy")

        loop = asyncio.new_event_loop()
        import threading
        t = threading.Thread(target=loop.run_forever, daemon=True)
        t.start()
        fake = _FakeWS(data={"user_id": 1})
        try:
            monkeypatch.setattr(
                "plugins.platforms.onebot.client.get_live_client",
                lambda: (_Client(), loop))
            with patch("websockets.connect", _fake_connect(fake)):
                data = onebot_call("get_login_info")
        finally:
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=5)
            loop.close()
        assert data == {"user_id": 1}

    def test_prefer_live_can_be_switched_off(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_WS_URL", "ws://127.0.0.1:3001")

        class _Client:
            async def call_action(self, action, timeout=None):
                raise AssertionError("must not be consulted")

        monkeypatch.setattr(
            "plugins.platforms.onebot.client.get_live_client",
            lambda: (_Client(), asyncio.new_event_loop()))
        fake = _FakeWS(data={"ok": 1})
        with patch("websockets.connect", _fake_connect(fake)):
            assert onebot_call("get_status", prefer_live=False) == {"ok": 1}
