#!/bin/bash
# Undo deploy.sh. Run as root on P1 with hermes.service STOPPED, from the same
# staging directory:  bash rollback.sh /opt/hermes/backups/persona-kc2-<TS>
#
# Restores both grantley copies, the three code files and the 立绘 dir from the
# backup, then forks the bound lanes once more so they adopt the restored
# persona. Messages received after the deploy are kept; state.db is never
# copied back over them (it stays in the backup for a manual restore).
set -euo pipefail
S=$(cd "$(dirname "$0")" && pwd)
B=${1:?usage: rollback.sh BACKUP_DIR}
DATA=/opt/hermes/data
REPO=/opt/hermes/repo
RUN="sudo -u hermes env HERMES_HOME=$DATA PYTHONPATH=$REPO HERMES_DISABLE_LAZY_INSTALLS=1 /opt/hermes/venv/bin/python"

systemctl is-active --quiet hermes.service && { echo "hermes is running — abort"; exit 1; }
(cd "$B" && sha256sum -c --quiet SHA256SUMS)
CHARS=$(cd /tmp && $RUN -c 'from plugins.image_gen.cornna.characters import character_dir; print(character_dir())')

for pair in "grantley-data.tar:$DATA/plugins" "grantley-repo.tar:$REPO/plugins"; do
  dest=${pair#*:}
  rm -f "$dest"/grantley/assets/stickers/*.png
  tar xf "$B/${pair%%:*}" -C "$dest"
done
tar xf "$B/code.tar" -C "$REPO"
rm -f "$CHARS"/12_felix.png "$CHARS"/13_gamir.png "$CHARS"/14_bayfleet.png
tar xf "$B/characters.tar" -C "$(dirname "$CHARS")"

cd "$S"
$RUN deploy_persona.py rollback > "$B/rollback.json"
python3 -c "import json; d=json.load(open('$B/rollback.json')); print('rollback', d['passed'], len(d['revisions']))"
echo "restored from $B — start hermes.service"
