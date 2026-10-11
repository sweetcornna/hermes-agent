"""The live-adapter seam: what makes a gateway-guarded send possible at all.

``client.set_live_adapter`` / ``get_live_adapter`` are the symmetric twin of
the existing live-*client* registry. The client is enough for a read
(``get_cookies``); an outbound voice message needs the adapter, because the
emergency mute, the Tencent content policy and the recent-chatter buffer all
hang off it.

``plugins/qq_voice/common.py::live_adapter`` is the consumer, and its
contract is deliberately forgiving: ``(None, None)`` means "no gateway here",
which is the *normal* answer under the CLI and cron, not an error.

Nothing in this file opens a socket; the "client" is a two-attribute stand-in
because liveness is all ``get_live_adapter`` reads from it.
"""

from __future__ import annotations

import asyncio

import pytest

from plugins.platforms.onebot.client import (
    get_live_adapter,
    get_live_client,
    set_live_adapter,
    set_live_client,
)
from plugins.qq_voice import common


class _FakeClient:
    def __init__(self, connected: bool = True) -> None:
        self.connected = connected


class _FakeAdapter:
    def __init__(self, connected: bool = True) -> None:
        self._client = _FakeClient(connected)


@pytest.fixture(autouse=True)
def _clear_registry():
    """Both registries are process-wide; leave them the way we found them."""
    set_live_adapter(None, None)
    yield
    set_live_adapter(None, None)
    set_live_client(None, None)


class TestRegistry:
    def test_nothing_published_is_none_none(self):
        assert get_live_adapter() == (None, None)

    def test_publishing_returns_adapter_and_loop(self):
        adapter = _FakeAdapter()
        loop = asyncio.new_event_loop()
        try:
            set_live_adapter(adapter, loop)
            assert get_live_adapter() == (adapter, loop)
        finally:
            loop.close()

    def test_a_dropped_socket_is_not_live(self):
        """Liveness mirrors ``get_live_client``: a dead socket is no adapter.

        Handing one out would turn a clean fallback into a timeout.
        """
        adapter = _FakeAdapter(connected=False)
        loop = asyncio.new_event_loop()
        try:
            set_live_adapter(adapter, loop)
            assert get_live_adapter() == (None, None)
        finally:
            loop.close()

    def test_an_adapter_that_never_connected_is_not_live(self):
        adapter = _FakeAdapter()
        adapter._client = None
        loop = asyncio.new_event_loop()
        try:
            set_live_adapter(adapter, loop)
            assert get_live_adapter() == (None, None)
        finally:
            loop.close()

    def test_clearing_with_none(self):
        loop = asyncio.new_event_loop()
        try:
            set_live_adapter(_FakeAdapter(), loop)
            set_live_adapter(None, None)
            assert get_live_adapter() == (None, None)
        finally:
            loop.close()

    def test_the_two_registries_are_independent(self):
        """Publishing an adapter must not fabricate a live client, or vice versa."""
        loop = asyncio.new_event_loop()
        try:
            set_live_adapter(_FakeAdapter(), loop)
            assert get_live_client() == (None, None)
            set_live_adapter(None, None)
            set_live_client(_FakeClient(), loop)
            assert get_live_adapter() == (None, None)
        finally:
            loop.close()


class TestCommonLiveAdapter:
    def test_no_gateway_is_the_normal_cli_answer(self):
        assert common.live_adapter() == (None, None)

    def test_prefers_a_published_adapter(self):
        adapter = _FakeAdapter()
        loop = asyncio.new_event_loop()
        try:
            set_live_adapter(adapter, loop)
            assert common.live_adapter() == (adapter, loop)
        finally:
            loop.close()

    def test_a_closed_loop_falls_back(self):
        adapter = _FakeAdapter()
        loop = asyncio.new_event_loop()
        loop.close()
        set_live_adapter(adapter, loop)
        assert common.live_adapter() == (None, None)

    def test_on_the_adapters_own_loop_it_falls_back(self):
        """Blocking on the loop we are running on would deadlock.

        Same guard ``tools/onebot_client.py::_try_live_client`` keeps.
        """
        adapter = _FakeAdapter()
        loop = asyncio.new_event_loop()

        async def probe():
            return common.live_adapter()

        try:
            set_live_adapter(adapter, loop)
            assert loop.run_until_complete(probe()) == (None, None)
        finally:
            loop.close()
