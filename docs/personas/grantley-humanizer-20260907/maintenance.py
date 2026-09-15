"""Container-side private preflight, history revision and installed verification."""
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import yaml

sys.path.insert(0, "/opt/hermes/repo")
production = Path("/opt/hermes/data")
stage = Path(__file__).resolve().parent
mode = sys.argv[1]
config = yaml.safe_load((production / "config.yaml").read_text())
channels = config["plugins"]["entries"]["grantley"]["settings"]["channels"]


def migrate(home, rollback=False):
    from gateway.config import load_gateway_config
    from revise_sessions import revise
    return revise(home, load_gateway_config(), channels, rollback=rollback)


if mode == "preflight":
    with tempfile.TemporaryDirectory(prefix="grantley-humanizer-preflight-") as temporary:
        test_home = Path(temporary)
        os.environ["HERMES_HOME"] = str(test_home)
        (test_home / "config.yaml").write_text(yaml.safe_dump(config, allow_unicode=True))
        (test_home / "sessions").mkdir()
        shutil.copy2(production / "sessions/sessions.json", test_home / "sessions/sessions.json")
        source = sqlite3.connect(f"file:{production}/state.db?mode=ro", uri=True)
        target = sqlite3.connect(test_home / "state.db")
        source.backup(target)
        source.close()
        target.close()
        first = migrate(test_home)
        second = migrate(test_home)
        assert all(r.get("already_revised") or r.get("new_on_first_message") for r in second)
        restored = migrate(test_home, rollback=True)
        print(json.dumps({"passed": True, "private_snapshot": True, "revisions": first,
                          "idempotent": True, "rollback_forks": len(restored)}), flush=True)
elif mode in {"revise", "rollback"}:
    os.environ["HERMES_HOME"] = str(production)
    print(json.dumps({"passed": True, "revisions": migrate(production, rollback=mode == "rollback")}), flush=True)
elif mode == "verify":
    os.environ["HERMES_HOME"] = str(production)
    from plugins.grantley import register
    from plugins.grantley.persona import load_persona_document
    from plugins.grantley.channel_binding import bindings_from_config
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
    source = Path("/opt/hermes/repo/plugins/grantley/assets/grantley.md").read_bytes()
    installed = (production / "plugins/grantley/assets/grantley.md").read_bytes()
    manifest = json.loads((stage / "manifest.json").read_text())
    assert source == installed and hashlib.sha256(source).hexdigest() == manifest["after_sha256"]
    doc = load_persona_document()
    doc.assert_cache_safe()
    assert len(doc.stable) <= 4000
    manager = PluginManager()
    manager._discovered = True
    context = PluginContext(PluginManifest(name="grantley", key="grantley", source="bundled"), manager)
    register(context)
    checks = []
    for binding in bindings_from_config(channels).values():
        parts = manager.render_system_prompt_sections({"platform": "onebot", "chat_id": binding.chat_id,
                                                       "chat_type": "group" if binding.is_group else "dm"})
        assert any(part.id == "grantley.identity" and part.content == doc.stable.strip()
                   for part in parts), "Identity was not registered on a bound channel"
        checks.append({"chat_id": binding.chat_id, "identity_registered": True})
    for context in [{"platform": "telegram", "chat_id": "90726001", "chat_type": "dm"},
                    {"platform": "onebot", "chat_id": "unbound", "chat_type": "dm"}]:
        assert not manager.render_system_prompt_sections(context)
    print(json.dumps({"passed": True, "sha256": manifest["after_sha256"],
                      "stable_characters": len(doc.stable), "bound_checks": checks,
                      "unbound_identity_absent": True, "no_qq_sent": True}), flush=True)
else:
    raise SystemExit("unknown maintenance mode")
