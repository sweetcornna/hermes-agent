"""Wait for a sender to finish typing before handing a QQ turn to Hermes.

Unlike the core's busy-text buffer this also covers idle sessions, media and
unmentioned continuations of an addressed group message. Session locks keep
different speakers out of the core's single pending-message slot.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field, replace

from gateway.platforms.base import MessageType
from gateway.session import build_session_key

from . import protocol

logger = logging.getLogger(__name__)


def quiet_seconds(extra):
    """Opt-in YAML setting; invalid values retain the legacy immediate path."""
    try:
        value = float((extra or {}).get("reply_debounce_seconds", 0))
        return value if math.isfinite(value) and value > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def sender_key(event):
    return (event.self_id, event.message_type.value, event.group_id, event.user_id)


@dataclass
class Burst:
    items: list = field(default_factory=list)
    deadline: float = 0.0
    task: asyncio.Task | None = None


class ReplyBatcher:
    def __init__(self, adapter):
        self.adapter = adapter
        self.pending = {}
        self.tasks = set()
        self.locks = {}
        self.lock_users = {}

    def contains(self, event):
        return sender_key(event) in self.pending

    def enqueue(self, event, text, room_before, seconds):
        key = sender_key(event)
        burst = self.pending.get(key)
        if burst is None:
            burst = self.pending[key] = Burst()
            task = asyncio.create_task(self._run(key, burst), name="onebot-reply-burst")
            burst.task = task
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        burst.items.append((event, text, room_before))
        burst.deadline = time.monotonic() + seconds

    async def _quiet(self, burst):
        # No cancellation on arrival: a download or a pending handler must
        # never be killed by the next bubble. Only the deadline moves.
        while (delay := burst.deadline - time.monotonic()) > 0:
            await asyncio.sleep(delay)

    def _session_key(self, event):
        store = getattr(self.adapter, "_session_store", None)
        return build_session_key(
            event.source,
            group_sessions_per_user=self.adapter.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.adapter.config.extra.get("thread_sessions_per_user", False),
            profile=store._resolve_profile_for_key(event.source) if store else None,
        )

    async def _wait_active(self, key, event):
        while task := self.adapter._session_tasks.get(key):
            if task.done():
                break
            if event.allow_gateway_control:
                from tools.clarify_gateway import get_pending_for_session
                from tools.approval import has_blocking_approval
                if get_pending_for_session(key, include_choice_prompts=True) or has_blocking_approval(key):
                    # The core owns authorization and interpretation. Let it
                    # consume an answer that unblocks the active agent.
                    break
            # Poll the pending-input state as well as task completion: a
            # question can appear after this follow-up has begun waiting.
            # asyncio.wait never cancels the core task on timeout/shutdown.
            await asyncio.wait([task], timeout=0.1)

    async def _prepare(self, burst):
        items, burst.items = burst.items, []
        first, _, room = items[0]
        last = items[-1][0]
        segments = []
        for event, text, _ in items:
            if segments:
                segments.append(protocol.TextSegment(text="\n"))
            segments.extend(event.message or [protocol.TextSegment(text=text)])
        combined = replace(
            first, message=segments, message_id=last.message_id, time=last.time,
            raw_message="\n".join(text for _, text, _ in items),
        )
        event = await self.adapter._build_message_event(combined, combined.raw_message, room_before=room)
        event.metadata["onebot_burst_message_ids"] = [str(item[0].message_id) for item in items]
        return event

    @staticmethod
    def _merge(first, latest):
        first.text = "\n".join(text for text in (first.text, latest.text) if text)
        first.media_urls.extend(latest.media_urls)
        first.media_types.extend(latest.media_types)
        if first.message_type == MessageType.TEXT:
            first.message_type = latest.message_type
        first.message_id = latest.message_id
        first.source.message_id = latest.source.message_id
        first.timestamp = latest.timestamp
        ids = first.metadata["onebot_burst_message_ids"] + latest.metadata["onebot_burst_message_ids"]
        first.metadata.update(latest.metadata)
        first.metadata["onebot_burst_message_ids"] = ids
        return first

    async def _run(self, key, burst):
        session_key = None
        try:
            await self._quiet(burst)
            event = await self._prepare(burst)
            session_key = self._session_key(event)
            lock = self.locks.setdefault(session_key, asyncio.Lock())
            self.lock_users[session_key] = self.lock_users.get(session_key, 0) + 1
            async with lock:
                await self._wait_active(session_key, event)
                while burst.items or time.monotonic() < burst.deadline:
                    await self._quiet(burst)
                    if burst.items:
                        event = self._merge(event, await self._prepare(burst))
                # Detach only when the complete turn is ready, after both
                # media downloads and any preceding speaker's response.
                self.pending.pop(key, None)
                await self.adapter.handle_message(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("OneBot: reply burst failed")
        finally:
            if self.pending.get(key) is burst:
                self.pending.pop(key, None)
            if session_key is not None:
                self.lock_users[session_key] -= 1
                if not self.lock_users[session_key]:
                    self.lock_users.pop(session_key)
                    self.locks.pop(session_key)

    def discard_conversation(self, event):
        """Control commands discard unsent input, like core /stop and /new."""
        key = sender_key(event)
        for candidate, burst in list(self.pending.items()):
            if candidate[:3] == key[:3] and (
                event.message_type == protocol.MessageType.GROUP or candidate == key
            ):
                burst.task.cancel()
                self.pending.pop(candidate, None)

    async def close(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.pending.clear()
