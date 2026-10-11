"""Branch into an explicitly requested persona revision, preserving old prefixes.

Run with the gateway stopped. Touch only configured Grantley group lanes and
Argo's DM. Copy active conversation content while leaving original rows intact.
"""
import copy
from datetime import datetime
from gateway.session import SessionStore

REVISION = 'grantley-argo-20260906'


def without_row_ids(messages):
    return [{key: value for key, value in message.items() if key != '_row_id'} for message in messages]


def revise(home, gateway_config, configured_channels):
    store = SessionStore(home / 'sessions', gateway_config)
    store._ensure_loaded()
    reports = []
    try:
        for chat, setting in configured_channels.items():
            if setting.get('persona') != 'grantley':
                continue
            group = bool(setting.get('group'))
            if not group and str(chat) != '2104743984':
                continue
            lane = f'group:g{chat}' if group else f'dm:{chat}'
            key = f'agent:main:onebot:{lane}'
            old = store._entries.get(key)
            if old is None:
                reports.append({'lane': lane, 'new_on_first_message': True})
                continue
            target = f'{REVISION}-{lane.replace(":", "-")}'
            if old.session_id == target:
                reports.append({'lane': lane, 'already_revised': True})
                continue
            assert not store._db.get_session(target), 'Unexpected partial previous revision'
            history = without_row_ids([message for message in store.load_transcript(old.session_id) if message.get('role') != 'system'])
            origin = old.origin
            assert origin and origin.platform.value == 'onebot'
            store._db.create_session(target, 'onebot', parent_session_id=old.session_id,
                                     model_config={'_branched_from': old.session_id, '_persona_revision': REVISION},
                                     session_key=key, chat_id=origin.chat_id, chat_type=origin.chat_type,
                                     user_id=origin.user_id)
            if history:
                assert store._db.append_messages_batch(target, copy.deepcopy(history)) == len(history)
            new = copy.deepcopy(old)
            new.session_id = target
            new.created_at = datetime.now()
            new.metadata = {**new.metadata, 'persona_revision': REVISION, 'previous_session': old.session_id}
            new.last_prompt_tokens = 0
            with store._lock:
                store._entries[key] = new
                store._save()
            restored = without_row_ids(store.load_transcript(target))
            assert restored == history, {
                'counts': [len(history), len(restored)],
                'different_fields': [[key for key in set(before) | set(after) if before.get(key) != after.get(key)]
                                     for before, after in zip(history, restored)],
            }
            assert store._db.get_session(target)['system_prompt'] is None
            reports.append({'lane': lane, 'old_session_id': old.session_id,
                            'new_session_id': target, 'preserved_messages': len(history)})
    finally:
        for db in store._db_handles.values():
            if db is not None:
                db.close()
    return reports
