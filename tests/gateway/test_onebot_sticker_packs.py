"""Per-channel sticker packs — a second persona reusing the same mechanism.

The original sticker feature ships exactly one pack: nine images in
``plugins/grantley/assets/stickers`` described by the module-level
:data:`STICKER_CATALOG`.  Once a channel is routed to a *different* persona
(``gateway.profile_routes``), that single pack becomes a leak: the menu is
offered per-turn from adapter-global settings, so the second persona is shown
the first one's face and can send it.

These tests pin the seam that fixes it, and — just as importantly — pin that
the original deployment is untouched: a directory with no ``catalog.json``
still resolves to the bundled catalogue, and a channel with no override still
resolves to the bundled directory.
"""

from __future__ import annotations

import json

import pytest

from plugins.platforms.onebot import sticker


PACK = [
    {"slug": "sulk", "file": "sulk.jpg", "scene": "趴着掉眼泪", "when": "委屈"},
    {"slug": "cuddle", "file": "cuddle.gif", "scene": "凑到镜头前", "when": "撒娇"},
]


@pytest.fixture()
def pack_dir(tmp_path):
    d = tmp_path / "xhh"
    d.mkdir()
    (d / "catalog.json").write_text(json.dumps(PACK, ensure_ascii=False), "utf-8")
    (d / "sulk.jpg").write_bytes(b"\xff\xd8fake")
    (d / "cuddle.gif").write_bytes(b"GIF89afake")
    return d


# ── the bundled pack must keep behaving exactly as before ────────────────


def test_bundled_dir_has_no_catalog_file_and_uses_the_builtin_one():
    assert sticker.load_catalog() is sticker.STICKER_CATALOG


def test_unknown_dir_falls_back_to_the_builtin_catalog(tmp_path):
    assert sticker.load_catalog(str(tmp_path / "nope")) is sticker.STICKER_CATALOG


def test_channel_without_an_override_gets_the_default_dir():
    extra = {"sticker_dirs": {"894800697": "/packs/xhh"}}
    assert sticker.dir_from_extra(extra, "g149881991") == sticker.default_sticker_dir()
    assert sticker.dir_from_extra(extra, None) == sticker.default_sticker_dir()


def test_global_sticker_dir_still_wins_when_no_channel_override():
    extra = {"sticker_dir": "/packs/global"}
    assert sticker.dir_from_extra(extra, "g149881991") == "/packs/global"


# ── the new per-channel seam ─────────────────────────────────────────────


@pytest.mark.parametrize("chat_id", ["894800697", "g894800697"])
def test_channel_override_selects_that_pack_for_both_id_shapes(chat_id):
    extra = {"sticker_dirs": {"894800697": "/packs/xhh"}}
    assert sticker.dir_from_extra(extra, chat_id) == "/packs/xhh"


def test_channel_probability_override(monkeypatch):
    monkeypatch.delenv("ONEBOT_STICKER_PROBABILITY", raising=False)
    extra = {"sticker_probability": 0.1, "sticker_probabilities": {"894800697": 0.9}}
    assert sticker.probability_from_extra(extra, "g894800697") == pytest.approx(0.9)
    assert sticker.probability_from_extra(extra, "g149881991") == pytest.approx(0.1)


def test_pack_catalog_is_read_from_its_own_directory(pack_dir):
    entries = sticker.load_catalog(str(pack_dir))
    assert [e["slug"] for e in entries] == ["sulk", "cuddle"]


def test_pack_slugs_resolve_only_inside_their_own_pack(pack_dir):
    assert sticker.sticker_path("sulk", str(pack_dir)) == str(pack_dir / "sulk.jpg")
    # A slug from the OTHER persona's catalogue is not a path here.
    assert sticker.sticker_path("smug", str(pack_dir)) is None
    # ...and the second persona's slug is not a path in the bundled pack.
    assert sticker.sticker_path("sulk") is None


def test_menu_advertises_only_files_present_on_disk(pack_dir):
    (pack_dir / "cuddle.gif").unlink()
    slugs = [e["slug"] for e in sticker.available_stickers(str(pack_dir))]
    assert slugs == ["sulk"]


def test_offer_menu_uses_the_channel_pack(pack_dir):
    extra = {
        "sticker_probability": 1.0,
        "sticker_dirs": {"894800697": str(pack_dir)},
    }
    menu = sticker.offer_menu(extra, chat_id="g894800697")
    assert menu is not None
    assert "sulk" in menu and "撒娇" in menu
    assert "smug" not in menu


# ── a malformed pack degrades, it does not explode ───────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        "not a list",
        [],
        [{"slug": "ok", "file": "a.jpg", "scene": "s"}],  # missing `when`
        [{"slug": "UP PER", "file": "a.jpg", "scene": "s", "when": "w"}],
        [{"slug": "trav", "file": "../escape.jpg", "scene": "s", "when": "w"}],
        [{"slug": "trav", "file": "sub/dir.jpg", "scene": "s", "when": "w"}],
    ],
)
def test_malformed_catalog_falls_back_whole(tmp_path, raw):
    d = tmp_path / "bad"
    d.mkdir()
    (d / "catalog.json").write_text(json.dumps(raw), "utf-8")
    assert sticker.load_catalog(str(d)) is sticker.STICKER_CATALOG


def test_unreadable_catalog_falls_back(tmp_path):
    d = tmp_path / "broken"
    d.mkdir()
    (d / "catalog.json").write_text("{ this is not json", "utf-8")
    assert sticker.load_catalog(str(d)) is sticker.STICKER_CATALOG


def test_catalog_edit_is_picked_up_without_a_restart(pack_dir):
    assert len(sticker.load_catalog(str(pack_dir))) == 2
    edited = PACK + [
        {"slug": "extra", "file": "extra.jpg", "scene": "新的", "when": "新的"}
    ]
    path = pack_dir / "catalog.json"
    path.write_text(json.dumps(edited, ensure_ascii=False), "utf-8")
    # Force a distinct mtime even on a coarse-grained clock.
    import os

    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert len(sticker.load_catalog(str(pack_dir))) == 3
