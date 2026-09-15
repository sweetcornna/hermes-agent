"""Stopped-gateway persona revision; adapt the existing conversation migration.

Use existing SessionStore/SessionDB. Old prefixes and rows stay immutable.
Rollback forks the latest effective history again, adopting restored assets;
it never restores an old database over messages received after deployment.
"""
import copy
from datetime import datetime

from gateway.session import SessionStore
from plugins.grantley.channel_binding import bindings_from_config

REVISION = "grantley-humanizer-20260907"


def without_row_ids(messages):
    return [{key: value for key, value in row.items() if key != "_row_id"}
            for row in messages if row.get("role") != "system"]


def revise(home, gateway_config, channels, *, rollback=False):
    store = SessionStore(home / "sessions", gateway_config)
    store._ensure_loaded()
    records = []
    prefix = REVISION + ("-rollback" if rollback else "")
    try:
        for binding in bindings_from_config(channels).values():
            lane = f"group:g{binding.chat_id}" if binding.is_group else f"dm:{binding.chat_id}"
            key = f"agent:main:onebot:{lane}"
            current = store._entries.get(key)
            if current is None:
                records.append({"lane": lane, "new_on_first_message": True})
                continue
            target = f"{prefix}-{lane.replace(':', '-')}"
            if current.session_id == target:
                records.append({"lane": lane, "new_session_id": target, "already_revised": True})
                continue
            if rollback and not current.session_id.startswith(REVISION + "-"):
                continue
            source_id = current.session_id
            before_session = store._db.get_session(source_id)
            before_history = store.load_transcript(source_id)
            history = without_row_ids(before_history)
            origin = current.origin
            if not origin or origin.platform.value != "onebot":
                raise RuntimeError("Bound lane has unexpected origin")
            existing = store._db.get_session(target)
            if existing:
                # Resume a partially staged target only while it has no new turns.
                if existing.get("parent_session_id") != source_id or without_row_ids(store.load_transcript(target)) != history:
                    raise RuntimeError("Partial revision differs from its source; no history overwritten")
            else:
                store._db.create_session(
                    target, "onebot", parent_session_id=source_id,
                    model_config={"_branched_from": source_id, "_persona_revision": prefix},
                    session_key=key, chat_id=origin.chat_id,
                    chat_type=origin.chat_type, user_id=origin.user_id,
                )
                if history and store._db.append_messages_batch(target, copy.deepcopy(history)) != len(history):
                    raise RuntimeError("Incomplete history transfer")
            if store._db.get_session(target)["system_prompt"] is not None:
                raise RuntimeError("New revision already has a frozen identity")
            next_entry = copy.deepcopy(current)
            next_entry.session_id = target
            next_entry.created_at = datetime.now()
            next_entry.last_prompt_tokens = 0
            next_entry.metadata = {**next_entry.metadata, "persona_revision": prefix,
                                   "previous_session": source_id}
            with store._lock:
                store._entries[key] = next_entry
                store._save()
            if without_row_ids(store.load_transcript(target)) != history:
                raise RuntimeError("Restored history differs")
            if store._db.get_session(source_id) != before_session or store.load_transcript(source_id) != before_history:
                raise RuntimeError("Original session was modified")
            records.append({"lane": lane, "old_session_id": source_id,
                            "new_session_id": target, "preserved_messages": len(history)})
    finally:
        for database in store._db_handles.values():
            if database is not None:
                database.close()
    return records
