"""Branch reachable default-profile routes before enabling a new tool schema."""
import copy
import hashlib
from datetime import datetime

from gateway.session import SessionStore

REVISION = 'official-search-20260907'


def messages(rows):
    return [{k:v for k,v in row.items() if k != '_row_id'} for row in rows if row.get('role') != 'system']


def revise(home, config, *, rollback=False):
    store = SessionStore(home / 'sessions', config)
    store._ensure_loaded()
    results = []
    prefix = REVISION + ('-rollback' if rollback else '')
    try:
        for key, current in list(store._entries.items()):
            origin = current.origin
            if not key.startswith('agent:main:') or origin is None:
                continue
            # Per-person OneBot group lanes were retired in the earlier shared
            # conversation migration; keep those historical routes untouched.
            if origin.platform.value == 'onebot' and origin.chat_type == 'group' and key != f'agent:main:onebot:group:{origin.chat_id}':
                continue
            if rollback and not current.session_id.startswith(REVISION + '-'):
                continue
            target = prefix + '-' + hashlib.sha256(key.encode()).hexdigest()[:16]
            if current.session_id == target:
                results.append({'key':key, 'new_session_id':target, 'already_revised':True})
                continue
            old = current.session_id
            before = store._db.get_session(old)
            history = store.load_transcript(old)
            effective = messages(history)
            existing = store._db.get_session(target)
            if existing:
                assert existing.get('parent_session_id') == old and messages(store.load_transcript(target)) == effective
            else:
                store._db.create_session(target, origin.platform.value, parent_session_id=old,
                                         model_config={'_branched_from':old, '_tool_revision':prefix},
                                         session_key=key, chat_id=origin.chat_id, chat_type=origin.chat_type,
                                         user_id=origin.user_id)
                if effective:
                    assert store._db.append_messages_batch(target, copy.deepcopy(effective)) == len(effective)
            assert store._db.get_session(target)['system_prompt'] is None
            entry = copy.deepcopy(current)
            entry.session_id = target
            entry.created_at = datetime.now()
            entry.last_prompt_tokens = 0
            entry.active_turn_token = None
            entry.active_turn_started_at = None
            if effective and effective[-1].get('role') == 'assistant' and not effective[-1].get('tool_calls'):
                entry.resume_pending = False
                entry.resume_reason = None
            entry.metadata = {**entry.metadata, 'tool_revision':prefix, 'previous_session':old}
            with store._lock:
                store._entries[key] = entry
                store._save()
            assert messages(store.load_transcript(target)) == effective
            assert store._db.get_session(old) == before and store.load_transcript(old) == history
            results.append({'key':key, 'old_session_id':old, 'new_session_id':target, 'preserved_messages':len(effective)})
        return results
    finally:
        for db in store._db_handles.values():
            if db is not None:
                db.close()
