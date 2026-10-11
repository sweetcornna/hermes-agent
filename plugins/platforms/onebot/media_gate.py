"""Media screening — the last ungated thing on the outbound path.

Text has had two gates for a while. Pictures had none, and the gap was
tracked in ``adapter.send`` as ``TODO(media)``: the image and file send paths
never reach ``send()``, so nothing looked at the bytes. A caption was audited;
the picture it was attached to was not. That is the hole this module closes.

Why the ported ``moderate_media`` could not just be wired up
------------------------------------------------------------
``plugins.qzone.policy.moderate_media`` already exists and is byte-identical
to the source system's. But it only has two ways to say yes:
``classified_safe=True``, or the config field ``unclassified_media="allow"``
— and ``resolve_config()`` never sets that field. Everything else is denied.

Calling it as-is would therefore block **every image the bot sends**, stickers
included. That is not a hypothetical: the port notes record that the source
system's own QZone lane has image publishing effectively disabled for exactly
this reason, and flag "add a media classifier" as the prerequisite for turning
it back on. This module is that classifier. It supplies the ``classified_safe``
argument that the existing function has always been waiting for, so the ported
deny-by-default logic stays untouched and finally has a way to say yes.

Provenance first, pixels second
-------------------------------
Not every image deserves a model call. Two classes are separated:

* **Repo-shipped assets** — the sticker pack under ``plugins/grantley/assets``.
  A human committed these to the repository; anyone who can change them
  already has code execution on the box, at which point a vision check is not
  what is protecting the account. These are trusted by provenance, judged
  never, and cost nothing.
* **Everything else** — generated images, downloaded images, anything relayed
  from a conversation. These get looked at by a vision model.

The result is cached by **content digest**, so a given picture is judged once
per process no matter how many times it is sent, and an image that arrives by
three different paths is still one call.

This gate fails CLOSED — the opposite of the text agent gate
------------------------------------------------------------
:mod:`.agent_gate` fails open, because behind it sits a local deterministic
table that always renders a verdict. There is no such fallback here: this
module *is* the classifier, and if it cannot answer, the ported policy's
answer is "deny". So a vision timeout, a missing model, or an unreadable file
blocks the picture.

That asymmetry is deliberate, and it is safe because of how the caller
degrades: a blocked image does not fail the send, it drops to **text-only**,
which is the same degradation the source system performs (``publish.py``
sends the body without the attachment rather than failing outright). The
visible worst case for a vision outage is a bot that talks but stops posting
pictures — noticeable, logged at WARNING, and not account-threatening. The
worst case for failing open is a picture that cannot be recalled.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import os
import re
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Mapping, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "MEDIA_GATE_VERSION",
    "MediaGateConfig",
    "MediaVerdict",
    "is_trusted_asset",
    "judge_media_file",
    "resolve_media_gate_config",
]

MEDIA_GATE_VERSION: Final[str] = "onebot-media-gate-2026-08-28.1"

#: Vision calls are slower than text ones; this is sized like the text gate's
#: budget (measured, with headroom) rather than guessed.
_DEFAULT_TIMEOUT_SECS: Final[float] = 25.0
_DEFAULT_MAX_TOKENS: Final[int] = 200
_DEFAULT_CACHE_SIZE: Final[int] = 512
#: Above this, the base64 payload costs more to ship than the check is worth.
#: Oversized images are downscaled when Pillow is importable, and denied when
#: it is not — see the fail-closed note in the module docstring.
_DEFAULT_MAX_BYTES: Final[int] = 4 * 1024 * 1024
_DOWNSCALE_EDGE: Final[int] = 1024

_SYSTEM_PROMPT: Final[str] = """\
你是一个中国大陆 QQ 账号的图片发送安全审查员。给你一张机器人**即将发送**的图片，\
判断发出去是否会带来账号被腾讯风控冻结/封禁的风险。

判定为不安全（unsafe）的情形：
- 涉政：国家领导人的丑化/恶搞/表情包、政治标语口号、被取缔组织标识、\
分裂国家相关旗帜标语（台独港独疆独藏独）、政治敏感事件的历史照片
- 色情低俗：裸露、性暗示、涉未成年人的性化内容
- 血腥暴力：血腥伤害、尸体、自残、武器威胁场景
- 违法交易：毒品、枪支、赌博、诈骗相关的宣传图
- 二维码与引流：任何二维码、群号/微信号/联系方式的截图与名片
- 翻墙推广：机场节点、代理服务的截图或广告

