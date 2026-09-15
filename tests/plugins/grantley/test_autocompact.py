"""Grantley QQ rolling autocompaction integration contracts.

These tests deliberately drive the existing core compressor and SessionDB.
Grantley owns only constructor-time policy; it does not own a summarizer or a
parallel transcript store.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import shutil
import sqlite3
import sys
import threading
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from agent.compression_policy import (
    MAX_COMPRESSION_TAIL_TOKENS,
    MAX_COMPRESSION_THRESHOLD_TOKENS,
    MIN_COMPRESSION_TAIL_TOKENS,
    MIN_COMPRESSION_THRESHOLD_TOKENS,
    normalize_provider_compression_policy,
)
from agent.context_compressor import (
    COMPRESSED_SUMMARY_METADATA_KEY,
    ContextCompressor,
    is_compaction_summary_message,
)
from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider
from gateway.config import Platform
from gateway.session import SessionSource, build_session_key
from hermes_state import SessionDB
from hermes_cli import plugins as plugin_runtime
from hermes_cli.plugins import PluginManager
import plugins.memory as memory_plugins
from plugins.grantley.memory_provider import GrantleyMemoryProvider


GROUP_ID = "183287894"
OWNER_ID = "2104743984"
SECOND_OWNER_ID = "2104743985"
REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_CHANNELS = object()
_MISSING_CHANNELS = object()


def _provider(
    config: dict | None = None,
    *,
    platform: str = "onebot",
    chat_id: str = GROUP_ID,
    chat_type: str = "group",
) -> GrantleyMemoryProvider:
    provider = GrantleyMemoryProvider(
        config=config,
        connection=sqlite3.connect(":memory:", check_same_thread=False),
    )
    provider.initialize(
        "grantley-test",
        platform=platform,
        chat_id=chat_id,
        chat_type=chat_type,
    )
    return provider


def _channel_config(**autocompact) -> dict:
    return {
        "channels": {
            GROUP_ID: {
                "persona": "grantley",
                "channel_owner": OWNER_ID,
                "group": True,
            }
        },
        "autocompact": autocompact,
    }


def test_legacy_grantley_config_gets_rolling_defaults_only_on_qq_lane():
    provider = _provider(_channel_config())

    assert provider.context_compression_policy() == {
        "enabled": True,
        "threshold": 0.75,
        "threshold_tokens": None,
        "tail_tokens": 16_000,
        "abort_on_summary_failure": True,
        "in_place": True,
        "micro_compact": False,
        "min_tail_user_messages": 2,
    }

    assert _provider(_channel_config(), platform="cli").context_compression_policy() == {}
    assert _provider(
        _channel_config(), chat_id="999999999"
    ).context_compression_policy() == {}
    assert _provider(
        _channel_config(), chat_type="unknown"
    ).context_compression_policy() == {}

    numeric_key = _channel_config()
    numeric_key["channels"] = {
        int(GROUP_ID): numeric_key["channels"][GROUP_ID]
    }
    assert _provider(numeric_key).context_compression_policy()["enabled"] is True

    other_persona = _channel_config()
    other_persona["channels"][GROUP_ID]["persona"] = "someone-else"
    assert _provider(other_persona).context_compression_policy() == {}


@pytest.mark.parametrize(
    ("config", "chat_id"),
    [
        ({}, GROUP_ID),
        ({"channels": {}}, GROUP_ID),
        ({"channels": []}, GROUP_ID),
        ({"channels": {GROUP_ID: "malformed"}}, GROUP_ID),
        ({"channels": {GROUP_ID: {}}}, GROUP_ID),
        ({"channels": {GROUP_ID: {"persona": "someone-else"}}}, GROUP_ID),
        ({"channels": {"999999999": {"persona": "grantley"}}}, GROUP_ID),
        ({"channels": {GROUP_ID: {"persona": "grantley"}}}, f"g{GROUP_ID}"),
        (
            {
                "channels": {
                    int(GROUP_ID): {"persona": "grantley"},
                    GROUP_ID: {"persona": "someone-else"},
                }
            },
            GROUP_ID,
        ),
    ],
)
def test_grantley_requires_exact_explicit_channel_allowlist(config, chat_id):
    provider = _provider(config, chat_id=chat_id)

    assert provider.context_compression_policy() == {}
    assert provider._store is None  # noqa: SLF001
    assert provider.get_tool_schemas() == []
    assert provider.prefetch("must stay empty") == ""
    denied = json.loads(
        provider.handle_tool_call(
            "persona_life_set_state",
            {"state": "training"},
        )
    )
    assert denied["error"] == "forbidden_lane"


def test_grantley_tools_are_hidden_before_session_initialization():
    provider = GrantleyMemoryProvider(config=_channel_config())
    assert provider.get_tool_schemas() == []


def test_canonical_gateway_group_id_loads_the_same_persona_state_and_policy():
    from plugins.platforms.onebot.adapter import format_chat_id

    raw = _provider(_channel_config())
    canonical = _provider(_channel_config(), chat_id=format_chat_id(True, GROUP_ID))
    assert canonical.context_compression_policy() == raw.context_compression_policy()
    assert canonical.get_tool_schemas() == raw.get_tool_schemas()
    assert canonical.prefetch("今天在聊什么") == raw.prefetch("今天在聊什么")
    assert canonical._store is not None


@pytest.mark.parametrize("chat_id,chat_type,entry", [
    (f"g{GROUP_ID}", "dm", {"persona": "grantley", "group": True}),
    (f"g{GROUP_ID}", "group", {"persona": "grantley", "group": False}),
    (f"g{GROUP_ID}", "group", {"persona": "grantley", "group": "true"}),
    (f"gg{GROUP_ID}", "group", {"persona": "grantley", "group": True}),
    (f"group:{GROUP_ID}", "group", {"persona": "grantley", "group": True}),
    (f"g{GROUP_ID}", "group", {"persona": "other", "group": True}),
])
def test_canonical_group_resolution_does_not_expand_other_bindings(chat_id, chat_type, entry):
    provider = _provider({"channels": {GROUP_ID: entry}}, chat_id=chat_id, chat_type=chat_type)
    assert provider.get_tool_schemas() == []
    assert provider.prefetch("test") == ""
    assert provider.context_compression_policy() == {}


@pytest.mark.parametrize(
    ("platform", "chat_id", "chat_type", "bound"),
    [
        ("telegram", GROUP_ID, "group", False),
        ("cli", GROUP_ID, "group", False),
        ("onebot", "999999999", "group", False),
        ("onebot", GROUP_ID, "group", True),
    ],
)
def test_grantley_state_and_tools_are_bound_to_onebot_lane(
    tmp_path: Path,
    monkeypatch,
    platform,
    chat_id,
    chat_type,
    bound,
):
    home = tmp_path / f"home-{platform}-{chat_id}"
    monkeypatch.setenv("HERMES_HOME", str(home))
    provider = GrantleyMemoryProvider(config=_channel_config())
    provider.initialize(
        f"lane-{platform}-{chat_id}",
        platform=platform,
        chat_id=chat_id,
        chat_type=chat_type,
    )

    if bound:
        assert provider.context_compression_policy()["enabled"] is True
        assert provider._store is not None  # noqa: SLF001
        assert provider.prefetch("hello")
        result = json.loads(provider.handle_tool_call("persona_life_get", {}))
        assert result.get("error") != "forbidden_lane"
    else:
        assert provider.context_compression_policy() == {}
        assert provider._store is None  # noqa: SLF001
        assert not (home / "plugin-data" / "grantley" / "data.db").exists()
        provider._last_recall_count = 9  # noqa: SLF001
        assert provider.prefetch("must stay empty") == ""
        assert provider.recall_status() is None
        result = json.loads(
            provider.handle_tool_call(
                "persona_life_set_state",
                {"mood": "must-not-write"},
            )
        )
        assert result["ok"] is False
        assert result["error"] == "forbidden_lane"
        assert provider._store is None  # noqa: SLF001
        assert not (home / "plugin-data" / "grantley" / "data.db").exists()
    provider.shutdown()


def test_grantley_autocompact_switch_and_budget_validation():
    disabled = _provider(_channel_config(enabled=False))
    assert disabled.context_compression_policy() == {"enabled": False}

    custom = _provider(
        _channel_config(
            enabled=True,
            threshold=0.82,
            threshold_tokens=72_000,
            tail_tokens=12_000,
        )
    )
    policy = custom.context_compression_policy()
    assert policy["enabled"] is True
    assert policy["threshold"] == 0.82
    assert policy["threshold_tokens"] == 72_000
    assert policy["tail_tokens"] == 12_000

    invalid = _provider(
        _channel_config(threshold="later", threshold_tokens=-1, tail_tokens=0)
    ).context_compression_policy()
    assert invalid["threshold"] == 0.75
    assert invalid["threshold_tokens"] is None
    assert invalid["tail_tokens"] == 16_000

    # The manifest declares an object with typed members. Runtime validation
    # is authoritative: wrong container/member types do not become truthy
    # feature switches or one-token compaction loops.
    wrong_shape = _channel_config()
    wrong_shape["autocompact"] = False
    assert _provider(wrong_shape).context_compression_policy()["enabled"] is True
    wrong_bool = _provider(
        _channel_config(enabled="false")
    ).context_compression_policy()
    assert wrong_bool["enabled"] is True


@pytest.mark.parametrize(
    "invalid",
    [True, 1.5, float("nan"), float("inf"), 0, -1, "4096"],
)
def test_grantley_token_fields_reject_malformed_values_independently(invalid):
    policy = _provider(
        _channel_config(threshold_tokens=invalid, tail_tokens=invalid)
    ).context_compression_policy()
    assert policy["threshold_tokens"] is None
    assert policy["tail_tokens"] == 16_000


def test_grantley_token_fields_clamp_safe_lower_and_upper_bounds():
    low = _provider(
        _channel_config(threshold_tokens=1, tail_tokens=1)
    ).context_compression_policy()
    assert low["threshold_tokens"] == MIN_COMPRESSION_THRESHOLD_TOKENS
    assert low["tail_tokens"] == MIN_COMPRESSION_TAIL_TOKENS

    high = _provider(
        _channel_config(threshold_tokens=10**20, tail_tokens=10**20)
    ).context_compression_policy()
    assert high["threshold_tokens"] == MAX_COMPRESSION_THRESHOLD_TOKENS
    assert high["tail_tokens"] == MAX_COMPRESSION_TAIL_TOKENS


@pytest.mark.parametrize(
    "bad_threshold",
    [True, "0.82", float("nan"), float("inf"), 0.01, 0.99],
)
def test_policy_validation_rejects_bad_threshold_without_losing_valid_fields(
    bad_threshold,
):
    normalized, rejected = normalize_provider_compression_policy(
        {
            "enabled": True,
            "threshold": bad_threshold,
            "tail_tokens": 12_000,
        }
    )
    assert normalized == {"enabled": True, "tail_tokens": 12_000}
    assert rejected == ("threshold",)


def test_absolute_tail_budget_survives_model_switch_and_leaves_headroom():
    compressor = ContextCompressor(
        model="test/model",
        threshold_percent=0.75,
        threshold_tokens_cap=10_000,
        tail_tokens=12_000,
        config_context_length=128_000,
        quiet_mode=True,
    )

    assert compressor.threshold_tokens == 10_000
    assert compressor.tail_token_budget == 8_000

    one_token = ContextCompressor(
        model="test/model",
        threshold_percent=0.75,
        threshold_tokens_cap=1,
        tail_tokens=1,
        config_context_length=128_000,
        quiet_mode=True,
    )
    assert one_token.threshold_tokens == MIN_COMPRESSION_THRESHOLD_TOKENS
    assert one_token.tail_token_budget == MIN_COMPRESSION_TAIL_TOKENS
    compressor.update_model("test/next", 256_000, max_tokens=32_000)
    assert compressor.threshold_tokens == 10_000
    assert compressor.tail_token_budget == 8_000


def _write_config(
    home: Path,
    *,
    context_length: int = 128_000,
    global_compression: dict | None = None,
    inject_identity_section: bool = False,
    channels=_DEFAULT_CHANNELS,
) -> None:
    home.mkdir(parents=True, exist_ok=True)
    settings = _channel_config(
        enabled=True,
        threshold=0.80,
        threshold_tokens=12_000,
        tail_tokens=1_200,
    ) | {"inject_identity_section": inject_identity_section}
    if channels is _MISSING_CHANNELS:
        settings.pop("channels", None)
    elif channels is not _DEFAULT_CHANNELS:
        settings["channels"] = channels

    config = {
        "memory": {"provider": "grantley"},
        "model": {"context_length": context_length},
        # Prove the persona policy can enable only its own lane even when the
        # global default is disabled for every other persona.
        "compression": {
            "enabled": False,
            "context_timeout_seconds": 0,
            "protect_last_n": 6,
        },
        "plugins": {
            "entries": {
                "grantley": {
                    "settings": settings
                }
            }
        },
    }
    if global_compression:
        config["compression"].update(global_compression)
    (home / "config.yaml").write_text(
        yaml.safe_dump(config, allow_unicode=True),
        encoding="utf-8",
    )


def _deploy_grantley(home: Path) -> Path:
    """Copy the reviewed plugin into its real user-install discovery shape."""
    destination = home / "plugins" / "grantley"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        REPO_ROOT / "plugins" / "grantley",
        destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    for name in list(sys.modules):
        if name == "_hermes_user_memory.grantley" or name.startswith(
            "_hermes_user_memory.grantley."
        ):
            sys.modules.pop(name, None)
    return destination


def _make_agent(
    *,
    home: Path,
    db: SessionDB,
    session_id: str,
    chat_id: str = GROUP_ID,
    user_id: str = OWNER_ID,
    platform: str = "onebot",
    chat_type: str = "group",
    provider_override=None,
    context_length: int = 128_000,
):
    from run_agent import AIAgent

    with ExitStack() as stack:
        if provider_override is not None:
            stack.enter_context(
                patch(
                    "plugins.memory.load_memory_provider",
                    return_value=provider_override,
                )
            )
        stack.enter_context(
            patch(
                "agent.model_metadata.get_model_context_length",
                return_value=context_length,
            )
        )
        stack.enter_context(patch("run_agent.get_tool_definitions", return_value=[]))
        stack.enter_context(
            patch("run_agent.check_toolset_requirements", return_value={})
        )
        stack.enter_context(patch("run_agent.OpenAI"))
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            max_tokens=8_000,
            quiet_mode=True,
            session_db=db,
            session_id=session_id,
            platform=platform,
            chat_id=chat_id,
            chat_type=chat_type,
            user_id=user_id,
            gateway_session_key=(
                f"agent:main:{platform}:{chat_type}:{chat_id}:{user_id}"
            ),
            skip_context_files=True,
            skip_memory=False,
        )
    agent._compression_feasibility_checked = True
    return agent


def _summary_response(content: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _tool_schema_hash(manager: MemoryManager) -> str:
    payload = json.dumps(
        manager.get_all_tool_schemas(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


class _PolicyProvider(MemoryProvider):
    def __init__(self, policy):
        self._policy = policy

    @property
    def name(self) -> str:
        return "policy-test"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        return None

    def get_tool_schemas(self) -> list[dict]:
        return []

    def context_compression_policy(self):
        if isinstance(self._policy, Exception):
            raise self._policy
        return self._policy


@pytest.mark.parametrize(
    "policy",
    [RuntimeError("provider exploded"), None, [], "bad policy", float("nan")],
)
def test_memory_manager_policy_failure_isolated_to_global_config(policy):
    manager = MemoryManager()
    manager.add_provider(_PolicyProvider(policy))
    assert manager.context_compression_policy() == {}


def test_malformed_provider_policy_cannot_break_real_agent_init(
    tmp_path: Path,
    monkeypatch,
):
    home = tmp_path / "hermes-home"
    _write_config(
        home,
        context_length=1_000_000,
        global_compression={
            "enabled": False,
            "threshold": 0.66,
            "threshold_tokens": 70_000,
            "tail_tokens": 9_000,
            "in_place": False,
        },
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    provider = _PolicyProvider(
        {
            "enabled": True,
            "threshold": "eventually",
            "threshold_tokens": True,
            "tail_tokens": 12_000,
            "in_place": True,
            "unknown_future_field": 1,
        }
    )
    db = SessionDB(db_path=home / "state.db")

    agent = _make_agent(
        home=home,
        db=db,
        session_id="malformed-policy",
        provider_override=provider,
        context_length=1_000_000,
    )

    # Invalid fields fall through independently to the global values; valid
    # fields from the same provider still apply. Construction never raises.
    assert agent.compression_enabled is True
    assert agent.compression_in_place is True
    assert agent.context_compressor._config_threshold_percent == 0.66  # noqa: SLF001
    assert agent.context_compressor.threshold_tokens == 70_000
    assert agent.context_compressor.tail_token_budget == 12_000
    db.close()


def test_real_user_plugin_discovery_load_and_aiagent_selection(
    tmp_path: Path,
    monkeypatch,
):
    home = tmp_path / "hermes-home"
    _write_config(home, inject_identity_section=True)
    deployed = _deploy_grantley(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    manager = PluginManager()
    manager._discovered = True  # noqa: SLF001
    monkeypatch.setattr(plugin_runtime, "_plugin_manager", manager)

    assert memory_plugins.find_provider_dir("grantley") == deployed
    assert "grantley" in memory_plugins.list_memory_provider_names()
    loaded = memory_plugins.load_memory_provider("grantley")
    assert loaded is not None
    assert loaded.name == "grantley"
    assert loaded.__class__.__module__.startswith("_hermes_user_memory.grantley")
    loaded.initialize(
        "deployed-telegram",
        platform="telegram",
        chat_id=GROUP_ID,
        chat_type="group",
    )
    assert loaded.context_compression_policy() == {}
    assert loaded.prefetch("no persona leakage") == ""
    assert loaded._store is None  # noqa: SLF001
    assert loaded.get_tool_schemas() == []
    denied = json.loads(
        loaded.handle_tool_call(
            "persona_life_set_state",
            {"mood": "must-not-write"},
        )
    )
    assert denied["error"] == "forbidden_lane"

    db = SessionDB(db_path=home / "state.db")
    agent = _make_agent(home=home, db=db, session_id="real-discovery")
    assert agent._memory_manager is not None  # noqa: SLF001
    selected = agent._memory_manager.get_provider("grantley")  # noqa: SLF001
    assert selected is not None
    assert selected.__class__.__module__.startswith("_hermes_user_memory.grantley")
    assert agent.compression_enabled is True
    assert agent.context_compressor.threshold_tokens == 12_000
    bound_prompt = agent._build_system_prompt("DEPLOYED-IDENTITY")  # noqa: SLF001
    assert "## Plugin Context: grantley.identity" in bound_prompt

    # The same process/config may serve several surfaces. Building the bound
    # agent first must not mutate shared config and leak its policy or storage
    # into later non-bound agents.
    non_bound_agents = []
    for platform, chat_id, chat_type in (
        ("telegram", GROUP_ID, "group"),
        ("cli", GROUP_ID, "group"),
        ("onebot", "999999999", "group"),
    ):
        other = _make_agent(
            home=home,
            db=db,
            session_id=f"real-discovery-{platform}-{chat_id}",
            platform=platform,
            chat_id=chat_id,
            chat_type=chat_type,
        )
        non_bound_agents.append(other)
        assert other.compression_enabled is False
        other_prompt = other._build_system_prompt("DEPLOYED-IDENTITY")  # noqa: SLF001
        assert "## Plugin Context: grantley.identity" not in other_prompt
        other_provider = other._memory_manager.get_provider("grantley")  # noqa: SLF001
        assert other_provider is not None
        assert other_provider._store is None  # noqa: SLF001
        assert other_provider.prefetch("no persona leakage") == ""
        assert other_provider.get_tool_schemas() == []
        assert other._memory_manager.get_all_tool_schemas() == []  # noqa: SLF001
        assert not other._memory_manager.has_tool("persona_life_get")  # noqa: SLF001
        assert not any(
            name.startswith("persona_life_") for name in other.valid_tool_names
        )
        denied = json.loads(
            other_provider.handle_tool_call(
                "persona_life_set_state",
                {"mood": "must-not-write"},
            )
        )
        assert denied["error"] == "forbidden_lane"
    assert agent.compression_enabled is True
    loaded.shutdown()
    for other in non_bound_agents:
        other.shutdown_memory_provider([])
    agent.shutdown_memory_provider([])
    db.close()


@pytest.mark.parametrize(
    "channels",
    [
        pytest.param(_MISSING_CHANNELS, id="missing"),
        pytest.param({}, id="empty"),
        pytest.param([], id="non-mapping"),
        pytest.param({GROUP_ID: "malformed"}, id="malformed-entry"),
        pytest.param({GROUP_ID: {}}, id="missing-persona"),
        pytest.param(
            {GROUP_ID: {"persona": "someone-else"}},
            id="other-persona",
        ),
        pytest.param(
            {"999999999": {"persona": "grantley"}},
            id="unmatched-chat-id",
        ),
    ],
)
def test_real_loader_aiagent_fails_closed_without_explicit_binding(
    tmp_path: Path,
    monkeypatch,
    channels,
):
    home = tmp_path / "hermes-home"
    _write_config(
        home,
        inject_identity_section=True,
        channels=channels,
    )
    _deploy_grantley(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    manager = PluginManager()
    manager._discovered = True  # noqa: SLF001
    monkeypatch.setattr(plugin_runtime, "_plugin_manager", manager)
    db = SessionDB(db_path=home / "state.db")

    agent = _make_agent(
        home=home,
        db=db,
        session_id="strict-unbound",
        platform="onebot",
        chat_id=GROUP_ID,
        chat_type="group",
    )
    provider = agent._memory_manager.get_provider("grantley")  # noqa: SLF001
    assert provider is not None
    assert provider.__class__.__module__.startswith("_hermes_user_memory.grantley")
    assert provider._store is None  # noqa: SLF001
    assert provider.prefetch("no persona leakage") == ""
    assert provider.get_tool_schemas() == []
    assert agent._memory_manager.get_all_tool_schemas() == []  # noqa: SLF001
    assert not agent._memory_manager.has_tool("persona_life_get")  # noqa: SLF001
    assert not any(name.startswith("persona_life_") for name in agent.valid_tool_names)
    assert not any(
        str(tool.get("function", {}).get("name", "")).startswith("persona_life_")
        for tool in agent.tools
        if isinstance(tool, dict)
    )
    prompt = agent._build_system_prompt("STRICT-ALLOWLIST")  # noqa: SLF001
    assert "## Plugin Context: grantley.identity" not in prompt
    denied = json.loads(
        provider.handle_tool_call(
            "persona_life_set_state",
            {"state": "training"},
        )
    )
    assert denied["error"] == "forbidden_lane"
    assert not (home / "plugin-data" / "grantley" / "data.db").exists()
    agent.shutdown_memory_provider([])
    db.close()


@pytest.mark.parametrize("chat_type", ["group", "dm", "private"])
def test_real_loader_aiagent_bound_lane_gets_exact_stable_tool_surface(
    tmp_path: Path,
    monkeypatch,
    chat_type: str,
):
    home = tmp_path / "hermes-home"
    _write_config(
        home,
        inject_identity_section=True,
        channels={
            int(GROUP_ID): {
                "persona": "grantley",
                "group": chat_type == "group",
            }
        },
    )
    _deploy_grantley(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    manager = PluginManager()
    manager._discovered = True  # noqa: SLF001
    monkeypatch.setattr(plugin_runtime, "_plugin_manager", manager)
    db = SessionDB(db_path=home / "state.db")

    agent = _make_agent(
        home=home,
        db=db,
        session_id=f"strict-bound-{chat_type}",
        platform="onebot",
        chat_id=GROUP_ID,
        chat_type=chat_type,
    )
    provider = agent._memory_manager.get_provider("grantley")  # noqa: SLF001
    assert provider is not None
    assert provider._store is not None  # noqa: SLF001
    assert provider.prefetch("bound lane")
    assert len(provider.get_tool_schemas()) == 6
    assert len(agent._memory_manager.get_all_tool_schemas()) == 6  # noqa: SLF001
    assert len(
        [name for name in agent.valid_tool_names if name.startswith("persona_life_")]
    ) == 6
    assert agent._memory_manager.has_tool("persona_life_get")  # noqa: SLF001
    assert agent._memory_manager.has_tool("persona_life_set_state")  # noqa: SLF001
    assert len(
        [
            tool
            for tool in agent.tools
            if str(tool.get("function", {}).get("name", "")).startswith(
                "persona_life_"
            )
        ]
    ) == 6
    prompt = agent._build_system_prompt("STRICT-ALLOWLIST")  # noqa: SLF001
    assert "## Plugin Context: grantley.identity" in prompt

    before = _tool_schema_hash(agent._memory_manager)  # noqa: SLF001
    written = json.loads(
        provider.handle_tool_call(
            "persona_life_set_state",
            {"state": "training", "activity": "schema stability check"},
        )
    )
    assert written["ok"] is True
    assert _tool_schema_hash(agent._memory_manager) == before  # noqa: SLF001
    assert (home / "plugin-data" / "grantley" / "data.db").exists()
    agent.shutdown_memory_provider([])
    db.close()


def _append_complete_turns(messages: list[dict], label: str, count: int = 18) -> None:
    """Append completed turns with one complete assistant/tool/result group."""
    if messages and messages[-1].get("role") == "user":
        messages.append({"role": "assistant", "content": f"{label}-bridge"})
    payload = (label + "-detail-") * 180
    for index in range(count):
        messages.append(
            {"role": "user", "content": f"{label}-question-{index} {payload}"}
        )
        if index == count - 3:
            call_id = f"{label}-tool-call"
            messages.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path":"notes.txt"}',
                            },
                        }
                    ],
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": f"{label}-tool-result {payload}",
                }
            )
            messages.append(
                {"role": "assistant", "content": f"{label}-tool-finished"}
            )
        else:
            messages.append(
                {"role": "assistant", "content": f"{label}-answer-{index}"}
            )


def _assert_tool_pairs_and_alternation(messages: list[dict]) -> None:
    call_ids = {
        call.get("id")
        for message in messages
        for call in (message.get("tool_calls") or [])
        if isinstance(call, dict) and call.get("id")
    }
    result_ids = {
        message.get("tool_call_id")
        for message in messages
        if message.get("role") == "tool"
    }
    assert call_ids == result_ids

    visible_roles: list[str] = []
    for message in messages:
        role = message.get("role")
        if role not in {"user", "assistant"}:
            continue
        if role == "assistant" and message.get("tool_calls"):
            continue
        visible_roles.append(role)
    assert all(left != right for left, right in zip(visible_roles, visible_roles[1:]))


def _append_unfinished_tool_turn(messages: list[dict], label: str) -> list[dict]:
    """Append a valid tool result whose assistant follow-up is still pending."""
    call_id = f"{label}-pending-call"
    suffix = [
        {"role": "user", "content": f"{label}-pending-question"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path":"pending.txt"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": call_id,
            "content": f"{label}-pending-result",
        },
    ]
    messages.extend(copy.deepcopy(suffix))
    return suffix


def _assert_no_unmarked_synthetic_user(
    before: list[dict],
    after: list[dict],
) -> None:
    authored = {
        str(message.get("content"))
        for message in before
        if message.get("role") == "user"
    }
    for message in after:
        if message.get("role") != "user":
            continue
        if str(message.get("content")) in authored:
            continue
        assert (
            is_compaction_summary_message(message)
            or message.get("_todo_snapshot_synthetic") is True
        ), message


def _install_summary_llm(seen_prompts: list[str], *, prefix: str = "GRANTLEY-ROLLING"):
    def summarize(**kwargs):
        prompt = str(kwargs["messages"][0]["content"])
        seen_prompts.append(prompt)
        return _summary_response(f"{prefix}-{len(seen_prompts)}")

    return patch("agent.context_compressor.call_llm", side_effect=summarize)


def _assert_system_bytes_equal(actual: str, expected: str) -> None:
    assert actual.encode() == expected.encode(), "\n".join(
        difflib.unified_diff(
            expected.splitlines(),
            actual.splitlines(),
            fromfile="cached-system",
            tofile="post-compaction-system",
            lineterm="",
        )
    )


def test_real_config_sessiondb_repeats_resumes_and_rolls_back_failure(
    tmp_path: Path,
    monkeypatch,
):
    """Two live boundaries + restart boundary + durable failed retry.

    This is intentionally an E2E integration of the real config loader,
    AIAgent wiring, ContextCompressor, in-place SessionDB transaction, rolling
    handoff rehydration, and durable failure cooldown. Only the summary model
    call itself is replaced with deterministic local text.
    """
    home = tmp_path / "hermes-home"
    _write_config(home)
    _deploy_grantley(home)
    monkeypatch.setenv("HERMES_HOME", str(home))

    db_path = home / "state.db"
    db = SessionDB(db_path=db_path)
    session_id = "grantley-qq-lane"
    agent = _make_agent(home=home, db=db, session_id=session_id)
    compressor = agent.context_compressor
    stable_tool_schema_hash = _tool_schema_hash(agent._memory_manager)  # noqa: SLF001
    assert len(agent._memory_manager.get_all_tool_schemas()) == 6  # noqa: SLF001

    assert agent.compression_enabled is True
    assert agent.compression_in_place is True
    assert compressor.abort_on_summary_failure is True
    assert compressor.threshold_percent == 0.80
    assert compressor.threshold_tokens == 12_000
    assert compressor.tail_token_budget == 1_200
    assert compressor.should_compress(11_999) is False
    assert compressor.should_compress(12_000) is True

    stable_system = agent._build_system_prompt("GRANTLEY-STABLE-SYSTEM")
    agent._cached_system_prompt = stable_system
    messages: list[dict] = [{"role": "system", "content": stable_system}]
    _append_complete_turns(messages, "epoch-one")
    pending_suffix = _append_unfinished_tool_turn(messages, "epoch-one")
    agent._ensure_db_session()
    assert agent._flush_messages_to_session_db(messages, []) is True

    seen: list[str] = []
    with _install_summary_llm(seen):
        first, first_system = agent._compress_context(
            messages,
            "GRANTLEY-STABLE-SYSTEM",
            approx_tokens=20_000,
        )
    _assert_system_bytes_equal(first_system, stable_system)
    assert agent.session_id == session_id
    assert compressor.compression_count == 1
    assert len([m for m in first if is_compaction_summary_message(m)]) == 1
    assert _tool_schema_hash(agent._memory_manager) == stable_tool_schema_hash  # noqa: SLF001
    assert [
        {key: value for key, value in message.items() if key != "_row_id"}
        for message in first[-len(pending_suffix) :]
    ] == pending_suffix
    _assert_tool_pairs_and_alternation(first)
    _assert_no_unmarked_synthetic_user(messages, first)

    compressor.update_from_response(
        {"prompt_tokens": 4_000, "completion_tokens": 100}
    )
    second_input = copy.deepcopy(first)
    _append_complete_turns(second_input, "epoch-two")
    with _install_summary_llm(seen):
        second, second_system = agent._compress_context(
            second_input,
            "GRANTLEY-STABLE-SYSTEM",
            approx_tokens=21_000,
        )
    _assert_system_bytes_equal(second_system, stable_system)
    assert agent.session_id == session_id
    assert compressor.compression_count == 2
    assert len([m for m in second if is_compaction_summary_message(m)]) == 1
    assert _tool_schema_hash(agent._memory_manager) == stable_tool_schema_hash  # noqa: SLF001
    assert "PREVIOUS SUMMARY:" in seen[1]
    assert "GRANTLEY-ROLLING-1" in seen[1]
    _assert_tool_pairs_and_alternation(second)
    _assert_no_unmarked_synthetic_user(second_input, second)

    # A process restart reloads only the active compacted rows under the same
    # stable lane id; the next compressor rehydrates the persisted handoff.
    db.close()
    resumed_db = SessionDB(db_path=db_path)
    resumed_agent = _make_agent(
        home=home,
        db=resumed_db,
        session_id=session_id,
    )
    assert _tool_schema_hash(resumed_agent._memory_manager) == stable_tool_schema_hash  # noqa: SLF001
    resumed = resumed_db.get_messages_as_conversation(session_id)
    _append_complete_turns(resumed, "epoch-three")
    resumed_system_before = resumed_agent._build_system_prompt(
        "GRANTLEY-STABLE-SYSTEM"
    )
    resumed_agent._cached_system_prompt = resumed_system_before
    resumed_seen: list[str] = []
    with _install_summary_llm(resumed_seen, prefix="GRANTLEY-RESUMED"):
        third, third_system = resumed_agent._compress_context(
            resumed,
            "GRANTLEY-STABLE-SYSTEM",
            approx_tokens=22_000,
        )
    _assert_system_bytes_equal(third_system, resumed_system_before)
    assert resumed_agent.session_id == session_id
    assert resumed_seen and "GRANTLEY-ROLLING-2" in resumed_seen[0]
    assert len([m for m in third if is_compaction_summary_message(m)]) == 1
    assert _tool_schema_hash(resumed_agent._memory_manager) == stable_tool_schema_hash  # noqa: SLF001
    _assert_tool_pairs_and_alternation(third)
    _assert_no_unmarked_synthetic_user(resumed, third)

    # Failed summary generation is transactional: original live text and DB
    # active rows stay intact, and a durable cooldown prevents a hot retry.
    resumed_agent.context_compressor.update_from_response(
        {"prompt_tokens": 4_000, "completion_tokens": 100}
    )
    failure_input = copy.deepcopy(third)
    _append_complete_turns(failure_input, "failure-epoch")
    db_before_failure = resumed_db.get_messages_as_conversation(session_id)

    with patch(
        "agent.context_compressor.call_llm",
        side_effect=TimeoutError("simulated summary timed out"),
    ):
        returned, returned_system = resumed_agent._compress_context(
            failure_input,
            "GRANTLEY-STABLE-SYSTEM",
            approx_tokens=23_000,
        )
    assert returned == failure_input
    _assert_system_bytes_equal(returned_system, resumed_system_before)
    assert resumed_db.get_messages_as_conversation(session_id) == db_before_failure
    cooldown = resumed_db.get_compression_failure_cooldown(session_id)
    assert cooldown and cooldown["remaining_seconds"] > 0
    should_retry, reason = resumed_agent.context_compressor.should_compress_info(
        23_000
    )
    assert should_retry is False
    assert reason and reason.startswith("cooldown:")

    # The guard is durable rather than an in-memory side effect. A fully new
    # SessionDB and AIAgent must refuse the same automatic summary before the
    # external LLM stub is reached.
    resumed_db.close()
    cooldown_db = SessionDB(db_path=db_path)
    cooldown_agent = _make_agent(
        home=home,
        db=cooldown_db,
        session_id=session_id,
    )
    cooldown_agent._cached_system_prompt = cooldown_agent._build_system_prompt(  # noqa: SLF001
        "GRANTLEY-STABLE-SYSTEM"
    )
    with patch(
        "agent.context_compressor.call_llm",
        return_value=_summary_response("must-not-run"),
    ) as summary_llm:
        cooled_returned, _ = cooldown_agent._compress_context(
            failure_input,
            "GRANTLEY-STABLE-SYSTEM",
            approx_tokens=23_000,
        )
    assert cooled_returned == failure_input
    summary_llm.assert_not_called()

    # Compaction handoffs may occupy a provider-required user role, but they
    # are always metadata-marked and never become a new actionable user turn.
    for message in third:
        if message.get("role") == "user" and message.get(
            COMPRESSED_SUMMARY_METADATA_KEY
        ):
            assert is_compaction_summary_message(message)

    cooldown_db.close()


class _ReloadingBuiltinMemory:
    def __init__(self, value: str):
        self.disk_value = value
        self.snapshot = value

    def load_from_disk(self) -> None:
        self.snapshot = self.disk_value

    def format_for_system_prompt(self, target: str):
        if target != "memory":
            return None
        return f"BUILTIN-MEMORY-SNAPSHOT:{self.snapshot}"


@pytest.mark.parametrize("memory_changed", [False, True])
def test_grantley_prompt_stability_also_requires_fresh_builtin_memory(
    tmp_path: Path,
    monkeypatch,
    memory_changed: bool,
):
    home = tmp_path / "hermes-home"
    _write_config(home)
    _deploy_grantley(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    db = SessionDB(db_path=home / "state.db")
    agent = _make_agent(
        home=home,
        db=db,
        session_id=f"prompt-stability-{memory_changed}",
    )
    assert agent._memory_manager.system_prompt_stable_across_compression() is True  # noqa: SLF001

    store = _ReloadingBuiltinMemory("old")
    agent._memory_store = store  # noqa: SLF001
    agent._memory_enabled = True  # noqa: SLF001
    cached = agent._build_system_prompt("STABLE-SYSTEM")  # noqa: SLF001
    agent._cached_system_prompt = cached  # noqa: SLF001
    messages = [{"role": "system", "content": cached}]
    _append_complete_turns(messages, "memory-check")
    agent._ensure_db_session()  # noqa: SLF001
    assert agent._flush_messages_to_session_db(messages, []) is True  # noqa: SLF001
    if memory_changed:
        store.disk_value = "new"

    seen: list[str] = []
    with _install_summary_llm(seen, prefix="MEMORY-CHECK"):
        compressed, rebuilt = agent._compress_context(
            messages,
            "STABLE-SYSTEM",
            approx_tokens=20_000,
        )

    if memory_changed:
        assert rebuilt != cached
        assert "BUILTIN-MEMORY-SNAPSHOT:new" in rebuilt
        assert "BUILTIN-MEMORY-SNAPSHOT:old" not in rebuilt
    else:
        _assert_system_bytes_equal(rebuilt, cached)
    _assert_tool_pairs_and_alternation(compressed)
    db.close()


def test_other_memory_provider_keeps_conservative_prompt_rebuild_default():
    manager = MemoryManager()
    manager.add_provider(_PolicyProvider({}))
    assert manager.system_prompt_stable_across_compression() is False


def test_same_grantley_session_concurrency_compacts_once(
    tmp_path: Path,
    monkeypatch,
):
    home = tmp_path / "hermes-home"
    _write_config(home)
    _deploy_grantley(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    db_path = home / "state.db"
    db_one = SessionDB(db_path=db_path)
    session_id = "grantley-concurrent-lane"
    agent_one = _make_agent(home=home, db=db_one, session_id=session_id)
    stable = agent_one._build_system_prompt("CONCURRENT-SYSTEM")  # noqa: SLF001
    messages = [{"role": "system", "content": stable}]
    _append_complete_turns(messages, "concurrent")
    agent_one._cached_system_prompt = stable  # noqa: SLF001
    agent_one._ensure_db_session()  # noqa: SLF001
    assert agent_one._flush_messages_to_session_db(messages, []) is True  # noqa: SLF001

    db_two = SessionDB(db_path=db_path)
    agent_two = _make_agent(home=home, db=db_two, session_id=session_id)
    agent_two._cached_system_prompt = stable  # noqa: SLF001
    assert agent_one._memory_manager.get_provider("grantley") is not None  # noqa: SLF001
    assert agent_two._memory_manager.get_provider("grantley") is not None  # noqa: SLF001

    start = threading.Barrier(3)
    summary_entered = threading.Event()
    release_summary = threading.Event()
    one_finished = threading.Event()
    call_lock = threading.Lock()
    call_count = 0
    results: list[tuple[list[dict], str]] = []
    errors: list[BaseException] = []

    def slow_summary(**_kwargs):
        nonlocal call_count
        with call_lock:
            call_count += 1
            number = call_count
        summary_entered.set()
        assert release_summary.wait(timeout=5)
        return _summary_response(f"CONCURRENT-SUMMARY-{number}")

    def run(agent) -> None:
        try:
            start.wait(timeout=5)
            results.append(
                agent._compress_context(
                    copy.deepcopy(messages),
                    "CONCURRENT-SYSTEM",
                    approx_tokens=20_000,
                )
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            one_finished.set()

    with patch("agent.context_compressor.call_llm", side_effect=slow_summary):
        workers = [
            threading.Thread(target=run, args=(agent_one,), daemon=True),
            threading.Thread(target=run, args=(agent_two,), daemon=True),
        ]
        for worker in workers:
            worker.start()
        start.wait(timeout=5)
        assert summary_entered.wait(timeout=5)
        # The losing contender returns while the winner remains inside the
        # external summary call, proving the durable lock serialized work.
        assert one_finished.wait(timeout=5)
        release_summary.set()
        for worker in workers:
            worker.join(timeout=5)
            assert not worker.is_alive()

    assert errors == []
    assert call_count == 1
    assert len(results) == 2
    lengths = sorted(len(result[0]) for result in results)
    assert lengths[0] < lengths[1]
    assert lengths[1] == len(messages)
    durable = db_one.get_messages_as_conversation(session_id)
    assert len([m for m in durable if is_compaction_summary_message(m)]) == 1
    db_two.close()
    db_one.close()


def test_two_real_grantley_onebot_lanes_roll_independently(
    tmp_path: Path,
    monkeypatch,
):
    home = tmp_path / "hermes-home"
    _write_config(home)
    _deploy_grantley(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    db = SessionDB(db_path=home / "state.db")
    onebot = Platform("onebot")
    lane_ids = {
        "lane-a": build_session_key(
            SessionSource(
                platform=onebot,
                chat_id=GROUP_ID,
                chat_type="group",
                user_id=OWNER_ID,
            )
        ),
        "lane-b": build_session_key(
            SessionSource(
                platform=onebot,
                chat_id=GROUP_ID,
                chat_type="group",
                user_id=SECOND_OWNER_ID,
            )
        ),
    }
    assert len(set(lane_ids.values())) == 2
    agents = {
        "lane-a": _make_agent(
            home=home,
            db=db,
            session_id=lane_ids["lane-a"],
            user_id=OWNER_ID,
        ),
        "lane-b": _make_agent(
            home=home,
            db=db,
            session_id=lane_ids["lane-b"],
            user_id=SECOND_OWNER_ID,
        ),
    }
    messages: dict[str, list[dict]] = {}
    for lane, agent in agents.items():
        assert agent._memory_manager.get_provider("grantley") is not None  # noqa: SLF001
        stable = agent._build_system_prompt(f"{lane}-SYSTEM")  # noqa: SLF001
        agent._cached_system_prompt = stable  # noqa: SLF001
        lane_messages = [{"role": "system", "content": stable}]
        _append_complete_turns(lane_messages, f"{lane}-epoch-one")
        messages[lane] = lane_messages
        agent._ensure_db_session()  # noqa: SLF001
        assert agent._flush_messages_to_session_db(lane_messages, []) is True  # noqa: SLF001

    prompts: dict[str, list[str]] = {"lane-a": [], "lane-b": []}

    def lane_summary(**kwargs):
        prompt = str(kwargs["messages"][0]["content"])
        lane = "lane-a" if "lane-a-epoch" in prompt else "lane-b"
        prompts[lane].append(prompt)
        return _summary_response(f"{lane}-SUMMARY-{len(prompts[lane])}")

    with patch("agent.context_compressor.call_llm", side_effect=lane_summary):
        for lane, agent in agents.items():
            messages[lane], _ = agent._compress_context(
                messages[lane],
                f"{lane}-SYSTEM",
                approx_tokens=20_000,
            )
        for lane, agent in agents.items():
            agent.context_compressor.update_from_response(
                {"prompt_tokens": 4_000, "completion_tokens": 100}
            )
            _append_complete_turns(messages[lane], f"{lane}-epoch-two")
            compaction_input = copy.deepcopy(messages[lane])
            messages[lane], _ = agent._compress_context(
                messages[lane],
                f"{lane}-SYSTEM",
                approx_tokens=21_000,
            )
            _assert_no_unmarked_synthetic_user(compaction_input, messages[lane])

    for lane, other in (("lane-a", "lane-b"), ("lane-b", "lane-a")):
        assert len(prompts[lane]) == 2
        assert "PREVIOUS SUMMARY:" in prompts[lane][1]
        assert f"{lane}-SUMMARY-1" in prompts[lane][1]
        assert f"{other}-SUMMARY-1" not in prompts[lane][1]
        assert len(
            [m for m in messages[lane] if is_compaction_summary_message(m)]
        ) == 1
        _assert_tool_pairs_and_alternation(messages[lane])
        durable_text = "\n".join(
            str(m.get("content") or "")
            for m in db.get_messages_as_conversation(lane_ids[lane])
        )
        assert f"{other}-SUMMARY" not in durable_text
    db.close()


def test_onebot_lane_keys_and_sessiondb_compaction_are_isolated(tmp_path: Path):
    onebot = Platform("onebot")
    group_a_user_1 = build_session_key(
        SessionSource(
            platform=onebot,
            chat_id="group-a",
            chat_type="group",
            user_id="user-1",
        )
    )
    group_a_user_2 = build_session_key(
        SessionSource(
            platform=onebot,
            chat_id="group-a",
            chat_type="group",
            user_id="user-2",
        )
    )
    group_b_user_1 = build_session_key(
        SessionSource(
            platform=onebot,
            chat_id="group-b",
            chat_type="group",
            user_id="user-1",
        )
    )
    dm_user_1 = build_session_key(
        SessionSource(
            platform=onebot,
            chat_id="user-1",
            chat_type="dm",
            user_id="user-1",
        )
    )
    lane_ids = {
        group_a_user_1,
        group_a_user_2,
        group_b_user_1,
        dm_user_1,
    }
    assert len(lane_ids) == 4
    # Existing shared-group mode is still honored (including proactive/cron
    # events that intentionally seed the same group/user source).
    assert build_session_key(
        SessionSource(
            platform=onebot,
            chat_id="group-a",
            chat_type="group",
            user_id="user-1",
        ),
        group_sessions_per_user=False,
    ) == build_session_key(
        SessionSource(
            platform=onebot,
            chat_id="group-a",
            chat_type="group",
            user_id="user-2",
        ),
        group_sessions_per_user=False,
    )

    db = SessionDB(db_path=tmp_path / "state.db")
    for lane_id in lane_ids:
        db.create_session(lane_id, source="onebot")
        db.append_message(lane_id, "user", f"original:{lane_id}")

    db.archive_and_compact(
        group_a_user_1,
        [
            {"role": "assistant", "content": "compacted group-a/user-1"},
            {"role": "user", "content": "recent group-a/user-1"},
        ],
    )
    assert [
        row["content"] for row in db.get_messages_as_conversation(group_a_user_1)
    ] == ["compacted group-a/user-1", "recent group-a/user-1"]
    for lane_id in lane_ids - {group_a_user_1}:
        assert [
            row["content"] for row in db.get_messages_as_conversation(lane_id)
        ] == [f"original:{lane_id}"]
    db.close()
