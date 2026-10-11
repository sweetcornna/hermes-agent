"""OneBot transport-vs-account state and outbound safety contracts.

These tests deliberately use a small in-process client double.  The real
WebSocket pump is covered by ``test_onebot_transport.py``; this file exercises
the adapter's account-state projection, probe fallback and send refusal
without contacting a QQ/OneBot service.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from websockets.asyncio.server import serve

from gateway.config import PlatformConfig
from plugins.platforms.onebot import adapter as A
from plugins.platforms.onebot import client as C
from plugins.platforms.onebot import protocol as P
from plugins.platforms.onebot import voice as V


class FakeClient:
    """Connected-looking OneBot client with controllable probe responses."""

    def __init__(self, responses: Optional[List[Any]] = None) -> None:
        self.connected = True
        self.last_self_id = 100
        self.last_event_at_ms = 10_000
        self.last_status_online: Optional[bool] = None
        self.last_status_online_at_ms: Optional[int] = None
        self.last_status_reason: Optional[str] = None
        self.last_heartbeat_at_ms: Optional[int] = None
        self.inbound_dropped_count = 0
        self.outbound_queue_depth = 0
        self.responses = list(responses or [])
        self.actions: List[Any] = []

    async def call_action(self, action: Any, *, timeout: Optional[float] = None):
        self.actions.append(action)
        if not self.responses:
            return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    async def send_action(self, action: Any) -> None:
        self.actions.append(action)

    def observe_account_status(
        self,
        online: bool,
        *,
        now_ms: Optional[int] = None,
        reason: str = "probe",
        notify: bool = True,
    ) -> None:
        self.last_status_online = bool(online)
        self.last_status_online_at_ms = now_ms
        self.last_status_reason = reason

    async def close(self) -> None:
        self.connected = False


def make_adapter(client: Optional[FakeClient] = None) -> A.OneBotAdapter:
    adapter = A.OneBotAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "ws_url": "ws://127.0.0.1:3001",
                "group_replies_enabled": True,
                "account_probe_secs": 60.0,
            },
        )
    )
    adapter._client = client or FakeClient()
    adapter._running = True
    adapter._semaphore = asyncio.Semaphore(2)
    adapter.router.group_replies_enabled = True
    return adapter


@asynccontextmanager
async def ws_server(handler):
    server = await serve(handler, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()


@pytest.fixture(autouse=True)
def _clean_onebot_module_state():
    A._reset_module_state()
    yield
    A._reset_module_state()


def test_transport_up_does_not_claim_qq_account_online():
    client = FakeClient()
    adapter = make_adapter(client)

    assert client.connected is True
    snapshot = adapter.health_snapshot()

    assert snapshot["link_online"] is True
    assert snapshot["account_online"] is None
    assert snapshot["account_state"] == A.ACCOUNT_STATE_UNKNOWN
    assert snapshot["account_auth_required"] is False


@pytest.mark.asyncio
async def test_live_websocket_up_with_offline_qq_account_stays_connected():
    async def handler(ws):
        await ws.send(
            json.dumps(
                {
                    "post_type": "meta_event",
                    "meta_event_type": "heartbeat",
                    "self_id": 100,
                    "status": {"online": False},
                }
            )
        )
        try:
            async for _ in ws:
                pass
        except Exception:
            pass

    async with ws_server(handler) as url:
        client = C.OneBotClient(C.OneBotConfig(url=url))
        adapter = make_adapter(client)
        client.set_account_status_callback(adapter._on_client_account_status)
        await client.connect()
        try:
            for _ in range(100):
                if client.connected and client.last_status_online is False:
                    break
                await asyncio.sleep(0.01)
            snapshot = adapter.health_snapshot()
            assert client.connected is True
            assert snapshot["link_online"] is True
            assert snapshot["account_online"] is False
        finally:
            await client.close()


def test_bot_offline_notice_and_status_callback_mark_auth_required(monkeypatch):
    client = C.OneBotClient(C.OneBotConfig(url="ws://x"))
    client._connected = True
    adapter = make_adapter()
    adapter._client = client
    writes: List[Dict[str, Any]] = []
    monkeypatch.setattr(
        adapter,
        "_write_runtime_status_safe",
        lambda context, **kwargs: writes.append({"context": context, **kwargs}),
    )
    client.set_account_status_callback(adapter._on_client_account_status)

    client._absorb_status(
        {
            "post_type": "notice",
            "notice_type": "bot_offline",
        },
        1234,
    )

    snapshot = adapter.health_snapshot()
    assert snapshot["link_online"] is True
    assert snapshot["account_online"] is False
    assert snapshot["account_state"] == A.ACCOUNT_STATE_AUTH_REQUIRED
    assert snapshot["account_auth_required"] is True
    assert snapshot["account_status_reason"] == "bot_offline_notice"
    assert writes[-1]["platform_state"] == "auth_required"
    assert writes[-1]["error_code"] == A.ACCOUNT_AUTH_REQUIRED_REASON
    assert "heartbeat_age" in writes[-1]["error_message"]


def test_false_to_true_probe_recovery_updates_both_client_and_adapter(monkeypatch):
    client = FakeClient(
        responses=[
            {
                "status": "ok",
                "retcode": 0,
                "data": {"online": True, "good": True},
            }
        ]
    )
    adapter = make_adapter(client)
    writes: List[Dict[str, Any]] = []
    monkeypatch.setattr(
        adapter,
        "_write_runtime_status_safe",
        lambda context, **kwargs: writes.append({"context": context, **kwargs}),
    )
    adapter._set_account_status(False, at_ms=100, reason="bot_offline_notice")

    asyncio.run(adapter._probe_account(client))

    assert adapter._account_online is True
    assert adapter._account_state == A.ACCOUNT_STATE_ONLINE
    assert client.last_status_online is True
    assert client.last_status_reason == "get_status"
    assert writes[-1]["platform_state"] == "connected"
    assert writes[-1]["error_code"] is None


def test_state_transition_logs_are_deduplicated(caplog, monkeypatch):
    adapter = make_adapter()
    monkeypatch.setattr(adapter, "_write_runtime_status_safe", lambda *a, **kw: None)
    caplog.set_level(logging.INFO, logger=A.logger.name)

    adapter._set_account_status(True, at_ms=1, reason="heartbeat")
    caplog.clear()
    adapter._set_account_status(False, at_ms=2, reason="heartbeat")
    adapter._set_account_status(False, at_ms=3, reason="heartbeat")
    adapter._set_account_status(True, at_ms=4, reason="get_status")
    adapter._set_account_status(True, at_ms=5, reason="heartbeat")

    transitions = [
        record.getMessage()
        for record in caplog.records
        if "QQ account state" in record.getMessage()
    ]
    assert transitions == [
        "OneBot: QQ account state online -> auth-required (source=heartbeat); "
        "the WebSocket remains a transport link and will not be restarted",
        "OneBot: QQ account state auth-required -> online (source=get_status)",
    ]


@pytest.mark.asyncio
async def test_unknown_account_preserves_existing_send_behavior():
    client = FakeClient()
    adapter = make_adapter(client)

    result = await adapter.send("777", "hello")

    assert result.success is True
    assert len(client.actions) == 1
    assert isinstance(client.actions[0], P.SendPrivateMsg)


@pytest.mark.asyncio
async def test_auth_required_rejects_every_outbound_entry_before_work_or_wire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    client = FakeClient()
    adapter = make_adapter(client)
    adapter._account_online = False
    adapter._account_state = A.ACCOUNT_STATE_AUTH_REQUIRED

    synth_calls: List[str] = []

    async def forbidden_synthesize(text: str, **_kwargs: Any) -> V.SynthResult:
        synth_calls.append(text)
        return V.SynthResult(ok=False, error="must not run")

    monkeypatch.setattr(V, "synthesize", forbidden_synthesize)
    missing = str(tmp_path / "must-not-be-read.mp3")
    results = [
        await adapter.send("777", "hello"),
        await adapter.send_image_file("777", missing),
        await adapter.send_image("777", "https://example.invalid/image.png"),
        await adapter.send_voice("777", missing),
        await adapter.send_video("777", missing),
        await adapter.send_document("777", missing),
    ]
    spoken = await V.speak(adapter, "777", "hello")
    ai_spoken = await V.speak_ai_character(adapter, "g777", "hello", "lucy")
    await adapter.send_typing("777")
    await adapter.stop_typing("777")

    for result in results:
        assert result.success is False
        assert result.retryable is False
        assert result.error_kind == A.ACCOUNT_AUTH_REQUIRED_ERROR_KIND
        assert A.is_account_auth_required_send_result(result) is True
        assert "auth-required" in result.error
    for outcome in (spoken, ai_spoken):
        assert outcome.spoke is False
        assert outcome.failure is not None
        assert outcome.failure.retryable is False
        assert outcome.failure.error_kind == A.ACCOUNT_AUTH_REQUIRED_ERROR_KIND
        assert A.is_account_auth_required_send_result(outcome.failure) is True
    assert synth_calls == []
    assert client.actions == []


@pytest.mark.asyncio
async def test_probe_success_and_failure_paths_have_conservative_states():
    # Explicit get_status online=false is an account signal.
    offline_client = FakeClient(
        responses=[
            {"status": "ok", "retcode": 0, "data": {"online": False}},
        ]
    )
    offline_adapter = make_adapter(offline_client)
    await offline_adapter._probe_account(offline_client)
    assert offline_adapter._account_online is False

    # A failed/timeout status and login probe leave startup unknown rather
    # than turning a missing optional status field into auth-required/online.
    unknown_client = FakeClient(
        responses=[RuntimeError("unsupported"), asyncio.TimeoutError()]
    )
    unknown_adapter = make_adapter(unknown_client)
    await unknown_adapter._probe_account(unknown_client)
    assert unknown_adapter._account_online is None

    # Successful envelopes with no account-shaped payload are still not
    # evidence.  This catches a fake/default action response being mistaken
    # for a real login-info result.
    malformed_success_client = FakeClient(
        responses=[
            {"status": "ok", "retcode": 0, "data": {}},
            {"status": "ok", "retcode": 0, "data": {"message_id": 1}},
        ]
    )
    malformed_success_adapter = make_adapter(malformed_success_client)
    await malformed_success_adapter._probe_account(malformed_success_client)
    assert malformed_success_adapter._account_online is None

    # get_status unsupported but a successful login-info response recovers.
    login_client = FakeClient(
        responses=[
            {"status": "failed", "retcode": 1404, "data": None},
            {
                "status": "ok",
                "retcode": 0,
                "data": {"user_id": 100, "nickname": "qq"},
            },
        ]
    )
    login_adapter = make_adapter(login_client)
    await login_adapter._probe_account(login_client)
    assert login_adapter._account_online is True
    assert login_adapter._account_status_reason == "get_login_info"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "responses",
    [
        # A failed status string overrides an otherwise-success-looking code.
        [
            {"status": "failed", "retcode": 0, "data": {"online": True}},
            {"status": "failed", "retcode": 1404, "data": None},
        ],
        # The same contradiction must not make login-info's user id proof.
        [
            {"status": "failed", "retcode": 1404, "data": None},
            {"status": "failed", "retcode": 0, "data": {"user_id": 100}},
        ],
        # bool is an int subclass, but not a valid OneBot retcode.
        [
            {"status": "ok", "retcode": False, "data": {"online": True}},
            {"status": "failed", "retcode": 1404, "data": None},
        ],
        # Nor may a bool stand in for a QQ user id.
        [
            {"status": "failed", "retcode": 1404, "data": None},
            {"status": "ok", "retcode": 0, "data": {"user_id": True}},
        ],
        # Empty user ids are not account identity evidence.
        [
            {"status": "failed", "retcode": 1404, "data": None},
            {"status": "ok", "retcode": 0, "data": {"user_id": ""}},
        ],
        # Two explicit flags in one response disagree.
        [
            {
                "status": "ok",
                "retcode": 0,
                "online": True,
                "data": {"online": False},
            },
            {"status": "failed", "retcode": 1404, "data": None},
        ],
        # Positive identity plus explicit offline is internally contradictory.
        [
            {"status": "failed", "retcode": 1404, "data": None},
            {
                "status": "ok",
                "retcode": 0,
                "data": {"user_id": 100, "online": False},
            },
        ],
        # The two actions disagree across the same probe cycle.
        [
            {"status": "ok", "retcode": 0, "data": {"online": False}},
            {"status": "ok", "retcode": 0, "data": {"user_id": 100}},
        ],
        # Explicit online flags conflict even when the offline login-info
        # response quite reasonably has no logged-in user id.
        [
            {"status": "ok", "retcode": 0, "data": {"online": True}},
            {"status": "ok", "retcode": 0, "data": {"online": False}},
        ],
    ],
)
async def test_probe_never_recovers_online_from_invalid_or_conflicting_evidence(
    responses: List[Any],
):
    client = FakeClient(responses=responses)
    adapter = make_adapter(client)
    adapter._set_account_status(False, at_ms=123, reason="bot_offline_notice")

    await adapter._probe_account(client)

    assert adapter._account_online is False
    assert adapter._account_status_at_ms == 123
    assert adapter._account_status_reason == "bot_offline_notice"


@pytest.mark.asyncio
async def test_probe_failures_and_timeouts_preserve_known_state():
    client = FakeClient(responses=[RuntimeError("down"), asyncio.TimeoutError()])
    adapter = make_adapter(client)
    adapter._set_account_status(True, at_ms=321, reason="heartbeat")

    await adapter._probe_account(client)

    assert adapter._account_online is True
    assert adapter._account_status_at_ms == 321
    assert adapter._account_status_reason == "heartbeat"


@pytest.mark.parametrize(
    ("configured", "expected"),
    [(0, 0.0), (-1, 0.0), (0.1, 5.0), (5, 5.0), (60, 60.0)],
)
def test_account_probe_interval_has_disable_semantics_and_safe_floor(
    configured: float, expected: float
):
    adapter = A.OneBotAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "ws_url": "ws://127.0.0.1:3001",
                "account_probe_secs": configured,
            },
        )
    )
    assert adapter.account_probe_secs == expected


def test_runtime_snapshot_reports_heartbeat_age_and_last_status_time():
    client = FakeClient()
    client.last_heartbeat_at_ms = 1_000
    adapter = make_adapter(client)
    adapter._account_online = False
    adapter._account_state = A.ACCOUNT_STATE_AUTH_REQUIRED
    adapter._account_status_reason = "heartbeat"
    adapter._account_status_at_ms = 1_000

    snapshot = adapter.health_snapshot()
    age = adapter._heartbeat_age_seconds(now_ms=3_500)

    assert snapshot["last_heartbeat_at_ms"] == 1_000
    assert snapshot["last_status_online_at_ms"] == 1_000
    assert age == pytest.approx(2.5)
    assert snapshot["heartbeat_age_seconds"] >= 0


@pytest.mark.asyncio
async def test_account_offline_does_not_restart_connected_client(monkeypatch):
    client = FakeClient(
        responses=[
            {"status": "ok", "retcode": 0, "data": {"online": False}},
        ]
    )
    adapter = make_adapter(client)
    adapter.health_probe_secs = 0.0
    adapter.account_probe_secs = 0.0
    adapter._account_online = False
    adapter._account_state = A.ACCOUNT_STATE_AUTH_REQUIRED
    sleeps = 0

    async def one_tick(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            adapter._running = False

    monkeypatch.setattr(A.asyncio, "sleep", one_tick)
    await adapter._health_loop()

    assert client.connected is True
    assert client.actions == []


@pytest.mark.asyncio
async def test_client_reconnect_ladder_remains_transport_owned(monkeypatch):
    client = C.OneBotClient(
        C.OneBotConfig(url="ws://x", reconnect_schedule=(1.0, 2.0, 5.0))
    )
    connect_calls = 0
    delays: List[float] = []

    async def fake_connect_once():
        nonlocal connect_calls
        connect_calls += 1
        client._connected = True
        if connect_calls <= 2:
            if connect_calls == 1:
                client._record_account_status(False, 10, "bot_offline_notice")
            client._connected = False
            raise RuntimeError("simulated WebSocket disconnect")
        client._closed = True

    async def fake_sleep(delay: float):
        delays.append(delay)

    monkeypatch.setattr(client, "_connect_once", fake_connect_once)
    monkeypatch.setattr(C.asyncio, "sleep", fake_sleep)
    await client._reader_loop()

    assert connect_calls == 3
    assert delays == [1.0, 2.0]
    assert client.last_status_online is False
