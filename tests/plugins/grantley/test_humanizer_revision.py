"""Real SQLite migration, re-entry, rollback and channel isolation contracts."""
import importlib.util
from datetime import datetime
from pathlib import Path

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionEntry, SessionSource, SessionStore


def _migration():
    path = Path(__file__).resolve().parents[3] / "docs/personas/grantley-humanizer-20260907/revise_sessions.py"
    spec = importlib.util.spec_from_file_location("grantley_humanizer_revision", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_and_rollback_preserve_new_messages_and_old_prefixes(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    migration = _migration()
    cfg = GatewayConfig(group_sessions_per_user=False)
    store = SessionStore(tmp_path / "sessions", cfg)
    store._ensure_loaded()
    now = datetime.now()
    originals = {}
    for sid, chat, kind in [("old-group", "g100", "group"), ("old-dm", "17", "dm"), ("unbound", "18", "dm")]:
        key = f"agent:main:onebot:{kind}:{chat}"
        origin = SessionSource(platform=Platform("onebot"), chat_id=chat, chat_type=kind, user_id="17")
        store._db.create_session(sid, "onebot", system_prompt="FROZEN-" + sid,
                                 session_key=key, chat_id=chat, chat_type=kind)
        store._db.append_messages_batch(sid, [
            {"role": "user", "content": "remember me", "api_content": "exact sent bytes"},
            {"role": "assistant", "content": "remembered"},
        ])
        store._entries[key] = SessionEntry(key, sid, now, now, origin=origin)
    with store._lock:
        store._save()
    originals = {sid: (store._db.get_session(sid), store.load_transcript(sid))
                 for sid in ["old-group", "old-dm", "unbound"]}
    channels = {"100": {"persona": "grantley", "group": True},
                "17": {"persona": "grantley", "group": False},
                "18": {"persona": "other", "group": False}}
    result = migration.revise(tmp_path, cfg, channels)
    assert len(result) == 2
    assert all(row["already_revised"] for row in migration.revise(tmp_path, cfg, channels))
    new_dm = next(row["new_session_id"] for row in result if row["lane"] == "dm:17")
    store._db.append_messages_batch(new_dm, [
        {"role": "user", "content": "arrived after deploy"},
        {"role": "assistant", "content": "new response"},
    ])
    rollback = migration.revise(tmp_path, cfg, channels, rollback=True)
    recovered = next(row["new_session_id"] for row in rollback if row["lane"] == "dm:17")
    assert migration.without_row_ids(store.load_transcript(recovered)) == migration.without_row_ids(store.load_transcript(new_dm))
    assert store.load_transcript(recovered)[-2]["content"] == "arrived after deploy"
    assert store._db.get_session(recovered)["system_prompt"] is None
    assert all(row["already_revised"] for row in migration.revise(tmp_path, cfg, channels, rollback=True))
    for sid, (row, history) in originals.items():
        assert store._db.get_session(sid) == row
        assert store.load_transcript(sid) == history
    reopened = SessionStore(tmp_path / "sessions", cfg)
    reopened._ensure_loaded()
    assert reopened._entries["agent:main:onebot:dm:18"].session_id == "unbound"
    assert reopened._entries["agent:main:onebot:dm:17"].session_id == recovered
    for item in [store, reopened]:
        for db in item._db_handles.values():
            if db is not None:
                db.close()
