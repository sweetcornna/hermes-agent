"""主窗口 — the one chat allowed to drive this gateway.

Two independent holes are pinned here, because either one alone hands a QQ
group the console:

* **delivery** — a group id in the ``home_channel`` slot makes bare
  ``deliver=onebot`` and every gateway notification land in a group;
* **control** — ``looks_like_command`` treats ``/x`` in a group as an explicit
  summons, and ``gateway/slash_access.py`` disables slash gating outright for
  a scope with no ``allow_admin_from`` (the staged production config's state
  for groups). Together: flip ``group_replies_enabled`` on and any member of
  a whitelisted group can run any registered slash command.

The lock is structural, not a denial message: a group event carries
``allow_gateway_control=False``, so ``MessageEvent.is_command()`` is False and
the run-loop's command paths are never entered.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

import pytest

from gateway.config import HomeChannel, Platform, PlatformConfig

from plugins.platforms.onebot import adapter as A
from plugins.platforms.onebot import main_window as MW
from plugins.platforms.onebot import protocol as P

from tests.gateway.test_onebot_plugin import (  # noqa: F401 — fixture reuse
    FakeClient,
    group_event,
    private_event,
)


OWNER = "2104743984"
STRANGER = 1010679324
GROUP = 183287894


def make_adapter(extra: Optional[Dict[str, Any]] = None) -> A.OneBotAdapter:
    base: Dict[str, Any] = {"ws_url": "ws://127.0.0.1:3001"}
    base.update(extra or {})
    ad = A.OneBotAdapter(PlatformConfig(enabled=True, extra=base))
    ad._client = FakeClient()
    ad._running = True
    ad._semaphore = asyncio.Semaphore(4)
    return ad


@pytest.fixture(autouse=True)
def _clean_module_state(monkeypatch):
    # The env form is read at resolve time; a stray value on the developer's
    # machine must not decide what these tests assert.
    monkeypatch.delenv(MW.MAIN_WINDOW_ENV, raising=False)
    A._reset_module_state()
    yield


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

class TestResolveMainWindow:

    def test_explicit_key_wins(self):
        assert MW.resolve_main_window({"main_window_chat_id": OWNER}) == OWNER

    def test_falls_back_to_home_channel(self):
        extra = {"home_channel": {"chat_id": OWNER, "name": "主窗口"}}
        assert MW.resolve_main_window(extra) == OWNER

    def test_reads_a_home_channel_dataclass_too(self):
        """The core loader turns the dict into a HomeChannel before we see it."""
        class _Home:
            chat_id = OWNER

        assert MW.resolve_main_window({"home_channel": _Home()}) == OWNER

    def test_env_is_consulted_between_key_and_home(self, monkeypatch):
        monkeypatch.setenv(MW.MAIN_WINDOW_ENV, OWNER)
        assert MW.resolve_main_window({}) == OWNER
        # YAML still wins over env.
        assert MW.resolve_main_window({"main_window_chat_id": "999"}) == "999"

    def test_accepts_the_private_spellings(self):
        for spelling in (OWNER, f"private:{OWNER}", f"user:{OWNER}", f" {OWNER} "):
            assert MW.resolve_main_window({"main_window_chat_id": spelling}) == OWNER

    def test_a_group_id_is_refused_not_promoted(self):
        """The whole point: a group cannot become the console by config typo."""
        assert MW.resolve_main_window({"main_window_chat_id": f"g{GROUP}"}) is None
        assert MW.resolve_main_window({"home_channel": {"chat_id": f"g{GROUP}"}}) is None
        assert MW.resolve_main_window({"main_window_chat_id": f"group:{GROUP}"}) is None

    def test_a_group_home_channel_does_not_shadow_a_real_main_window(self):
        extra = {
            "main_window_chat_id": OWNER,
            "home_channel": {"chat_id": f"g{GROUP}"},
        }
        assert MW.resolve_main_window(extra) == OWNER

    def test_unset_resolves_to_none(self):
        assert MW.resolve_main_window({}) is None
        assert MW.resolve_main_window(None) is None


class TestGroupControlSwitch:

    def test_off_unless_written(self):
        assert MW.group_control_enabled({}) is False
        assert MW.group_control_enabled(None) is False
        assert MW.group_control_enabled({"group_gateway_control_enabled": False}) is False

    def test_yaml_truthy_forms(self):
        for raw in (True, "true", "yes", "on", "1"):
            assert MW.group_control_enabled(
                {"group_gateway_control_enabled": raw}
            ) is True

    def test_env_is_not_a_way_in(self, monkeypatch):
        """This switch decides whether a room can operate the agent: YAML only."""
        monkeypatch.setenv("ONEBOT_GROUP_GATEWAY_CONTROL_ENABLED", "true")
        assert MW.group_control_enabled({}) is False


class TestGatewayControlAllowed:

    def test_group_is_locked_even_with_no_main_window_configured(self):
        assert MW.gateway_control_allowed({}, chat_id=GROUP, is_group=True) is False

    def test_group_is_locked_even_when_it_is_the_home_channel(self):
        extra = {"home_channel": {"chat_id": f"g{GROUP}"}}
        assert MW.gateway_control_allowed(extra, chat_id=GROUP, is_group=True) is False

    def test_escape_hatch_unlocks_the_group(self):
        extra = {"group_gateway_control_enabled": True}
        assert MW.gateway_control_allowed(extra, chat_id=GROUP, is_group=True) is True

    def test_dm_from_the_main_window_is_allowed(self):
        extra = {"main_window_chat_id": OWNER}
        assert MW.gateway_control_allowed(extra, chat_id=OWNER, is_group=False) is True

    def test_dm_from_anyone_else_is_not(self):
        extra = {"main_window_chat_id": OWNER}
        assert MW.gateway_control_allowed(
            extra, chat_id=STRANGER, is_group=False
        ) is False

    def test_unconfigured_dms_keep_todays_behaviour(self):
        """No main window ⇒ DMs are exactly as they were before this module."""
        assert MW.gateway_control_allowed({}, chat_id=STRANGER, is_group=False) is True


class TestIsMainWindow:

    def test_group_never_is(self):
        extra = {"main_window_chat_id": OWNER}
        assert MW.is_main_window(extra, chat_id=OWNER, is_group=True) is False

    def test_the_configured_peer_is(self):
        extra = {"main_window_chat_id": OWNER}
        assert MW.is_main_window(extra, chat_id=int(OWNER), is_group=False) is True

    def test_nothing_is_when_unconfigured(self):
        assert MW.is_main_window({}, chat_id=OWNER, is_group=False) is False


# ---------------------------------------------------------------------------
# The adapter stamps it onto every inbound event
# ---------------------------------------------------------------------------

class TestInboundEventStamping:

    @pytest.mark.asyncio
    async def test_group_message_cannot_be_a_command(self):
        ad = make_adapter({"main_window_chat_id": OWNER})
        ev = await ad._build_message_event(group_event("/status"), "/status")
        assert ev.allow_gateway_control is False
        assert ev.is_command() is False
        assert ev.get_command() is None
        # The text still reaches the model as ordinary conversation.
        assert ev.text == "/status"

    @pytest.mark.asyncio
    async def test_group_message_is_a_command_with_the_hatch_open(self):
        ad = make_adapter({
            "main_window_chat_id": OWNER,
            "group_gateway_control_enabled": True,
        })
        ev = await ad._build_message_event(group_event("/status"), "/status")
        assert ev.allow_gateway_control is True
        assert ev.get_command() == "status"

    @pytest.mark.asyncio
    async def test_main_window_dm_keeps_its_commands(self):
        ad = make_adapter({"main_window_chat_id": OWNER})
        ev = await ad._build_message_event(
            private_event("/status", uid=int(OWNER)), "/status"
        )
        assert ev.allow_gateway_control is True
        assert ev.get_command() == "status"

    @pytest.mark.asyncio
    async def test_other_dm_loses_them_once_a_main_window_exists(self):
        ad = make_adapter({"main_window_chat_id": OWNER})
        ev = await ad._build_message_event(
            private_event("/status", uid=STRANGER), "/status"
        )
        assert ev.allow_gateway_control is False
        assert ev.is_command() is False

    @pytest.mark.asyncio
    async def test_dm_is_untouched_when_no_main_window_is_configured(self):
        ad = make_adapter()
        ev = await ad._build_message_event(
            private_event("/status", uid=STRANGER), "/status"
        )
        assert ev.allow_gateway_control is True

    @pytest.mark.asyncio
    async def test_home_channel_alone_is_enough_to_arm_the_dm_rule(self):
        ad = make_adapter({"home_channel": {"chat_id": OWNER, "name": "主窗口"}})
        allowed = await ad._build_message_event(
            private_event("/status", uid=int(OWNER)), "/status"
        )
        denied = await ad._build_message_event(
            private_event("/status", uid=STRANGER), "/status"
        )
        assert allowed.allow_gateway_control is True
        assert denied.allow_gateway_control is False

    @pytest.mark.asyncio
    async def test_a_live_config_edit_hot_applies(self):
        """``_gh_live_extra`` reads config.extra, which a reconcile mutates."""
        ad = make_adapter({"main_window_chat_id": OWNER})
        ad.config.extra["group_gateway_control_enabled"] = True
        ev = await ad._build_message_event(group_event("/status"), "/status")
        assert ev.allow_gateway_control is True


# ---------------------------------------------------------------------------
# Config plumbing
# ---------------------------------------------------------------------------

class TestConfigPlumbing:

    def test_keys_survive_the_yaml_merge(self):
        """Anything missing from _PRIVATE_YAML_KEYS is dropped in silence."""
        assert "main_window_chat_id" in A._PRIVATE_YAML_KEYS
        assert "group_gateway_control_enabled" in A._PRIVATE_YAML_KEYS

    def test_env_seeds_the_main_window(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_WS_URL", "ws://127.0.0.1:3001")
        monkeypatch.setenv(MW.MAIN_WINDOW_ENV, f"private:{OWNER}")
        seed = A._env_enablement()
        assert seed is not None and seed["main_window_chat_id"] == OWNER

    def test_env_refuses_a_group_id(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_WS_URL", "ws://127.0.0.1:3001")
        monkeypatch.setenv(MW.MAIN_WINDOW_ENV, f"g{GROUP}")
        seed = A._env_enablement()
        assert seed is not None and "main_window_chat_id" not in seed

    def test_adapter_exposes_the_resolved_state(self):
        ad = make_adapter({"main_window_chat_id": OWNER})
        assert ad.main_window_chat_id == OWNER
        assert ad.group_gateway_control_enabled is False
        assert ad.router.group_commands_enabled is False
        assert MW.is_main_window(
            ad.router.main_window_settings,
            chat_id=OWNER,
            is_group=False,
        )

    def test_router_inherits_the_escape_hatch(self):
        ad = make_adapter({"group_gateway_control_enabled": True})
        assert ad.router.group_commands_enabled is True

    def test_yaml_home_channel_arms_the_rule(self):
        """YAML home_channel lands on PlatformConfig, never in ``extra``.

        Resolving from ``extra`` alone would leave a YAML-configured
        deployment with no main window while the env-configured one has
        it — a default that looks on and is off.
        """
        cfg = PlatformConfig(
            enabled=True,
            extra={"ws_url": "ws://127.0.0.1:3001"},
            home_channel=HomeChannel(
                platform=Platform.ONEBOT, chat_id=OWNER, name="主窗口"
            ),
        )
        ad = A.OneBotAdapter(cfg)
        assert ad.main_window_chat_id == OWNER

    def test_group_home_channel_warns(self, caplog):
        with caplog.at_level("WARNING"):
            make_adapter({"home_channel": {"chat_id": f"g{GROUP}"}})
        assert any(
            "home_channel is GROUP" in r.getMessage() for r in caplog.records
        )
