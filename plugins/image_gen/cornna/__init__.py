"""Cornna image generation backend.

Exposes the operator's own OpenAI-compatible image endpoint
(``https://api.cornna.xyz/v1`` by default) as an
:class:`ImageGenProvider` implementation.

Shape is identical to ``plugins/image_gen/xai`` — a plain
``POST {base_url}/images/generations`` with a JSON body and a bearer
token — with two differences:

* credentials come from the environment (``CORNNA_IMAGE_API_KEY``)
  rather than a hardcoded resolver, and
* the base URL is configurable, so the same plugin works against a
  moved/mirrored deployment without a code change.

Credentials
-----------
``CORNNA_IMAGE_API_KEY`` **only**. This is deliberately NOT the same key
as ``CORNNA_API_KEY`` (the text/chat-completions credential wired up
under ``providers.cornna``): the endpoint issues separate keys for the
two surfaces, and reusing the chat key here yields a 401.

Base URL selection (first hit wins):

1. ``CORNNA_IMAGE_BASE_URL`` env var
2. ``image_gen.cornna.base_url`` in ``config.yaml``
3. :data:`DEFAULT_BASE_URL`

Model selection (first hit wins):

1. ``CORNNA_IMAGE_MODEL`` env var (accepts any id the endpoint serves,
   not just the catalog below — the endpoint's model list can grow)
2. ``image_gen.cornna.model`` in ``config.yaml``
3. the ``model`` kwarg the tool layer forwards from ``image_gen.model``
   (accepted only when it names a known image model, so a text-model id
   left in that shared config key can't leak into an image request)
4. :data:`DEFAULT_MODEL`

Reference-anchored generation
-----------------------------
Passing reference images switches the provider off ``/images/generations``
and onto one of two shapes, selected by ``image_gen.cornna.refs_backend``:

``responses`` (default)
    ``POST {base_url}/responses`` — a ``gpt-5.5`` host model with the
    ``image_generation`` tool forced on, and the references attached as
    ``input_image`` data URLs. This is the shape ``personal_hermes``'s
    ``image_with_refs`` used, and it *composes a new scene* that inherits
    the reference's markings/colors/clothing rather than editing the
    reference itself.

``edits``
    ``POST {base_url}/images/edits`` — plain multipart, references in the
    ``image`` field. Closer to an edit of the source than a new scene.

Both are exercised against the live endpoint. Which one is right depends
on whether the caller wants "a new picture *of* this character" (the
former) or "this picture, changed" (the latter).

References come in three forms, mixed freely in ``reference_image_urls``:
a ``character:<short name>`` entry resolved through the allowlist in
:mod:`.characters`, an ``http(s)://`` / ``data:`` URL, or a local path.
``reference_characters=["grantley", ...]`` is the ergonomic spelling of
the first form for in-process callers.

Known limitation: ``size`` is not honored once a reference is attached
------------------------------------------------------------------------
Verified against the live endpoint (2026-08-19), against real production
credentials and a real 立绘: once a reference image is attached, **both**
``responses`` and ``edits`` silently ignore the requested ``size`` and
return the endpoint's own choice of pixel dimensions instead. Without any
mitigation, ``aspect_ratio="square"`` and ``aspect_ratio="landscape"``
both came back a portrait image matching the reference's own orientation
(962x1634/962x1635) — i.e. the request's aspect_ratio had no effect at
all. Pre-padding the reference onto the target canvas was tried and
rejected: a reference letterboxed onto an exact 1536x1024 landscape frame
came back 1024x1536 anyway — the model reads the character's silhouette
inside the padding, not the canvas, and can pick the *wrong* orientation
outright.

What does move the needle: telling the model the required orientation in
the prompt text (see :data:`_REF_ASPECT_INSTRUCTIONS`, applied whenever a
named character is present). The same landscape request, with that
instruction prepended, came back genuinely landscape (1672x941, ~16:9) on
both backends. This is steering, not a pin — the endpoint still chooses
its own exact pixel dimensions, never the literal ``size`` string — so
:func:`generate` measures every reference-anchored result with Pillow
before returning it and compares the actual landscape/square/portrait
class against what was requested. A mismatch is never silent: it is
logged at WARNING and surfaced as ``aspect_mismatch: True`` /
``actual_size`` in the response (see :func:`_flag_if_aspect_mismatch`).
Also declared in :meth:`CornnaImageGenProvider.capabilities` as
``reference_size_pinned: False``.

The reference-free ``/images/generations`` path above is unaffected — its
48 tests are all built on ``size`` actually being honored, which it is
when there is no reference image in the request.

Response handling
-----------------
The endpoint returns ``{"data": [{"b64_json": ..., "revised_prompt": ...}],
"usage": {...}}`` — inline base64, no ``url``. The base64 is decoded to
``$HERMES_HOME/cache/images/`` via :func:`save_b64_image` and the
absolute path is returned as ``image``; a ``url`` field is still handled
as a fallback in case the endpoint's response shape changes. Both forms
satisfy ``plugins/qzone/publish.py::_load_image_reference``, which
accepts an ``http(s)://`` URL or an existing local file path.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

import requests

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    normalize_reference_images,
    resolve_aspect_ratio,
    save_b64_image,
    save_url_image,
    success_response,
)
from agent.secret_scope import get_secret

from .characters import (
    CHARACTER_KEYS,
    InvalidCharacterAssetError,
    MissingCharacterAssetError,
    UnknownCharacterError,
    available_characters,
    load_character_image,
)

logger = logging.getLogger(__name__)

# Re-exported so callers and the picker can read the character allowlist
# without reaching into the submodule.
__all__ = [
    "CHARACTER_KEYS",
    "CornnaImageGenProvider",
    "MAX_REFERENCE_IMAGES",
    "RECOMMENDED_REFERENCE_IMAGES",
    "available_characters",
    "register",
]


# ---------------------------------------------------------------------------
# Constants / catalog
# ---------------------------------------------------------------------------

PROVIDER_NAME = "cornna"

API_KEY_ENV = "CORNNA_IMAGE_API_KEY"
BASE_URL_ENV = "CORNNA_IMAGE_BASE_URL"
MODEL_ENV = "CORNNA_IMAGE_MODEL"
SIZE_ENV = "CORNNA_IMAGE_SIZE"

DEFAULT_BASE_URL = "https://api.cornna.xyz/v1"

# Catalog verified against ``GET /v1/models`` on the live endpoint: of the
# 20 ids it serves, exactly these three are image models. (There is no
# model called ``image2`` — "image2" colloquially means ``gpt-image-2``.)
_MODELS: Dict[str, Dict[str, Any]] = {
    "gpt-image-2": {
        "display": "GPT Image 2",
        "speed": "~20-40s",
        "strengths": "Default. Strongest prompt adherence of the three.",
    },
    "gpt-image-1.5": {
        "display": "GPT Image 1.5",
        "speed": "~15-30s",
        "strengths": "Previous generation; kept for parity with the endpoint catalog.",
    },
    "gpt-image-1": {
        "display": "GPT Image 1",
        "speed": "~10-25s",
        "strengths": "Oldest of the three; cheapest fallback.",
    },
}

DEFAULT_MODEL = "gpt-image-2"

# The tool surface only speaks landscape/square/portrait
# (``agent.image_gen_provider.VALID_ASPECT_RATIOS``); translate to the
# OpenAI-style ``size`` string the endpoint expects. Callers name the
# aspect, never the pixel string — there is exactly one table and this is
# it.
#
# These three values are the ones ``personal_hermes``'s ``image_with_refs``
# ran against this same endpoint for the daily 说说 job, so all three are
# authoritative rather than assumed. The 400-retry below predates that and
# is kept as a cheap safety net for a future endpoint that narrows the set;
# it never fires on a size an operator pinned deliberately.
_SIZES = {
    "landscape": "1536x1024",
    "square": "1024x1024",
    "portrait": "1024x1536",
}

VERIFIED_SIZE = "1024x1024"

REQUEST_TIMEOUT = 180

# ---------------------------------------------------------------------------
# Reference-anchored generation
# ---------------------------------------------------------------------------

REFS_BACKEND_ENV = "CORNNA_IMAGE_REFS_BACKEND"

#: ``responses`` builds a new scene anchored on the references; ``edits``
#: edits the references themselves. Both work against the live endpoint.
REFS_BACKENDS: Tuple[str, ...] = ("responses", "edits")
DEFAULT_REFS_BACKEND = "responses"

#: The Responses-API host model that issues the ``image_generation`` tool
#: call. The picture itself is drawn by the resolved image model
#: (:data:`DEFAULT_MODEL`); this one is only the wrapper. Overridable via
#: ``image_gen.cornna.refs_chat_model`` because the endpoint's chat catalog
#: moves independently of its image catalog.
REFS_CHAT_MODEL = "gpt-5.5"

REFS_QUALITY = "medium"

#: Hard cap on references per request. Four is the ceiling the old
#: implementation set and the number this provider advertises, but it is a
#: ceiling and not a target: more references mean a larger payload, a longer
#: generation, and — the part callers get wrong — a model that starts
#: confusing the characters with each other and drifting off the prompt.
#: One to three reads best in practice; :data:`RECOMMENDED_REFERENCE_IMAGES`
#: is what the capability surface recommends and what the over-cap error
#: points people back at.
MAX_REFERENCE_IMAGES = 4
RECOMMENDED_REFERENCE_IMAGES = 3

#: Prefix that turns a ``reference_image_urls`` entry into an allowlist
#: lookup instead of a fetch. ``character:grantley`` is the only way model-
#: reachable input can name a 立绘 — it never names a path.
CHARACTER_REF_PREFIX = "character:"

#: A reference-anchored render is minutes, not seconds: ~40s for one
#: reference at medium quality, and it climbs with quality and with each
#: extra reference. The old implementation budgeted 2-5 minutes; 10 is the
#: ceiling so a wedged request still eventually returns a structured error.
REFS_REQUEST_TIMEOUT = 600

#: Reference fetches are ordinary downloads and get an ordinary budget.
REF_FETCH_TIMEOUT = 60

#: Image generation is flaky on any backend — a transport hiccup or a 5xx
#: mid-render is common enough that one-shot failure is the wrong default.
#: Only retryable outcomes (timeout, connection reset, 5xx, an empty result)
#: are retried; a 4xx is the caller's problem and is surfaced immediately.
GENERATION_MAX_ATTEMPTS = 3
GENERATION_RETRY_SLEEP_SECONDS = 8

#: Prepended to the caller's prompt whenever references are attached. This
#: is the anchoring instruction — without it the model treats the reference
#: as a mood board and redesigns the character. Deliberately style-neutral:
#: the old implementation also baked in a kemono-anime house style and a
#: page of composition direction for the 说说 job, which belongs in that
#: job's prompt, not in a general-purpose provider.
_REF_ANCHOR_PREAMBLE = (
    "You are given character reference portraits. For each character that "
    "appears in the scene below, strictly match their species, fur or skin "
    "color and markings, eye color, ear and hair shape, and clothing from "
    "their reference image. Render them performing the scene's action in "
    "the described setting.\n\n"
)

_REFS_INSTRUCTIONS = (
    "You are an assistant that must fulfill image generation requests by "
    "using the image_generation tool when provided."
)

#: Verified against the live endpoint (2026-08-19): once a reference image
#: is attached, the ``image_generation`` tool's ``size`` field is silently
#: ignored — both the ``responses`` and ``edits`` backends returned the raw
#: reference's own pixel dimensions (a portrait 立绘) for a *square* and a
#: *landscape* request alike. Pre-padding the reference to the target
#: canvas was tried and tested worse than doing nothing: a reference
#: letterboxed onto an exact 1536x1024 landscape canvas came back
#: 1024x1536 — the model reasoned from the character's own (portrait)
#: silhouette inside the padding rather than the canvas, and picked the
#: *wrong* orientation outright.
#:
#: What does work: telling the model the required orientation in the
#: prompt itself. The same landscape request, with this instruction
#: prepended, twice came back genuinely landscape (1672x941, ~16:9) on
#: both backends. It is not a `size` pin — the endpoint still chooses its
#: own exact pixel dimensions — so this is steering, not control; see
#: :func:`_measure_and_flag_aspect_mismatch` for the mandatory backstop
#: that catches it when the steering doesn't take.
_REF_ASPECT_INSTRUCTIONS: Dict[str, str] = {
    "landscape": (
        "The output image MUST be landscape-oriented: significantly wider "
        "than it is tall (a wide, horizontal frame), regardless of the "
        "reference portraits' own orientation.\n\n"
    ),
    "square": (
        "The output image MUST be perfectly square: width equal to height "
        "(a 1:1 frame), regardless of the reference portraits' own "
        "orientation.\n\n"
    ),
    "portrait": (
        "The output image MUST be portrait-oriented: significantly taller "
        "than it is wide (a tall, vertical frame), regardless of the "
        "reference portraits' own orientation.\n\n"
    ),
}

#: Aspect classes close enough to 1:1 count as "square" on both sides of
#: the comparison — matches how a caller reasons about the three named
#: aspects, not a pixel-perfect ratio check.
_SQUARE_RATIO_TOLERANCE = 0.05


# ---------------------------------------------------------------------------
# Config / credential resolution
# ---------------------------------------------------------------------------


def _load_cornna_image_config() -> Dict[str, Any]:
    """Read ``image_gen.cornna`` from config.yaml (``{}`` on any failure)."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        section = cfg.get("image_gen") if isinstance(cfg, dict) else None
        sub = section.get(PROVIDER_NAME) if isinstance(section, dict) else None
        return sub if isinstance(sub, dict) else {}
    except Exception as exc:  # noqa: BLE001 — config is best-effort
        logger.debug("Could not load image_gen.cornna config: %s", exc)
        return {}


