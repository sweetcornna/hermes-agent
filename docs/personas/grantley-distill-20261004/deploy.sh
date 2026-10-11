#!/bin/bash
# Grantley persona re-distill 2026-10-04. Run as root on P1 with hermes.service STOPPED.
set -euo pipefail
systemctl is-active --quiet hermes.service && { echo "hermes is running — abort"; exit 1; }
TS=$(date -u +%Y%m%dT%H%M%SZ)
B=/opt/hermes/backups/persona-distill-$TS
mkdir -p $B
tar cf $B/grantley-data.tar --exclude=__pycache__ -C /opt/hermes/data/plugins grantley
tar cf $B/grantley-repo.tar --exclude=__pycache__ -C /opt/hermes/repo/plugins grantley
cp -a /opt/hermes/data/sessions/sessions.json $B/sessions.json
/opt/hermes/venv/bin/python - "$B" <<'PY'
import sqlite3, sys
b = sys.argv[1]
for src, dst in (("/opt/hermes/data/state.db", "state.db"), ("/opt/hermes/data/plugin-data/grantley/data.db", "grantley-data.db")):
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True); t = sqlite3.connect(f"{b}/{dst}"); s.backup(t); t.close(); s.close()
PY
(cd $B && sha256sum * > SHA256SUMS)
for root in /opt/hermes/data/plugins/grantley /opt/hermes/repo/plugins/grantley; do
  tar xf /opt/hermes/sim/persona/grantley-new.tar -C $root
  (cd $root && chown hermes: assets/grantley.md assets/voice.md assets/life_seeds/grantley.yaml memory_provider.py persona.py)
  sudo -u hermes /opt/hermes/venv/bin/python -m py_compile $root/persona.py $root/memory_provider.py
done
# Retire the pre-revision seed beats (missions, non-canon companions).
sudo -u hermes /opt/hermes/venv/bin/python - <<'PY'
import sqlite3
c = sqlite3.connect("/opt/hermes/data/plugin-data/grantley/data.db")
with c:
    n = c.execute("update life_events set salience=0 where persona_id='grantley' and id between 1 and 15").rowcount
print("retired", n)
PY
cd /opt/hermes/sim/persona
RUN="sudo -u hermes env HERMES_HOME=/opt/hermes/data PYTHONPATH=/opt/hermes/repo HERMES_DISABLE_LAZY_INSTALLS=1 /opt/hermes/venv/bin/python deploy_persona.py"
$RUN preflight | python3 -c "import json,sys; d=json.load(sys.stdin); print('preflight', d['passed'], len(d['revisions']), d['rollback_forks'])"
$RUN revise > $B/revise.json
python3 -c "import json; d=json.load(open('$B/revise.json')); print('revise', d['passed'], len(d['revisions']))"
$RUN verify
echo "backup=$B"
