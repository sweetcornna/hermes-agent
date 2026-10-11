"""Images an addressed QQ turn should carry besides its own segments.

A group rarely asks about a picture in the same message that carries it:
someone posts the photo, then a second line — often from someone else —
says "格兰你看这个".  The trigger message has no image, so the turn reached
the model as text only.  Two sources fill that gap, both delivered through
the turn's ordinary ``media_urls`` so the core's native image routing
handles them exactly like an attached photo:

* **Recent group images** — image posts the room made shortly before the
  trigger, remembered here per group and consumed once attached, so the
  next question does not re-send the same picture.
* **The quoted message** — when the trigger replies to an earlier message,
  that message is fetched with OneBot ``get_msg``; its images are attached
  and its text fills the core's ``reply_to_text`` context.
"""
from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Deque, Dict, Iterable, List, Optional, Sequence, Tuple

from . import protocol

logger = logging.getLogger(__name__)

_MAX_REMEMBERED_PER_GROUP = 20
_DEFAULT_LOOKBACK_LIMIT = 3
_QUOTE_TIMEOUT_SECONDS = 5.0


def lookback_seconds(extra: Optional[Dict[str, Any]]) -> float:
    """Opt-in window; invalid or non-positive values disable the lookback."""
    try:
        value = float((extra or {}).get("reply_image_lookback_seconds", 0))
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


def lookback_limit(extra: Optional[Dict[str, Any]]) -> int:
    try:
        value = int((extra or {}).get("reply_image_lookback_max", _DEFAULT_LOOKBACK_LIMIT))
    except (TypeError, ValueError):
        return _DEFAULT_LOOKBACK_LIMIT
    return max(0, value)


@dataclass
class RememberedImages:
    at: float
    qq_time: int
    message_id: str
    sender_name: str
    refs: List[protocol.MediaRef]


class RecentImages:
    """Per-group image posts, newest last, bounded and consumed on use."""

    def __init__(self) -> None:
        self._groups: Dict[Any, Deque[RememberedImages]] = {}

    def remember(self, group_id: Any, event: protocol.MessageEvent, sender_name: str) -> None:
        # Stickers are reactions, not pictures anyone will ask about; carrying
        # them into the next question is how a 表情包 of someone's face got
        # described as a photo that person had posted.
        refs = [
            ref
            for ref in protocol.segments_to_media(event.message)
            if ref.kind == "image" and not ref.sticker
        ]
        if not refs:
            return
        bucket = self._groups.setdefault(group_id, deque(maxlen=_MAX_REMEMBERED_PER_GROUP))
        bucket.append(
            RememberedImages(
                time.monotonic(), int(event.time or 0), str(event.message_id), sender_name, refs
            )
        )

    def take(
        self,
        group_id: Any,
        *,
        exclude_ids: Iterable[str],
        within_seconds: float,
        limit: int,
        trigger_time: int = 0,
    ) -> List[Tuple[str, protocol.MediaRef]]:
        """Return up to *limit* recent images, oldest first, and forget them.

        Age is measured on QQ's clock back from *trigger_time* when both sides
        carry a timestamp — building the turn happens after the reply window,
        so "now" would quietly shorten the lookback by that window.

        Entries belonging to the turn itself (*exclude_ids*) are dropped too:
        their images already ride in the turn's own segments, and leaving them
        behind would re-attach the same picture to the next question.
        """
        # Everything left is either attached now, part of this turn, or too
        # old to be what the question is about — none of it is kept.
        bucket = self._groups.pop(group_id, None)
        if not bucket or limit <= 0:
            return []
        excluded = {str(i) for i in exclude_ids}
        cutoff = time.monotonic() - within_seconds

        def too_old(entry: RememberedImages) -> bool:
            if trigger_time and entry.qq_time:
                return entry.qq_time < trigger_time - within_seconds
            return entry.at < cutoff

        chosen: List[RememberedImages] = []
        count = 0
        for entry in reversed(bucket):
            if count >= limit:
                break
            if entry.message_id in excluded or too_old(entry):
                continue
            chosen.append(entry)
            count += len(entry.refs)
        picked = [
            (entry.sender_name, ref) for entry in reversed(chosen) for ref in entry.refs
        ]
        return picked[-limit:]

    def clear(self) -> None:
        self._groups.clear()


@dataclass
class QuotedMessage:
    text: str
    sender_name: str
    user_id: str
    images: List[protocol.MediaRef]


async def fetch_quoted(
    call: Callable[..., Awaitable[Dict[str, Any]]], message_id: str
) -> Optional[QuotedMessage]:
    """Resolve a quoted message with ``get_msg``; ``None`` when unavailable."""
    try:
        resp = await call(
            protocol.RawAction(action="get_msg", params={"message_id": int(message_id)}),
            timeout=_QUOTE_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 — a missing quote must not cost the turn
        logger.info("OneBot: get_msg for quoted message %s failed (%s)", message_id, exc)
        return None
    data = resp.get("data") if isinstance(resp, dict) else None
    if not isinstance(data, dict):
        return None
    raw_segments = data.get("message")
    if isinstance(raw_segments, list):
        segments = protocol.parse_segments(raw_segments)
    else:
        segments = [protocol.TextSegment(text=str(data.get("raw_message") or ""))]
    sender = data.get("sender") if isinstance(data.get("sender"), dict) else {}
    name = str(sender.get("card") or sender.get("nickname") or sender.get("user_id") or "")
    return QuotedMessage(
        text=protocol.segments_to_text(segments).strip(),
        sender_name=name,
        user_id=str(sender.get("user_id") or data.get("user_id") or ""),
        images=[ref for ref in protocol.segments_to_media(segments) if ref.kind == "image"],
    )


@dataclass(frozen=True)
class ImageLabel:
    """Who an attached picture came from, and whether it is a sticker."""

    owner: str
    sticker: bool
    #: ``posted`` (in this turn), ``quoted`` (the replied-to message) or
    #: ``recent`` (the room lookback).
    origin: str = "posted"


def attachment_note(labels: Sequence[ImageLabel]) -> str:
    """One line mapping each attached picture to its sender and kind.

    Attachments reach the model as a bare, ordered list of images; without
    this it guesses who posted what, and a sticker of a face reads as that
    person's photo.  A lone photo the sender attached needs no note.
    """
    if not labels:
        return ""
    if len(labels) == 1 and labels[0].origin == "posted" and not labels[0].sticker:
        return ""
    parts = []
    for i, label in enumerate(labels, start=1):
        kind = "表情包" if label.sticker else "图片"
        who = label.owner or "群友"
        if label.origin == "quoted":
            desc = f"{who} 被引用的那条消息里的{kind}"
        elif label.origin == "recent":
            desc = f"{who} 刚才在群里发的{kind}"
        else:
            desc = f"{who} 发的{kind}"
        parts.append(f"第{i}张是{desc}")
    note = "[附图说明：" + "；".join(parts)
    if any(label.sticker for label in labels):
        note += "。表情包只是聊天贴图，不是发图人本人的照片或头像"
    return note + "]"
