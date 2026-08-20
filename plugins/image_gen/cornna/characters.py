"""Character 立绘 registry for the Cornna image backend.

What this is
------------
A fixed, eleven-entry allowlist mapping a short name (``grantley``) to the
PNG that holds that character's canonical 立绘 (``02_grantley_bell.png``).
Ported from ``personal_hermes``'s ``tools/image_with_refs.py``, where the
same table let the model anchor a generated scene to the real characters
instead of inventing its own anthro defaults.

Why an allowlist and not a path
-------------------------------
The short name arrives from a caller — and once a tool wrapper sits on top
of this provider, from model output. Joining caller-authored text onto a
directory is exactly how ``../../etc/passwd`` gets read off disk, so the
name is checked against :data:`CHARACTER_KEYS` **before** a
:class:`~pathlib.Path` is built and long before the filesystem is touched.
A name that is not one of the eleven is not a path at all. Same shape as
``plugins/platforms/onebot/sticker.py``'s ``sticker_path()``, for the same
reason.

Where the files live (first hit wins)
-------------------------------------
1. ``image_gen.cornna.character_dir`` in ``config.yaml``
2. ``$HERMES_HOME/characters``

Note: ``$HERMES_HOME``, not a hardcoded ``~/.hermes``. The old
implementation hardcoded the latter; profile-aware deployment must resolve
the same ``characters`` location in local and service environments.

This registry only resolves and validates assets. The provider decides whether
an absent *named* asset can safely fall back to a text-only request and marks
that fallback in its response.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, List, Mapping, Optional, Tuple

__all__ = [
    "CHARACTER_KEYS",
    "UnknownCharacterError",
    "MissingCharacterAssetError",
    "InvalidCharacterAssetError",
    "character_dir",
    "character_path",
    "resolve_character_file",
    "load_character_image",
    "character_data_url",
    "available_characters",
]


#: Short name → filename under the character directory. Carried over
#: verbatim from ``image_with_refs.CHARACTER_KEYS`` — the filenames are
#: numbered because that is how they are stored, and renaming them here
#: would silently orphan every existing deployment's assets.
CHARACTER_KEYS: dict = {
    "grantley": "02_grantley_bell.png",  # the tiger persona himself
    "algo": "01_algo_northrop.png",  # 艾尔戈, owner
    "oscar": "03_oscar_lawrence.png",  # 铁三角
    "diedrich": "04_diedrich_olsen.png",
    "paul": "05_paul_pfizner.png",
    "theo": "06_theo_prince.png",
    "julius": "07_julius_kinial.png",
    "hermann": "08_hermann_furst.png",
    "helio": "09_helio_delatre.png",
    "shayat": "10_shayat.png",
    "bating": "11_bating.png",
}

_MIME_BY_FORMAT = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}


class UnknownCharacterError(ValueError):
    """The requested short name is not in :data:`CHARACTER_KEYS`."""


class MissingCharacterAssetError(FileNotFoundError):
    """The short name is valid but its 立绘 is not on this box."""


class InvalidCharacterAssetError(ValueError):
    """A named character asset exists but is not a supported, valid image."""


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _config(cfg: Optional[Mapping[str, Any]] = None) -> Mapping[str, Any]:
    """The ``image_gen.cornna`` config section, loaded lazily.

    The import is deferred to call time: the provider package imports this
    module at load, so a module-level import back into it would be a cycle.
    """
    if cfg is not None:
        return cfg if isinstance(cfg, dict) else {}
    from . import _load_cornna_image_config  # noqa: PLC0415 — breaks an import cycle

    return _load_cornna_image_config()


def character_dir(cfg: Optional[Mapping[str, Any]] = None) -> Path:
    """Directory holding canonical art — config > ``$HERMES_HOME/characters``."""
    section = _config(cfg)
    candidate = section.get("character_dir") if isinstance(section, dict) else None
    if isinstance(candidate, str) and candidate.strip():
        return Path(candidate.strip()).expanduser()

    from hermes_constants import get_hermes_home  # noqa: PLC0415 — import-cheap, call-time home

    return get_hermes_home() / "characters"


def character_path(
    name: Any,
    *,
    cfg: Optional[Mapping[str, Any]] = None,
    directory: Optional[Any] = None,
) -> Path:
    """Where *name*'s 立绘 would live. Does **not** touch the filesystem.

    The allowlist is consulted first and the caller's string is never used
    to build the path — only the filename this module owns is. So a name
    like ``../../etc/passwd`` is rejected here, before any join, any
    ``resolve()``, and any read.
    """
    key = str(name or "").strip().lower()
    filename = CHARACTER_KEYS.get(key)
    if filename is None:
        valid = ", ".join(sorted(CHARACTER_KEYS))
        raise UnknownCharacterError(
            f"unknown character short name {str(name)!r}. Valid names: {valid}"
        )
    base = Path(directory) if directory is not None else character_dir(cfg)
    return base / filename


def _image_mime(path: Path) -> str:
    """Verify *path* decodes as a supported image and return its real MIME type."""
    try:
        from PIL import Image

        with Image.open(path) as image:
            image_format = (image.format or "").upper()
            image.verify()
    except Exception as exc:  # noqa: BLE001 - Pillow normalizes decoder failures poorly
        raise InvalidCharacterAssetError(
            f"character asset is not a valid image: {path} ({exc})"
        ) from exc

    mime = _MIME_BY_FORMAT.get(image_format)
    if mime is None:
        raise InvalidCharacterAssetError(
            f"character asset uses unsupported image format {image_format!r}: {path}"
        )
    return mime


def resolve_character_file(
    name: Any,
    *,
    cfg: Optional[Mapping[str, Any]] = None,
    directory: Optional[Any] = None,
) -> Path:
    """Absolute path to *name*'s reference image, or raise.

    Raises :class:`UnknownCharacterError` for a name outside the allowlist
    and :class:`MissingCharacterAssetError` when the file is absent. Never
    returns ``None`` — a caller that got a path got a real anchor.
    """
    key = str(name or "").strip().lower()
    path = character_path(key, cfg=cfg, directory=directory)
    if path.is_file():
        _image_mime(path)
        return path

    raise MissingCharacterAssetError(
        f"character 立绘 missing on disk: short name {key!r} expects {path}. "
        "Put that PNG under $HERMES_HOME/characters (or configure "
        "image_gen.cornna.character_dir) and retry."
    )


def load_character_image(
    name: Any,
    *,
    cfg: Optional[Mapping[str, Any]] = None,
    directory: Optional[Any] = None,
) -> Tuple[bytes, str, str]:
    """Return ``(bytes, filename, mime)`` for *name*'s reference image."""
    path = resolve_character_file(name, cfg=cfg, directory=directory)
    return path.read_bytes(), path.name, _image_mime(path)


def character_data_url(
    name: Any,
    *,
    cfg: Optional[Mapping[str, Any]] = None,
    directory: Optional[Any] = None,
) -> str:
    """``data:image/png;base64,...`` for *name* — the Responses-API ref form."""
    data, _filename, mime = load_character_image(name, cfg=cfg, directory=directory)
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def available_characters(
    *,
    cfg: Optional[Mapping[str, Any]] = None,
    directory: Optional[Any] = None,
) -> List[str]:
    """Short names whose reference image is actually present on this box.

    Sorted, so it is stable to print and to assert on. Filtered against the
    disk for the same reason ``available_stickers()`` is: an upper layer
    that offers a name this returns must not then hit the missing-asset
    error. An empty list means the assets have not been deployed yet, and
    reference-anchored generation cannot work at all.
    """
    result: List[str] = []
    for name in CHARACTER_KEYS:
        try:
            resolve_character_file(name, cfg=cfg, directory=directory)
        except (MissingCharacterAssetError, InvalidCharacterAssetError):
            continue
        result.append(name)
    return result
