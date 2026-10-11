"""OneBot v11 wire protocol — events, message segments, and outbound actions.

Pure data + pure functions.  No I/O, no third-party imports, no Hermes
imports: everything here is stdlib-only so the parser can be unit-tested
in isolation and reused by the outbound tool layer
(``tools/onebot_client.py``) without dragging the gateway in.

The vocabulary follows the OneBot v11 specification
(<https://github.com/botuniverse/onebot-11>) as implemented by NapCat /
NapCat / Lagrange / go-cqhttp.

Design rules that are load-bearing (do not "simplify" them away):

* **Nothing raises on a malformed frame.**  ``_coerce_int`` returns a
  default instead of propagating ``ValueError``; unknown ``post_type``
  values collapse to :class:`UnknownEvent`; unknown segment types collapse
  to :class:`OtherSegment` carrying the raw JSON.  One bad field must never
  unwind the reader loop and drop the WebSocket (that is a reconnect storm
  plus message loss).
* **Segment round-trips keep both key dialects** where the backends
  disagree — see :func:`_forward_node_to_wire`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Union

__all__ = [
    "AtSegment",
    "FaceSegment",
    "FileSegment",
    "ForwardNode",
    "ForwardSegment",
    "GetAiCharacters",
    "GetAiRecord",
    "GetChatInfo",
    "GetRecord",
    "ImageSegment",
    "image_stickers",
    "MediaRef",
    "MessageEvent",
    "MessageType",
    "MetaEvent",
    "NoticeEvent",
    "OtherSegment",
    "RawAction",
    "RecordSegment",
    "ReplySegment",
    "RequestEvent",
    "SendGroupAiRecord",
    "SendGroupForwardMsg",
    "SendGroupMsg",
    "SendPoke",
    "SendPrivateForwardMsg",
    "SendPrivateMsg",
    "Sender",
    "SetInputStatus",
    "TextSegment",
    "UnknownEvent",
    "UploadGroupFile",
    "UploadPrivateFile",
    "VideoSegment",
    "action_to_wire",
    "is_mentioned",
    "parse_event",
    "humanize_cq",
    "parse_segments",
    "segments_to_media",
    "segments_to_text",
    "strip_self_mention",
]


# ===========================================================================
# Events
# ===========================================================================




class MessageType(str, Enum):
    """OneBot ``message_type`` — ``private`` (DM) vs ``group``."""

    PRIVATE = "private"
    GROUP = "group"


@dataclass
class Sender:
    """Inner ``sender`` object of a :class:`MessageEvent`.

    ``card`` is the per-group display name (群名片) and takes precedence
    over ``nickname`` when rendering "who said this".
    """

    user_id: Optional[int] = None
    nickname: Optional[str] = None
    card: Optional[str] = None
    role: Optional[str] = None

    def display_name(self) -> str:
        """Best-effort human name: group card → nickname → bare uin."""
        for candidate in (self.card, self.nickname):
            if candidate and str(candidate).strip():
                return str(candidate).strip()
        return str(self.user_id) if self.user_id else ""


@dataclass
class MessageEvent:
    """OneBot ``post_type = "message"`` event."""

    self_id: int
    message_type: MessageType
    user_id: int
    message_id: int
    message: List["MessageSegment"]
    time: int
    sub_type: Optional[str] = None
    group_id: Optional[int] = None
    raw_message: str = ""
    sender: Optional[Sender] = None


@dataclass
class NoticeEvent:
    """OneBot ``post_type = "notice"`` event — parsed, currently unused."""

    self_id: int
    notice_type: str
    time: int
    group_id: Optional[int] = None
    user_id: Optional[int] = None


@dataclass
class MetaEvent:
    """OneBot ``post_type = "meta_event"`` (heartbeat / lifecycle)."""

    self_id: int
    meta_event_type: str
    time: int


@dataclass
class RequestEvent:
    """OneBot ``post_type = "request"`` (friend / group-join request)."""

    self_id: int
    request_type: str
    time: int
    user_id: Optional[int] = None
    group_id: Optional[int] = None
    flag: Optional[str] = None


@dataclass
class UnknownEvent:
    """Fallback for ``post_type`` values we do not model.

    Carries the raw JSON so callers can log the unexpected shape instead
    of tearing down the connection.
    """

    raw: Dict[str, Any]


Event = Union[MessageEvent, NoticeEvent, MetaEvent, RequestEvent, UnknownEvent]


# ===========================================================================
# Message segments
# ===========================================================================


@dataclass
class TextSegment:
    """``{"type": "text", "data": {"text": ...}}``."""

    text: str


@dataclass
class AtSegment:
    """``{"type": "at", "data": {"qq": ...}}``.  ``qq == "all"`` for @全体."""

    qq: str


@dataclass
class ImageSegment:
    """``{"type": "image", "data": {"url": ..., "file": ...}}``.

    Inbound only: ``sub_type`` 1 (summary ``[动画表情]``) marks a sticker —
    a 表情包, frequently a cropped face — rather than a photo somebody took.
    """

    url: str = ""
    file: Optional[str] = None
    sub_type: int = 0
    summary: str = ""

    @property
    def is_sticker(self) -> bool:
        return self.sub_type == 1 or "表情" in (self.summary or "")


@dataclass
class ReplySegment:
    """``{"type": "reply", "data": {"id": ...}}`` — quote of another message."""

    id: str


@dataclass
class FaceSegment:
    """``{"type": "face", "data": {"id": ...}}`` — a built-in QQ emoticon."""

    id: str


@dataclass
class RecordSegment:
    """``{"type": "record", ...}`` — a voice message (SILK/AMR container)."""

    url: str = ""
    file: Optional[str] = None


@dataclass
class VideoSegment:
    """``{"type": "video", ...}`` — a short video from the backend."""

    url: str = ""
    file: Optional[str] = None


@dataclass
class FileSegment:
    """``{"type": "file", ...}`` — a OneBot v11 extension (NapCat / SnowLuma).

    Older clients ship the document as a name-only segment with no
    ``url``; :func:`segments_to_media` skips those the same way it skips
    url-less images.
    """

    url: str = ""
    file: Optional[str] = None


@dataclass
class ForwardSegment:
    """``{"type": "forward", "data": {"id": ...}}`` — a merged-forward card."""

    id: str


@dataclass
class OtherSegment:
    """Wrapper for segment types we do not model (poke, dice, xml, json…).

    Keeps the raw JSON so the reader loop survives protocol drift and the
    outbound serializer can echo it back verbatim.
    """

    raw: Dict[str, Any]


MessageSegment = Union[
    TextSegment,
    AtSegment,
    ImageSegment,
    ReplySegment,
    FaceSegment,
    RecordSegment,
    VideoSegment,
    FileSegment,
    ForwardSegment,
    OtherSegment,
]


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


_SEGMENT_PARSERS: Dict[str, Callable[[Dict[str, Any]], MessageSegment]] = {
    "text": lambda d: TextSegment(text=str(d.get("text", ""))),
    "at": lambda d: AtSegment(qq=str(d.get("qq", ""))),
    "image": lambda d: ImageSegment(
        url=str(d.get("url", "")),
        file=d.get("file"),
        sub_type=_as_int(d.get("sub_type")),
        summary=str(d.get("summary") or ""),
    ),
    "reply": lambda d: ReplySegment(id=str(d.get("id", ""))),
    "face": lambda d: FaceSegment(id=str(d.get("id", ""))),
    "record": lambda d: RecordSegment(url=str(d.get("url", "")), file=d.get("file")),
    "video": lambda d: VideoSegment(url=str(d.get("url", "")), file=d.get("file")),
    "file": lambda d: FileSegment(url=str(d.get("url", "")), file=d.get("file")),
    "forward": lambda d: ForwardSegment(id=str(d.get("id", ""))),
}


def _parse_segment(raw: Dict[str, Any]) -> MessageSegment:
    """Decode one segment dict into the matching dataclass."""
    ty = raw.get("type")
    parser = _SEGMENT_PARSERS.get(ty if isinstance(ty, str) else "")
    if parser is None:
        return OtherSegment(raw=raw)
    data = raw.get("data") or {}
    if not isinstance(data, dict):
        return OtherSegment(raw=raw)
    return parser(data)



def parse_segments(raw: Iterable[Any]) -> List[MessageSegment]:
    """Decode a wire segment array (e.g. a ``get_msg`` reply's ``message``)."""
    return [_parse_segment(s) for s in raw if isinstance(s, dict)]

def _coerce_int(value: Any, default: int = 0) -> int:
    """Best-effort int coercion that never raises.

    ``self_id`` / ``user_id`` / ``message_id`` / ``time`` are supposed to be
    numeric, but a misbehaving upstream client can ship a string, ``null``,
    or a float-as-string.  A bare ``int()`` there raises, unwinds the reader
    pump and drops the whole WebSocket — reconnect churn plus message loss.
    Falling back to ``default`` keeps the frame parseable.
    """
    if isinstance(value, bool):
        # bool is an int subclass; treat it as the default rather than 0/1.
        return default
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_event(raw: Dict[str, Any]) -> Event:
    """Decode one OneBot event dict into the matching :data:`Event`.

    Unknown ``post_type`` collapses to :class:`UnknownEvent`; numeric fields
    go through :func:`_coerce_int`.  This function never raises for any
    JSON-decodable input.
    """
    post_type = raw.get("post_type")
    if post_type == "message":
        msg_type_raw = raw.get("message_type")
        if not isinstance(msg_type_raw, str):
            return UnknownEvent(raw=raw)
        try:
            msg_type = MessageType(msg_type_raw)
        except ValueError:
            return UnknownEvent(raw=raw)
        sender_raw = raw.get("sender")
        sender: Optional[Sender] = None
        if isinstance(sender_raw, dict):
            sender = Sender(
                user_id=sender_raw.get("user_id"),
                nickname=sender_raw.get("nickname"),
                card=sender_raw.get("card"),
                role=sender_raw.get("role"),
            )
        message_raw = raw.get("message") or []
        if isinstance(message_raw, str):
            # ``messagePostFormat: "string"`` backends ship CQ codes rather
            # than a segment array.  Keep the text so routing still works;
            # media/mention extraction degrades (documented in README).
            segments: List[MessageSegment] = [TextSegment(text=message_raw)]
        elif isinstance(message_raw, list):
            segments = [_parse_segment(s) for s in message_raw if isinstance(s, dict)]
        else:
            segments = []
        return MessageEvent(
            self_id=_coerce_int(raw.get("self_id", 0)),
            message_type=msg_type,
            user_id=_coerce_int(raw.get("user_id", 0)),
            message_id=_coerce_int(raw.get("message_id", 0)),
            message=segments,
            time=_coerce_int(raw.get("time", 0)),
            sub_type=raw.get("sub_type"),
            group_id=raw.get("group_id"),
            raw_message=str(raw.get("raw_message", "")),
            sender=sender,
        )
    if post_type == "notice":
        return NoticeEvent(
            self_id=_coerce_int(raw.get("self_id", 0)),
            notice_type=str(raw.get("notice_type", "")),
            time=_coerce_int(raw.get("time", 0)),
            group_id=raw.get("group_id"),
            user_id=raw.get("user_id"),
        )
    if post_type == "meta_event":
        return MetaEvent(
            self_id=_coerce_int(raw.get("self_id", 0)),
            meta_event_type=str(raw.get("meta_event_type", "")),
            time=_coerce_int(raw.get("time", 0)),
        )
    if post_type == "request":
        return RequestEvent(
            self_id=_coerce_int(raw.get("self_id", 0)),
            request_type=str(raw.get("request_type", "")),
            time=_coerce_int(raw.get("time", 0)),
            user_id=raw.get("user_id"),
            group_id=raw.get("group_id"),
            flag=raw.get("flag"),
        )
    return UnknownEvent(raw=raw)


# ===========================================================================
# Segment helpers
# ===========================================================================


def segments_to_text(segments: Iterable[MessageSegment]) -> str:
    """Flatten segments to a single string.

    ``at`` segments render as ``@<qq> `` so keyword routing still sees the
    address — the group gate matches against this text, and a rule like
    "reply when someone writes 格兰" must not be defeated by an @ living in
    a structured segment.
    """
    out: List[str] = []
    for seg in segments:
        if isinstance(seg, TextSegment):
            out.append(seg.text)
        elif isinstance(seg, AtSegment):
            out.append(f"@{seg.qq} ")
    return "".join(out)



_CQ_CODE = re.compile(r"\[CQ:([A-Za-z_]+)((?:,[^\]]*)?)\]")
_CQ_PLACEHOLDERS = {
    "image": "[图片]",
    "face": "[表情]",
    "mface": "[表情]",
    "record": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "forward": "[聊天记录]",
    "json": "[卡片]",
    "xml": "[卡片]",
    "reply": "",
}


def humanize_cq(text: str, stickers: Sequence[bool] = ()) -> str:
    """Replace CQ codes with short placeholders for model-facing transcripts.

    A ``[CQ:image,...,url=...]`` code carries an escaped (``&amp;``), signed
    download link.  Shown to a model it reads as an invitation to fetch the
    picture, and the copy it makes is truncated or still escaped, so QQ
    answers 400 and the bot reports "the link is broken".  The picture itself
    reaches the model as a real attachment (see ``.turn_media``); the
    transcript only needs to say that one was there.

    *stickers* says, per image code in order, whether it is a sticker; the CQ
    text itself does not carry that flag, so the caller reads it from the
    parsed segments.  A sticker renders as ``[表情包]``, never as a picture
    somebody posted.
    """
    images = iter(stickers)

    def _one(match: "re.Match[str]") -> str:
        kind, params = match.group(1).lower(), match.group(2)
        if kind == "image" and next(images, False):
            return "[表情包]"
        if kind == "at":
            qq = re.search(r"qq=([^,\]]+)", params)
            return f"@{qq.group(1)} " if qq else ""
        return _CQ_PLACEHOLDERS.get(kind, f"[{kind}]")

    return _CQ_CODE.sub(_one, text or "")

def strip_self_mention(segments: Iterable[MessageSegment], self_id: int) -> str:
    """Agent-facing text: like :func:`segments_to_text` but drops ``@bot``.

    The routing layer wants the raw address visible; the *model* does not
    need to read its own QQ number at the head of every group message, and
    leaving it in wastes tokens and confuses "who is being addressed".
    Mentions of OTHER users are preserved (they are conversational content).
    """
    target = str(self_id)
    out: List[str] = []
    for seg in segments:
        if isinstance(seg, TextSegment):
            out.append(seg.text)
        elif isinstance(seg, AtSegment):
            if seg.qq == target:
                continue
            out.append(f"@{seg.qq} ")
    return "".join(out).strip()


@dataclass
class MediaRef:
    """One inbound attachment referenced by URL.

    Transport-neutral on purpose: the gateway adapter turns these into
    locally cached files via ``gateway.platforms.base.cache_*_from_url``.
    ``kind`` is one of ``image`` / ``audio`` / ``video`` / ``document``.

    An empty ``url`` means one specific thing and only for ``audio``:
    "resolve me by id, the id is in ``file_name``" — see
    :func:`segments_to_media`.
    """

    kind: str
    url: str
    mime: str
    file_name: Optional[str] = None
    #: An image that is a sticker (表情包), not a photo — see ImageSegment.
    sticker: bool = False


def image_stickers(segments: Iterable[MessageSegment]) -> List[bool]:
    """Per image segment, in order: is it a sticker?"""
    return [seg.is_sticker for seg in segments if isinstance(seg, ImageSegment)]


def segments_to_media(segments: Iterable[MessageSegment]) -> List[MediaRef]:
    """Pull image / voice / video / file attachments out of a segment list.

    Segments with an empty ``url`` are skipped: gocq ships an empty URL for
    offline media, and some backends ship name-only ``file`` segments — neither can
    be downloaded, and emitting a MediaRef for them produces a broken
    attachment rather than a missing one.

    **Voice is the exception**, because voice is the one kind that can still
    be recovered without a URL: :class:`GetRecord` resolves a ``record`` by
    its ``file`` id server-side.  So a url-less record with a ``file`` is
    emitted as ``MediaRef("audio", url="", file_name=<the file id>)`` — the
    empty ``url`` *is* the "resolve me by id" signal, which is why this needs
    no extra field.  The adapter's ``_download_media``
    (``plugins/platforms/onebot/adapter.py``) is the only consumer and knows
    to call ``get_record`` instead of fetching.  Dropping these instead —
    what we did before — made a NapCat voice note vanish with no attachment
    and no log line.

    A record with neither ``url`` nor ``file`` is still skipped: nothing can
    recover that one, and a phantom attachment is worse than a missing one.
    """
    out: List[MediaRef] = []
    for seg in segments:
        if isinstance(seg, ImageSegment) and seg.url:
            out.append(MediaRef("image", seg.url, "image/*", seg.file, sticker=seg.is_sticker))
        elif isinstance(seg, RecordSegment) and (seg.url or seg.file):
            out.append(MediaRef("audio", seg.url, "audio/*", seg.file))
        elif isinstance(seg, VideoSegment) and seg.url:
            out.append(MediaRef("video", seg.url, "video/*", seg.file))
        elif isinstance(seg, FileSegment) and seg.url:
            # QQ exposes no precise content type for shared files.
            out.append(
                MediaRef("document", seg.url, "application/octet-stream", seg.file)
            )
    return out


def is_mentioned(segments: Iterable[MessageSegment], self_id: int) -> bool:
    """True when any ``at`` segment targets ``self_id`` (or is ``@all``)."""
    target = str(self_id)
    for seg in segments:
        if isinstance(seg, AtSegment) and (seg.qq == target or seg.qq == "all"):
            return True
    return False


def reply_target(segments: Iterable[MessageSegment]) -> Optional[str]:
    """Message id this message quotes, if any (``reply`` segment)."""
    for seg in segments:
        if isinstance(seg, ReplySegment) and seg.id:
            return seg.id
    return None


# ===========================================================================
# Outbound actions
# ===========================================================================


@dataclass
class SendPrivateMsg:
    """``action = "send_private_msg"``."""

    user_id: int
    message: List[MessageSegment]


@dataclass
class SendGroupMsg:
    """``action = "send_group_msg"``."""

    group_id: int
    message: List[MessageSegment]


@dataclass
class ForwardNode:
    """One node of a merged-forward ("聊天记录") card."""

    name: str
    uin: str
    content: List[MessageSegment]


@dataclass
class SendGroupForwardMsg:
    """``action = "send_group_forward_msg"``."""

    group_id: int
    messages: List[ForwardNode]


@dataclass
class SendPrivateForwardMsg:
    """``action = "send_private_forward_msg"`` — OneBot v11 extension (NapCat / SnowLuma)."""

    user_id: int
    messages: List[ForwardNode]


@dataclass
class SetInputStatus:
    """``action = "set_input_status"`` — OneBot v11 extension (NapCat / SnowLuma).

    ``event_type`` is 1 (typing) or 0 (cancel).  The indicator is transient,
    so a long turn re-fires it.  Private chats only — QQ group clients do not
    render a typing state.  Backends that do not implement this extension
    answer with an "unsupported action" envelope; callers treat any failure
    as a no-op.
    """

    user_id: int
    event_type: int = 1


@dataclass
class UploadPrivateFile:
    """``action = "upload_private_file"`` — OneBot v11 extension (NapCat / SnowLuma).

    ``file`` accepts ``base64://…`` (preferred: the backend may run in a
    different container and cannot read our paths), an absolute path, or an
    ``http(s)://`` URL.
    """

    user_id: int
    file: str
    name: Optional[str] = None


@dataclass
class UploadGroupFile:
    """``action = "upload_group_file"`` — OneBot v11 extension (NapCat / SnowLuma).

    ``folder`` defaults to the root of the group's file area.
    """

    group_id: int
    file: str
    name: Optional[str] = None
    folder: Optional[str] = None


@dataclass
class GetRecord:
    """``action = "get_record"`` — OneBot v11 standard action.

    Inbound voice arrives as SILK/AMR (see :class:`RecordSegment`), which
    nothing outside a QQ client can decode.  This asks the backend to
    transcode ``file`` (the segment's ``file`` value) to ``out_format`` so
    the result can be fed to a normal ASR pipeline.
    """

    file: str
    out_format: str = "mp3"


@dataclass
class SendGroupAiRecord:
    """``action = "send_group_ai_record"`` — OneBot v11 extension (NapCat / SnowLuma).

    Posts a voice message spoken by one of QQ's own built-in AI voice
    characters (群 AI 声聊), picked by ``character`` (a ``character_id``
    from :class:`GetAiCharacters`).  Group-only: QQ does not expose an
    AI-voice equivalent for private chats, so there is no
    ``send_private_ai_record``.  NapCat and SnowLuma implement this extension;
    a Lagrange or go-cqhttp backend may reject it.
    """

    group_id: int
    character: str
    text: str


@dataclass
class GetAiRecord:
    """``action = "get_ai_record"`` — OneBot v11 extension (NapCat / SnowLuma).

    Same inputs as :class:`SendGroupAiRecord`, but returns a URL to the
    synthesized audio (in the response ``data``) instead of posting it.
    ``group_id`` is required even though nothing is sent to that group —
    QQ scopes the AI character list per group.  NapCat and SnowLuma implement
    this extension; a Lagrange or go-cqhttp backend may reject it.
    """

    group_id: int
    character: str
    text: str


@dataclass
class GetAiCharacters:
    """``action = "get_ai_characters"`` — OneBot v11 extension (NapCat / SnowLuma).

    Lists the AI voice characters available to a group, grouped by style
    (``data`` is ``[{"type": "推荐"|"古风"|"现代", "characters": [...]}]``).
    NapCat and SnowLuma implement this extension; a Lagrange or go-cqhttp
    backend may reject it.
    """

    group_id: int
    chat_type: Optional[int] = None


@dataclass
class SendPoke:
    """``action = "send_poke"`` — OneBot v11 extension (NapCat / SnowLuma).

    ``group_id`` omitted sends a private poke to ``user_id``; set it to
    poke someone inside a group instead.  NapCat and SnowLuma implement this
    extension; a Lagrange or go-cqhttp backend may reject it.
    """

    user_id: int
    group_id: Optional[int] = None


@dataclass
class RawAction:
    """Escape hatch for any OneBot action we do not model as a dataclass.

    This is the seam the outbound tool layer builds on: ``get_login_info``,
    ``get_cookies``, ``get_friend_list``, ``get_group_list``,
    ``get_group_info``, ``get_stranger_info``… all go through here, so a new
    consumer (e.g. the Qzone tool family, which borrows the QQ login state
    via ``get_cookies``) needs no change to this module.
    """

    action: str
    params: Dict[str, Any] = field(default_factory=dict)


#: Convenience alias used by ``get_chat_info`` — kept in ``__all__`` so the
#: adapter's intent reads clearly at the call site.
GetChatInfo = RawAction


Action = Union[
    SendPrivateMsg,
    SendGroupMsg,
    SendGroupForwardMsg,
    SendPrivateForwardMsg,
    SetInputStatus,
    UploadPrivateFile,
    UploadGroupFile,
    GetRecord,
    SendGroupAiRecord,
    GetAiRecord,
    GetAiCharacters,
    SendPoke,
    RawAction,
]


def _segment_to_wire(seg: MessageSegment) -> Dict[str, Any]:
    """Serialize a single segment back to OneBot wire form."""
    if isinstance(seg, TextSegment):
        return {"type": "text", "data": {"text": seg.text}}
    if isinstance(seg, AtSegment):
        return {"type": "at", "data": {"qq": seg.qq}}
    if isinstance(seg, ImageSegment):
        data: Dict[str, Any] = {}
        if seg.url:
            data["url"] = seg.url
        if seg.file is not None:
            data["file"] = seg.file
        return {"type": "image", "data": data}
    if isinstance(seg, ReplySegment):
        return {"type": "reply", "data": {"id": seg.id}}
    if isinstance(seg, FaceSegment):
        return {"type": "face", "data": {"id": seg.id}}
    if isinstance(seg, RecordSegment):
        rdata: Dict[str, Any] = {}
        if seg.url:
            rdata["url"] = seg.url
        if seg.file is not None:
            rdata["file"] = seg.file
        return {"type": "record", "data": rdata}
    if isinstance(seg, VideoSegment):
        vdata: Dict[str, Any] = {"url": seg.url}
        if seg.file is not None:
            vdata["file"] = seg.file
        return {"type": "video", "data": vdata}
    if isinstance(seg, FileSegment):
        fdata: Dict[str, Any] = {"url": seg.url}
        if seg.file is not None:
            fdata["file"] = seg.file
        return {"type": "file", "data": fdata}
    if isinstance(seg, ForwardSegment):
        return {"type": "forward", "data": {"id": seg.id}}
    # OtherSegment falls through to its raw form.
    return seg.raw


def _forward_node_to_wire(node: ForwardNode) -> Dict[str, Any]:
    """Serialize one merged-forward node.

    Emits BOTH the go-cqhttp key pair (``name`` / ``uin``) and the NapCat /
    NapCat pair (``nickname`` / ``user_id``).  Backends read whichever
    dialect they know and ignore the other, so one wire shape covers both — this is
    cheaper and far less fragile than sniffing the backend at runtime.
    """
    return {
        "type": "node",
        "data": {
            "name": node.name,
            "uin": node.uin,
            "nickname": node.name,
            "user_id": node.uin,
            "content": [_segment_to_wire(s) for s in node.content],
        },
    }


def action_to_wire(action: Action) -> Dict[str, Any]:
    """Serialize an :data:`Action` to ``{"action": ..., "params": {...}}``."""
    if isinstance(action, RawAction):
        return {"action": action.action, "params": dict(action.params)}
    if isinstance(action, SendPrivateMsg):
        return {
            "action": "send_private_msg",
            "params": {
                "user_id": action.user_id,
                "message": [_segment_to_wire(s) for s in action.message],
            },
        }
    if isinstance(action, SendGroupMsg):
        return {
            "action": "send_group_msg",
            "params": {
                "group_id": action.group_id,
                "message": [_segment_to_wire(s) for s in action.message],
            },
        }
    if isinstance(action, SetInputStatus):
        return {
            "action": "set_input_status",
            "params": {
                "user_id": action.user_id,
                "event_type": action.event_type,
            },
        }
    if isinstance(action, UploadPrivateFile):
        params: Dict[str, Any] = {"user_id": action.user_id, "file": action.file}
        if action.name is not None:
            params["name"] = action.name
        return {"action": "upload_private_file", "params": params}
    if isinstance(action, UploadGroupFile):
        gparams: Dict[str, Any] = {"group_id": action.group_id, "file": action.file}
        if action.name is not None:
            gparams["name"] = action.name
        if action.folder is not None:
            gparams["folder"] = action.folder
        return {"action": "upload_group_file", "params": gparams}
    if isinstance(action, SendPrivateForwardMsg):
        return {
            "action": "send_private_forward_msg",
            "params": {
                "user_id": action.user_id,
                "messages": [_forward_node_to_wire(n) for n in action.messages],
            },
        }
    if isinstance(action, GetRecord):
        return {
            "action": "get_record",
            "params": {"file": action.file, "out_format": action.out_format},
        }
    if isinstance(action, SendGroupAiRecord):
        # NapCat's own docs disagree with themselves on this field's name:
        # the live API doc (https://napneko.github.io/develop/api/doc) says
        # "text", the commit that introduced those docs
        # (github.com/NapNeko/NapCatDocs commit 42538ddf) says "test" — an
        # upstream typo that may or may not match what actually shipped.
        # NapCat's generated action catalog
        # (packages/mcp/src/generated/catalog.json) names the canonical
        # parameter "text" and sets additionalProperties to true, so the
        # redundant "test" key is harmless there too.  Keep both spellings
        # with the same value for compatibility with older NapCat builds.
        # Do not "clean up" the duplicate; it is deliberate.
        return {
            "action": "send_group_ai_record",
            "params": {
                "group_id": action.group_id,
                "character": action.character,
                "text": action.text,
                "test": action.text,
            },
        }
    if isinstance(action, GetAiRecord):
        # NapCat's generated action catalog
        # (packages/mcp/src/generated/catalog.json) names the canonical
        # parameter "text" and sets additionalProperties to true, so the
        # redundant "test" key is harmless there too.  Keep both spellings
        # with the same value for compatibility with older NapCat builds.
        return {
            "action": "get_ai_record",
            "params": {
                "group_id": action.group_id,
                "character": action.character,
                "text": action.text,
                "test": action.text,
            },
        }
    if isinstance(action, GetAiCharacters):
        aiparams: Dict[str, Any] = {"group_id": action.group_id}
        if action.chat_type is not None:
            aiparams["chat_type"] = action.chat_type
        return {"action": "get_ai_characters", "params": aiparams}
    if isinstance(action, SendPoke):
        pokeparams: Dict[str, Any] = {"user_id": action.user_id}
        if action.group_id is not None:
            pokeparams["group_id"] = action.group_id
        return {"action": "send_poke", "params": pokeparams}
    # SendGroupForwardMsg
    return {
        "action": "send_group_forward_msg",
        "params": {
            "group_id": action.group_id,
            "messages": [_forward_node_to_wire(n) for n in action.messages],
        },
    }
