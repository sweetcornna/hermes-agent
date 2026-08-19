"""Transport tests for the OneBot (QQ) WebSocket client.

Runs a real ``websockets`` server in-process on an ephemeral port — no
network, no NapCat. What is pinned here:

* the access token is sent BOTH as a handshake header and as a query
  parameter (the two source implementations picked one each);
* ``echo`` correlates a response envelope back to its caller;
* the inbound queue drops the OLDEST event on overflow, never the newest;
* a failed write requeues at the FRONT (ordering) and a poison payload is
  dropped after two attempts instead of looping forever;
* only ``message`` post types surface; meta/notice/request are absorbed.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any, Callable, Dict, List

import pytest
import websockets
from websockets.asyncio.server import serve

from plugins.platforms.onebot.client import (
    OneBotClient,
    OneBotConfig,
    OneBotConfigError,
    OneBotTransportError,
    get_live_client,
    set_live_client,
)
from plugins.platforms.onebot.protocol import (
    MessageEvent,
    RawAction,
    SendGroupMsg,
    TextSegment,
    UploadGroupFile,
    parse_event,
)


@asynccontextmanager
async def ws_server(handler: Callable):
    """Start a one-off WebSocket server on an ephemeral localhost port."""
    server = await serve(handler, "127.0.0.1", 0)
    try:
        socks = server.sockets or []
        if not socks:  # pragma: no cover - defensive
            raise RuntimeError("ws server has no socket")
        yield f"ws://127.0.0.1:{socks[0].getsockname()[1]}"
    finally:
        server.close()
        await server.wait_closed()


GROUP_FRAME = {
    "post_type": "message", "message_type": "group",
    "self_id": 100, "user_id": 555, "group_id": 12345, "message_id": 42,
    "message": [{"type": "at", "data": {"qq": "100"}},
                {"type": "text", "data": {"text": "hello"}}],
    "raw_message": "@100 hello", "time": 1_700_000_000,
}


async def _first_event(client: OneBotClient, timeout: float = 5.0):
    async def _pull():
        async for ev in client.inbound():
            return ev
        return None
    return await asyncio.wait_for(_pull(), timeout=timeout)


class TestConfigValidation:

    def test_empty_url_raises_config_error(self):
        with pytest.raises(OneBotConfigError):
            OneBotClient(OneBotConfig(url=""))

    def test_whitespace_url_raises_config_error(self):
        with pytest.raises(OneBotConfigError):
            OneBotClient(OneBotConfig(url="   "))


class TestHandshakeAuth:
    """The token goes in both places; either alone is a silent 401 risk."""

    def test_token_sent_as_header_and_query(self):
        c = OneBotClient(OneBotConfig(url="ws://h:3001", access_token="s3cr3t"))
        uri, headers = c._handshake()
        assert ("Authorization", "Bearer s3cr3t") in headers
        assert "access_token=s3cr3t" in uri

    def test_query_can_be_disabled_for_strict_backends(self):
        c = OneBotClient(OneBotConfig(url="ws://h:3001", access_token="s3cr3t",
                                      token_in_query=False))
        uri, headers = c._handshake()
        assert ("Authorization", "Bearer s3cr3t") in headers
        assert "access_token" not in uri

    def test_no_token_means_no_header_and_no_query(self):
        c = OneBotClient(OneBotConfig(url="ws://h:3001"))
        uri, headers = c._handshake()
        assert headers == [] and uri == "ws://h:3001"

    def test_query_separator_respects_an_existing_query_string(self):
        c = OneBotClient(OneBotConfig(url="ws://h:3001/onebot?x=1", access_token="t"))
        uri, _ = c._handshake()
        assert uri.endswith("?x=1&access_token=t")

    def test_token_is_url_encoded(self):
        c = OneBotClient(OneBotConfig(url="ws://h:3001", access_token="a b/c"))
        uri, _ = c._handshake()
        assert "access_token=a%20b/c" in uri or "access_token=a%20b%2Fc" in uri


class TestIntegration:

    @pytest.mark.asyncio
    async def test_yields_group_message_event(self):
        async def handler(ws):
            await ws.send(json.dumps(GROUP_FRAME))
            try:
                async for _ in ws:
                    pass
            except Exception:
                pass

        async with ws_server(handler) as url:
            async with OneBotClient(OneBotConfig(url=url, self_ids=[100])) as client:
                ev = await _first_event(client)
                assert isinstance(ev, MessageEvent)
                assert ev.group_id == 12345 and ev.user_id == 555
                assert ev.message_id == 42

    @pytest.mark.asyncio
    async def test_handshake_headers_reach_the_server(self):
        seen: Dict[str, Any] = {}

        async def handler(ws):
            seen["auth"] = ws.request.headers.get("Authorization")
            seen["path"] = ws.request.path
            await ws.send(json.dumps(GROUP_FRAME))
            try:
                async for _ in ws:
                    pass
            except Exception:
                pass

        async with ws_server(handler) as url:
            async with OneBotClient(OneBotConfig(url=url, access_token="tok")) as client:
                await _first_event(client)
        assert seen["auth"] == "Bearer tok"
        assert "access_token=tok" in seen["path"]

    @pytest.mark.asyncio
    async def test_non_message_events_are_absorbed(self):
        async def handler(ws):
            await ws.send(json.dumps({"post_type": "meta_event",
                                      "meta_event_type": "heartbeat",
                                      "self_id": 100, "time": 1}))
            await ws.send(json.dumps({"post_type": "notice", "notice_type": "x",
                                      "self_id": 100, "time": 1}))
            await ws.send(json.dumps({
                "post_type": "message", "message_type": "private",
                "self_id": 100, "user_id": 200, "message_id": 7,
                "message": [{"type": "text", "data": {"text": "yo"}}], "time": 2}))
            try:
                async for _ in ws:
                    pass
            except Exception:
                pass

        async with ws_server(handler) as url:
            async with OneBotClient(OneBotConfig(url=url)) as client:
                ev = await _first_event(client)
                assert isinstance(ev, MessageEvent) and ev.message_id == 7

    @pytest.mark.asyncio
    async def test_heartbeat_reveals_self_id_before_any_message(self):
        seen: List[int] = []

        async def handler(ws):
            await ws.send(json.dumps({"post_type": "meta_event",
                                      "meta_event_type": "heartbeat",
                                      "self_id": 123456, "time": 1,
                                      "status": {"online": True}}))
            await asyncio.sleep(0.2)

        async with ws_server(handler) as url:
            async with OneBotClient(OneBotConfig(url=url), on_self_id=seen.append) as client:
                async def wait():
                    while client.last_self_id is None:
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(wait(), timeout=5.0)
                assert client.last_self_id == 123456
                assert seen == [123456]
                assert client.last_status_online is True

    @pytest.mark.asyncio
    async def test_self_id_observer_only_fires_on_change(self):
        seen: List[int] = []

        async def handler(ws):
            for self_id in (0, 100, 100, 200):
                await ws.send(json.dumps({"post_type": "meta_event",
                                          "meta_event_type": "heartbeat",
                                          "self_id": self_id, "time": 1,
                                          "status": {"online": True}}))
            await asyncio.sleep(0.2)

        async with ws_server(handler) as url:
            async with OneBotClient(OneBotConfig(url=url), on_self_id=seen.append) as client:
                async def wait():
                    while client.last_self_id != 200:
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(wait(), timeout=5.0)
                assert seen == [100, 200]

    @pytest.mark.asyncio
    async def test_self_id_observer_failure_does_not_break_the_pump(self):
        def boom(_self_id):
            raise RuntimeError("observer detail")

        async def handler(ws):
            await ws.send(json.dumps({
                "post_type": "message", "message_type": "private",
                "self_id": 100, "user_id": 200, "message_id": 7,
                "message": [{"type": "text", "data": {"text": "yo"}}], "time": 2}))
            await asyncio.sleep(0.2)

        async with ws_server(handler) as url:
            async with OneBotClient(OneBotConfig(url=url), on_self_id=boom) as client:
                ev = await _first_event(client)
                assert isinstance(ev, MessageEvent)
                assert client.last_self_id == 100

    @pytest.mark.asyncio
    async def test_self_id_observer_retries_after_a_failure(self):
        attempts: List[int] = []

        def fail_once(self_id):
            attempts.append(self_id)
            if len(attempts) == 1:
                raise RuntimeError("transient observer failure")

        async def handler(ws):
            for _ in range(2):
                await ws.send(json.dumps({"post_type": "meta_event",
                                          "meta_event_type": "heartbeat",
                                          "self_id": 100, "time": 1,
                                          "status": {"online": True}}))
            await asyncio.sleep(0.2)

        async with ws_server(handler) as url:
            async with OneBotClient(OneBotConfig(url=url), on_self_id=fail_once) as client:
                async def wait():
                    while len(attempts) < 2:
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(wait(), timeout=5.0)
                assert attempts == [100, 100]

    @pytest.mark.asyncio
    async def test_send_action_round_trips(self):
        received: List[str] = []

        async def handler(ws):
            try:
                async for raw in ws:
                    received.append(raw if isinstance(raw, str) else raw.decode())
                    break
            except Exception:
                pass

        async with ws_server(handler) as url:
            async with OneBotClient(OneBotConfig(url=url)) as client:
                await asyncio.sleep(0.1)
                await client.send_action(
                    SendGroupMsg(group_id=10, message=[TextSegment(text="hi")]))
                for _ in range(40):
                    if received:
                        break
                    await asyncio.sleep(0.05)
        assert received, "server never received a frame"
        payload = json.loads(received[0])
        assert payload["action"] == "send_group_msg"
        assert payload["params"]["message"][0]["data"]["text"] == "hi"
        assert "echo" not in payload  # fire-and-forget carries no echo

    @pytest.mark.asyncio
    async def test_call_action_correlates_response_by_echo(self):
        async def handler(ws):
            try:
                async for raw in ws:
                    frame = json.loads(raw)
                    if frame.get("action") == "upload_group_file":
                        await ws.send(json.dumps({
                            "status": "failed", "retcode": 1200, "data": None,
                            "message": "file not found", "echo": frame["echo"]}))
            except Exception:
                pass

        async with ws_server(handler) as url:
            async with OneBotClient(OneBotConfig(url=url)) as client:
                await asyncio.sleep(0.1)
                resp = await asyncio.wait_for(
                    client.call_action(
                        UploadGroupFile(group_id=1, file="base64://x", name="a.py")),
                    timeout=5.0)
        assert resp["retcode"] == 1200 and resp["message"] == "file not found"

    @pytest.mark.asyncio
    async def test_call_action_response_never_reaches_the_event_queue(self):
        """A response envelope is not an event."""
        async def handler(ws):
            try:
                async for raw in ws:
                    frame = json.loads(raw)
                    await ws.send(json.dumps({"status": "ok", "retcode": 0,
                                              "data": {"user_id": 1},
                                              "echo": frame["echo"]}))
                    await ws.send(json.dumps(GROUP_FRAME))
            except Exception:
                pass

        async with ws_server(handler) as url:
            async with OneBotClient(OneBotConfig(url=url)) as client:
                await asyncio.sleep(0.1)
                resp = await asyncio.wait_for(
                    client.call_action(RawAction("get_login_info")), timeout=5.0)
                assert resp["data"] == {"user_id": 1}
                ev = await _first_event(client)
                assert isinstance(ev, MessageEvent) and ev.message_id == 42

    @pytest.mark.asyncio
    async def test_call_action_times_out_without_a_response(self):
        async def handler(ws):
            try:
                async for _ in ws:
                    pass  # swallow, never answer
            except Exception:
                pass

        async with ws_server(handler) as url:
            async with OneBotClient(OneBotConfig(url=url)) as client:
                await asyncio.sleep(0.1)
                with pytest.raises((asyncio.TimeoutError, TimeoutError)):
                    await client.call_action(
                        UploadGroupFile(group_id=1, file="base64://x"), timeout=0.3)

    @pytest.mark.asyncio
    async def test_close_fails_inflight_callers_immediately(self):
        async def handler(ws):
            try:
                async for _ in ws:
                    pass
            except Exception:
                pass

        async with ws_server(handler) as url:
            client = OneBotClient(OneBotConfig(url=url))
            await client.connect()
            await asyncio.sleep(0.1)
            task = asyncio.create_task(
                client.call_action(RawAction("get_status"), timeout=30))
            await asyncio.sleep(0.1)
            await client.close()
            with pytest.raises(OneBotTransportError):
                await asyncio.wait_for(task, timeout=5.0)

    @pytest.mark.asyncio
    async def test_send_on_closed_client_raises(self):
        client = OneBotClient(OneBotConfig(url="ws://127.0.0.1:1"))
        await client.close()
        with pytest.raises(OneBotTransportError):
            await client.send_action(SendGroupMsg(group_id=1, message=[]))


class TestHeartbeatStatus:
    """``status.online`` is the only signal for a kicked-offline account."""

    @pytest.mark.parametrize(("status", "expected"), [
        ({"online": True}, True),
        ({"online": 1}, True),
        ({"online": False}, False),
        ({"app": {"online": True}}, True),
        ({"good": True}, True),
        ({}, False),          # heartbeat with no flag at all
        (None, False),
    ])
    def test_status_shapes(self, status, expected):
        c = OneBotClient(OneBotConfig(url="ws://x"))
        frame = {"post_type": "meta_event", "meta_event_type": "heartbeat"}
        if status is not None:
            frame["status"] = status
        c._absorb_status(frame, 1234)
        assert c.last_status_online is expected
        assert c.last_status_online_at_ms == 1234

    def test_a_real_message_implies_the_account_is_up(self):
        c = OneBotClient(OneBotConfig(url="ws://x"))
        c._absorb_status({"post_type": "message"}, 99)
        assert c.last_status_online is True

    def test_non_heartbeat_meta_event_leaves_status_untouched(self):
        c = OneBotClient(OneBotConfig(url="ws://x"))
        c._absorb_status({"post_type": "meta_event", "meta_event_type": "lifecycle"}, 1)
        assert c.last_status_online is None


class TestInboundOverflow:
    """Drop the OLDEST — the message the user just sent is the one that matters."""

    def test_overflow_drops_oldest_and_counts(self):
        client = OneBotClient(OneBotConfig(url="ws://127.0.0.1:1"))
        client._inbound_q = asyncio.Queue(maxsize=2)
        assert client.inbound_dropped_count == 0
        for i in (1, 2, 3, 4):
            client._enqueue_inbound(parse_event({
                "post_type": "message", "message_type": "private",
                "self_id": 1, "user_id": 1, "message_id": i,
                "message": [{"type": "text", "data": {"text": f"m{i}"}}], "time": i}))
        assert client._inbound_q.qsize() == 2
        assert client.inbound_dropped_count == 2
        first = client._inbound_q.get_nowait()
        second = client._inbound_q.get_nowait()
        assert (first.message_id, second.message_id) == (3, 4)


class TestWriterRequeue:

    class _FailingWs:
        def __init__(self, fail_predicate):
            self.sent: List[str] = []
            self._fail = fail_predicate

        async def send(self, payload: str) -> None:
            if self._fail(payload):
                raise RuntimeError("simulated transport failure")
            self.sent.append(payload)

    @pytest.mark.asyncio
    async def test_send_failure_requeues_at_the_front(self):
        client = OneBotClient(OneBotConfig(url="ws://127.0.0.1:1"))
        action = SendGroupMsg(group_id=42, message=[TextSegment(text="ping")])
        attempts = {"n": 0}

        def fail_first(_payload):
            attempts["n"] += 1
            return attempts["n"] == 1

        ws = self._FailingWs(fail_first)
        await client._outbound_q.put(action)
        with pytest.raises(OneBotTransportError):
            await client._writer_loop(ws)
        assert list(client._outbound_front) == [action]
        assert client._outbound_retries.get(id(action)) == 1

        task = asyncio.create_task(client._writer_loop(ws))
        for _ in range(40):
            if ws.sent:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert len(ws.sent) == 1, f"expected exactly one wire send, got {ws.sent}"
        assert json.loads(ws.sent[0])["params"]["group_id"] == 42
        assert id(action) not in client._outbound_retries

    @pytest.mark.asyncio
    async def test_two_failures_drop_the_poison_action(self):
        client = OneBotClient(OneBotConfig(url="ws://127.0.0.1:1"))
        poison = SendGroupMsg(group_id=1, message=[TextSegment(text="x")])
        good = SendGroupMsg(group_id=2, message=[TextSegment(text="ok")])
        ws = self._FailingWs(lambda p: json.loads(p)["params"]["group_id"] == 1)
        await client._outbound_q.put(poison)
        await client._outbound_q.put(good)

        with pytest.raises(OneBotTransportError):
            await client._writer_loop(ws)

        task = asyncio.create_task(client._writer_loop(ws))
        for _ in range(60):
            if ws.sent:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert ws.sent, "the non-poison action must still land"
        assert json.loads(ws.sent[0])["params"]["group_id"] == 2
        assert id(poison) not in client._outbound_retries

    @pytest.mark.asyncio
    async def test_cancellation_requeues_without_counting_a_retry(self):
        client = OneBotClient(OneBotConfig(url="ws://127.0.0.1:1"))
        action = SendGroupMsg(group_id=3, message=[TextSegment(text="x")])

        class _CancellingWs:
            async def send(self, payload: str) -> None:
                raise asyncio.CancelledError()

        await client._outbound_q.put(action)
        with pytest.raises(asyncio.CancelledError):
            await client._writer_loop(_CancellingWs())
        assert list(client._outbound_front) == [action]
        assert id(action) not in client._outbound_retries


class TestLiveClientRegistry:
    """The seam the synchronous tool layer uses to reuse this connection."""

    def teardown_method(self):
        set_live_client(None, None)

    def test_unset_returns_none(self):
        set_live_client(None, None)
        assert get_live_client() == (None, None)

    def test_disconnected_client_is_not_offered(self):
        client = OneBotClient(OneBotConfig(url="ws://127.0.0.1:1"))
        set_live_client(client, asyncio.new_event_loop())
        assert get_live_client() == (None, None)

    def test_connected_client_is_offered(self):
        client = OneBotClient(OneBotConfig(url="ws://127.0.0.1:1"))
        client._connected = True
        loop = asyncio.new_event_loop()
        try:
            set_live_client(client, loop)
            got_client, got_loop = get_live_client()
            assert got_client is client and got_loop is loop
        finally:
            loop.close()