def _resolve_api_key() -> str:
    """Return the image API key, or ``""``.

    Routed through :func:`agent.secret_scope.get_secret` so a multiplexed
    gateway turn reads the *active profile's* key rather than whatever the
    process environment happens to hold. Never raises — an unscoped read
    under multiplexing raises ``UnscopedSecretError``, and a provider
    availability probe must not blow up the picker.
    """
    try:
        return str(get_secret(API_KEY_ENV, "") or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not resolve %s: %s", API_KEY_ENV, exc)
        return ""


def _resolve_base_url(cfg: Optional[Dict[str, Any]] = None) -> str:
    """Return the endpoint base URL (env > config > default), no trailing slash."""
    env_override = os.environ.get(BASE_URL_ENV, "").strip()
    if env_override:
        return env_override.rstrip("/")
    if cfg is None:
        cfg = _load_cornna_image_config()
    candidate = cfg.get("base_url") if isinstance(cfg, dict) else None
    if isinstance(candidate, str) and candidate.strip():
        return candidate.strip().rstrip("/")
    return DEFAULT_BASE_URL


def _resolve_model(
    override: Optional[str] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Decide which model to use and return ``(model_id, meta)``.

    ``override`` is the ``model`` kwarg the tool layer forwards from the
    shared ``image_gen.model`` config key; it is honored only when it
    names a model in :data:`_MODELS`.
    """
    env_override = os.environ.get(MODEL_ENV, "").strip()
    if env_override:
        # Deliberately not restricted to _MODELS: the endpoint's catalog can
        # grow, and an operator pinning a new id shouldn't need a code change.
        return env_override, _MODELS.get(env_override, {})

    if cfg is None:
        cfg = _load_cornna_image_config()
    cfg_model = cfg.get("model") if isinstance(cfg, dict) else None
    if isinstance(cfg_model, str) and cfg_model.strip():
        value = cfg_model.strip()
        return value, _MODELS.get(value, {})

    if isinstance(override, str) and override.strip() in _MODELS:
        value = override.strip()
        return value, _MODELS[value]

    return DEFAULT_MODEL, _MODELS[DEFAULT_MODEL]


def _resolve_size(
    aspect: str, cfg: Optional[Dict[str, Any]] = None
) -> Tuple[str, bool]:
    """Return ``(size, pinned)`` for an aspect ratio.

    ``pinned`` is True when the operator explicitly chose the size (env or
    config), in which case ``generate()`` must NOT silently rewrite it to
    :data:`VERIFIED_SIZE` on a 400.
    """
    env_size = os.environ.get(SIZE_ENV, "").strip()
    if env_size:
        return env_size, True
    if cfg is None:
        cfg = _load_cornna_image_config()
    cfg_size = cfg.get("size") if isinstance(cfg, dict) else None
    if isinstance(cfg_size, str) and cfg_size.strip():
        return cfg_size.strip(), True
    return _SIZES.get(aspect, VERIFIED_SIZE), False


def _resolve_refs_backend(cfg: Optional[Dict[str, Any]] = None) -> str:
    """Return the reference backend name (env > config > default).

    Raises :class:`ValueError` on an unrecognised value rather than quietly
    picking one: the two backends produce materially different pictures
    from the same inputs, so silently substituting the other would look
    like a model regression rather than a typo in ``config.yaml``.
    """
    raw = os.environ.get(REFS_BACKEND_ENV, "").strip()
    source = REFS_BACKEND_ENV
    if not raw:
        if cfg is None:
            cfg = _load_cornna_image_config()
        candidate = cfg.get("refs_backend") if isinstance(cfg, dict) else None
        raw = candidate.strip() if isinstance(candidate, str) else ""
        source = "image_gen.cornna.refs_backend"
    if not raw:
        return DEFAULT_REFS_BACKEND
    value = raw.lower()
    if value not in REFS_BACKENDS:
        raise ValueError(
            f"{source}={raw!r} is not a known reference backend. "
            f"Valid values: {', '.join(REFS_BACKENDS)}."
        )
    return value


def _resolve_refs_chat_model(cfg: Optional[Dict[str, Any]] = None) -> str:
    """Host chat model for the ``responses`` backend (config > default)."""
    if cfg is None:
        cfg = _load_cornna_image_config()
    candidate = cfg.get("refs_chat_model") if isinstance(cfg, dict) else None
    if isinstance(candidate, str) and candidate.strip():
        return candidate.strip()
    return REFS_CHAT_MODEL


# ---------------------------------------------------------------------------
# Reference loading
# ---------------------------------------------------------------------------


class _RefImage(NamedTuple):
    """One loaded reference: bytes plus what the wire formats need."""

    character: Optional[str]  # short name, or None for a URL/path reference
    data: bytes
    filename: str
    mime: str

    def data_url(self) -> str:
        return f"data:{self.mime};base64,{base64.b64encode(self.data).decode('ascii')}"


_IMAGE_FORMATS = {
    "PNG": ("image/png", "png"),
    "JPEG": ("image/jpeg", "jpg"),
    "WEBP": ("image/webp", "webp"),
    "GIF": ("image/gif", "gif"),
}


def _validate_image_bytes(data: bytes, source: str) -> Tuple[str, str]:
    """Return the actual ``(mime, extension)`` for a complete raster image.

    Reference URLs, data URLs, local files, and provider output are all
    untrusted bytes. Do not rely on a filename or a response Content-Type:
    those can describe an HTML error page or a truncated image just as easily
    as a real reference. Pillow is a core Hermes dependency, so validation is
    deterministic in every supported runtime.
    """
    if not isinstance(data, bytes) or not data:
        raise ValueError(f"{source} returned no image bytes")
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            image_format = (image.format or "").upper()
            image.verify()
    except Exception as exc:  # noqa: BLE001 - decoder exceptions differ by format
        raise ValueError(f"{source} is not a valid image: {exc}") from exc

    result = _IMAGE_FORMATS.get(image_format)
    if result is None:
        raise ValueError(f"{source} uses unsupported image format {image_format!r}")
    return result


def _load_ref_source(ref: str, cfg: Optional[Dict[str, Any]] = None) -> _RefImage:
    """Load one reference into memory.

    ``character:<short name>`` goes through the allowlist in
    :mod:`.characters` and never touches the filesystem until the name has
    been recognised. Everything else is an ordinary source image — URL,
    ``data:`` URL, or local path — handled the same way the bundled OpenAI
    plugin handles them, credential read-guard included.
    """
    value = str(ref or "").strip()
    lower = value.lower()

    if lower.startswith(CHARACTER_REF_PREFIX):
        name = value[len(CHARACTER_REF_PREFIX) :].strip()
        data, filename, mime = load_character_image(name, cfg=cfg)
        return _RefImage(name.lower(), data, filename, mime)

    if lower.startswith(("http://", "https://")):
        response = requests.get(value, timeout=REF_FETCH_TIMEOUT)
        response.raise_for_status()
        filename = value.split("?", 1)[0].rsplit("/", 1)[-1] or "reference.png"
        data = response.content
        mime, _extension = _validate_image_bytes(data, f"reference URL {value}")
        return _RefImage(None, data, filename, mime)

    if lower.startswith("data:"):
        header, _, payload = value.partition(",")
        header_lower = header.lower()
        if not header_lower.startswith("data:image/") or ";base64" not in header_lower:
            raise ValueError(
                "reference data URL must be a base64-encoded image/* value"
            )
        data = base64.b64decode(payload, validate=True)
        mime, _extension = _validate_image_bytes(data, "reference data URL")
        return _RefImage(None, data, "reference.png", mime)

    # Local path. Same guard the OpenAI plugin applies, so an edit source
    # can't be used to exfiltrate a credential file the agent may not read.
    from agent.file_safety import raise_if_read_blocked  # noqa: PLC0415 — call-time guard

    raise_if_read_blocked(value)
    with open(value, "rb") as handle:
        data = handle.read()
    filename = os.path.basename(value) or "reference.png"
    mime, _extension = _validate_image_bytes(data, f"reference file {value}")
    return _RefImage(None, data, filename, mime)


def _build_ref_prompt(prompt: str, refs: Sequence[_RefImage], aspect: str) -> str:
    """Prefix the caller's scene prompt with the anchoring instruction.

    Only when at least one reference is a named character. An ordinary
    image-to-image call ("make this photo look like winter") gets its prompt
    through verbatim — telling that request to match a character's species
    and fur markings would actively steer it wrong, and forcing an
    orientation on it would fight the "edit this photo" intent the same
    way (see the module docstring's known-limitation note on ``size``).

    The named-character path also gets an explicit orientation instruction
    (:data:`_REF_ASPECT_INSTRUCTIONS`) ahead of the scene text. This is a
    verified-necessary workaround, not decoration: the endpoint ignores the
    ``size`` field outright once a reference is attached, and a request for
    a different aspect than the reference's own silently came back at the
    reference's dimensions until the prompt said otherwise.
    """
    named = [ref.character for ref in refs if ref.character]
    if not named:
        return prompt
    intro = _REF_ANCHOR_PREAMBLE
    intro += f"Reference characters provided in order: {', '.join(named)}.\n\n"
    intro += _REF_ASPECT_INSTRUCTIONS.get(aspect, "")
    return f"{intro}Scene:\n\n{prompt}"


def _image_b64_from_responses_output(body: Any) -> Optional[str]:
    """Pull the ``image_generation_call`` result out of a ``/responses`` body.

    This provider calls ``/responses`` non-streaming, so the body in hand is
    already the terminal document and one sweep over ``output`` is the whole
    story. The streaming port this is derived from had to re-scan
    ``final.output`` *after* draining its event loop, because the
    ``response.output_item.done`` event carrying the image could land after
    the iterator was exhausted; that race has no analogue here, so the
    second pass is deliberately absent rather than forgotten.
    """
    output = body.get("output") if isinstance(body, dict) else None
    if not isinstance(output, list):
        return None
    found: Optional[str] = None
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "image_generation_call":
            continue
        result = item.get("result")
        if isinstance(result, str) and result:
            found = result  # last non-empty wins, as in the streaming original
    return found


def _error_message(response: Optional[Any]) -> str:
    """Best-effort human-readable message out of an error response."""
    if response is None:
        return ""
    try:
        body = response.json()
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict) and err.get("message"):
                return str(err["message"])
            if isinstance(err, str) and err:
                return err
            if body.get("message"):
                return str(body["message"])
    except Exception:  # noqa: BLE001 — fall through to raw text
        pass
    text = getattr(response, "text", "") or ""
    return str(text)[:300]


def _materialise_image(
    entry: Dict[str, Any],
    prefix: str,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Turn one ``data[]`` entry into a validated local cache path.

    Returns ``(image_ref, error_type, error_message)`` with exactly one of
    the first and the last two populated. Shared by every request shape this
    provider issues so they can't drift on how a result gets cached.
    """
    b64 = entry.get("b64_json")
    url = entry.get("url")

    if b64:
        try:
            raw = base64.b64decode(b64, validate=True)
            _mime, extension = _validate_image_bytes(raw, "Cornna response image")
        except Exception as exc:  # noqa: BLE001 - malformed provider output is not success
            return None, "invalid_response", f"Cornna returned an invalid image: {exc}"
        try:
            return (
                str(save_b64_image(b64, prefix=prefix, extension=extension)),
                None,
                None,
            )
        except Exception as exc:  # noqa: BLE001
            return None, "io_error", f"Could not save image to cache: {exc}"

    if url:
        try:
            cached = Path(save_url_image(url, prefix=prefix))
        except Exception as exc:  # noqa: BLE001
            return None, "io_error", f"Could not cache Cornna image URL: {exc}"
        try:
            _validate_image_bytes(cached.read_bytes(), "Cornna response image URL")
        except Exception as exc:  # noqa: BLE001
            try:
                cached.unlink(missing_ok=True)
            except OSError:
                pass
            return (
                None,
                "invalid_response",
                f"Cornna returned an invalid image URL: {exc}",
            )
        return str(cached), None, None

    return None, "empty_response", "Cornna response contained neither b64_json nor url"


def _measure_image_size(image_ref: str) -> Optional[Tuple[int, int]]:
    """Return ``(width, height)`` of a materialised image, or ``None``.

    :func:`_materialise_image` validates and caches every successful image,
    so ``image_ref`` is always a local path. This helper deliberately does
    not re-open a remote URL or turn a measurement failure into a generation
    failure.

    Never raises: a measurement failure (corrupt bytes, missing Pillow)
    must not fail an otherwise-successful generation.
    """
    if not image_ref or image_ref.startswith(("http://", "https://")):
        return None
    try:
        from PIL import Image

        with Image.open(image_ref) as im:
            return im.size
    except Exception as exc:  # noqa: BLE001 — measurement is best-effort
        logger.debug("Cornna: could not measure returned image %s: %s", image_ref, exc)
        return None


def _classify_aspect(width: int, height: int) -> Optional[str]:
    """Classify pixel dimensions into ``landscape`` / ``square`` / ``portrait``.

    Same three buckets :data:`_SIZES` uses, so the result is directly
    comparable to a caller's ``aspect_ratio``. ``None`` on a degenerate
    (zero or negative) dimension.
    """
    if width <= 0 or height <= 0:
        return None
    ratio = width / height
    if abs(ratio - 1.0) <= _SQUARE_RATIO_TOLERANCE:
        return "square"
    return "landscape" if ratio > 1.0 else "portrait"


def _flag_if_aspect_mismatch(
    image_ref: str,
    requested_aspect: str,
    requested_size: str,
    extra: Dict[str, Any],
) -> None:
    """Measure the returned image and flag it in-place if the aspect is wrong.

    Mandatory backstop for the reference-anchored path: this endpoint has
    been verified to ignore ``size`` once a reference is attached (see the
    module docstring and :data:`_REF_ASPECT_INSTRUCTIONS`), and the prompt
    instruction that steers it is exactly that — steering, not a pin. When
    the returned image's aspect class doesn't match what was asked for,
    this logs a WARNING and sets ``extra["aspect_mismatch"] = True`` so a
    wrong-aspect image is never handed back silently. ``extra["actual_size"]``
    is set whenever the image could be measured at all, mismatch or not.
    """
    size = _measure_image_size(image_ref)
    if size is None:
        return
    width, height = size
    extra["actual_size"] = f"{width}x{height}"
    actual_aspect = _classify_aspect(width, height)
    if actual_aspect is not None and actual_aspect != requested_aspect:
        logger.warning(
            "Cornna reference-anchored image aspect mismatch: requested "
            "aspect_ratio=%s (size=%s) but the endpoint returned %dx%d "
            "(%s). This endpoint does not reliably honor size/aspect once "
            "a reference image is attached — see capabilities() and the "
            "module docstring.",
            requested_aspect,
            requested_size,
            width,
            height,
            actual_aspect,
        )
        extra["aspect_mismatch"] = True


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class CornnaImageGenProvider(ImageGenProvider):
    """OpenAI-compatible ``/images/generations`` backend on api.cornna.xyz."""

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @property
    def display_name(self) -> str:
        return "Cornna"

    def is_available(self) -> bool:
        """True only when the image key is present. Never raises."""
        try:
            return bool(_resolve_api_key())
        except Exception as exc:  # noqa: BLE001 — availability probes must not throw
            logger.debug("cornna image provider availability check failed: %s", exc)
            return False

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": model_id,
                "display": meta.get("display", model_id),
                "speed": meta.get("speed", ""),
                "strengths": meta.get("strengths", ""),
            }
            for model_id, meta in _MODELS.items()
        ]

    def default_model(self) -> Optional[str]:
        return DEFAULT_MODEL

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Cornna (self-hosted)",
            "badge": "paid",
            "tag": (
                "gpt-image-2 / 1.5 / 1 via an OpenAI-compatible endpoint. "
                "Uses its own key — NOT the CORNNA_API_KEY used for chat."
            ),
            "env_vars": [
                {
                    "key": API_KEY_ENV,
                    "prompt": "Cornna image API key (separate from CORNNA_API_KEY)",
                    "url": DEFAULT_BASE_URL,
                },
            ],
        }

    def capabilities(self) -> Dict[str, Any]:
        # Both reference backends have now been exercised against the live
        # endpoint, so image-conditioned generation is declared for real.
        # ``max_reference_images`` is the enforced ceiling, not the advice —
        # see MAX_REFERENCE_IMAGES for why the two differ.
        return {
            "modalities": ["text", "image"],
            "max_reference_images": MAX_REFERENCE_IMAGES,
            "recommended_reference_images": RECOMMENDED_REFERENCE_IMAGES,
            # Known endpoint limitation, verified live: once a reference
            # image is attached, `size` is ignored on both refs backends —
            # the endpoint picks its own exact pixel dimensions regardless
            # of what was requested. A prompt-level orientation instruction
            # (_REF_ASPECT_INSTRUCTIONS) steers it toward the requested
            # landscape/square/portrait class in practice, but never pins
            # exact pixels, and generate() logs a WARNING and sets
            # `aspect_mismatch` in the response whenever the steering
            # doesn't take. Text-to-image (no reference) is unaffected —
            # `size` is honored there.
            "reference_size_pinned": False,
        }

    def available_characters(self) -> List[str]:
        """Short names whose 立绘 is on this box — see :mod:`.characters`.

        Exposed on the provider so an upper layer can decide whether
        reference-anchored generation is possible at all without importing
        the registry module directly.
        """
        return available_characters()

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        *,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
        reference_characters: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Generate an image, optionally anchored on character references.

        ``reference_characters`` is the caller-facing way to name 立绘:
        short names only, resolved through the allowlist in
        :mod:`.characters`. It is a deliberate parameter rather than
        provider-side inference — the old ``image_with_refs`` tool let the
        *model* choose which characters a picture needed, and a tool wrapper
        over this method can hand that choice straight through. Nothing here
        guesses a cast or defaults one in.

        ``reference_image_urls`` accepts the same names spelled
        ``character:<short name>``, alongside ordinary URLs and paths, so
        the existing ``image_generate`` tool schema can reach the registry
        without a schema change.
        """
        prompt = (prompt or "").strip()
        aspect = resolve_aspect_ratio(aspect_ratio)

        if not prompt:
            return error_response(
                error="Prompt is required and must be a non-empty string",
                error_type="invalid_argument",
                provider=PROVIDER_NAME,
                aspect_ratio=aspect,
            )

        # Collect every reference the caller gave us, in call order:
        # primary source image, then explicit reference URLs/paths, then
        # named characters. ``characters`` is accepted as an alias because
        # that is what the old tool's schema called the field.
        sources: List[str] = []
        if isinstance(image_url, str) and image_url.strip():
            sources.append(image_url.strip())
        sources.extend(normalize_reference_images(reference_image_urls) or [])
        named = normalize_reference_images(reference_characters)
        if named is None:
            named = normalize_reference_images(kwargs.get("characters"))
        sources.extend(f"{CHARACTER_REF_PREFIX}{name}" for name in named or [])

        api_key = _resolve_api_key()
        if not api_key:
            return error_response(
                error=(
                    f"{API_KEY_ENV} not set. Export the Cornna *image* key "
                    f"(it is a different key from CORNNA_API_KEY, which is "
                    f"the chat credential) and retry."
                ),
                error_type="auth_required",
                provider=PROVIDER_NAME,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        cfg = _load_cornna_image_config()
        base_url = _resolve_base_url(cfg)
        model_id, _meta = _resolve_model(kwargs.get("model"), cfg)
        size, size_pinned = _resolve_size(aspect, cfg)

        if sources:
            return self._generate_with_refs(
                prompt=prompt,
                aspect=aspect,
                sources=sources,
                cfg=cfg,
                api_key=api_key,
                base_url=base_url,
                model_id=model_id,
                size=size,
            )

        endpoint_url = f"{base_url}/images/generations"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        size_fallback_used = False
        attempted_sizes: List[str] = []
        last_error = "Cornna image generation failed"
        last_error_type = "api_error"
        result: Dict[str, Any] = {}
        first: Dict[str, Any] = {}
        image_ref: Optional[str] = None

        for attempt in range(1, GENERATION_MAX_ATTEMPTS + 1):
            payload: Dict[str, Any] = {
                "model": model_id,
                "prompt": prompt,
                "n": 1,
                "size": size,
            }
            attempted_sizes.append(size)
            try:
                response = requests.post(
                    endpoint_url,
                    headers=headers,
                    json=payload,
                    timeout=REQUEST_TIMEOUT,
                )
            except requests.Timeout:
                last_error = f"Cornna image generation timed out ({REQUEST_TIMEOUT}s)"
                last_error_type = "timeout"
            except requests.ConnectionError as exc:
                last_error = f"Cornna connection error: {exc}"
                last_error_type = "connection_error"
            except requests.RequestException as exc:
                last_error = f"Cornna image generation request failed: {exc}"
                last_error_type = "api_error"
            else:
                status = int(getattr(response, "status_code", 0) or 0)
                if 200 <= status < 300:
                    try:
                        raw_result = response.json()
                    except Exception as exc:  # noqa: BLE001 - decoder errors are terminal
                        return error_response(
                            error=f"Cornna returned invalid JSON: {exc}",
                            error_type="invalid_response",
                            provider=PROVIDER_NAME,
                            model=model_id,
                            prompt=prompt,
                            aspect_ratio=aspect,
                        )
                    data = (
                        raw_result.get("data") if isinstance(raw_result, dict) else None
                    )
                    if not data or not isinstance(data, list):
                        last_error = "Cornna returned no image data"
                        last_error_type = "empty_response"
                    else:
                        candidate = data[0] if isinstance(data[0], dict) else {}
                        prefix = f"cornna_{str(model_id).replace('/', '_').replace(':', '_')}"
                        image_ref, err_type, err_msg = _materialise_image(
                            candidate, prefix
                        )
                        if image_ref is not None:
                            result = raw_result
                            first = candidate
                            break
                        if err_type != "empty_response":
                            return error_response(
                                error=err_msg or "Cornna returned no usable image",
                                error_type=err_type or "invalid_response",
                                provider=PROVIDER_NAME,
                                model=model_id,
                                prompt=prompt,
                                aspect_ratio=aspect,
                            )
                        last_error = err_msg or "Cornna returned no usable image"
                        last_error_type = "empty_response"
                elif (
                    status == 400
                    and not size_pinned
                    and not size_fallback_used
                    and size != VERIFIED_SIZE
                ):
                    logger.warning(
                        "Cornna rejected size %s (400); retrying at the verified %s",
                        size,
                        VERIFIED_SIZE,
                    )
                    size = VERIFIED_SIZE
                    size_fallback_used = True
                    continue
                else:
                    err_msg = _error_message(response)
                    last_error = f"Cornna image generation failed ({status}): {err_msg}"
                    last_error_type = "api_error"
                    if 400 <= status < 500:
                        logger.error(
                            "Cornna image gen failed (%s): %s", status, err_msg
                        )
                        return error_response(
                            error=last_error,
                            error_type=last_error_type,
                            provider=PROVIDER_NAME,
                            model=model_id,
                            prompt=prompt,
                            aspect_ratio=aspect,
                        )

            logger.warning(
                "Cornna text generation attempt %d/%d failed: %s",
                attempt,
                GENERATION_MAX_ATTEMPTS,
                last_error,
            )
            if attempt < GENERATION_MAX_ATTEMPTS:
                time.sleep(GENERATION_RETRY_SLEEP_SECONDS)
        else:
            return error_response(
                error=last_error,
                error_type=last_error_type,
                provider=PROVIDER_NAME,
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        extra: Dict[str, Any] = {"size": size}
        if size_fallback_used:
            extra["size_fallback"] = True
            extra["requested_size"] = attempted_sizes[0]
        revised = first.get("revised_prompt")
        if isinstance(revised, str) and revised:
            extra["revised_prompt"] = revised
        if isinstance(result, dict) and result.get("usage"):
            extra["usage"] = result["usage"]

        return success_response(
            image=image_ref,
            model=model_id,
            prompt=prompt,
            aspect_ratio=aspect,
            provider=PROVIDER_NAME,
            modality="text",
            extra=extra,
        )

    # -- reference-anchored path ------------------------------------------

    def _generate_with_refs(
        self,
        *,
        prompt: str,
        aspect: str,
        sources: List[str],
        cfg: Dict[str, Any],
        api_key: str,
        base_url: str,
        model_id: str,
        size: str,
    ) -> Dict[str, Any]:
        """Run a reference-anchored generation through the selected backend."""

        def fail(error: str, error_type: str) -> Dict[str, Any]:
            return error_response(
                error=error,
                error_type=error_type,
                provider=PROVIDER_NAME,
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        # Cap before loading: an over-long list is a caller mistake, and
        # there is no reason to read four files off disk to report it.
        if len(sources) > MAX_REFERENCE_IMAGES:
            return fail(
                f"too many reference images ({len(sources)}); the cap is "
                f"{MAX_REFERENCE_IMAGES} and {RECOMMENDED_REFERENCE_IMAGES} or "
                f"fewer is what actually works. More references slow the "
                f"generation down and degrade prompt adherence — the model "
                f"starts confusing the characters with each other and drifting "
                f"off the described scene.",
                "invalid_argument",
            )

        try:
            backend = _resolve_refs_backend(cfg)
        except ValueError as exc:
            return fail(
                f"Cornna reference backend misconfigured: {exc}",
                "invalid_configuration",
            )

        refs: List[_RefImage] = []
        missing_characters: List[str] = []
        try:
            for source in sources:
                try:
                    refs.append(_load_ref_source(source, cfg))
                except MissingCharacterAssetError:
                    # Named character art is optional operationally: a local
                    # install without its private asset pack must still be
                    # able to make a plain image. The success response below
                    # carries the fallback marker so no caller mistakes it
                    # for an anchored render.
                    value = str(source or "").strip()
                    missing_characters.append(
                        value[len(CHARACTER_REF_PREFIX) :].strip().lower()
                    )
        except UnknownCharacterError as exc:
            # Never reached the filesystem — the name failed the allowlist.
            return fail(f"Cornna reference rejected: {exc}", "invalid_argument")
        except InvalidCharacterAssetError as exc:
            return fail(f"Cornna reference rejected: {exc}", "invalid_reference")
        except ValueError as exc:
            return fail(f"Cornna reference rejected: {exc}", "invalid_reference")
        except requests.Timeout as exc:
            return fail(f"Could not load reference image: {exc}", "timeout")
        except requests.ConnectionError as exc:
            return fail(f"Could not load reference image: {exc}", "connection_error")
        except requests.RequestException as exc:
            return fail(f"Could not load reference image: {exc}", "api_error")
        except Exception as exc:  # noqa: BLE001 — one clear message per failure class
            return fail(f"Could not load reference image: {exc}", "io_error")

        if not refs:
            fallback = self.generate(
                prompt=prompt,
                aspect_ratio=aspect,
                model=model_id,
            )
            fallback["missing_characters"] = missing_characters
            fallback["reference_fallback"] = "missing_character_assets"
            return fallback

        if len(refs) > RECOMMENDED_REFERENCE_IMAGES:
            logger.warning(
                "Cornna: %d reference images is over the recommended %d — "
                "expect a slower render and weaker prompt adherence.",
                len(refs),
                RECOMMENDED_REFERENCE_IMAGES,
            )

        full_prompt = _build_ref_prompt(prompt, refs, aspect)
        characters = [ref.character for ref in refs if ref.character]

        if backend == "edits":
            outcome = self._refs_via_edits(
                prompt=full_prompt,
                refs=refs,
                api_key=api_key,
                base_url=base_url,
                model_id=model_id,
                size=size,
            )
        else:
            outcome = self._refs_via_responses(
                prompt=full_prompt,
                refs=refs,
                cfg=cfg,
                api_key=api_key,
                base_url=base_url,
                model_id=model_id,
                size=size,
            )

        entry, err_type, err_msg = outcome
        if entry is None:
            return fail(
                err_msg or "Cornna reference-anchored generation failed",
                err_type or "api_error",
            )

        prefix = f"cornna_refs_{str(model_id).replace('/', '_').replace(':', '_')}"
        image_ref, err_type, err_msg = _materialise_image(entry, prefix)
        if image_ref is None:
            return fail(
                err_msg or "Cornna returned no usable image",
                err_type or "empty_response",
            )

        extra: Dict[str, Any] = {
            "size": size,
            "refs_backend": backend,
            "reference_images": len(refs),
        }
        if characters:
            extra["characters"] = characters
        if missing_characters:
            extra["missing_characters"] = missing_characters
        revised = entry.get("revised_prompt")
        if isinstance(revised, str) and revised:
            extra["revised_prompt"] = revised
        if entry.get("usage"):
            extra["usage"] = entry["usage"]

        # Mandatory backstop (not optional polish): the prompt-level
        # orientation instruction above is steering, not a `size` pin — the
        # endpoint has been verified to ignore `size` on this path and pick
        # its own exact pixel dimensions regardless. A caller (qzone's
        # semantic landscape/square/portrait slots, notably) must never be
        # handed a wrong-aspect image without a signal, so every
        # reference-anchored result is measured and compared before it goes
        # out; a mismatch is logged and flagged, never swallowed.
        _flag_if_aspect_mismatch(image_ref, aspect, size, extra)

        return success_response(
            image=image_ref,
            model=model_id,
            prompt=prompt,
            aspect_ratio=aspect,
            provider=PROVIDER_NAME,
            modality="image",
            extra=extra,
        )

    def _refs_via_edits(
        self,
        *,
        prompt: str,
        refs: Sequence[_RefImage],
        api_key: str,
        base_url: str,
        model_id: str,
        size: str,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]:
        """``POST /images/edits`` — multipart, references in ``image``.

        Returns a ``data[0]``-shaped entry, or ``(None, error_type, msg)``.

        Multiple references go out as repeated ``image`` parts. One
        reference is the shape that has been exercised against this
        endpoint; the repeated form is the conventional OpenAI-compatible
        spelling but is unverified here, in the same sense the size table
        used to carry.
        """
        endpoint_url = f"{base_url}/images/edits"
        headers = {"Authorization": f"Bearer {api_key}"}
        # No Content-Type header: requests must set the multipart boundary.
        # Raw bytes rather than file handles, so a retry re-sends the same
        # parts — a handle would be at EOF on the second attempt and post an
        # empty image field.
        files = [("image", (ref.filename, ref.data, ref.mime)) for ref in refs]
        data = {"model": model_id, "prompt": prompt, "n": "1", "size": size}

        return self._post_for_image(
            endpoint_url,
            headers=headers,
            files=files,
            data=data,
            label="edits",
            extract=_first_data_entry,
        )

    def _refs_via_responses(
        self,
        *,
        prompt: str,
        refs: Sequence[_RefImage],
        cfg: Dict[str, Any],
        api_key: str,
        base_url: str,
        model_id: str,
        size: str,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]:
        """``POST /responses`` — chat model + forced ``image_generation`` tool.

        The references ride along as ``input_image`` data URLs on the user
        message, which is what makes this backend *compose a new scene* that
        inherits the reference's markings and clothing rather than editing
        the reference pixels.

        Non-streaming: the endpoint returns the finished
        ``image_generation_call`` in one document, and this provider has no
        use for ``partial_images`` progress events, so there is no stream to
        drain and no ``response.output_item.done`` race to guard against.
        """
        endpoint_url = f"{base_url}/responses"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        content: List[Dict[str, Any]] = [{"type": "input_text", "text": prompt}]
        for ref in refs:
            content.append({
                "type": "input_image",
                "image_url": ref.data_url(),
                "detail": "high",
            })

        payload: Dict[str, Any] = {
            "model": _resolve_refs_chat_model(cfg),
            # Verified accepted by this endpoint. Off so a prompt carrying
            # persona material isn't retained server-side.
            "store": False,
            "instructions": _REFS_INSTRUCTIONS,
            "input": [{"type": "message", "role": "user", "content": content}],
            "tools": [
                {
                    "type": "image_generation",
                    "model": model_id,
                    "size": size,
                    "quality": REFS_QUALITY,
                    "output_format": "png",
                    "background": "opaque",
                }
            ],
            # Forced, not merely offered: a host model that answers in prose
            # burns the whole (minutes-long) call and returns no picture.
            "tool_choice": {
                "type": "allowed_tools",
                "mode": "required",
                "tools": [{"type": "image_generation"}],
            },
        }

        return self._post_for_image(
            endpoint_url,
            headers=headers,
            json_body=payload,
            label="responses",
            extract=_responses_data_entry,
        )

    def _post_for_image(
        self,
        endpoint_url: str,
        *,
        headers: Dict[str, str],
        extract: Any,
        label: str,
        json_body: Optional[Dict[str, Any]] = None,
        files: Optional[List[Any]] = None,
        data: Optional[Dict[str, str]] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]:
        """POST with retries and hand the decoded body to *extract*.

        Retried: transport failures, 5xx, and a 2xx that carried no image.
        Not retried: 4xx — the request itself is wrong and a second identical
        one will be wrong the same way.
        """
        last_type = "api_error"
        last_msg = f"Cornna {label} request failed"

        for attempt in range(1, GENERATION_MAX_ATTEMPTS + 1):
            started = time.time()
            try:
                response = requests.post(
                    endpoint_url,
                    headers=headers,
                    json=json_body,
                    files=files,
                    data=data,
                    timeout=REFS_REQUEST_TIMEOUT,
                )
            except requests.Timeout:
                last_type = "timeout"
                last_msg = (
                    f"Cornna {label} generation timed out ({REFS_REQUEST_TIMEOUT}s)"
                )
            except requests.ConnectionError as exc:
                last_type = "connection_error"
                last_msg = f"Cornna connection error: {exc}"
            except requests.RequestException as exc:
                last_type = "api_error"
                last_msg = f"Cornna {label} request failed: {exc}"
            else:
                status = int(getattr(response, "status_code", 0) or 0)
                if 400 <= status < 500:
                    err = _error_message(response)
                    logger.error("Cornna %s failed (%s): %s", label, status, err)
                    return None, "api_error", f"Cornna {label} failed ({status}): {err}"
                if status >= 500 or status < 200:
                    last_type = "api_error"
                    last_msg = (
                        f"Cornna {label} failed ({status}): {_error_message(response)}"
                    )
                else:
                    try:
                        body = response.json()
                    except Exception as exc:  # noqa: BLE001 — any decoder error is one class
                        return (
                            None,
                            "invalid_response",
                            f"Cornna returned invalid JSON: {exc}",
                        )
                    entry = extract(body)
                    if entry is not None:
                        logger.info(
                            "Cornna %s OK in %.1fs (attempt %d/%d)",
                            label,
                            time.time() - started,
                            attempt,
                            GENERATION_MAX_ATTEMPTS,
                        )
                        return entry, None, None
                    last_type = "empty_response"
                    last_msg = f"Cornna {label} returned no image data"

            logger.warning(
                "Cornna %s attempt %d/%d failed after %.1fs: %s",
                label,
                attempt,
                GENERATION_MAX_ATTEMPTS,
                time.time() - started,
                last_msg,
            )
            if attempt < GENERATION_MAX_ATTEMPTS:
                time.sleep(GENERATION_RETRY_SLEEP_SECONDS)

        return None, last_type, last_msg


def _first_data_entry(body: Any) -> Optional[Dict[str, Any]]:
    """``{"data": [{...}]}`` → the first entry, or ``None`` when empty."""
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    if not isinstance(first, dict):
        return None
    if not (first.get("b64_json") or first.get("url")):
        return None
    entry = dict(first)
    if isinstance(body, dict) and body.get("usage"):
        entry.setdefault("usage", body["usage"])
    return entry


def _responses_data_entry(body: Any) -> Optional[Dict[str, Any]]:
    """A ``/responses`` body → the same ``data[]``-shaped entry as above."""
    b64 = _image_b64_from_responses_output(body)
    if not b64:
        return None
    entry: Dict[str, Any] = {"b64_json": b64}
    if isinstance(body, dict) and body.get("usage"):
        entry["usage"] = body["usage"]
    return entry


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------


def register(ctx: Any) -> None:
    """Register this provider with the image gen registry."""
    ctx.register_image_gen_provider(CornnaImageGenProvider())
