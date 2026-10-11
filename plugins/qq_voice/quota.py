"""Length and budget caps for the two model-callable QQ voice tools.

Why a plugin needs a cap at all
-------------------------------
Every *autonomous* voice path in this install is already bounded — the
proactive lane has ``proactive_daily_max`` (4 per group per day), the call
lane has ``proactive_call_daily_max`` (1 per day), spoken proactive posts are
cut off at ``proactive_voice_max_chars`` (120), and all of them share the
adapter's sliding-window speech cap.  ``qq_send_voice`` is the one voice path
with none of that: the model decides when to call it, and each call runs a
synthesis through whatever ``tts.provider`` the operator configured.  On the
deployment this plugin was ported from that provider is a paid per-synthesis
one, so an uncapped model-callable tool bills per call with nothing between
it and the invoice.

Two caps, and they exist for two *different* reasons — which is why they do
not apply to the same set of tools:

* :attr:`VoiceLimits.max_text_chars` — **a listener-side cap.**  A
  multi-minute voice bubble is a chore on QQ in a way a long text message is
  not: it cannot be skimmed, searched, or read at the reader's own pace.  That
  is true no matter who paid for the synthesis, so this one applies to
  ``qq_ai_voice`` as well.  Leaving it off there would also hand the model a
  free way around the rule (speak the wall of text in one of Tencent's AI
  characters instead).
* :attr:`VoiceLimits.daily_max` — **a cost cap on us.**  It counts
  ``qq_send_voice`` sends only.  ``qq_ai_voice`` is synthesized on Tencent's
  side and costs this install nothing per call, so charging it against a paid
  budget would make the model ration the wrong resource, and would let a free
  tool starve the paid one.

Configuration
-------------
Two keys in this plugin's own settings block in ``~/.hermes/config.yaml``,
declared in ``plugin.yaml``'s ``config_schema``::

    plugins:
      entries:
        qq_voice:
          settings:
            max_text_chars: 200   # 0 or less disables the cap
            daily_max: 30         # 0 or less disables the cap

``plugins.entries.<id>.settings`` is the *native* plugin-config path — the one
``hermes_cli/plugins.py::PluginContext.get_config`` reads and the only one
``_validate_plugin_config_schema`` type-checks against ``config_schema`` at
load.  A plugin owns no top-level ``config.yaml`` key (those are core sections
registered in ``DEFAULT_CONFIG``), so a hand-rolled ``qq_voice:`` block of our
own would be undeclared, unvalidated and undiscoverable; see :func:`_settings`
for why no fallback to one is kept.

An absent block yields ``{}`` and the defaults below apply, so nothing has to
be registered in ``DEFAULT_CONFIG`` and an operator who never heard of this
plugin still gets the caps.  Resolved fresh on every call
(:func:`resolve_limits`), so raising the budget mid-conversation takes effect
on the next tool call rather than on the next restart.

These are thresholds, not credentials, so they are config keys and **not**
environment variables — see AGENTS.md, "What we don't want": ``.env`` is for
secrets only.  (``ONEBOT_WS_URL`` / ``ONEBOT_ACCESS_TOKEN`` stay in the
environment for the same rubric read the other way round: an endpoint and a
token *are* credentials, and they belong to the OneBot backend this plugin
borrows rather than to these caps.)  The reference deployment spelled the caps
``QQ_VOICE_MAX_TEXT_LEN`` and ``QQ_VOICE_DAILY_LIMIT``; the prefix was only
ever there because they were env vars, and the plugin's settings block already
namespaces them.

``0`` or a negative value means "no cap" for both keys, matching
``plugins/platforms/onebot/rate_limit.py::SlidingWindowCounter.allow``, which
treats ``max_count <= 0`` the same way.  A disabled cap records nothing, also
matching that class.

The counter file
----------------
``<hermes home>/plugin-data/qq_voice/voice_quota.json``, holding
``{"date": "YYYY-MM-DD", "count": N}`` — the same two fields the reference
deployment wrote, so an existing file can be copied straight in.

The directory comes from :func:`plugins.plugin_storage.plugin_data_dir`, which
is this repo's convention for plugin-owned durable state and the one
``plugins/qzone/state.py`` already uses; the reference deployment's
``~/.hermes/state/`` was its own ad-hoc path, and inventing a second location
convention here is exactly what ``plugin_storage``'s docstring exists to stop.
It resolves through ``get_hermes_home()`` on every call, so the budget follows
the active profile instead of leaking across profiles.

The date is the **operator's local day**, not UTC.  A budget that resets at
09:00 local because the box is in UTC+9 is a budget the operator cannot
reason about, and the reference deployment reset locally too.

Failure modes
-------------
A missing, empty, truncated or otherwise unreadable counter file reads as
"nobody has sent anything today" and logs one warning.  Refusing every voice
message because a *counter* is damaged would be a worse outcome than briefly
over-spending a soft budget: the cap protects a bill, and the file is
rewritten whole on the next successful send anyway.

Two tool calls can overlap (the agent runs handlers on worker threads), so
every increment is a read-modify-write under both an in-process lock and an
``flock``'d sidecar, written with ``utils.atomic_json_write`` — the same
pattern, and for the same reason, as ``plugins/qzone/state.py``.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

__all__ = [
    "CONFIG_PATH",
    "CONFIG_SECTION",
    "DEFAULT_DAILY_MAX",
    "DEFAULT_MAX_TEXT_CHARS",
    "PLUGIN_ID",
    "QuotaSnapshot",
    "VoiceLimits",
    "charge_one_send",
    "local_day",
    "quota_path",
    "quota_refusal",
    "quota_snapshot",
    "resolve_limits",
    "spoken_length",
    "text_cap_refusal",
]

#: This plugin's registry id — the ``plugins.entries.<id>`` key the loader
#: files it under, and the ``plugin-data/<id>`` directory it owns.  Must match
#: ``name:`` in ``plugin.yaml``.
PLUGIN_ID = "qq_voice"

#: Where the two caps live in ``config.yaml``: the native plugin settings
#: subtree, not a top-level section of our own.  See the module docstring.
CONFIG_PATH: Tuple[str, ...] = ("plugins", "entries", PLUGIN_ID, "settings")

#: Dotted spelling of :data:`CONFIG_PATH`, for the refusal messages.  An
#: operator who has just been refused wants the path they can grep for in
#: ``config.yaml``, and that is the full one — nothing shorter is real.
CONFIG_SECTION = ".".join(CONFIG_PATH)

#: Ceiling on the ``text`` a voice tool will speak, in perceived characters.
#: 200 is the reference deployment's value and is calibrated for Chinese —
#: roughly a minute of speech, which is already at the outer edge of what a
#: QQ voice bubble can ask of a listener.
DEFAULT_MAX_TEXT_CHARS = 200

#: Voice messages ``qq_send_voice`` may deliver per local day.  30 is the
#: reference deployment's value: comfortably above a normal day's use, low
#: enough that a runaway loop stops costing money within one day.
DEFAULT_DAILY_MAX = 30

#: Counter filename inside the plugin's data directory.
_QUOTA_FILE = "voice_quota.json"

#: Unicode categories that occupy no width of their own: non-spacing and
#: enclosing marks (``Mn``/``Me``) and format characters (``Cf`` — ZWJ,
#: variation selectors, bidi controls).  Excluded from
#: :func:`spoken_length`; see the note there.
_WEIGHTLESS_CATEGORIES = frozenset({"Mn", "Me", "Cf"})

_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _now() -> float:
    """Wall clock, in one indirection so tests can wind it across midnight.

    Patching ``time.time`` itself would reach every other importer in the
    process; this seam reaches only the quota.
    """
    return time.time()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VoiceLimits:
    """The two caps, as resolved for one tool call.

    Either being ``<= 0`` means that cap is off — see the module docstring for
    why that spelling rather than a ``null`` sentinel.
    """

    max_text_chars: int
    daily_max: int

    def text_over_cap(self, length: int) -> bool:
        """Whether ``length`` perceived characters exceeds the text cap."""
        return 0 < self.max_text_chars < length


def _as_int(value: Any, default: int) -> int:
    """Coerce a YAML scalar to an int, falling back on anything unusable.

    Mirrors ``plugins/platforms/onebot/proactive.py::_as_int``: a typo in
    ``config.yaml`` must land on the default rather than crash a tool call.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _settings() -> Dict[str, Any]:
    """This plugin's ``settings`` block from ``config.yaml``, or ``{}``.

    Read with ``cfg_get`` rather than through ``PluginContext.get_config``
    because a tool handler runs long after ``register(ctx)`` returned and
    holds no context; ``plugins/grantley/__init__.py::load_plugin_config`` is
    the same shape for the same reason, and reaches the same subtree.

    :data:`CONFIG_PATH` is deliberately the *only* source.  An earlier
    revision of this branch read a hand-rolled top-level ``qq_voice:``
    section, copying how ``tools/tts_tool.py`` reads ``tts:`` — which is right
    for a core tool and wrong for a plugin.  No release ever carried it, so
    there is nothing installed to migrate and a compatibility fallback would
    buy only a second source of truth: ``config_schema`` cannot describe a
    top-level key, so the loader would stay silent about a typo there while
    this function quietly honoured it.  A stray ``qq_voice:`` block is
    therefore ignored, which is the loud failure (the caps stay at their
    defaults) rather than the quiet one.

    Total by design: an unreadable config must not be the reason a voice
    message fails, and an absent block is the normal case rather than an
    error.
    """
    try:
        from hermes_cli.config import cfg_get, load_config

        settings = cfg_get(load_config(), *CONFIG_PATH, default=None)
    except Exception:  # noqa: BLE001 — defaults are always a usable answer
        logger.debug("qq_voice: could not read %s", CONFIG_SECTION, exc_info=True)
        return {}
    return settings if isinstance(settings, dict) else {}


