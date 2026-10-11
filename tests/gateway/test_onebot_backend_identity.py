"""Backend identity probing for the OneBot v11 adapter.

The probe is diagnostic only: it runs after the adapter has started its normal
connection machinery, and a backend that does not answer ``get_version_info``
must not prevent message handling from starting.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.onebot import adapter as A
from plugins.platforms.onebot import protocol as P


class IdentityClient:
    """Small client double that can return or hold a version response."""

    def __init__(
        self,
        response: Optional[Dict[str, Any]] = None,
        error: Optional[BaseException] = None,
        *,
        wait_for_release: bool = False,
    ) -> None:
        self.response = response or {
            "status": "ok",
            "retcode": 0,
            "data": {"app_name": "SnowLuma", "app_version": "1.14.14"},
        }
        self.error = error
        self.wait_for_release = wait_for_release
        self.calls: List[Any] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.connected = False
        self.closed = False
        self.last_self_id = 100
        self.last_event_at_ms = 0
        self.last_status_online = True
        self.inbound_dropped_count = 0
        self.outbound_queue_depth = 0

    async def connect(self) -> None:
        self.connected = True

    async def call_action(self, action, *, timeout=None):  # noqa: ANN001
        self.calls.append((action, timeout))
        self.started.set()
        if self.wait_for_release:
            await self.release.wait()
        if self.error is not None:
            raise self.error
        return self.response

    async def inbound(self):
        stop = asyncio.Event()
        await stop.wait()
        yield  # pragma: no cover - keeps this an async generator

    async def close(self) -> None:
        self.connected = False
        self.closed = True


def make_adapter() -> A.OneBotAdapter:
    return A.OneBotAdapter(
        PlatformConfig(enabled=True, extra={"ws_url": "ws://127.0.0.1:3001"})
    )


@pytest.mark.asyncio
async def test_snowluma_version_info_normalizes_to_snowluma():
    adapter = make_adapter()
    client = IdentityClient(
        response={
            "status": "ok",
            "retcode": 0,
            "data": {"app_name": "SnowLuma", "app_version": "1.14.14"},
        }
    )

    await adapter._identify_backend(client)

    assert adapter._backend == "snowluma"
    assert adapter._backend_app_name == "SnowLuma"
    assert adapter._backend_app_version == "1.14.14"
    assert isinstance(client.calls[0][0], P.RawAction)
    assert client.calls[0][0].action == "get_version_info"


@pytest.mark.asyncio
async def test_napcat_version_info_normalizes_to_napcat():
    adapter = make_adapter()
    client = IdentityClient(
        response={
            "status": "ok",
            "retcode": 0,
            "data": {"app_name": "NapCat.Onebot", "app_version": "4.8.0"},
        }
    )

    await adapter._identify_backend(client)

    assert adapter._backend == "napcat"
    assert adapter._backend_app_name == "NapCat.Onebot"
    assert adapter._backend_app_version == "4.8.0"


@pytest.mark.asyncio
@pytest.mark.parametrize("app_name", ["LLOneBot", "LuckyLilliaBot", "LLBot"])
async def test_llbot_identity_keeps_standard_onebot_delivery(app_name):
    adapter = make_adapter()
    client = IdentityClient(
        response={
            "status": "ok",
            "retcode": 0,
            "data": {
                "app_name": app_name,
                "protocol_version": "v11",
                "app_version": "8.1.10",
            },
        }
    )

    await adapter._identify_backend(client)

    assert adapter.health_snapshot()["backend"] == "llbot"
    assert adapter._backend_app_name == app_name
    assert [action.action for action, _timeout in client.calls] == ["get_version_info"]
    adapter._client = client
    client.response = {"status": "ok", "retcode": 0, "data": {"message_id": 456}}

    sent, message_id, failure = await adapter._send_segments(
        False, 321, [P.ImageSegment(url="https://example.invalid/image.png")]
    )
    card = await adapter._deliver_forward(False, 321, ["Complete answer"], None, None)

    assert sent and message_id == "456" and failure is None
    assert card.success and card.message_id == "456"
    assert [P.action_to_wire(action)["action"] for action, _ in client.calls] == [
        "get_version_info", "send_private_msg", "send_private_forward_msg"
    ]


@pytest.mark.asyncio
async def test_failing_version_info_leaves_backend_unknown():
    adapter = make_adapter()
    client = IdentityClient(error=asyncio.TimeoutError())

    await adapter._identify_backend(client)

    assert adapter._backend == "unknown"
    assert adapter._backend_app_name is None
    assert adapter._backend_app_version is None


@pytest.mark.asyncio
async def test_connect_schedules_identity_probe_without_waiting_for_it(monkeypatch):
    adapter = make_adapter()
    client = IdentityClient(wait_for_release=True)

    monkeypatch.setattr(A, "OneBotClient", lambda config, on_self_id=None: client)

    async def _preflight(self) -> bool:  # noqa: ANN001
        return False

    monkeypatch.setattr(A.OneBotAdapter, "_preflight", _preflight)
    for method in (
        "_start_proactive_loop",
        "_start_proactive_call_loop",
        "_start_call_sweeper",
        "_start_group_history",
    ):
        monkeypatch.setattr(A.OneBotAdapter, method, lambda self: None)

    assert await asyncio.wait_for(adapter.connect(), timeout=0.2) is True
    try:
        await asyncio.wait_for(client.started.wait(), timeout=0.2)
        assert adapter._backend == "unknown"
        assert len(client.calls) == 1
        assert client.calls[0][0].action == "get_version_info"
        client.release.set()
        assert adapter._backend_identity_task is not None
        await asyncio.wait_for(adapter._backend_identity_task, timeout=0.2)
        assert adapter._backend == "snowluma"
    finally:
        await adapter.disconnect()
