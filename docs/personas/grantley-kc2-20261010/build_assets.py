"""Build Grantley's sticker pack and the character 立绘 set from the KC2 game export.

Everything here comes from the game's own data. Nothing is redrawn or generated:

* Stickers are face crops of Grantley's Knights College 2 sprites. The crop
  rectangle is not chosen by hand: it is the ``IconRect`` the game itself uses
  for Grantly's message-window face icon, read from the Character sheet of
  ``游戏设置原表.json``. Each sticker is one sprite pattern (outfit 1 = uniform)
  cut with that rectangle; transparency is kept.
* 立绘 are the KC2 ``10000`` sprite of each character (outfit 1, neutral face),
  copied byte for byte so they can be checked against the export's own
  ``校验清单.sha256``.

KC2 sprite pattern codes are five digits: outfit, mouth, eyebrows, eyes,
effect. Outfit 0 is unclothed and is never used here. Effect 1 adds a sweat
drop, 2 a blush, 3 both.

Usage (run from anywhere; the export is read straight out of the zip)::

    python build_assets.py EXPORT.zip \
        --stickers plugins/grantley/assets/stickers \
        --characters OUT/characters \
        --manifest docs/personas/grantley-kc2-20261010/assets-manifest.json
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path

from PIL import Image

SETTINGS = "台词与剧情/骑士学院2/原始数据/游戏设置原表.json"
GRANTLEY_SPRITES = "图片/格兰特利/骑士学院2/完整立绘_全部差分/{pattern}.png"
OTHER_SPRITES = "图片/其他角色/{role}/骑士学院2/完整立绘_全部差分/{pattern}.png"
GRANTLEY_ASSET = "Grantly.asset"

#: slug -> KC2 pattern. Faces are picked by how often Grantley wears them in
#: the KC2 script (the face digits of his 640 non-adult lines), so the pack
#: covers the expressions he actually uses rather than ones that read well.
STICKERS = {
    "grin": "11050",
    "laughing": "13050",
    "smug": "10000",
    "fired-up": "13200",
    "angry": "12221",
    "unimpressed": "10031",
    "doubtful": "10131",
    "shocked": "12071",
    "flustered": "12073",
    "shy": "11052",
    "sheepish": "10351",
    "aggrieved": "12371",
}

#: CHARACTER_KEYS key -> (target filename, KC2 role folder or None for Grantley).
#: Filenames for the first eleven keys are the deployed names and must not change.
PORTRAITS = {
    "algo": ("01_algo_northrop.png", "亚戈"),
    "grantley": ("02_grantley_bell.png", None),
    "oscar": ("03_oscar_lawrence.png", "奥斯卡"),
    "diedrich": ("04_diedrich_olsen.png", "迪德里奇"),
    "paul": ("05_paul_pfizner.png", "保罗"),
    "theo": ("06_theo_prince.png", "西奥"),
    "julius": ("07_julius_kinial.png", "尤利乌斯"),
    "hermann": ("08_hermann_furst.png", "赫尔曼"),
    "helio": ("09_helio_delatre.png", "希利欧"),
    "shayat": ("10_shayat.png", "沙亚特"),
    "bating": ("11_bating.png", "巴亭"),
    "felix": ("12_felix.png", "菲力克斯"),
    "gamir": ("13_gamir.png", "伽米尔"),
    "bayfleet": ("14_bayfleet.png", "贝弗利特"),
}
PORTRAIT_PATTERN = "10000"


def _entry_name(info: zipfile.ZipInfo) -> str:
    """The export stores UTF-8 names without the UTF-8 flag; undo cp437."""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


class Export:
    def __init__(self, path: Path):
        self.zip = zipfile.ZipFile(path)
        self.index = {_entry_name(i): i for i in self.zip.infolist()}

    def read(self, name: str) -> tuple[bytes, zipfile.ZipInfo]:
        info = self.index.get(name)
        if info is None:
            raise SystemExit(f"missing from export: {name}")
        return self.zip.read(info), info


def icon_rect(export: Export, asset: str) -> tuple[int, int, int, int]:
    """The game's own face-icon rectangle for *asset*, as (left, top, right, bottom)."""
    raw, _ = export.read(SETTINGS)
    sheets = json.loads(raw.decode("utf-8"))
    table = next(s for s in sheets if s["name"].endswith(":Character"))
    header = table["rows"][0]["strings"]
    col_file, col_rect = header.index("FileName"), header.index("IconRect")
    rects = {
        row["strings"][col_rect]
        for row in table["rows"][1:]
        if len(row["strings"]) > col_rect and row["strings"][col_file] == asset
    }
    if len(rects) != 1:
        raise SystemExit(f"{asset}: expected one IconRect, found {sorted(rects)}")
    x, y, w, h = (int(v) for v in rects.pop().split(","))
    return x, y, x + w, y + h


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_stickers(export: Export, out: Path) -> list[dict]:
    out.mkdir(parents=True, exist_ok=True)
    rect = icon_rect(export, GRANTLEY_ASSET)
    records = []
    for slug, pattern in STICKERS.items():
        name = GRANTLEY_SPRITES.format(pattern=pattern)
        raw, info = export.read(name)
        face = Image.open(io.BytesIO(raw)).convert("RGBA").crop(rect)
        buf = io.BytesIO()
        face.save(buf, format="PNG", optimize=True)
        data = buf.getvalue()
        (out / f"{slug}.png").write_bytes(data)
        records.append(
            {
                "kind": "sticker",
                "slug": slug,
                "file": f"{slug}.png",
                "pattern": pattern,
                "source": name,
                "source_crc32": f"{info.CRC:08x}",
                "source_sha256": _sha256(raw),
                "icon_rect": list(rect),
                "size": list(face.size),
                "sha256": _sha256(data),
                "bytes": len(data),
            }
        )
    return records