def resolve_limits() -> VoiceLimits:
    """Read both caps.  Call this per tool call, never cache the result.

    ``load_config`` is itself cached on the config file's mtime and size, so
    re-reading per call costs almost nothing and an operator raising the
    budget sees it apply on the very next call.
    """
    settings = _settings()
    return VoiceLimits(
        max_text_chars=_as_int(settings.get("max_text_chars"), DEFAULT_MAX_TEXT_CHARS),
        daily_max=_as_int(settings.get("daily_max"), DEFAULT_DAILY_MAX),
    )


# ---------------------------------------------------------------------------
# Perceived text length
# ---------------------------------------------------------------------------


def spoken_length(text: str) -> int:
    """How long ``text`` is, counted the way a listener perceives it.

    The cap is a promise about how long the resulting voice bubble will be, so
    it has to count what a reader would count, not what ``len()`` counts.
    ``len()`` counts code points, and the two disagree in exactly the places
    this plugin's traffic lives: ``é`` written as ``e`` + U+0301 is two code
    points and one letter, an emoji with a variation selector or a skin-tone
    modifier is two or three, and Korean text written in decomposed jamo can
    be triple.  Counting those as several characters would refuse a perfectly
    short sentence.

    So: normalise to NFC, then count everything that is not a zero-width
    combining mark or format character.  This is an approximation of grapheme
    clustering, not the real thing — full UAX #29 clustering needs a
    dependency this plugin will not take, and it still over-counts a ZWJ emoji
    sequence such as 👨‍👩‍👧 (3 rather than 1).  That residual error is in the
    safe direction for a cost cap: it refuses slightly sooner than a human
    would, never later.

    Counting the ``text`` argument as given, not the normalised TTS script
    (``common.spoken_script``): the model can predict the former and cannot
    predict the latter, and on the gateway path the script is produced inside
    ``voice.speak`` where this cap cannot see it anyway.
    """
    if not text:
        return 0
    normalized = unicodedata.normalize("NFC", text)
    return sum(
        1
        for char in normalized
        if unicodedata.category(char) not in _WEIGHTLESS_CATEGORIES
    )


