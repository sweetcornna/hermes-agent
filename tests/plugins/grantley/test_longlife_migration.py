"""Exercise the deployment handoff against real SQLite and gateway routing."""
import copy
import importlib.util
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionStore, SessionEntry, SessionSource, build_session_key
from agent.context_compressor import is_compaction_summary_message

DIRECTORY = Path(__file__).resolve().parents[3] / 'docs/personas/grantley-longlife'


def load(name):
    spec = importlib.util.spec_from_file_location(name, DIRECTORY / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_config_scopes_vision_and_keeps_other_settings():
    configure = load('configure')
    original = {'model': {'default': 'gemini-3.8-flash-high'},
                'providers': {'cornna': {'models': ['gemini-3.8-flash-high', 'other'], 'api_key': 'local-fixture'}},
                'plugins': {'entries': {'grantley': {'settings': {'channels': {'a': 'unchanged'}}}}},
                'session_reset': {'idle_minutes': 99}, 'unrelated': {'x': [1, 2]}}
    before = copy.deepcopy(original)
    result = configure.desired_config(original)
    assert original == before
    assert result['providers']['cornna']['models']['other'] == {}
    assert result['providers']['cornna']['api_key'] == 'local-fixture'
    assert result['unrelated'] == original['unrelated']
    assert result['session_reset'] == {'mode': 'none', 'idle_minutes': 99}
    assert configure.desired_config(result) == result


def test_shared_import_preserves_sources_resets_dms_and_other_personas(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    store._ensure_loaded()
    db = store._db
    now = datetime.now()
    def seed(sid, chat, user, *, reset=False, persona='main', chat_type='group'):
        source = SessionSource(platform=Platform('onebot'), chat_id=chat, user_id=user, chat_type=chat_type)
        key = build_session_key(source).replace('agent:main:', f'agent:{persona}:')
        db.create_session(sid, 'onebot', session_key=key, chat_id=chat, chat_type=chat_type, user_id=user)
        db.append_messages_batch(sid, [{'role': 'user', 'content': f'{sid}-question'},
                                      {'role': 'assistant', 'content': f'{sid}-answer'}])
        if reset:
            db.end_session(sid, 'session_reset')
        store._entries[key] = SessionEntry(key, sid, now, now, origin=source)
        return key
    seed('alice', 'g100', 'alice')
    seed('bob', 'g100', 'bob')
    seed('reset', 'g100', 'reset', reset=True)
    seed('foreign', 'g100', 'foreign', persona='xiaohuihui')
    seed('elsewhere', 'g200', 'alice')
    seed('private', 'alice', 'alice', chat_type='dm')
    with store._lock:
        store._save()
    old_keys = dict(store._entries)
    with sqlite3.connect(tmp_path / 'state.db') as connection:
        before_sessions = connection.execute('SELECT * FROM sessions ORDER BY id').fetchall()
        before_messages = connection.execute('SELECT * FROM messages ORDER BY id').fetchall()
    shared_config = GatewayConfig(group_sessions_per_user=False)
    result = load('migrate_groups').migrate(tmp_path, shared_config, ['100'], apply=True)
    assert result[0]['source_sessions'] == 2 and result[0]['source_messages'] == 4
    new = SessionStore(tmp_path / 'sessions', shared_config)
    new._ensure_loaded()
    key = 'agent:main:onebot:group:g100'
    sid = new._entries[key].session_id
    messages = new.load_transcript(sid)
    assert [m['role'] for m in messages] == ['user', 'assistant']
    assert is_compaction_summary_message(messages[0])
    text = json.dumps(messages)
    assert all(f'{s}-{part}' in text for s in ('alice', 'bob') for part in ('question', 'answer'))
    assert all(f'{s}-question' not in text for s in ('private', 'elsewhere', 'foreign', 'reset'))
    for old_key, entry in old_keys.items():
        if entry.session_id == 'reset':
            # The ordinary gateway loader prunes stale reset routing entries.
            assert old_key not in new._entries
            continue
        assert new._entries[old_key].session_id == entry.session_id
    with sqlite3.connect(tmp_path / 'state.db') as connection:
        after_sessions = connection.execute('SELECT * FROM sessions WHERE id != ? ORDER BY id', (sid,)).fetchall()
        after_messages = connection.execute('SELECT * FROM messages WHERE session_id != ? ORDER BY id', (sid,)).fetchall()
    assert before_sessions == after_sessions and before_messages == after_messages
    # Real gateway lookup survives reopening and uses one lane for both users.
    for user in ('alice', 'bob'):
        source = SessionSource(platform=Platform('onebot'), chat_id='g100', chat_type='group', user_id=user)
        assert new.get_or_create_session(source).session_id == sid
    again = load('migrate_groups').migrate(tmp_path, shared_config, ['100'], apply=True)
    assert again[0]['already_migrated']
    for current in (store, new):
        for handle in current._db_handles.values():
            if handle:
                handle.close()
