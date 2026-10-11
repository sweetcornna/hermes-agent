"""Exercise QQ tool transport against real local backends and isolated profiles."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import websockets
import yaml

from agent import secret_scope
from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
from plugins.platforms.onebot.client import OneBotClient, OneBotConfig, set_live_client
from tools.onebot_client import onebot_access_token, onebot_base_url, onebot_call, onebot_configured


@pytest.fixture(autouse=True)
def isolated_backend(monkeypatch):
    for name in ("ONEBOT_HTTP_URL", "ONEBOT_WS_URL", "ONEBOT_ACCESS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    previous_mode = secret_scope.is_multiplex_active()
    scope = secret_scope.set_secret_scope(None)
    secret_scope.set_multiplex_active(False)
    set_live_client(None)
    yield
    set_live_client(None)
    secret_scope.reset_secret_scope(scope)
    secret_scope.set_multiplex_active(previous_mode)


@pytest.fixture
def http_backend():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append({
                "action": self.path.lstrip("/"),
                "params": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                "authorization": self.headers.get("Authorization"),
            })
            data = {
                "get_login_info": {"user_id": 123456789},
                "get_cookies": {"cookies": "uin=o123456789; p_skey=test-qzone-key; skey=test-key"},
                "get_friend_list": [{"user_id": 987654321}],
            }.get(calls[-1]["action"], {"message_id": 42})
            body = json.dumps({"status": "ok", "retcode": 0, "data": data}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def write_platform_config(settings, *, nested=True):
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    config = {"platforms": {"onebot": {"extra": settings} if nested else settings}}
    path = home / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


@pytest.mark.parametrize("nested", [True, False])
def test_yaml_only_endpoint_enables_tools_and_borrows_qzone_auth(http_backend, nested):
    from plugins.qq_voice import _check_qq_voice_available
    from plugins.qzone import _check_qzone_available
    from plugins.qzone.client import qzone_auth

    url, calls = http_backend
    path = write_platform_config({"http_url": url}, nested=nested)
    (path.parent / ".env").write_text("ONEBOT_ACCESS_TOKEN=test-profile-token\n")
    before = path.read_bytes()

    assert onebot_configured() and _check_qq_voice_available() and _check_qzone_available()
    auth = qzone_auth()

    assert auth.uin == "123456789"
    assert [call["action"] for call in calls] == ["get_login_info", "get_cookies"]
    assert all(call["authorization"] == "Bearer test-profile-token" for call in calls)
    assert path.read_bytes() == before


def test_yaml_backend_wins_over_stale_legacy_endpoints(monkeypatch):
    monkeypatch.setenv("ONEBOT_HTTP_URL", "http://stale.invalid")
    monkeypatch.setenv("ONEBOT_WS_URL", "ws://stale.invalid")
    write_platform_config({"ws_url": "ws://127.0.0.1:3301/"})
    assert onebot_base_url() == "ws://127.0.0.1:3301"


def test_profile_dotenv_endpoint_works_without_exporting_environment(http_backend):
    url, calls = http_backend
    get_hermes_home().mkdir(parents=True, exist_ok=True)
    (get_hermes_home() / ".env").write_text(
        f"ONEBOT_HTTP_URL={url}\nONEBOT_ACCESS_TOKEN=dotenv-token\n"
    )
    assert onebot_call("get_login_info", prefer_live=False)["user_id"] == 123456789
    assert calls[0]["authorization"] == "Bearer dotenv-token"


def test_multiplex_profile_config_and_secrets_do_not_borrow_global_values(
    tmp_path, monkeypatch, http_backend
):
    url, calls = http_backend
    monkeypatch.setenv("ONEBOT_HTTP_URL", "http://other-profile.invalid")
    monkeypatch.setenv("ONEBOT_ACCESS_TOKEN", "other-profile-token")
    secret_scope.set_multiplex_active(True)
    for name, token in (("alice", "alice-token"), ("bob", "bob-token"), ("empty", None)):
        home_scope = set_hermes_home_override(tmp_path / name)
        credentials = secret_scope.set_secret_scope(
            {"ONEBOT_ACCESS_TOKEN": token} if token else {}
        )
        try:
            write_platform_config({"http_url": url})
            onebot_call("get_login_info", prefer_live=False)
        finally:
            secret_scope.reset_secret_scope(credentials)
            reset_hermes_home_override(home_scope)
    assert [call["authorization"] for call in calls] == ["Bearer alice-token", "Bearer bob-token", None]


def test_scoped_missing_endpoint_never_uses_another_profile(monkeypatch):
    monkeypatch.setenv("ONEBOT_WS_URL", "ws://other-profile.invalid")
    monkeypatch.setenv("ONEBOT_ACCESS_TOKEN", "other-profile-token")
    secret_scope.set_multiplex_active(True)
    token = secret_scope.set_secret_scope({})
    try:
        assert not onebot_configured()
        assert onebot_access_token() == ""
    finally:
        secret_scope.reset_secret_scope(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("inside_event_loop", [False, True])
async def test_yaml_websocket_roundtrip_uses_scoped_auth(inside_event_loop):
    received = []

    async def handler(ws):
        request = json.loads(await ws.recv())
        received.append((request, ws.request.headers.get("Authorization")))
        await ws.send(json.dumps({"status": "ok", "retcode": 0,
            "data": {"user_id": 123456789}, "echo": request["echo"]}))

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        write_platform_config({"ws_url": url})
        secret_scope.set_multiplex_active(True)
        scope = secret_scope.set_secret_scope({"ONEBOT_ACCESS_TOKEN": "ws-profile-token"})
        try:
            if inside_event_loop:
                async def call_from_loop():
                    return onebot_call("get_login_info")

                # The synchronous wrapper has to start its own worker here.
                # That worker must retain both the profile home and secrets.
                result = await asyncio.to_thread(asyncio.run, call_from_loop())
            else:
                result = await asyncio.to_thread(onebot_call, "get_login_info")
        finally:
            secret_scope.reset_secret_scope(scope)
    assert result == {"user_id": 123456789}
    assert received[0][1] == "Bearer ws-profile-token"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["send_msg", "send_group_ai_record", "custom_extension"])
async def test_uncertain_live_action_is_never_resent_over_direct_transport(http_backend, action):
    from plugins.platforms.onebot.protocol import RawAction

    url, direct_calls = http_backend
    write_platform_config({"http_url": url})
    live_calls = []

    async def handler(ws):
        async for raw in ws:
            request = json.loads(raw)
            if request["action"] == "get_status":
                await ws.send(json.dumps({"status": "ok", "retcode": 0,
                    "data": {"online": True}, "echo": request["echo"]}))
            else:
                # The action crossed a real socket; its reply was lost.
                live_calls.append(request)

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        client = OneBotClient(OneBotConfig(
            url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"))
        await client.connect()
        try:
            await client.call_action(RawAction("get_status", {}), timeout=5)
            set_live_client(client, asyncio.get_running_loop())
            with pytest.raises(RuntimeError, match="unknown outcome.*not retried"):
                await asyncio.to_thread(onebot_call, action, {"user_id": 123456789}, timeout=2)
            assert [call["action"] for call in live_calls] == [action]
            assert direct_calls == []
        finally:
            set_live_client(None)
            await client.close()


@pytest.mark.asyncio
async def test_known_read_can_fall_back_after_live_timeout(http_backend):
    from plugins.platforms.onebot.protocol import RawAction

    url, direct_calls = http_backend
    write_platform_config({"http_url": url})
    live_calls = []

    async def handler(ws):
        async for raw in ws:
            request = json.loads(raw)
            if request["action"] == "get_status":
                await ws.send(json.dumps({"status": "ok", "retcode": 0,
                    "data": {"online": True}, "echo": request["echo"]}))
            else:
                live_calls.append(request)

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        client = OneBotClient(OneBotConfig(
            url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"))
        await client.connect()
        try:
            await client.call_action(RawAction("get_status", {}), timeout=5)
            set_live_client(client, asyncio.get_running_loop())
            result = await asyncio.to_thread(onebot_call, "get_friend_list", timeout=2)
            assert result == [{"user_id": 987654321}]
            assert [call["action"] for call in live_calls] == ["get_friend_list"]
            assert [call["action"] for call in direct_calls] == ["get_friend_list"]
        finally:
            set_live_client(None)
            await client.close()