# ---------------------------------------------------------------------------
# The daily counter
# ---------------------------------------------------------------------------


def local_day(now: Optional[float] = None) -> str:
    """``YYYY-MM-DD`` for ``now`` in the **operator's local timezone**.

    Deliberately ``time.localtime`` and not ``time.gmtime``: the budget is
    something an operator reasons about in their own day, and a UTC reset
    lands mid-morning or mid-evening for most of the world.  The reference
    deployment reset locally too, and a cutover that silently changed the
    reset hour would look like a broken counter.
    """
    return time.strftime("%Y-%m-%d", time.localtime(_now() if now is None else now))


def quota_path() -> Path:
    """Where the counter lives.  Resolved per call, so it follows the profile."""
    from plugins.plugin_storage import plugin_data_dir  # noqa: PLC0415 — see below

    # Imported lazily: it touches the filesystem (it creates the data dir), and
    # module import of this file happens on every plugin load, including on
    # installs that never call a voice tool.
    return plugin_data_dir(PLUGIN_ID) / _QUOTA_FILE


def _process_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


@contextmanager
def _exclusive(path: Path) -> Iterator[None]:
    """Hold both an in-process and a cross-process lock for ``path``.

    Ported from ``plugins/qzone/state.py::_exclusive`` for the same reason it
    exists there: the mutation is a read-modify-write and two agent worker
    threads (or a CLI call racing the gateway) can be inside it at once, so a
    lost update means a send that was never counted.  The cross-process half
    is a sidecar ``.lock`` file, which leaves the data file free to be
    replaced atomically.  Degrades to the in-process lock alone where neither
    ``fcntl`` nor ``msvcrt`` exists.
    """
    with _process_lock(path):
        lock_path = path.with_suffix(path.suffix + ".lock")
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            yield
            return
        if fcntl is None and msvcrt is None:  # pragma: no cover - exotic platform
            yield
            return
        try:
            handle = open(lock_path, "a+", encoding="utf-8")
        except OSError:  # pragma: no cover - unwritable state dir
            yield
            return
        try:
            if fcntl is not None:
                fcntl.flock(handle, fcntl.LOCK_EX)
            else:  # pragma: no cover - Windows
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            yield
        finally:
            try:
                if fcntl is not None:
                    fcntl.flock(handle, fcntl.LOCK_UN)
                else:  # pragma: no cover - Windows
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            handle.close()