判定为安全（safe）的情形：
- 日常照片、风景、动物、食物、截图、代码截图、表情包（不涉及上述内容的）
- 图表、文档、示意图、界面截图

拿不准的时候倾向 unsafe——图片发出去撤不回来，而漏判的代价是封号。

只输出 JSON，不要任何其他文字：
{"safe": true 或 false, "reason": "简短中文理由", "category": "类别代码或空字符串"}
"""

#: QZone 空间说说 screens against a different risk model than QQ chat, so it
#: gets its own prompt rather than a threshold knob on the shared one.
#:
#: The account-threatening categories are identical and stay verbatim —
#: 涉政, 二维码/引流, 翻墙推广, 违法交易, 血腥暴力 are what actually gets an
#: account frozen, and none of them are negotiable.
#:
#: What differs is 色情低俗. On the chat path that category is deliberately
#: broad: the bot replies to strangers in groups, and "裸露" there means an
#: unsolicited picture sent at somebody. A 说说 is the persona's own timeline,
#: the pictures are generated from that persona's reference art, and the
#: operator asked for shirtless / wet-clothes / towel framing on purpose. A
#: gate that reads 赤膊 as pornography does not make that safer, it just makes
#: the feature unusable — and it did: on 2026-08-28 it refused an entire
#: three-picture post over one shirtless frame.
#:
#: So this prompt keeps the hard limits (性器官, 性行为, 未成年) and drops the
#: broad-nudity clause. It is not "the gate off": every other category still
#: denies, and the fail-closed posture is unchanged.
_QZONE_SYSTEM_PROMPT: Final[str] = """\
你是一个中国大陆 QQ 空间说说的配图审查员。给你一张**即将随说说发布**的插画，\
判断发出去是否会带来账号被腾讯风控冻结/封禁的风险。

判定为不安全（unsafe）的情形：
- 涉政：国家领导人的丑化/恶搞/表情包、政治标语口号、被取缔组织标识、\
分裂国家相关旗帜标语（台独港独疆独藏独）、政治敏感事件的历史照片
- 硬色情：暴露的生殖器或肛门、性行为或其明确暗示（体位、插入、体液）、\
任何涉未成年人的性化内容
- 血腥暴力：血腥伤害、尸体、自残、武器威胁场景
- 违法交易：毒品、枪支、赌博、诈骗相关的宣传图
- 二维码与引流：任何二维码、群号/微信号/联系方式的截图与名片
- 翻墙推广：机场节点、代理服务的截图或广告

判定为安全（safe）的情形：
- 角色插画中的赤膊、湿衣贴身、浴巾或浴袍遮挡、泳装、腰线与肌肉展示——\
只要关键部位有遮挡且没有性行为暗示，这些都算安全，不要判为色情
- 日常照片、风景、动物、食物、截图、表情包、图表与界面截图

注意：尺度偏性感但关键部位有遮挡的角色插画属于安全。\
只有越过上面「硬色情」那条线才判 unsafe。其余类别拿不准时仍倾向 unsafe。

