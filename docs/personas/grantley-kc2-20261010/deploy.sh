#!/bin/bash
# Grantley KC2 revision 2026-10-10. Run as root on P1 with hermes.service STOPPED,
# from the staging directory stage.py produced (e.g. /opt/hermes/sim/kc2).
#
#   1. staged bytes match SHA256SUMS; production still holds BASE.sha256
#   2. backup: both grantley copies, the three code files, the 立绘 dir,
#      sessions.json, state.db, grantley data.db
#   3. install: persona text + 12 PNG stickers into both grantley copies
#      (the 9 old JPGs removed), sticker.py / characters.py / prompts.py into
#      the repo, 14 立绘 into the configured character dir
#   4. deploy_persona.py preflight -> revise -> verify
set -euo pipefail
S=$(cd "$(dirname "$0")" && pwd)
DATA=/opt/hermes/data
REPO=/opt/hermes/repo
RUN="sudo -u hermes env HERMES_HOME=$DATA PYTHONPATH=$REPO HERMES_DISABLE_LAZY_INSTALLS=1 /opt/hermes/venv/bin/python"
CODE="plugins/platforms/onebot/sticker.py plugins/image_gen/cornna/characters.py plugins/corlinman_jobs/prompts.py"

systemctl is-active --quiet hermes.service && { echo "hermes is running — abort"; exit 1; }
(cd "$S" && sha256sum -c --quiet SHA256SUMS)

# A sticker or 立绘 directory override would leave the new files unread.
if grep -nE "sticker_dir|ONEBOT_STICKER_DIR" "$DATA/config.yaml" "$DATA/.env" 2>/dev/null; then
  echo "sticker dir is overridden — install the PNGs there by hand, then rerun without this check"; exit 1
fi
CHARS=$(cd /tmp && $RUN -c 'from plugins.image_gen.cornna.characters import character_dir; print(character_dir())')
[ -d "$CHARS" ] || { echo "character dir $CHARS missing — abort"; exit 1; }

drift=0
while read -r sum rel; do
  case $rel in plugins/grantley/*) roots="$DATA $REPO" ;; *) roots="$REPO" ;; esac
  for root in $roots; do
    got=$(sha256sum "$root/$rel" 2>/dev/null | cut -d' ' -f1 || true)
    [ "$got" = "$sum" ] || { echo "drift: $root/$rel"; drift=1; }
  done
done < "$S/BASE.sha256"
[ $drift = 0 ] || { echo "production differs from the base this release was cut from — abort"; exit 1; }

TS=$(date -u +%Y%m%dT%H%M%SZ)
B=/opt/hermes/backups/persona-kc2-$TS
mkdir -p "$B"
tar cf "$B/grantley-data.tar" --exclude=__pycache__ -C "$DATA/plugins" grantley
tar cf "$B/grantley-repo.tar" --exclude=__pycache__ -C "$REPO/plugins" grantley
tar cf "$B/code.tar" -C "$REPO" $CODE
tar cf "$B/characters.tar" -C "$(dirname "$CHARS")" "$(basename "$CHARS")"
cp -a "$DATA/sessions/sessions.json" "$B/sessions.json"
/opt/hermes/venv/bin/python - "$B" <<'PY'
import sqlite3, sys
b = sys.argv[1]
for src, dst in (("/opt/hermes/data/state.db", "state.db"), ("/opt/hermes/data/plugin-data/grantley/data.db", "grantley-data.db")):
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True); t = sqlite3.connect(f"{b}/{dst}"); s.backup(t); t.close(); s.close()
PY
(cd "$B" && sha256sum -- * > SHA256SUMS)
echo "backup=$B"

for root in "$DATA" "$REPO"; do
  rm -f "$root"/plugins/grantley/assets/stickers/*.jpg
  tar xf "$S/release.tar" -C "$root" --no-same-owner plugins/grantley
  chown -R hermes: "$root/plugins/grantley/assets"
done
tar xf "$S/release.tar" -C "$REPO" --no-same-owner $CODE
(cd "$REPO" && chown hermes: $CODE && sudo -u hermes /opt/hermes/venv/bin/python -m py_compile $CODE)
install -o hermes -g hermes -m 0644 "$S"/characters/*.png "$CHARS/"
tar tf "$S/release.tar" | while read -r rel; do
  case $rel in plugins/grantley/*) roots="$DATA $REPO" ;; *) roots="$REPO" ;; esac
  for root in $roots; do
    cmp -s <(tar xOf "$S/release.tar" "$rel") "$root/$rel" || { echo "install mismatch: $root/$rel"; exit 1; }
  done
done
for f in "$S"/characters/*.png; do cmp -s "$f" "$CHARS/$(basename "$f")" || { echo "install mismatch: $f"; exit 1; }; done

cd "$S"
$RUN deploy_persona.py preflight | python3 -c "import json,sys; d=json.load(sys.stdin); print('preflight', d['passed'], len(d['revisions']), d['rollback_forks'])"
$RUN deploy_persona.py revise > "$B/revise.json"
python3 -c "import json; d=json.load(open('$B/revise.json')); print('revise', d['passed'], len(d['revisions']))"
$RUN deploy_persona.py verify | tee "$B/verify.json"
echo "done; backup=$B — start hermes.service"
