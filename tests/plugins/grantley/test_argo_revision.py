"""Explicit persona revisions preserve old prefixes and carry history forward."""
import importlib.util
from datetime import datetime
from pathlib import Path

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionEntry, SessionSource, SessionStore


def test_persona_revision_branches_only_bound_groups_and_argos_dm(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    path = Path(__file__).resolve().parents[3] / 'docs/migration-corlinman/search-repair-20260906/revise_sessions.py'
    spec = importlib.util.spec_from_file_location('argo_revision', path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    cfg = GatewayConfig(group_sessions_per_user=False)
    store = SessionStore(tmp_path / 'sessions', cfg)
    store._ensure_loaded()
    now = datetime.now()
    for sid, chat, chat_type in [('group-old', 'g100', 'group'), ('argo-old', '2104743984', 'dm'), ('other-old', '17', 'dm')]:
        key = f'agent:main:onebot:{chat_type}:{chat}'
        source = SessionSource(platform=Platform('onebot'), chat_id=chat, chat_type=chat_type, user_id='2104743984')
        store._db.create_session(sid, 'onebot', system_prompt='FROZEN-' + sid,
                                 session_key=key, chat_id=chat, chat_type=chat_type)
        store._db.append_messages_batch(sid, [
            {'role': 'user', 'content': sid + ' remembered detail', 'api_content': sid + ' sent bytes'},
            {'role': 'assistant', 'content': sid + ' reply'},
        ])
        store._entries[key] = SessionEntry(key, sid, now, now, origin=source)
    with store._lock:
        store._save()
    old_rows = {sid: store._db.get_session(sid) for sid in ['group-old', 'argo-old', 'other-old']}
    old_histories = {sid: store.load_transcript(sid) for sid in old_rows}
    channels = {'100': {'persona': 'grantley', 'group': True},
                '2104743984': {'persona': 'grantley', 'group': False},
                '17': {'persona': 'grantley', 'group': False}}
    result = revision.revise(tmp_path, cfg, channels)
    assert len(result) == 2
    resumed = SessionStore(tmp_path / 'sessions', cfg)
    resumed._ensure_loaded()
    for record in result:
        new_id = record['new_session_id']
        assert revision.without_row_ids(resumed.load_transcript(new_id)) == revision.without_row_ids(old_histories[record['old_session_id']])
        assert resumed._db.get_session(new_id)['system_prompt'] is None
    for sid, before in old_rows.items():
        assert resumed._db.get_session(sid) == before
        assert resumed.load_transcript(sid) == old_histories[sid]
    assert resumed._entries['agent:main:onebot:dm:17'].session_id == 'other-old'
    assert all(record['already_revised'] for record in revision.revise(tmp_path, cfg, channels))
    for current in [store, resumed]:
        for db in current._db_handles.values():
            if db is not None:
                db.close()
