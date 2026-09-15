"""Stage only this revision's assets and run migration against a private snapshot."""
import base64
import hashlib
import json
import shlex
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
before = json.loads((HERE / "production-before.json").read_text())
runtime = json.loads((HERE / "runtime-before.json").read_text())
persona = (ROOT / "plugins/grantley/assets/grantley.md").read_bytes()
manifest = {
    "image": runtime["image"], "before_sha256": before["asset_sha256"],
    "after_sha256": hashlib.sha256(persona).hexdigest(),
    "config_sha256": before["config_sha256"],
}
(HERE / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
files = {name: (HERE / name).read_bytes() for name in
         ["maintenance.py", "revise_sessions.py", "deploy.py", "manifest.json"]}
files["grantley.md"] = persona
payload = {name: base64.b64encode(content).decode() for name, content in files.items()}
remote = '''import base64,json,os,sys
from pathlib import Path
stage=Path('/opt/hermes-cloud/data/.deployments/grantley-humanizer-20260907')
stage.mkdir(mode=0o700)
os.chown(stage,991,988)
for name, encoded in json.load(sys.stdin).items():
    assert Path(name).name==name
    path=stage/name
    with path.open('xb') as output: output.write(base64.b64decode(encoded))
    path.chmod(0o600)
    os.chown(path,991,988)
print(json.dumps({'staged':True,'path':str(stage)}))
'''
result = subprocess.run(["ssh", "-o", "BatchMode=yes", "cornna-cn",
                         shlex.join(["python3", "-c", remote])],
                        input=json.dumps(payload), capture_output=True, text=True, timeout=30)
if result.returncode:
    raise SystemExit(f"Staging failed ({result.returncode}); inspect remote stage before retrying")
print(result.stdout, flush=True)
command = shlex.join(["docker", "exec", "hermes-cloud", "python",
                     "/opt/hermes/data/.deployments/grantley-humanizer-20260907/maintenance.py", "preflight"])
result = subprocess.run(["ssh", "-o", "BatchMode=yes", "cornna-cn", command],
                        capture_output=True, text=True, timeout=60)
(HERE / "migration-preflight.txt").write_text(result.stdout)
if result.returncode:
    (HERE / "migration-preflight-error.txt").write_text(result.stderr)
    raise SystemExit("Private migration preflight failed; no live route changed")
print(result.stdout, flush=True)
