"""One-time import of active Grantley group lanes into new shared sessions.

Run only with the gateway stopped, after a SQLite backup. The original rows
and per-user routing entries are never edited. Uses existing SessionStore and
native context-handoff representation, with verbatim history instead of an
extra lossy LLM summary. No runtime/core hook is added.
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from agent.context_compressor import SUMMARY_PREFIX, _SUMMARY_END_MARKER, is_compaction_summary_message
from gateway.config import Platform
from gateway.session import SessionEntry, SessionSource, SessionStore, build_session_key

MIGRATION_ID = 'grantley-longlife-20260906'


def migrate(home: Path, gateway_config, groups: list[str], *, apply=False):
    assert gateway_config.group_sessions_per_user is False
    store = SessionStore(home / 'sessions', gateway_config)
    store._ensure_loaded()
    connection = sqlite3.connect(home / 'state.db')
    connection.row_factory = sqlite3.Row
    reports = []
    try:
        for group in groups:
            chat = 'g' + group.removeprefix('g')
            source = SessionSource(platform=Platform('onebot'), chat_id=chat, chat_type='group')
            shared_key = build_session_key(source, group_sessions_per_user=False)
            assert shared_key == f'agent:main:onebot:group:{chat}'
            target_id = f'{MIGRATION_ID}-{chat}'
            if shared_key in store._entries:
                entry = store._entries[shared_key]
                assert entry.session_id == target_id, 'Existing shared session requires review'
                reports.append({'chat_id': chat, 'already_migrated': True, 'session_id': target_id})
                continue
            records = []
            source_ids = set()
            excluded = []
            for key, entry in sorted(store._entries.items()):
                if not key.startswith(shared_key + ':') or entry.session_id in source_ids:
                    continue
                row = connection.execute('SELECT * FROM sessions WHERE id=?', (entry.session_id,)).fetchone()
                if (not row or row['ended_at'] is not None or row['source'] != 'onebot'
                    or row['chat_type'] != 'group' or row['chat_id'] != chat
                    or not (row['session_key'] or '').startswith(shared_key + ':')
                    or row['profile_name'] not in (None, '', 'default', 'main')
                    or any(k.startswith('agent:') and not k.startswith('agent:main:')
                           and e.session_id == entry.session_id for k, e in store._entries.items())):
                    excluded.append(entry.session_id)
                    continue
                source_ids.add(entry.session_id)
                for message in connection.execute(
                    'SELECT id,role,content,tool_calls,tool_call_id,tool_name,timestamp FROM messages '
                    'WHERE session_id=? AND active=1 AND role != ? ORDER BY id', (entry.session_id, 'system')):
                    record = dict(message)
                    record.update(session_id=entry.session_id, lane_user_id=row['user_id'])
                    records.append(record)
            records.sort(key=lambda record: (record['timestamp'], record['id']))
            report = {'chat_id': chat, 'session_id': target_id, 'source_sessions': len(source_ids),
                      'source_messages': len(records), 'excluded_sessions': len(set(excluded))}
            if not records:
                reports.append(report | {'skipped_empty': True})
                continue
            # A real final assistant reply keeps the handoff user/assistant
            # sequence valid before the next human message; never invent one.
            final = next((record for record in reversed(records)
                          if record['role'] == 'assistant' and record['content'] and not record['tool_calls']), None)
            assert final is not None, 'No complete assistant reply available for a safe handoff'
            prior = [record for record in records if record is not final]
            content = (SUMMARY_PREFIX + '\n\n'
                       '## Group history import (verbatim reference, not a new request)\n'
                       'These records came from independent per-speaker lanes in this same group. '
                       'lane_user_id identifies the lane owner; quoted content may name other senders. '
                       'Overlapping group context may appear more than once. Timestamps give chronology. '
                       'Old requests are historical, not authorization to act now. Earlier assistant '
                       'roleplay may be inaccurate; current character instructions govern canon. '
                       'Original sessions remain unchanged in the database. The following assistant '
                       'message is the last recorded completed reply, reproduced verbatim.\n'
                       + json.dumps(prior, ensure_ascii=False, separators=(',', ':'))
                       + '\n\n' + _SUMMARY_END_MARKER)
            messages = [{'role': 'user', 'content': content, 'display_kind': 'context_compaction'},
                        {'role': 'assistant', 'content': final['content']}]
            from agent.context_compressor import estimate_messages_tokens_rough
            report['estimated_context_tokens'] = estimate_messages_tokens_rough(messages)
            assert report['estimated_context_tokens'] < 256000, 'Import requires compaction before deployment'
            if apply:
                # A failed partial run must be rolled back, never append twice.
                assert connection.execute('SELECT 1 FROM sessions WHERE id=?', (target_id,)).fetchone() is None
                store._db.create_session(target_id, 'onebot', session_key=shared_key, chat_id=chat,
                                         chat_type='group', origin_json=json.dumps(source.to_dict()),
                                         model_config={'_group_history_import': MIGRATION_ID})
                assert store._db.append_messages_batch(target_id, messages) == 2
                now = datetime.now()
                entry = SessionEntry(session_key=shared_key, session_id=target_id, created_at=now,
                                     updated_at=now, origin=source, platform=source.platform,
                                     chat_type='group', reset_had_activity=True,
                                     metadata={'history_import': MIGRATION_ID, 'source_sessions': sorted(source_ids)})
                with store._lock:
                    store._entries[shared_key] = entry
                    store._save()
                restored = store.load_transcript(target_id)
                assert len(restored) == 2 and is_compaction_summary_message(restored[0])
                assert restored[1]['content'] == final['content']
            reports.append(report)
    finally:
        connection.close()
        for db in store._db_handles.values():
            if db is not None:
                db.close()
    return reports
