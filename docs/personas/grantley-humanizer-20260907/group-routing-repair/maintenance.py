"""Probe canonical group metadata and fork only identities that missed loading.

Preflight uses a private database snapshot and a private plugin copy. Apply and
rollback are called only while the gateway is stopped. No messages are sent.
"""
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path("/opt/hermes/repo")
DATA = Path("/opt/hermes/data")
STAGE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(STAGE.parent))
REVISION = "grantley-group-routing-20260907"


def copy_db(source, target):
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    src.backup(dst)
    src.close()
    dst.close()


def migrate(home, *, rollback=False):
    from gateway.config import load_gateway_config
    from gateway.session import SessionStore
    from plugins.grantley.channel_binding import bindings_from_config
    from plugins.grantley.persona import load_persona_document
    import revise_sessions

    config = yaml.safe_load((home / "config.yaml").read_text())
    channels = config["plugins"]["entries"]["grantley"]["settings"]["channels"]
    stable = load_persona_document().stable.strip()
    store = SessionStore(home / "sessions", load_gateway_config())
    store._ensure_loaded()
    selected, unchanged = {}, []
    try:
        for chat, binding in bindings_from_config(channels).items():
            if not binding.is_group:
                continue
            entry = store._entries.get(f"agent:main:onebot:group:g{chat}")
            if not entry:
                continue
            prompt = store._db.get_session(entry.session_id).get("system_prompt")
            # A never-started session will load the repaired identity normally.
            if rollback:
                eligible = entry.session_id.startswith(REVISION + "-")
            else:
                eligible = bool(prompt) and stable not in prompt
            if eligible:
                selected[chat] = channels[chat]
            else:
                unchanged.append({"session_id": entry.session_id, "has_correct_identity": bool(prompt and stable in prompt),
                                  "awaiting_first_turn": not prompt})
    finally:
        for db in store._db_handles.values():
            if db is not None:
                db.close()
    revise_sessions.REVISION = REVISION
    return {"revisions": revise_sessions.revise(home, load_gateway_config(), selected, rollback=rollback),
            "unchanged_group_sessions": unchanged}


def probe(home):
    from hermes_cli import plugins as runtime
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
    from plugins.platforms.onebot.adapter import format_chat_id
    from run_agent import AIAgent

    directory = home / "plugins/grantley"
    spec = importlib.util.spec_from_file_location("_routing_probe", directory / "__init__.py",
                                                submodule_search_locations=[str(directory)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    persona = importlib.import_module("_routing_probe.persona").load_persona_document()
    manager = PluginManager()
    manager._discovered = True
    module.register(PluginContext(PluginManifest(name="grantley", key="grantley", source="bundled"), manager))
    runtime._plugin_manager = manager
    config = yaml.safe_load((home / "config.yaml").read_text())
    settings = config["plugins"]["entries"]["grantley"]["settings"]
    provider_cls = importlib.import_module("_routing_probe.memory_provider").GrantleyMemoryProvider
    results = []
    for chat, binding in settings["channels"].items():
        group = binding.get("group") is True
        chat_id = format_chat_id(group, chat)
        kind = "group" if group else "dm"
        agent = AIAgent(api_key="test-key", base_url="http://127.0.0.1:1", provider="custom",
                        model="test/model", platform="onebot", chat_id=chat_id, chat_type=kind,
                        session_id="routing-probe-" + chat_id, enabled_toolsets=[], quiet_mode=True,
                        skip_memory=True, skip_context_files=True, skip_background_review=True)
        prompt = agent._build_system_prompt()
        assert persona.stable.strip() in prompt
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        provider = provider_cls(config=settings, connection=connection)
        provider.initialize("routing-probe", platform="onebot", chat_id=chat_id, chat_type=kind)
        assert provider.get_tool_schemas()
        assert "<persona-state>" in provider.prefetch("hello")
        assert provider.context_compression_policy()
        connection.close()
        results.append({"chat_id": chat_id, "identity_assembled": True, "state_and_policy_enabled": True})
    assert not manager.render_system_prompt_sections({"platform": "telegram", "chat_id": "183287894", "chat_type": "group"})
    return results


mode = sys.argv[1]
if mode in {"preflight", "verify"}:
    with tempfile.TemporaryDirectory(prefix="grantley-routing-") as temporary:
        home = Path(temporary)
        (home / "sessions").mkdir()
        config = yaml.safe_load((DATA / "config.yaml").read_text())
        # Limit probe config to the actual binding settings; no messaging or secrets.
        (home / "config.yaml").write_text(yaml.safe_dump({
            "plugins": {"enabled": [], "entries": {"grantley": config["plugins"]["entries"]["grantley"]}},
            "memory": {"provider": "none"}, "compression": {"enabled": False},
        }, allow_unicode=True))
        shutil.copytree(DATA / "plugins/grantley", home / "plugins/grantley", ignore=shutil.ignore_patterns("__pycache__"))
        if mode == "preflight":
            shutil.copy2(STAGE / "channel_binding.py", home / "plugins/grantley/channel_binding.py")
        os.environ["HERMES_HOME"] = str(home)
        checks = probe(home)
        # Re-read actual config in the private home for faithful session routing.
        (home / "config.yaml").write_text(yaml.safe_dump(config, allow_unicode=True))
        shutil.copy2(DATA / "sessions/sessions.json", home / "sessions/sessions.json")
        copy_db(DATA / "state.db", home / "state.db")
        first = migrate(home)
        second = migrate(home)
        assert not second["revisions"]
        if mode == "preflight":
            recovered = migrate(home, rollback=True)
            assert len(recovered["revisions"]) == len(first["revisions"])
        print(json.dumps({"passed": True, "mode": mode, "canonical_sessions": checks,
                          "private_migration": first, "idempotent": True, "no_messages_sent": True}), flush=True)
elif mode in {"apply", "rollback"}:
    os.environ["HERMES_HOME"] = str(DATA)
    print(json.dumps({"passed": True, **migrate(DATA, rollback=mode == "rollback")}), flush=True)
else:
    raise SystemExit("Unknown operation")