只输出 JSON，不要任何其他文字：
{"safe": true 或 false, "reason": "简短中文理由", "category": "类别代码或空字符串"}
"""

#: Profile name → system prompt. ``judge_media_file`` takes the name, so a
#: caller cannot inject its own screening instructions.
_PROFILE_PROMPTS: Final[dict[str, str]] = {
    "chat": _SYSTEM_PROMPT,
    "qzone": _QZONE_SYSTEM_PROMPT,
}

_JSON_RE = re.compile(r"\{.*\}", re.S)

#: Repo-shipped asset roots, trusted by provenance. Resolved once at import.
_ASSET_ROOTS: Final[tuple[Path, ...]] = tuple(
    p.resolve()
    for p in (Path(__file__).resolve().parents[2] / "grantley" / "assets",)
)


@dataclass(frozen=True, slots=True)
class MediaGateConfig:
    enabled: bool = True
    model: Optional[str] = None
    timeout_secs: float = _DEFAULT_TIMEOUT_SECS
    max_tokens: int = _DEFAULT_MAX_TOKENS
    cache_size: int = _DEFAULT_CACHE_SIZE
    max_bytes: int = _DEFAULT_MAX_BYTES
    #: Trust the repo's own sticker pack without a model call. Switchable for
    #: operators who would rather pay for the certainty.
    trust_repo_assets: bool = True

    @classmethod
    def from_mapping(cls, value: object) -> MediaGateConfig:
        """Parse a config snapshot. Only an explicit ``False`` disables."""
        if not isinstance(value, Mapping):
            return cls()

        def _num(key: str, default: Any, cast: Any) -> Any:
            try:
                out = cast(value.get(key, default))
            except (TypeError, ValueError):
                return default
            return out if out > 0 else default

        model = value.get("model")
        return cls(
            enabled=value.get("enabled") is not False,
            model=str(model) if isinstance(model, str) and model.strip() else None,
            timeout_secs=_num("timeout_secs", _DEFAULT_TIMEOUT_SECS, float),
            max_tokens=_num("max_tokens", _DEFAULT_MAX_TOKENS, int),
            cache_size=_num("cache_size", _DEFAULT_CACHE_SIZE, int),
            max_bytes=_num("max_bytes", _DEFAULT_MAX_BYTES, int),
            trust_repo_assets=value.get("trust_repo_assets") is not False,
        )


@dataclass(frozen=True, slots=True)
class MediaVerdict:
    """The media screening result.

    ``safe`` is the answer the caller acts on. ``consulted`` says whether a
    model actually looked; ``safe=False, consulted=False`` means "could not
    check, so denied", which is a different operational story from "looked and
    found something" and is logged differently.
    """

    safe: bool
    consulted: bool
    reason: str = ""
    category: str = ""
    fallthrough_cause: str = ""

    def audit_fields(self) -> dict[str, object]:
        """Loggable fields. Deliberately excludes the image bytes and path."""
        return {
            "gate": "media",
            "gate_version": MEDIA_GATE_VERSION,
            "safe": self.safe,
            "consulted": self.consulted,
            "category": self.category,
            "fallthrough_cause": self.fallthrough_cause,
        }


_TRUSTED = MediaVerdict(safe=True, consulted=False, fallthrough_cause="trusted_asset")

_CACHE: "OrderedDict[tuple[str, str], MediaVerdict]" = OrderedDict()


def resolve_media_gate_config(snapshot: object = None) -> MediaGateConfig:
    """Resolve config, fail-safe to enabled-with-defaults."""
    try:
        return MediaGateConfig.from_mapping(snapshot)
    except Exception:  # noqa: BLE001
        return MediaGateConfig()


def is_trusted_asset(path: str | os.PathLike[str]) -> bool:
    """True for files inside a repo-shipped asset root.

    Uses a resolved-prefix comparison so ``assets/../../etc/passwd`` cannot
    present itself as trusted.
    """
    try:
        target = Path(path).resolve()
    except (OSError, ValueError):
        return False
    for root in _ASSET_ROOTS:
        try:
            target.relative_to(root)
        except ValueError:
            continue
        return True
    return False


def _cache_get(key: tuple[str, str]) -> Optional[MediaVerdict]:
    verdict = _CACHE.get(key)
    if verdict is not None:
        _CACHE.move_to_end(key)
    return verdict


def _cache_put(key: tuple[str, str], verdict: MediaVerdict, limit: int) -> None:
    # Only model-backed verdicts are cached. Caching a "could not check" would
    # pin a transient vision outage onto that image for the process's lifetime.
    if not verdict.consulted:
        return
    _CACHE[key] = verdict
    _CACHE.move_to_end(key)
    while len(_CACHE) > max(1, limit):
        _CACHE.popitem(last=False)


def _file_digest(path: Path) -> Optional[str]:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()[:24]
    except OSError:
        return None


def _downscale(raw: bytes) -> Optional[bytes]:
    """Shrink an oversized image so it can be judged. ``None`` if impossible.

    Pillow is optional here — it is not a declared dependency, and the gate
    must behave predictably whether or not some other package pulled it in.
    """
    try:
        import io  # noqa: PLC0415

        from PIL import Image  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    try:
        with Image.open(io.BytesIO(raw)) as img:
            img = img.convert("RGB")
            img.thumbnail((_DOWNSCALE_EDGE, _DOWNSCALE_EDGE))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=80)
            return buf.getvalue()
    except Exception:  # noqa: BLE001
        return None


def _data_url(path: Path, max_bytes: int) -> Optional[str]:
    """Read *path* as a data: URL small enough to ship, or ``None``."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if not raw:
        return None
    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    if len(raw) > max_bytes:
        shrunk = _downscale(raw)
        if shrunk is None:
            return None
        raw, mime = shrunk, "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _parse_verdict(raw: str) -> Optional[MediaVerdict]:
    """Parse the model's JSON answer. ``None`` means unparseable."""
    if not raw:
        return None
    match = _JSON_RE.search(raw)
    if match is None:
        return None
    try:
        payload = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    safe = payload.get("safe")
    if not isinstance(safe, bool):
        # Same reasoning as the text gate: a truthy string is not an answer,
        # and treating it as approval is how a gate becomes a no-op.
        return None
    reason = payload.get("reason")
    category = payload.get("category")
    return MediaVerdict(
        safe=safe,
        consulted=True,
        reason=str(reason)[:200] if isinstance(reason, str) else "",
        category=str(category)[:64] if isinstance(category, str) else "",
    )