def _read_counter(path: Path) -> Tuple[str, int]:
    """``(date, count)`` from disk; ``("", 0)`` for anything unreadable.

    Fails open on purpose (see the module docstring): a damaged counter must
    degrade the budget, never block every voice message.  A missing file is
    the ordinary first-run case and says nothing, so it warns only about a
    file that exists and cannot be trusted.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "", 0
    except (OSError, ValueError):
        logger.warning("qq_voice: ignoring unreadable voice quota file %s", path)
        return "", 0
    date = raw.get("date") if isinstance(raw, dict) else None
    count = raw.get("count") if isinstance(raw, dict) else None
    # ``bool`` is an ``int`` subclass and ``{"count": true}`` is corruption,
    # not a count of one.
    if (
        not isinstance(date, str)
        or not date
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
    ):
        logger.warning("qq_voice: ignoring malformed voice quota file %s", path)
        return "", 0
    return date, count


def _write_counter(path: Path, day: str, count: int) -> None:
    """Replace the counter file.  Best effort — never fails a delivered send.

    The message is already in the chat by the time this runs; raising here
    would report a successful send as an error and invite a double-post.
    """
    try:
        from utils import atomic_json_write

        atomic_json_write(path, {"date": day, "count": count}, indent=2)
    except Exception as exc:  # noqa: BLE001 — the send already happened
        logger.warning("qq_voice: could not write voice quota file %s (%s)", path, exc)


@dataclass(frozen=True)
class QuotaSnapshot:
    """Today's budget as the model should see it.

    ``limit <= 0`` means the cap is off; ``remaining`` is ``None`` there
    rather than a made-up number.
    """

    used_today: int
    limit: int

    @property
    def remaining(self) -> Optional[int]:
        if self.limit <= 0:
            return None
        return max(0, self.limit - self.used_today)

    @property
    def exhausted(self) -> bool:
        return self.limit > 0 and self.used_today >= self.limit

    def as_dict(self) -> Dict[str, Any]:
        """The ``voice_quota`` block carried in every ``qq_send_voice`` result."""
        return {
            "used_today": self.used_today,
            "limit": self.limit,
            "remaining": self.remaining,
        }


def quota_snapshot(
    limits: VoiceLimits, *, now: Optional[float] = None
) -> QuotaSnapshot:
    """Peek at today's usage without recording anything.

    The pre-check half of the pattern ``SlidingWindowCounter.allow`` documents
    as ``record=False``: check before the expensive work, charge only once the
    message actually went out.  Two overlapping calls can therefore both pass
    at ``limit - 1`` and overshoot by one, which is the right trade — the
    alternative is reserving up front and refunding on failure, and a refund
    that gets lost (crash, kill, unwritable disk) burns budget for a message
    that never existed.  Each increment itself is atomic; only the
    check-then-send window is not.
    """
    if limits.daily_max <= 0:
        return QuotaSnapshot(used_today=0, limit=limits.daily_max)
    day = local_day(now)
    stored_day, count = _read_counter(quota_path())
    return QuotaSnapshot(
        used_today=count if stored_day == day else 0, limit=limits.daily_max
    )


def charge_one_send(
    limits: VoiceLimits, *, now: Optional[float] = None
) -> QuotaSnapshot:
    """Record **one** delivered voice message and return the new snapshot.

    One unit per tool call, not per audio chunk: long synthesized text comes
    back from the TTS backend as several files and goes out as several
    bubbles, but the model asked for one message and the operator's budget is
    stated in messages.

    A disabled cap records nothing at all — no file is created and no disk is
    touched — matching ``SlidingWindowCounter.allow``'s behaviour when its own
    cap is off.
    """
    if limits.daily_max <= 0:
        return QuotaSnapshot(used_today=0, limit=limits.daily_max)
    day = local_day(now)
    path = quota_path()
    with _exclusive(path):
        stored_day, count = _read_counter(path)
        # A stored date that is not today's is yesterday's budget, spent.
        used = (count if stored_day == day else 0) + 1
        _write_counter(path, day, used)
    return QuotaSnapshot(used_today=used, limit=limits.daily_max)


# ---------------------------------------------------------------------------
# Refusal wording — shared so both tools say the same thing
# ---------------------------------------------------------------------------


def text_cap_refusal(length: int, limit: int) -> str:
    """Why a too-long script was not spoken, and what to do instead.

    Written as guidance rather than as a wall: whether to answer in voice at
    all is the persona's own choice, and the useful reply here is "this one is
    better as text", not "denied".
    """
    return (
        f"That is {length} characters, over the {limit}-character voice cap "
        f"({CONFIG_SECTION}.max_text_chars in ~/.hermes/config.yaml). A voice "
        "bubble that long is a chore on QQ — it cannot be skimmed, searched or "
        "re-read. Send this as a normal text message instead, or keep the voice "
        "message to the part that is genuinely worth hearing out loud and put "
        "the rest in text. Whether to use voice at all is still your call."
    )


def quota_refusal(snapshot: QuotaSnapshot) -> str:
    """Why nothing was sent once the day's budget is gone."""
    return (
        f"Today's voice budget is spent: {snapshot.used_today} of "
        f"{snapshot.limit} voice messages already sent "
        f"({CONFIG_SECTION}.daily_max in ~/.hermes/config.yaml). Each one costs "
        "a speech synthesis, so the budget is a real bill rather than a style "
        "rule. It resets at local midnight — send this as a text message."
    )
