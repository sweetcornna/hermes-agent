"""Grantley's own sticker set, offered to the model and shipped inline.

Who chooses
-----------
The model does.  This module never picks a sticker; it only decides — with a
probability — whether the persona is *shown* that it has any, and later
translates whatever the persona wrote into an image segment.

That split is the whole design.  A keyword table matching the reply text
against sticker descriptions was the obvious cheap version, and it is wrong
here: it re-derives an intent the writer already had, from the words it chose
to express something else.  "我哪知道" deserves ``shrug``; a keyword matcher
reading it for the token 知道 has no way to know that, while the thing that
wrote the sentence does.  So the roll gates *availability*, and selection is
the persona's.

The probability is what keeps it a habit rather than a tic: on most turns the
menu is simply absent from the frame, the model has no idea stickers exist,
and it writes plain text without being told not to.  Suppression by omission
needs no instruction and cannot be disobeyed — a "only use these rarely"
sentence in the prompt would be negotiated with on every single turn.

How the choice comes back
-------------------------
As an inline marker, ``[STICKER:thumbs-up]``, structurally identical to the
``[MSG_BREAK]`` the persona already writes.  Reusing that shape costs nothing:
no tool-call plumbing, no second model round-trip, no schema for the model to
get wrong — and the adapter is already in the business of lifting control
tokens out of a reply body.

Unknown slugs are dropped in silence.  A model that invents
``[STICKER:happy]`` has made a mistake the reader must never be shown; the
alternatives are worse in both directions — raising loses the whole reply
over a flourish, and passing it through prints raw syntax into a QQ window.

Wire form
---------
``ImageSegment(file="base64://…")``, which is what ``_send_attachment`` in
``adapter.py`` has always done for inline images, via ``encode_base64_file``.
The reason is recorded on ``BASE64_MAX_BYTES`` there: the OneBot backend
normally runs in its own container and cannot read this process's filesystem,
so a host path handed across that boundary resolves to nothing.  These files
are ~25 KB each, three orders of magnitude under the 8 MiB inlining ceiling,
so they always take the base64 branch and never the literal-path fallback.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "STICKER_CATALOG",
    "CATALOG_FILENAME",
    "DEFAULT_STICKER_PROBABILITY",
    "load_catalog",
    "MARKER_RE",
    "extract_markers",
    "available_stickers",
    "render_menu",
    "offer_menu",
    "should_offer",
    "sticker_path",
    "probability_from_extra",
    "dir_from_extra",
    "default_sticker_dir",
]

#: How often a turn is shown the sticker menu at all.
#:
#: Low on purpose.  This is the rate at which stickers become *possible*, and
#: the model declines plenty of the turns where it can, so the rate they
#: actually appear at is lower still.  The complaint this outbound path keeps
#: being reshaped around is noise per reply; a sticker roughly one turn in
#: five reads as a person with a habit, one every other turn as a bot with a
#: tic.  ``0`` disables the feature outright — menu never offered, and any
#: marker the model emits anyway is dropped.
DEFAULT_STICKER_PROBABILITY = 0.18

#: slug → what the picture actually shows, and when it fits.
#:
#: The descriptions are the *only* thing the model sees about each image, so
#: they carry both halves: the drawing (so it can tell them apart) and the
#: occasion (so it can choose). Slugs match the filenames in the asset dir.
STICKER_CATALOG: List[dict] = [
    {
        "slug": "heart-hug",
        "file": "heart-hug.jpg",
        "scene": "抱着一颗爱心，闭眼微笑",
        "when": "喜欢、感动、心软、被戳中",
    },
    {
        "slug": "fired-up",
        "file": "fired-up.jpg",
        "scene": "握拳，神情认真，旁边冒星星",
        "when": "有干劲、包在我身上、来吧",
    },
    {
        "slug": "unimpressed",
        "file": "unimpressed.jpg",
        "scene": "撇嘴别过脸，头顶叹气云",
        "when": "无语、不屑、懒得理",
    },
    {
        "slug": "flustered",
        "file": "flustered.jpg",
        "scene": "抓着头，脸红",
        "when": "害羞、尴尬、被看穿",
    },
    {
        "slug": "thumbs-up",
        "file": "thumbs-up.jpg",
        "scene": "竖起大拇指，旁边冒星星",
        "when": "赞、没问题、干得好",
    },
    {
        "slug": "shrug",
        "file": "shrug.jpg",
        "scene": "摊开双手，头顶叹气云",
        "when": "无奈、我哪知道、随便",
    },
    {
        "slug": "angry",
        "file": "angry.jpg",
        "scene": "抱着手臂生气，头上冒怒气符号",
        "when": "生气、不爽、别惹我",
    },
    {
        "slug": "laughing",
        "file": "laughing.jpg",
        "scene": "张嘴大笑，旁边一个感叹号",
        "when": "大笑、开心、嘲笑",
    },
    {
        "slug": "thinking",
        "file": "thinking.jpg",
        "scene": "托着下巴思考，旁边冒星星",
        "when": "思考、有主意、在想",
    },
]

_BY_SLUG = {entry["slug"]: entry for entry in STICKER_CATALOG}

#: ``[STICKER:slug]``.  Case-insensitive on the tag and forgiving about
#: internal spaces, because those are the ways a model gets the shape
#: *nearly* right; the slug charset stays strict so a malformed one is
#: dropped rather than reinterpreted as a path.
MARKER_RE = re.compile(r"\[\s*STICKER\s*:\s*([A-Za-z0-9_-]+)\s*\]", re.IGNORECASE)

#: The slug charset, shared by the marker and by pack validation.
_SLUG_RE = re.compile(r"[a-z0-9_-]+")

#: Where the images live when nobody says otherwise: the persona plugin's own
#: asset directory, resolved relative to THIS file rather than the process
#: working directory.  The deployed tree is rooted at ``/opt/hermes/repo``
#: and the gateway is not started from it, so a relative path would miss.
_DEFAULT_DIR = Path(__file__).resolve().parents[2] / "grantley" / "assets" / "stickers"


#: A pack may describe itself with this file, sitting next to its images.
#: When absent the bundled :data:`STICKER_CATALOG` is used, which is what
#: keeps the original single-pack deployment byte-identical: grantley's
#: directory has no ``catalog.json`` and never needs one.
CATALOG_FILENAME = "catalog.json"

#: ``{resolved_dir: (mtime_ns, catalog)}``.  The menu is composed on the
#: inbound hot path, and re-reading a small JSON file per turn is pure waste;
#: keying on mtime means an operator who edits a pack still gets picked up
#: without a restart.
_CATALOG_CACHE: dict = {}


def _coerce_catalog(raw: Any) -> Optional[List[dict]]:
    """Validate a decoded ``catalog.json`` into catalogue entries, or ``None``.

    Every field the rest of this module reads must be a non-empty string, and
    the slug must match the marker charset — otherwise a pack could advertise
    a name the model can write but :func:`sticker_path` would never resolve,
    which is precisely the "set up to fail by its own prompt" failure the
    disk filter in :func:`available_stickers` exists to prevent.  A malformed
    file degrades the whole pack to the bundled catalogue rather than to a
    half-loaded one: partial packs fail in ways nobody can read off a log.
    """
    if not isinstance(raw, list) or not raw:
        return None
    out: List[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        slug = str(item.get("slug") or "").strip().lower()
        file = str(item.get("file") or "").strip()
        scene = str(item.get("scene") or "").strip()
        when = str(item.get("when") or "").strip()
        if not (slug and file and scene and when):
            return None
        if not _SLUG_RE.fullmatch(slug):
            return None
        # A pack must not be able to name a path.  ``file`` is joined onto the
        # pack directory, so a separator or a parent ref here is a traversal.
        if "/" in file or "\\" in file or file.startswith("."):
            return None
        out.append({"slug": slug, "file": file, "scene": scene, "when": when})
    return out


def load_catalog(directory: Optional[str] = None) -> List[dict]:
    """The catalogue for one pack: its own ``catalog.json``, else the bundled one.

    This is the seam that lets a second persona reuse the whole sticker
    mechanism — probability gate, menu, ``[STICKER:slug]`` marker, base64
    inlining — with its own images and its own descriptions, without a second
    code path and without touching the original pack.
    """
    base = Path(directory or default_sticker_dir())
    path = base / CATALOG_FILENAME
    try:
        stat = path.stat()
    except OSError:
        return STICKER_CATALOG
    key = str(path)
    cached = _CATALOG_CACHE.get(key)
    if cached is not None and cached[0] == stat.st_mtime_ns:
        return cached[1]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — a broken pack must not cost a turn
        logger.warning("OneBot: sticker catalog %s is unreadable (%s)", path, exc)
        return STICKER_CATALOG
    entries = _coerce_catalog(raw)
    if entries is None:
        logger.warning("OneBot: sticker catalog %s is malformed; using the bundled set", path)
        return STICKER_CATALOG
    _CATALOG_CACHE[key] = (stat.st_mtime_ns, entries)
    return entries


def default_sticker_dir() -> str:
    return str(_DEFAULT_DIR)


def _as_float(value: Any, default: float) -> float:
    try:
        if value is None or str(value).strip() == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _per_channel(extra: Mapping[str, Any], key: str, chat_id: Any) -> Any:
    """One channel's override out of a ``{chat_id: value}`` map, or ``None``.

    *chat_id* is the platform-native id the operator writes in ``channels:``
    (a bare QQ group id), not the adapter's ``g``-prefixed form — both are
    accepted here because the two call sites historically pass different
    shapes and a silent miss would look exactly like "no override".
    """
    table = extra.get(key)
    if not isinstance(table, Mapping) or chat_id is None:
        return None
    raw = str(chat_id).strip()
    if not raw:
        return None
    for candidate in (raw, raw[1:] if raw[:1] in ("g", "G") else None, "g" + raw):
        if candidate and candidate in table:
            return table[candidate]
    return None


def probability_from_extra(
    extra: Optional[Mapping[str, Any]], chat_id: Any = None
) -> float:
    """Resolve the probability: per-channel → ``extra`` → environment → default.

    Clamped rather than validated.  An operator who writes ``2`` means
    "always", and refusing to start the whole gateway over a typo in a
    cosmetic setting would be a wildly disproportionate failure.
    """
    extra = extra or {}
    override = _per_channel(extra, "sticker_probabilities", chat_id)
    if override is not None:
        return min(1.0, max(0.0, _as_float(override, DEFAULT_STICKER_PROBABILITY)))
    raw = extra.get("sticker_probability", os.getenv("ONEBOT_STICKER_PROBABILITY"))
    return min(1.0, max(0.0, _as_float(raw, DEFAULT_STICKER_PROBABILITY)))


def dir_from_extra(extra: Optional[Mapping[str, Any]], chat_id: Any = None) -> str:
    """Resolve the asset directory: per-channel → ``extra`` → env → bundled.

    The per-channel map (``sticker_dirs``) is what gives a channel bound to a
    different persona its own pack.  Without it every channel on one OneBot
    account shares one set, and a second persona would be offered the first
    persona's face.
    """
    extra = extra or {}
    override = _per_channel(extra, "sticker_dirs", chat_id)
    if override and str(override).strip():
        return str(override).strip()
    raw = extra.get("sticker_dir") or os.getenv("ONEBOT_STICKER_DIR")
    return str(raw).strip() if raw and str(raw).strip() else default_sticker_dir()


def sticker_path(slug: str, directory: Optional[str] = None) -> Optional[str]:
    """Absolute path of *slug*'s image, or ``None`` if it is not usable.

    The catalogue is the allowlist, checked before the filesystem is touched:
    the slug arrives from model output, and resolving arbitrary model-authored
    text against a directory is how a traversal gets read off disk.  A name
    that is not one of the nine is not a path at all.
    """
    base = directory or default_sticker_dir()
    by_slug = {e["slug"]: e for e in load_catalog(base)}
    entry = by_slug.get(str(slug or "").strip().lower())
    if entry is None:
        return None
    path = Path(base) / entry["file"]
    if not path.is_file():
        logger.warning("OneBot: sticker %r is missing at %s", entry["slug"], path)
        return None
    return str(path)


def extract_markers(text: str) -> Tuple[str, List[str]]:
    """Split *text* into ``(body_without_markers, slugs_in_order)``.

    Always strips, whatever the slug turns out to be — validity is the
    caller's question, and an unrecognised marker must still never reach a
    reader.  Whitespace is renormalised afterwards because the marker is
    usually alone on its line, and removing it in place would otherwise leave
    a hole that reads as a paragraph break the persona did not write.
    """
    if not text:
        return "", []
    slugs = [m.group(1).strip().lower() for m in MARKER_RE.finditer(text)]
    if not slugs:
        return text, []
    body = MARKER_RE.sub("", text)
    body = re.sub(r"[ \t]+(\n|$)", r"\1", body)  # trailing space the marker left
    body = re.sub(r"\n{3,}", "\n\n", body)  # the hole it left between lines
    return body.strip(), slugs


def available_stickers(directory: Optional[str] = None) -> List[dict]:
    """Catalogue entries whose image is actually present on this box.

    Filtered against the disk so the menu can never advertise something the
    send path would then drop — a model that picks a missing sticker has been
    set up to fail by its own prompt.
    """
    resolved = directory or default_sticker_dir()
    base = Path(resolved)
    return [e for e in load_catalog(resolved) if (base / e["file"]).is_file()]


def render_menu(entries: Sequence[dict]) -> str:
    """The '可用表情' block appended to this turn's persona frame."""
    lines = [
        "## 可用表情",
        "",
        "你有下面这几张自己的表情图。想用的时候，在回复里单独起一行写 "
        "`[STICKER:名字]`，这一行会被换成对应的图片发出去；不想用就一个字都不用提。",
        "一条回复最多用一张，不要连发。",
        "",
    ]
    for entry in entries:
        lines.append(f"- `{entry['slug']}` — {entry['scene']}。用于：{entry['when']}")
    return "\n".join(lines)