def build_portraits(export: Export, out: Path) -> list[dict]:
    out.mkdir(parents=True, exist_ok=True)
    records = []
    for key, (filename, role) in PORTRAITS.items():
        if role is None:
            name = GRANTLEY_SPRITES.format(pattern=PORTRAIT_PATTERN)
        else:
            name = OTHER_SPRITES.format(role=role, pattern=PORTRAIT_PATTERN)
        raw, info = export.read(name)
        with Image.open(io.BytesIO(raw)) as im:
            im.load()
            if im.format != "PNG" or im.mode != "RGBA":
                raise SystemExit(f"{name}: expected an RGBA PNG, got {im.format} {im.mode}")
            size = list(im.size)
        (out / filename).write_bytes(raw)
        records.append(
            {
                "kind": "portrait",
                "key": key,
                "file": filename,
                "pattern": PORTRAIT_PATTERN,
                "source": name,
                "source_crc32": f"{info.CRC:08x}",
                "size": size,
                "sha256": _sha256(raw),
                "bytes": len(raw),
            }
        )
    return records


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("export", type=Path, help="骑士学院1和2_图片与台词全集.zip")
    ap.add_argument("--stickers", type=Path, help="write the sticker pack here")
    ap.add_argument("--characters", type=Path, help="write the 立绘 set here")
    ap.add_argument("--manifest", type=Path, help="write the provenance manifest here")
    args = ap.parse_args(argv)
    if not (args.stickers or args.characters):
        ap.error("nothing to do: pass --stickers and/or --characters")

    export = Export(args.export)
    records: list[dict] = []
    if args.stickers:
        records += build_stickers(export, args.stickers)
    if args.characters:
        records += build_portraits(export, args.characters)
    if args.manifest:
        args.manifest.write_text(
            json.dumps(
                {"export": args.export.name, "assets": records},
                ensure_ascii=False,
                indent=1,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
    for r in records:
        print(r["kind"], r.get("slug") or r.get("key"), r["file"], r["size"], r["bytes"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
