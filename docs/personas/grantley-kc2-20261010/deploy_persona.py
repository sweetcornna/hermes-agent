"""Grantly KC2 persona revision 2026-10-10 on P1 (native systemd, run as the hermes user).

preflight  fork every bound lane in a private copy of state.db/sessions.json twice
           (idempotent) and once more as rollback; nothing in production is touched
revise     fork bound lanes in production (gateway MUST be stopped)
rollback   fork again onto restored assets (gateway MUST be stopped)
verify     real memory-provider route: bound lanes get the new identity + voice,
           unbound lanes get nothing; the sticker menu offers the 12 KC2 faces
           and the 立绘 registry resolves all 14 keys
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import yaml

PRODUCTION = Path("/opt/hermes/data")
REVISION = "grantley-kc2-20261010"
sys.path.insert(0, "/opt/hermes/repo/docs/personas/grantley-humanizer-20260907")

mode = sys.argv[1]
config = yaml.safe_load((PRODUCTION / "config.yaml").read_text())
channels = config["plugins"]["entries"]["grantley"]["settings"]["channels"]


def migrate(home, rollback=False):
    import revise_sessions
    from gateway.config import load_gateway_config

    revise_sessions.REVISION = REVISION
    records = revise_sessions.revise(home, load_gateway_config(), channels, rollback=rollback)
    return records + reset_proactive(home)


def reset_proactive(home):
    """Proactive posts run in their own per-group sessions with their own frozen prompt.

    They carry no conversation worth branching, so a plain non-destructive reset
    (old row kept, new session id, prompt re-rendered) is enough.
    """
    from gateway.config import load_gateway_config
    from gateway.session import SessionStore
    from plugins.grantley.channel_binding import bindings_from_config

    store = SessionStore(home / "sessions", load_gateway_config())
    store._ensure_loaded()
    out = []
    try:
        for binding in bindings_from_config(channels).values():
            if not binding.is_group:
                continue
            key = f"agent:main:onebot:group:g{binding.chat_id}:proactive"
            if key not in store._entries:
                continue
            old = store._entries[key].session_id
            new = store.reset_session(key)
            out.append({"lane": key, "old_session_id": old, "new_session_id": new.session_id})
    finally:
        for database in store._db_handles.values():
            if database is not None:
                database.close()
    return out


if mode == "preflight":
    with tempfile.TemporaryDirectory(prefix="grantley-kc2-preflight-") as temporary:
        home = Path(temporary)
        os.environ["HERMES_HOME"] = str(home)
        (home / "config.yaml").write_text(yaml.safe_dump(config, allow_unicode=True))
        (home / "sessions").mkdir()
        shutil.copy2(PRODUCTION / "sessions/sessions.json", home / "sessions/sessions.json")
        source = sqlite3.connect(f"file:{PRODUCTION}/state.db?mode=ro", uri=True)
        target = sqlite3.connect(home / "state.db")
        source.backup(target)
        source.close()
        target.close()
        first = migrate(home)
        second = migrate(home)
        assert all(r.get("already_revised") or r.get("new_on_first_message") or r["lane"].endswith(":proactive")
                   for r in second), second
        restored = migrate(home, rollback=True)
        print(json.dumps({"passed": True, "revisions": first, "idempotent": True,
                          "rollback_forks": len(restored)}, ensure_ascii=False))
elif mode in {"revise", "rollback"}:
    os.environ["HERMES_HOME"] = str(PRODUCTION)
    print(json.dumps({"passed": True, "revisions": migrate(PRODUCTION, rollback=mode == "rollback")},
                     ensure_ascii=False))
elif mode == "verify":
    os.environ["HERMES_HOME"] = str(PRODUCTION)
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
    from plugins.grantley.channel_binding import bindings_from_config
    from plugins.memory import load_memory_provider

    provider = load_memory_provider("grantley")
    module = sys.modules[type(provider).__module__.rsplit(".", 1)[0]]
    persona = sys.modules[module.__name__ + ".persona"]
    doc = persona.load_persona_document()
    voice = persona.load_voice_document().strip()
    manager = PluginManager()
    manager._discovered = True
    module.register(PluginContext(PluginManifest(name="grantley", key="grantley", source="user"), manager))
    out = {"assets_dir": str(persona.ASSETS_DIR), "stable_chars": len(doc.stable.strip()),
           "voice_chars": len(voice), "lanes": {}}
    for binding in bindings_from_config(channels).values():
        chat_id = f"g{binding.chat_id}" if binding.is_group else binding.chat_id
        parts = manager.render_system_prompt_sections(
            {"platform": "onebot", "chat_id": chat_id, "chat_type": "group" if binding.is_group else "dm"})
        got = {p.id: p.content for p in parts}
        assert got.get("grantley.identity") == doc.stable.strip(), binding.chat_id
        assert got.get("grantley.voice") == voice, binding.chat_id
        out["lanes"][chat_id] = [(k, len(v)) for k, v in got.items()]
    for probe in ({"platform": "onebot", "chat_id": "g1", "chat_type": "group"},
                  {"platform": "telegram", "chat_id": "g894800697", "chat_type": "group"},
                  {"platform": "cli", "chat_id": "", "chat_type": ""}):
        assert not [p for p in manager.render_system_prompt_sections(probe)
                    if p.id.startswith("grantley.")], probe
    out["unbound_lanes_empty"] = True

    from plugins.image_gen.cornna import characters
    from plugins.platforms.onebot import sticker

    faces = [entry["slug"] for entry in sticker.available_stickers()]
    assert faces == [entry["slug"] for entry in sticker.STICKER_CATALOG], faces
    assert sticker.sticker_path("thumbs-up") == sticker.sticker_path("smug")
    menu = sticker.render_menu(sticker.available_stickers())
    assert "thumbs-up" not in menu and "[STICKER:" in menu
    out["stickers"] = faces
    present = characters.available_characters()
    assert sorted(present) == sorted(characters.CHARACTER_KEYS), present
    out["characters"] = {"dir": str(characters.character_dir()), "keys": len(present)}
    print(json.dumps(out, ensure_ascii=False))
else:
    raise SystemExit(f"unknown mode {mode}")