def _denied(cause: str) -> MediaVerdict:
    return MediaVerdict(safe=False, consulted=False, fallthrough_cause=cause)


async def judge_media_file(
    path: str | os.PathLike[str],
    *,
    config: MediaGateConfig | None = None,
    profile: str = "chat",
) -> MediaVerdict:
    """Decide whether the image at *path* is safe to put on QQ.

    *profile* selects the screening prompt: ``"chat"`` (the default, used by
    every send path) or ``"qzone"``, which keeps the account-risk categories
    and narrows 色情 to the hard limits — see :data:`_QZONE_SYSTEM_PROMPT`.
    An unknown name falls back to ``"chat"``, the stricter of the two, so a
    typo tightens the gate instead of opening it.

    Never raises. Every failure path returns ``safe=False`` with a cause, per
    the fail-closed posture explained in the module docstring.
    """
    system_prompt = _PROFILE_PROMPTS.get(profile, _SYSTEM_PROMPT)
    cfg = config or MediaGateConfig()
    if not cfg.enabled:
        return MediaVerdict(safe=True, consulted=False, fallthrough_cause="disabled")
    if cfg.trust_repo_assets and is_trusted_asset(path):
        return _TRUSTED

    target = Path(path)
    digest = _file_digest(target)
    if digest is None:
        logger.warning(
            "OneBot media gate: cannot read the file to screen it — blocking the "
            "attachment and sending text only"
        )
        return _denied("unreadable")

    model = cfg.model or os.getenv("ONEBOT_MEDIA_GATE_MODEL") or None
    # The profile is part of the key: the same bytes get a different verdict
    # under a different prompt, and sharing one cache slot between them would
    # let whichever path ran first decide for the other.
    key = (digest, f"{model or ''}|{profile}")
    cached = _cache_get(key)
    if cached is not None:
        return cached

    data_url = _data_url(target, cfg.max_bytes)
    if data_url is None:
        logger.warning(
            "OneBot media gate: image too large to screen and Pillow is "
            "unavailable to shrink it — blocking the attachment"
        )
        return _denied("too_large")

    try:
        from agent.auxiliary_client import (  # noqa: PLC0415
            async_call_llm,
            extract_content_or_reasoning,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "OneBot media gate: auxiliary client unavailable (%s) — blocking "
            "the attachment",
            type(exc).__name__,
        )
        return _denied("client_unavailable")

    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "审查这张即将发送的图片。"},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        },
    ]

    try:
        response: Any = await asyncio.wait_for(
            async_call_llm(
                task="vision",
                model=model,
                messages=messages,
                temperature=0.0,
                max_tokens=cfg.max_tokens,
            ),
            timeout=cfg.timeout_secs,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "OneBot media gate: vision check timed out after %.1fs — blocking "
            "the attachment and sending text only",
            cfg.timeout_secs,
        )
        return _denied("timeout")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "OneBot media gate: vision check failed (%s: %s) — blocking the "
            "attachment and sending text only",
            type(exc).__name__,
            exc,
        )
        return _denied("call_failed")

    try:
        raw = extract_content_or_reasoning(response)
    except Exception:  # noqa: BLE001
        raw = ""
    verdict = _parse_verdict(raw)
    if verdict is None:
        logger.warning(
            "OneBot media gate: unparseable vision answer (%d chars) — blocking "
            "the attachment",
            len(raw or ""),
        )
        return _denied("unparseable")

    _cache_put(key, verdict, cfg.cache_size)
    return verdict
