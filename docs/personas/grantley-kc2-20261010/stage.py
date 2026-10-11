"""Cut the KC2 revision into a staging directory that deploy.sh installs on P1.

Run on the machine that has the game export, from the repository root::

    python stage.py EXPORT.zip OUT

OUT gets:

* ``release.tar``: the changed repository files, repo-relative
  (``plugins/...``), taken from the working tree.
* ``characters/``: the 14 立绘, rebuilt from the export by build_assets.py.
  They live only in ``$HERMES_HOME/characters``, never in the repository.
* ``BASE.sha256``: what production must still hold at each path this release
  replaces or deletes. These are the HEAD blobs, and deploy.sh refuses to
  install over anything else.
* ``deploy.sh``, ``rollback.sh``, ``deploy_persona.py``, and ``SHA256SUMS``
  covering every file above.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import build_assets  # noqa: E402

REPO = HERE.parents[2]
ASSETS = "plugins/grantley/assets"
TEXT = [
    f"{ASSETS}/grantley.md",
    f"{ASSETS}/voice.md",
    f"{ASSETS}/life_seeds/grantley.yaml",
    "plugins/platforms/onebot/sticker.py",
    "plugins/image_gen/cornna/characters.py",
    "plugins/corlinman_jobs/prompts.py",
]
STICKERS = [f"{ASSETS}/stickers/{slug}.png" for slug in build_assets.STICKERS]
RETIRED = [
    f"{ASSETS}/stickers/{name}.jpg"
    for name in ("angry", "fired-up", "flustered", "heart-hug", "laughing",
                 "shrug", "thinking", "thumbs-up", "unimpressed")
]


def _git(*args: str) -> bytes:
    return subprocess.run(["git", "-C", str(REPO), *args], check=True,
                          capture_output=True).stdout


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        raise SystemExit(__doc__)
    export, out = Path(argv[0]), Path(argv[1])
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} is not empty")
    out.mkdir(parents=True, exist_ok=True)

    for rel in TEXT:
        if b"\r\n" in (REPO / rel).read_bytes():
            raise SystemExit(f"{rel} has CRLF line endings")
    with tarfile.open(out / "release.tar", "w", format=tarfile.PAX_FORMAT) as tar:
        for rel in TEXT + STICKERS:
            info = tar.gettarinfo(str(REPO / rel), arcname=rel)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o644
            with open(REPO / rel, "rb") as fh:
                tar.addfile(info, fh)

    # Ship only what the provenance manifest recorded, byte for byte.
    manifest = json.loads((HERE / "assets-manifest.json").read_text("utf-8"))["assets"]
    expected = {(r["kind"], r["file"]): r["sha256"] for r in manifest}
    for rel in STICKERS:
        if _sha((REPO / rel).read_bytes()) != expected[("sticker", Path(rel).name)]:
            raise SystemExit(f"{rel} differs from assets-manifest.json")
    portraits = build_assets.build_portraits(build_assets.Export(export), out / "characters")
    for record in portraits:
        if record["sha256"] != expected[("portrait", record["file"])]:
            raise SystemExit(f"{record['file']} differs from assets-manifest.json")

    base = [f"{_sha(_git('show', f'HEAD:{rel}'))}  {rel}" for rel in TEXT + RETIRED]
    (out / "BASE.sha256").write_bytes(("\n".join(base) + "\n").encode())

    for name in ("deploy.sh", "rollback.sh", "deploy_persona.py"):
        shutil.copyfile(HERE / name, out / name)

    files = sorted(p for p in out.rglob("*") if p.is_file())
    sums = [f"{_sha(p.read_bytes())}  {p.relative_to(out).as_posix()}" for p in files]
    (out / "SHA256SUMS").write_bytes(("\n".join(sums) + "\n").encode())
    print(f"staged {len(TEXT) + len(STICKERS)} repo files, {len(portraits)} 立绘 -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
