"""``qzone_publish`` — post a 说说 to QQ空间.

Ported from corlinman's ``corlinman_agent/qzone/publish.py``, which is
itself an evolution of the older hermes ``tools/qzone_tool.py`` and is the
implementation currently running in production. The wire format (form
fields, ``richval``, both success shapes) is identical in both sources and
is reproduced field-for-field here; what changed is the shell — synchronous
``urllib`` through this package's injectable transport instead of
``httpx.AsyncClient``, and a hermes tool handler instead of a gRPC
dispatcher.

Deliberate divergences from the sources are marked ``PORT NOTE`` inline and
collected in ``docs/migration-corlinman/C3-qzone-port-notes.md``.

Every 说说 body and every ``generate`` image prompt passes through
``plugins.qzone.policy.moderate_text`` before anything is built or sent —
see ``handle_qzone_publish`` below — and any request touching images passes
through ``moderate_media`` (deny-by-default on unclassified media,
fail-closed). This mirrors corlinman's own ``publish.py:708-729`` ordering:
policy runs immediately after arg validation, before the S17 idempotency
guard and before any network or file I/O.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from . import policy, state
from .client import (
    DESKTOP_UA,
    QZONE_TIMEOUT,
    QZONE_UPLOAD_TIMEOUT,
    QZoneAuth,
    QZoneError,
    Transport,
    extract_json_object,
    qzone_auth,
    qzone_post,
)

logger = logging.getLogger(__name__)

__all__ = ["QZONE_PUBLISH_SCHEMA", "QZONE_PUBLISH_TOOL", "handle_qzone_publish"]

#: Wire-stable tool name. Production personas and job prompts call it by
#: this exact string.
QZONE_PUBLISH_TOOL = "qzone_publish"

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
#
# Disagreement S14 — the two sources disagree on the publish host:
#   older hermes: https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/...
#   corlinman:    https://h5.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/...
# The paths are identical. corlinman's is the one that has published 19 real
# 说说 in production, and corlinman descends from the hermes version, so the
# host change was made deliberately downstream of the older spelling. We use
# it. The legacy host is kept below so a rollback is a one-line change rather
# than an archaeology exercise.
#
# Both are fixed constants, never built from user input — there is no SSRF
# surface here, and no env override, deliberately: a redirectable publish URL
# would send the borrowed QQ cookie jar wherever the environment said.
QZONE_PUBLISH_URL = (
    "https://h5.qzone.qq.com/proxy/domain/taotao.qzone.qq.com"
    "/cgi-bin/emotion_cgi_publish_v6"
)
QZONE_PUBLISH_URL_LEGACY = (
    "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com"
    "/cgi-bin/emotion_cgi_publish_v6"
)
QZONE_UPLOAD_URL = "https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image"

# 说说 attachment limits.
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_MAX_IMAGES = 9

# PORT NOTE: both sources cap an attachment at 20 MiB. The deployment target
# has 1.9 GB of RAM, and an inline upload holds the file, its base64 form
# (+33%) and the urlencoded body simultaneously — three copies. The OneBot
# adapter in this repo settled on 8 MiB for the same reason, so this matches
# it rather than inventing a third number. Larger images are refused before
# any network call, with a message that names the limit.
_MAX_IMAGE_BYTES = 8 * 1024 * 1024

#: QZone renders feed images best near-square, unlike ``image_generate``'s
#: own default.
_DEFAULT_GEN_ASPECT = "square"


# ---------------------------------------------------------------------------
# Pure wire-format helpers
# ---------------------------------------------------------------------------


def _extract_pic_info(data: Dict[str, Any]) -> Dict[str, Any]:
    """Pull the ``richval`` fields out of an upload response.

    QZone has shipped several response shapes over the years; each value has
    a fallback so a missing optional field degrades instead of raising.
    """
    return {
        "albumid": data.get("albumid", ""),
        "lloc": data.get("lloc") or data.get("photoid", ""),
        "sloc": data.get("sloc") or data.get("photoid", ""),
        "type": data.get("type", 0),
        "width": data.get("width", 0),
        "height": data.get("height", 0),
        "url": data.get("url") or data.get("pre", ""),
    }


def _build_richval(pic_infos: List[Dict[str, Any]]) -> str:
    """Build the ``richval`` string for an image 说说.

    Reverse-engineered: one comma-delimited segment per image, segments
    joined by a TAB. If Tencent changes the wire format, this function is the
    single place to fix — both sources say so in the same words.
    """
    segments = []
    for pic in pic_infos:
        segments.append(
            ",{albumid},{lloc},{sloc},{type},{height},{width},,{height},{width}".format(
                albumid=pic.get("albumid", ""),
                lloc=pic.get("lloc", ""),
                sloc=pic.get("sloc", ""),
                type=pic.get("type", 0),
                height=pic.get("height", 0),
                width=pic.get("width", 0),
            )
        )
    return "\t".join(segments)


def _build_publish_form(
    text: str, uin: Any, pic_infos: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, str]:
    """Build the form body for ``emotion_cgi_publish_v6``."""
    form = {
        "syn_tweet_verson": "1",
        "paramstr": "1",
        "pic_template": "",
        "richtype": "",
        "richval": "",
        "special_url": "",
        "subrichtype": "",
        "who": "1",
        "con": text,
        "feedversion": "1",
        "ver": "1",
        "ugc_right": "1",
        "to_sign": "0",
        "hostuin": str(uin),
        "code_version": "1",
        "format": "json",
        "qzreferrer": f"https://user.qzone.qq.com/{uin}",
    }
    if pic_infos:
        form["richtype"] = "1"
        form["richval"] = _build_richval(pic_infos)
    return form


def _build_upload_form(
    image_b64: str, filename: str, uin: Any, skey: str, p_skey: str, gtk: int
) -> Dict[str, str]:
    """Build the form body for ``cgi_upload_image``."""
    return {
        "filename": filename,
        "uploadtype": "1",
        "albumtype": "7",
        "exttype": "0",
        "refer": "shuoshuo",
        "output_type": "json",
        "charset": "utf-8",
        "output_charset": "utf-8",
        "upload_hd": "1",
        "hd_width": "2048",
        "hd_height": "10000",
        "hd_quality": "96",
        "backUrls": (
            "http://upbak.photo.qzone.qq.com/cgi-bin/upload/cgi_upload_image,"
            "http://119.147.64.75/cgi-bin/upload/cgi_upload_image"
        ),
        "url": f"{QZONE_UPLOAD_URL}?g_tk={gtk}",
        "base64": "1",
        "zzpaneluin": str(uin),
        "p_uin": str(uin),
        "uin": str(uin),
        "skey": skey,
        "p_skey": p_skey,
        "qzonetoken": "",
        "picfile": image_b64,
    }


def _parse_upload_response(raw: Any) -> Dict[str, Any]:
    """Parse the ``cgi_upload_image`` response (a JSONP shim)."""
    obj = extract_json_object(raw)
    if obj is None:
        return {"ok": False, "error": "unparseable upload response"}
    ret = obj.get("ret")
    if ret != 0:
        return {"ok": False, "code": ret, "error": f"ret={ret}"}
    return {"ok": True, "pic": _extract_pic_info(obj.get("data") or {})}


def _parse_publish_response(raw: Any) -> Dict[str, Any]:
    """Parse the ``emotion_cgi_publish_v6`` response.

    The CGI reports success two ways: ``{"ret":0,"tid":...}`` (classic) and
    ``{"code":0,"tid":...,"feedinfo":...}`` (newer — the older hermes source
    annotates this shape "verified live: NapCat/QZone returns ``code`` with
    no ``ret``"). Either zero status counts, provided ``subcode`` is not an
    error. Both sources agree on this exactly.
    """
    obj = extract_json_object(raw)
    if obj is None:
        return {"ok": False, "error": "unparseable QZone response"}

    ret = obj.get("ret")
    code = obj.get("code")
    subcode = obj.get("subcode", 0)
    status = ret if ret is not None else code
    if status == 0 and subcode in (0, None):
        return {"ok": True, "tid": obj.get("tid") or obj.get("t1_tid"), "raw": obj}
    detail = obj.get("msg") or obj.get("message") or ""
    err = f"ret={ret}, code={code}, subcode={subcode}"
    if detail:
        err = f"{err}: {detail}"
    return {"ok": False, "code": status, "error": err}


def _qzone_url(uin: str, tid: Optional[str]) -> Optional[str]:
    """The user-facing 说说 permalink, or ``None`` when the tid is missing."""
    return f"https://user.qzone.qq.com/{uin}/mood/{tid}" if tid else None


def _policy_error(decision: "policy.PolicyDecision") -> str:
    """Render a content-policy refusal in the same envelope shape as any
    other ``qzone_publish`` failure, distinguishable by ``code``.

    Deliberately does NOT touch ``state`` — the request was never sent, so
    there is nothing ambiguous to record. See
    ``TestPolicyRefusalDoesNotPoisonRetryLedger`` in
    ``tests/plugins/qzone/test_qzone_policy_wiring.py``.
    """
    from tools.registry import tool_error

    return tool_error(
        "Tencent content policy blocked this QZone publish.",
        code="content_policy_blocked",
        **policy.policy_error_payload(decision),
    )


# ---------------------------------------------------------------------------
# Local files and image generation
# ---------------------------------------------------------------------------


def _read_image_file(path: str) -> Tuple[bytes, str]:
    """Read a local image, returning ``(bytes, basename)``.

    Raises ``ValueError`` with a readable reason so the handler can fail
    before touching the network — a bad path must never leave a half-done
    upload behind.
    """
    resolved = os.path.expanduser(str(path))
    if not os.path.isfile(resolved):
        raise ValueError("file not found")
    ext = os.path.splitext(resolved)[1].lower()
    if ext not in _IMAGE_EXTS:
        raise ValueError(f"unsupported image type '{ext}' (allowed: {sorted(_IMAGE_EXTS)})")
    size = os.path.getsize(resolved)
    if size == 0:
        raise ValueError("file is empty")
    if size > _MAX_IMAGE_BYTES:
        raise ValueError(f"image too large ({size} bytes; max {_MAX_IMAGE_BYTES})")
    with open(resolved, "rb") as fh:
        return fh.read(), os.path.basename(resolved)


def _download_image(url: str) -> Tuple[bytes, str]:
    """Fetch a generated image from a URL, returning ``(bytes, filename)``."""
    request = urllib.request.Request(url, headers={"User-Agent": DESKTOP_UA})
    try:
        with urllib.request.urlopen(request, timeout=QZONE_UPLOAD_TIMEOUT) as response:
            data = response.read()
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        raise RuntimeError(f"could not download generated image: {exc}") from exc
    if not data:
        raise RuntimeError("downloaded generated image is empty")
    if len(data) > _MAX_IMAGE_BYTES:
        raise RuntimeError(
            f"generated image too large ({len(data)} bytes; max {_MAX_IMAGE_BYTES})"
        )
    name = os.path.basename(urllib.parse.urlparse(url).path)
    if not name or os.path.splitext(name)[1].lower() not in _IMAGE_EXTS:
        name = "generated.png"
    return data, name


def _load_image_reference(ref: str) -> Tuple[bytes, str]:
    """Resolve an ``image_generate`` result (URL or local path) to bytes."""
    ref = str(ref).strip()
    if ref.startswith(("http://", "https://")):
        return _download_image(ref)
    if os.path.isfile(os.path.expanduser(ref)):
        return _read_image_file(ref)
    raise RuntimeError(f"image_generate returned an unusable reference: {ref[:200]}")


def _generate_image(prompt: str, aspect_ratio: str) -> Tuple[bytes, str]:
    """Generate a 配图 through hermes's configured image backend.

    PORT NOTE: corlinman's character-anchored ``image_with_refs`` path is now
    available through Cornna's ``character:<short name>`` image references.
    QZone reads its optional cast from config and falls back to plain prompt
    generation when none of the configured reference art is deployed.
    """
    from tools.image_generation_tool import (  # noqa: PLC0415 — heavy, lazy on purpose
        _handle_image_generate,
        check_image_generation_requirements,
    )

    if not check_image_generation_requirements():
        raise RuntimeError(
            "no image-generation backend is configured — set one up via "
            "`hermes tools` → Image Generation (FAL / OpenAI / xAI)."
        )

    generation_args: Dict[str, Any] = {"prompt": prompt, "aspect_ratio": aspect_ratio}
    try:
        from hermes_cli.config import cfg_get, load_config

        configured_characters = cfg_get(
            load_config(), "qzone", "reference_characters", default=[]
        )
        reference_characters = (
            list(configured_characters) if isinstance(configured_characters, list) else []
        )
        if reference_characters:
            from plugins.image_gen.cornna import (
                MAX_REFERENCE_IMAGES,
                RECOMMENDED_REFERENCE_IMAGES,
                available_characters,
            )

            reference_limit = min(RECOMMENDED_REFERENCE_IMAGES, MAX_REFERENCE_IMAGES)
            if len(reference_characters) > reference_limit:
                logger.warning(
                    "qzone: limiting %d configured reference characters to %d; "
                    "too many reference images can confuse the model and reduce "
                    "prompt adherence",
                    len(reference_characters),
                    reference_limit,
                )
                reference_characters = reference_characters[:reference_limit]

            available = set(available_characters())
            missing_characters = [
                character for character in reference_characters if character not in available
            ]
            if missing_characters:
                logger.info(
                    "qzone: skipping reference characters with no deployed art: %s",
                    ", ".join(missing_characters),
                )
            reference_characters = [
                character for character in reference_characters if character in available
            ]
        if reference_characters:
            terminal_backend = (os.getenv("TERMINAL_ENV") or "local").strip().lower()
            if terminal_backend not in ("", "local"):
                logger.info(
                    "qzone: terminal backend %s does not support character: references; "
                    "using plain generation",
                    terminal_backend,
                )
                reference_characters = []
        if reference_characters:
            # ``image_generate`` has no ``reference_characters`` argument.  Passing
            # ``character:<name>`` via its supported ``reference_image_urls`` field
            # keeps QZone on the configured image backend; calling Cornna's provider
            # directly would hard-code that provider and bypass the tool layer.
            generation_args["reference_image_urls"] = [
                f"character:{character}" for character in reference_characters
            ]
    except Exception as exc:  # noqa: BLE001 — references must not block a post
        logger.info("qzone: character references unavailable; using plain generation (%s)", exc)

    def _generate_reference(args: Dict[str, Any]) -> str:
        raw = _handle_image_generate(args)
        if isinstance(raw, str):
            try:
                result = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"image_generate returned non-JSON: {raw[:200]}") from exc
        elif isinstance(raw, dict):
            result = raw
        else:
            raise RuntimeError(
                f"image_generate returned an unexpected type: {type(raw).__name__}"
            )

        if result.get("error"):
            raise RuntimeError(str(result["error"]))
        image_ref = result.get("image")
        if not image_ref:
            raise RuntimeError("image_generate produced no image.")
        return str(image_ref)

    if "reference_image_urls" in generation_args:
        plain_generation_args = dict(generation_args)
        plain_generation_args.pop("reference_image_urls")
        try:
            image_ref = _generate_reference(generation_args)
        except Exception as exc:  # noqa: BLE001 — references are optional
            logger.warning(
                "qzone: reference image generation failed; retrying without references (%s)",
                exc,
            )
            image_ref = _generate_reference(plain_generation_args)
    else:
        image_ref = _generate_reference(generation_args)
    return _load_image_reference(image_ref)


# ---------------------------------------------------------------------------
# Network steps
# ---------------------------------------------------------------------------


def _upload_one_image(
    image_bytes: bytes,
    filename: str,
    auth: QZoneAuth,
    *,
    transport: Optional[Transport] = None,
) -> Dict[str, Any]:
    """Upload one image and return its parsed pic info."""
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    form = _build_upload_form(
        image_b64, filename, auth.uin, auth.skey, auth.p_skey, auth.gtk
    )
    url = f"{QZONE_UPLOAD_URL}?g_tk={auth.gtk}"
    raw = qzone_post(
        url, form, auth.cookie, auth.uin, QZONE_UPLOAD_TIMEOUT, transport=transport
    )
    result = _parse_upload_response(raw)
    if not result.get("ok"):
        raise QZoneError(
            str(result.get("error", "unknown upload error")), "image_upload_failed"
        )
    pic = result["pic"]
    return pic if isinstance(pic, dict) else {}


def _publish_post(
    form: Dict[str, str], auth: QZoneAuth, *, transport: Optional[Transport] = None
) -> bytes:
    url = f"{QZONE_PUBLISH_URL}?g_tk={auth.gtk}"
    return qzone_post(
        url, form, auth.cookie, auth.uin, QZONE_TIMEOUT, transport=transport
    )


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


def handle_qzone_publish(args: Dict[str, Any], **_kw: Any) -> str:
    """Publish one 说说. Returns a JSON string; never raises."""
    from tools.registry import tool_error

    transport: Optional[Transport] = _kw.get("transport")
    persona_id = (args.get("persona_id") or "").strip() or None
    job = (args.get("job") or "").strip()

    text = (args.get("text") or "").strip()
    images = args.get("images") or []
    if isinstance(images, str):  # tolerate a single path passed as a bare string
        images = [images]
    if not isinstance(images, list):
        return tool_error(
            "qzone_publish 'images' must be a list of file paths.", code="invalid_args"
        )

    generate = args.get("generate")
    if generate is not None and not isinstance(generate, str):
        return tool_error(
            "qzone_publish 'generate' must be a text prompt string.", code="invalid_args"
        )
    generate = (generate or "").strip()
    aspect_ratio = (args.get("aspect_ratio") or _DEFAULT_GEN_ASPECT).strip()

    if not text and not images and not generate:
        return tool_error(
            "qzone_publish requires 'text', 'images', or 'generate'.", code="invalid_args"
        )

    # Content policy — ported from corlinman publish.py:708-729. Runs first,
    # before the S17 idempotency guard and before any file/network I/O, so a
    # refusal here never touches state.py (see plugins/qzone/policy.py and
    # the "content_policy_blocked" test group).
    policy_cfg = policy.resolve_config(_kw.get("policy_resolver"))
    try:
        text_decision = policy.moderate_text(text, policy_cfg).decision
        if not text_decision.allowed:
            return _policy_error(text_decision)
        prompt_decision = policy.moderate_text(generate, policy_cfg).decision
        if not prompt_decision.allowed:
            return _policy_error(prompt_decision)
        media_requested = bool(images or generate)
        if media_requested:
            media_decision = policy.moderate_media(config=policy_cfg)
            if not media_decision.allowed:
                if text:
                    # Identical to corlinman publish.py:721-725: degrade to a
                    # text-only post rather than failing the whole call over
                    # unclassified media (this repo has no media classifier,
                    # so this branch — or the refusal below — is what every
                    # 'images'/'generate' request hits today; see the C3
                    # notes judgement-call log).
                    images = []
                    generate = ""
                else:
                    return _policy_error(media_decision)
    except Exception:
        return _policy_error(policy.classifier_failure_decision(text))

    total_images = len(images) + (1 if generate else 0)
    if total_images > _MAX_IMAGES:
        return tool_error(
            f"QZone说说 supports at most {_MAX_IMAGES} images (requested {total_images}).",
            code="too_many_images",
        )

    # S17 guard — refuse to re-send a body whose previous attempt ended in an
    # unknown state. That attempt may already be public; a cron retry that
    # blindly re-published would put the same 说说 on a real feed twice.
    blocked = state.unknown_publish_guard(text, persona_id)
    if blocked is not None:
        return tool_error(
            "A previous publish of this exact text failed in transport, so it may "
            f"already be live (recorded {blocked.get('ts')}). Not re-publishing. "
            "Check the feed with qzone_list_feed, then either edit the text or "
            "confirm the post is missing before trying again.",
            code="qzone_publish_unknown_pending",
        )

    # Read every local file up front so a bad path fails before the network.
    image_payloads: List[Tuple[bytes, str]] = []
    for path in images:
        try:
            image_payloads.append(_read_image_file(path))
        except ValueError as exc:
            return tool_error(f"Image '{path}': {exc}", code="image_not_found")

    if generate:
        try:
            image_payloads.append(_generate_image(generate, aspect_ratio))
        except RuntimeError as exc:
            return tool_error(f"Image generation failed: {exc}", code="image_generate_failed")

    try:
        auth = qzone_auth(_kw.get("onebot_call"))
    except QZoneError as exc:
        return tool_error(str(exc), code=exc.code)

    pic_infos: List[Dict[str, Any]] = []
    for image_bytes, filename in image_payloads:
        try:
            pic_infos.append(_upload_one_image(image_bytes, filename, auth, transport=transport))
        except QZoneError as exc:
            # An upload failure happens before the 说说 exists, so nothing is
            # public and nothing is recorded — a retry here is safe.
            return tool_error(
                f"Image upload failed for '{filename}': {exc}", code=exc.code
            )

    form = _build_publish_form(text, auth.uin, pic_infos)
    try:
        raw = _publish_post(form, auth, transport=transport)
    except QZoneError as exc:
        # S17: transport died mid-write. The 说说 may be live. Record the
        # attempt as `unknown` — never `failed` — so the guard above and the
        # anti-repeat corpus both know about it.
        state.record_publish(
            persona_id=persona_id,
            text=text,
            tid=None,
            qzone_url=None,
            outcome=state.OUTCOME_UNKNOWN,
            job=job,
        )
        return tool_error(
            f"QZone publish failed in transport: {exc}. The post MAY have been "
            "published — recorded as 'unknown'. Verify with qzone_list_feed "
            "before retrying; a blind retry can double-post.",
            code="qzone_publish_unknown",
        )

    result = _parse_publish_response(raw)
    if not result.get("ok"):
        # QZone answered and said no: definitively not published, so nothing
        # is written to the log and a corrected retry is safe.
        return tool_error(
            f"QZone rejected the post: {result.get('error')}",
            code="qzone_rejected",
            qzone_code=result.get("code"),
        )

    tid = result.get("tid")
    tid_str = str(tid) if tid is not None else None
    url = _qzone_url(auth.uin, tid_str)
    state.record_publish(
        persona_id=persona_id,
        text=text,
        tid=tid_str,
        qzone_url=url,
        outcome=state.OUTCOME_SENT,
        job=job,
    )
    if not tid_str:
        # R5 early-warning: QZone accepted the post but returned no tid,
        # which is what a silent wire-format drift looks like from here.
        logger.warning("qzone_publish: accepted with no tid — check the wire format")
    return json.dumps(
        {
            "success": True,
            "tid": tid_str,
            "qzone_url": url,
            "uin": auth.uin,
            "images": len(pic_infos),
            "generated": bool(generate),
            "message": "说说 published to QQ空间.",
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

QZONE_PUBLISH_SCHEMA = {
    "name": QZONE_PUBLISH_TOOL,
    "description": (
        "Publish a 说说 (status update) to the bound QQ account's QQ空间 (QZone). "
        "Supports text, attached local images, and/or one AI-generated image from "
        "a prompt. The QQ login state is borrowed from the running NapCat / "
        "Lagrange instance — no QQ password is needed. Returns {tid, qzone_url} on "
        "success. This posts publicly to a real social feed and cannot be undone "
        "from here; there is no delete tool. It drives unofficial QZone web "
        "endpoints, so it can fail when the login is stale or Tencent risk control "
        "fires."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": (
                    "The 说说 body. May be empty when 'images' or 'generate' is "
                    "given; otherwise required."
                ),
            },
            "images": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    f"Optional local image paths to attach (max {_MAX_IMAGES}, "
                    f"{_MAX_IMAGE_BYTES // (1024 * 1024)} MiB each). Allowed types: "
                    "JPG, PNG, GIF, WebP, BMP."
                ),
            },
            "generate": {
                "type": "string",
                "description": (
                    "Optional prompt — generates one image with the configured "
                    "backend and attaches it. Counts toward the image limit."
                ),
            },
            "aspect_ratio": {
                "type": "string",
                "enum": ["square", "landscape", "portrait"],
                "description": "Aspect ratio for the generated image. Default: square.",
            },
            "persona_id": {
                "type": "string",
                "description": (
                    "Which persona's publish history to record this under. "
                    "Defaults to QZONE_PERSONA_ID, else 'default'."
                ),
            },
            "job": {
                "type": "string",
                "description": "Optional job label stored alongside the post record.",
            },
        },
        "required": [],
    },
}
