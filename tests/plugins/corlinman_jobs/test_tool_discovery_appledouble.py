"""Regression: a macOS AppleDouble sidecar in ``tools/`` must not kill a cron run.

Deploying this repo to the corlinman host from a macOS workstation copies the
tree with its extended attributes.  Every source file the Mac has touched
carries ``com.apple.provenance``, and any copy that preserves xattrs onto a
filesystem that has none (bsdtar → GNU tar, ``rsync -X``, some scp wrappers)
materialises each xattr set as a literal AppleDouble sidecar next to its
parent: ``tools/onebot_client.py`` gains ``tools/._onebot_client.py``.

That sidecar is a 163-byte binary blob, and — fatally — it matches the
``*.py`` glob that :func:`tools.registry.discover_builtin_tools` walks.
``_module_registers_tools`` read it with ``encoding="utf-8"`` inside a
``try/except OSError``; ``UnicodeDecodeError`` is a ``ValueError``, so it was
not caught.  It escaped through the ``discover_builtin_tools()`` call at
``model_tools`` import time, through ``from run_agent import AIAgent`` inside
``cron.scheduler.run_job``, and landed in ``_run_one_job_body``'s
``except BaseException`` — which records ``str(e)`` with no ``exc_info``.

The production symptom was ``hermes.analysis_digest`` failing in ~2.5s with a
bare, tracebackless::

    'utf-8' codec can't decode byte 0xa3 in position 45: invalid start byte

The job's own material script never imports ``run_agent``, which is exactly
why running it standalone succeeded while the ``no_agent=False`` cron path —
the only path that boots the agent — did not.

Byte 45 is not in anybody's Chinese text: it is the low byte of AppleDouble
entry #2's offset field, ``0x000000A3`` = 163 = the sidecar's own length (a
zero-length resource fork parked at EOF).  The header below reproduces that
byte-for-byte, so this test asserts the real error string, not a lookalike.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import registry  # noqa: E402


def _appledouble_sidecar_bytes() -> bytes:
    """The exact shape macOS writes for a file carrying only an xattr set.

    Header (26 bytes): magic ``0x00051607``, version ``0x00020000``, a
    16-byte ``"Mac OS X"`` filler, then the entry count.  Then two 12-byte
    entry descriptors — Finder info (id 9) and the resource fork (id 2) —
    followed by the Finder info block and the ``ATTR`` xattr segment.
    """
    total_len = 163
    finder_info_off, finder_info_len = 50, 113
    header = b"".join(
        [
            struct.pack(">I", 0x00051607),
            struct.pack(">I", 0x00020000),
            b"Mac OS X" + b" " * 8,
            struct.pack(">H", 2),
            struct.pack(">III", 9, finder_info_off, finder_info_len),
            # Entry #2: resource fork, length 0, offset == EOF == 163 == 0xA3.
            # Its offset field occupies bytes 42..45; byte 45 is 0xA3.
            struct.pack(">III", 2, total_len, 0),
        ]
    )
    assert len(header) == 50, len(header)
    body = bytearray(total_len - len(header))
    body[32:36] = b"ATTR"  # xattr segment magic, as macOS lays it out
    body[-len(b"com.apple.provenance") - 8:] = (
        b"com.apple.provenance" + b"\x00\x01\x02\x00\x50\x29\x26\xde"
    )
    return bytes(header + bytes(body))


def test_appledouble_bytes_reproduce_the_production_error_string() -> None:
    """Guard the fixture itself: it must fail to decode exactly as prod did."""
    with pytest.raises(UnicodeDecodeError) as excinfo:
        _appledouble_sidecar_bytes().decode("utf-8")
    assert str(excinfo.value) == (
        "'utf-8' codec can't decode byte 0xa3 in position 45: invalid start byte"
    )


def test_module_registers_tools_rejects_a_non_utf8_file(tmp_path: Path) -> None:
    """An undecodable ``*.py`` is "not a tool module", not an exception."""
    sidecar = tmp_path / "._onebot_client.py"
    sidecar.write_bytes(_appledouble_sidecar_bytes())

    assert registry._module_registers_tools(sidecar) is False


def test_module_registers_tools_rejects_a_file_with_nul_bytes(tmp_path: Path) -> None:
    """Decodable but NUL-bearing source is the adjacent case, already covered.

    On CPython 3.11 ``ast.parse`` reports null bytes as ``SyntaxError``, which
    the existing handler catches, so this passed before the fix too. Pinned
    here so the next reader does not have to re-derive that the fix needs to
    cover only the read, not the parse.
    """
    nul_file = tmp_path / "._registry_register.py"
    nul_file.write_bytes(b"registry.register(\x00)\n")

    assert registry._module_registers_tools(nul_file) is False


def test_discover_builtin_tools_survives_an_appledouble_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discovery must still find the real module standing next to the sidecar.

    The sidecar sorts *before* every real module (``.`` < any letter), so it
    is the first file the glob hands to ``_module_registers_tools`` — the
    unguarded read took the whole agent bootstrap down before a single tool
    was seen.
    """
    monkeypatch.setattr(registry, "_load_discovery_cache", dict)
    monkeypatch.setattr(registry, "_save_discovery_cache", lambda cache: None)
    monkeypatch.setattr(registry.importlib, "import_module", lambda name: None)

    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "._onebot_client.py").write_bytes(_appledouble_sidecar_bytes())
    (tools_dir / "onebot_client.py").write_text(
        "from tools import registry\n"
        "registry.register(name='onebot_send')\n",
        encoding="utf-8",
    )

    assert registry.discover_builtin_tools(tools_dir) == ["tools.onebot_client"]
