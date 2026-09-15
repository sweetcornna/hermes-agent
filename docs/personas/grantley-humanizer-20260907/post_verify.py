"""Verify deployment against private database snapshots; never send messages."""
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

PRODUCTION = Path("/opt/hermes/data")
STAGE = PRODUCTION / ".deployments/grantley-humanizer-20260907"
sys.path.insert(0, "/opt/hermes/repo")
manifest = json.loads((STAGE / "manifest.json").read_text())
deployed = json.loads((STAGE / "deployed.json").read_text())
revisions = next(json.loads(line)["revisions"] for line in (STAGE / "revise-result.txt").read_text().splitlines()
                 if line.startswith('{"passed":'))
resources = [Path("/opt/hermes/repo/plugins/grantley/assets/grantley.md"), PRODUCTION / "plugins/grantley/assets/grantley.md"]
assert all(hashlib.sha256(path.read_bytes()).hexdigest() == manifest["after_sha256"] for path in resources)
assert hashlib.sha256((PRODUCTION / "config.yaml").read_bytes()).hexdigest() == manifest["config_sha256"]

with tempfile.TemporaryDirectory(prefix="grantley-post-deploy-") as temporary:
    snapshots = {}
    for label, source in [("before", STAGE / "backup"), ("after", PRODUCTION)]:
        home = Path(temporary) / label
        (home / "sessions").mkdir(parents=True)
        shutil.copy2(PRODUCTION / "config.yaml", home / "config.yaml")
        route_file = source / "sessions.json" if label == "before" else source / "sessions/sessions.json"
        shutil.copy2(route_file, home / "sessions/sessions.json")
        src = sqlite3.connect(f"file:{source}/state.db?mode=ro", uri=True)
        dst = sqlite3.connect(home / "state.db")
        src.backup(dst)
        src.close()
        dst.close()
        os.environ["HERMES_HOME"] = str(home)
        from gateway.config import load_gateway_config
        from gateway.session import SessionStore
        store = SessionStore(home / "sessions", load_gateway_config())
        store._ensure_loaded()
        entries = {key: entry.session_id for key, entry in store._entries.items()}
        ids = {r[key] for r in revisions for key in ["old_session_id", "new_session_id"] if key in r}
        sessions = {sid: store._db.get_session(sid) for sid in ids}
        transcripts = {sid: store.load_transcript(sid) for sid in ids if sessions[sid] is not None}
        snapshots[label] = {"entries": entries, "sessions": sessions, "transcripts": transcripts}
        for database in store._db_handles.values():
            if database is not None:
                database.close()

    os.environ["HERMES_HOME"] = str(PRODUCTION)
    from plugins.grantley.persona import load_persona_document
    stable = load_persona_document().stable
    before, after = snapshots["before"], snapshots["after"]
    checks = []
    bound_keys = set()
    for revision in revisions:
        if revision.get("new_on_first_message"):
            continue
        old, new = revision["old_session_id"], revision["new_session_id"]
        key = "agent:main:onebot:" + revision["lane"]
        bound_keys.add(key)
        assert before["entries"][key] == old and after["entries"][key] == new
        assert before["sessions"][old] == after["sessions"][old]
        assert before["transcripts"][old] == after["transcripts"][old]
        normalize = lambda rows: [{k:v for k,v in row.items() if k != "_row_id"}
                                  for row in rows if row.get("role") != "system"]
        source = normalize(before["transcripts"][old])
        target = normalize(after["transcripts"][new])
        assert target[:len(source)] == source
        prompt = after["sessions"][new].get("system_prompt")
        assert prompt is None or stable in prompt
        checks.append({"lane": revision["lane"], "new_session_id": new,
                       "preserved_messages": len(source), "new_messages_since_deploy": len(target) - len(source),
                       "old_session_and_prefix_unchanged": True,
                       "prompt": "new_identity_frozen" if prompt else "loads_new_identity_on_first_turn"})
    for key, sid in before["entries"].items():
        if key not in bound_keys:
            assert after["entries"].get(key) == sid, "Unbound route changed"

state = json.loads((PRODUCTION / "gateway_state.json").read_text())
assert state["gateway_state"] == "running"
qq_state = state["platforms"]["onebot"]["state"]
result = {"passed": qq_state == "connected", "deployment_verified": True,
          "operational_ready": qq_state == "connected", "remaining_action": "manual_qq_login" if qq_state == "auth_required" else None,
          "deployed_at": deployed["started_at"], "persona_sha256": manifest["after_sha256"],
          "stable_characters": len(stable), "resource_copies_match": True, "config_unchanged": True,
          "qq_runtime_unchanged": deployed["qq_runtime_unchanged"], "gateway": state["gateway_state"],
          "platforms": {key: value["state"] for key, value in state["platforms"].items()},
          "sessions": checks, "unbound_routes_unchanged": True, "no_test_messages_sent": True}
print(json.dumps(result, ensure_ascii=False), flush=True)