def should_offer(probability: float, rng: Optional[random.Random] = None) -> bool:
    """Roll the availability gate.

    ``<= 0`` and ``>= 1`` short-circuit without touching *rng*, so both ends
    of the range are decisions rather than very likely coin tosses — an
    operator (and a test) can rely on "0 never" and "1 always" absolutely,
    with no seed in the picture.
    """
    if probability <= 0:
        return False
    if probability >= 1:
        return True
    return (rng or random).random() < probability


def offer_menu(
    extra: Optional[Mapping[str, Any]],
    rng: Optional[random.Random] = None,
    chat_id: Any = None,
) -> Optional[str]:
    """This turn's sticker menu, or ``None`` to say nothing about stickers.

    ``None`` is the quiet path in every sense: the model is not told stickers
    exist, not told to use them sparingly, and not told it just failed a dice
    roll.  It simply writes a reply.
    """
    if not should_offer(probability_from_extra(extra, chat_id), rng):
        return None
    entries = available_stickers(dir_from_extra(extra, chat_id))
    if not entries:
        return None
    return render_menu(entries)


def standing_menu(
    extra: Optional[Mapping[str, Any]],
    chat_id: Any = None,
) -> Optional[str]:
    """The menu whenever stickers are on for *chat_id*, with no dice.

    The menu rides the system message, and a per-turn roll there changed
    those bytes on ~30% of consecutive turns, discarding the whole cached
    history each time. The roll lives on the send side instead
    (``OneBotAdapter._sticker_segment``), so the prompt stays byte-stable.
    """
    if probability_from_extra(extra, chat_id) <= 0:
        return None
    entries = available_stickers(dir_from_extra(extra, chat_id))
    if not entries:
        return None
    return render_menu(entries)
