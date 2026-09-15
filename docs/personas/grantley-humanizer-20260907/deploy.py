"""Host-side narrow, CAS-checked deployment; only persona assets are replaced."""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path("/opt/hermes-cloud")
DATA = ROOT / "data"
STAGE = DATA / ".deployments/grantley-humanizer-20260907"
BACKUP = STAGE / "backup"
manifest = json.loads((STAGE / "manifest.json").read_text())


def run(*args):
    return subprocess.check_output(args, stderr=subprocess.STDOUT, timeout=120).decode()


def inspect(name):
    return json.loads(run("docker", "inspect", name))[0]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic(path, content, stat):
    fd, name = tempfile.mkstemp(prefix=".humanizer-", dir=path.parent)
    with os.fdopen(fd, "wb") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    os.chmod(name, stat.st_mode & 0o777)
    os.chown(name, stat.st_uid, stat.st_gid)
    os.replace(name, path)


def maintenance(mode):
    result = run("docker", "run", "--rm", "--network", "none", "--volumes-from", "hermes-cloud",
                 "--user", info["Config"]["User"], "-e", "HERMES_HOME=/opt/hermes/data",
                 "-e", "PYTHONDONTWRITEBYTECODE=1", info["Image"], "python",
                 "/opt/hermes/data/.deployments/grantley-humanizer-20260907/maintenance.py", mode)
    (STAGE / f"{mode}-result.txt").write_text(result)
    return result


gate = json.loads((STAGE / "acceptance.json").read_text())
assert gate["passed"] and gate["persona_sha256"] == manifest["after_sha256"]
assert not BACKUP.exists(), "Deployment already attempted; inspect saved results first"
info = inspect("hermes-cloud")
assert info["State"]["Running"] and info["Image"] == manifest["image"]
targets = [ROOT / "repo/plugins/grantley/assets/grantley.md", DATA / "plugins/grantley/assets/grantley.md"]
assert all(sha(p) == manifest["before_sha256"] for p in targets)
assert sha(DATA / "config.yaml") == manifest["config_sha256"]
assert sha(STAGE / "grantley.md") == manifest["after_sha256"]
protected = {}
for name in run("docker", "ps", "-a", "--format", "{{.Names}}").splitlines():
    if "snowluma" in name.lower() or "qq" in name.lower():
        state = inspect(name)["State"]
        protected[name] = {k: state[k] for k in ["StartedAt", "Pid"]}
# Require several quiet observations covering the existing 3-second input merge.
quiet = 0
for index in range(300):
    state = json.loads((DATA / "gateway_state.json").read_text())
    updated = datetime.fromisoformat(state["updated_at"].replace("Z", "+00:00")).timestamp()
    idle = state.get("gateway_state") == "running" and state.get("active_agents", 0) == 0 and time.time() - updated < 30
    quiet = quiet + 1 if idle else 0
    if quiet >= 5:
        break
    if index % 15 == 0:
        print(json.dumps({"phase": "waiting_for_idle", "active_agents": state.get("active_agents")}), flush=True)
    time.sleep(2)
else:
    raise RuntimeError("Gateway remained busy; nothing changed")
BACKUP.mkdir(mode=0o700)
stats = {str(path): path.stat() for path in targets}
for i, path in enumerate(targets):
    shutil.copy2(path, BACKUP / f"persona-{i}.md")
shutil.copy2(DATA / "config.yaml", BACKUP / "config.yaml")
stopped = False
revision_attempted = False
activated = False
try:
    run("docker", "stop", "--time", "45", "hermes-cloud")
    stopped = True
    assert sha(DATA / "config.yaml") == manifest["config_sha256"]
    assert all(sha(path) == manifest["before_sha256"] for path in targets)
    source = sqlite3.connect(f"file:{DATA}/state.db?mode=ro", uri=True)
    backup = sqlite3.connect(BACKUP / "state.db")
    source.backup(backup)
    source.close()
    backup.close()
    shutil.copy2(DATA / "sessions/sessions.json", BACKUP / "sessions.json")
    for path in targets:
        atomic(path, (STAGE / "grantley.md").read_bytes(), stats[str(path)])
    maintenance("verify")
    revision_attempted = True
    maintenance("revise")
    run("docker", "start", "hermes-cloud")
    activated = True
    started = datetime.fromisoformat(inspect("hermes-cloud")["State"]["StartedAt"].replace("Z", "+00:00")).timestamp()
    for _ in range(45):
        state = json.loads((DATA / "gateway_state.json").read_text())
        updated = datetime.fromisoformat(state["updated_at"].replace("Z", "+00:00")).timestamp()
        if updated >= started and state.get("gateway_state") == "running":
            break
        time.sleep(2)
    else:
        raise RuntimeError("Gateway readiness timed out")
    assert all(sha(path) == manifest["after_sha256"] for path in targets)
    assert sha(DATA / "config.yaml") == manifest["config_sha256"]
    unchanged = all({k: inspect(name)["State"][k] for k in before} == before for name, before in protected.items())
    assert unchanged, "Protected QQ runtime changed during deployment"
    result = {"deployed": True, "started_at": inspect("hermes-cloud")["State"]["StartedAt"],
              "gateway": state["gateway_state"], "onebot": state.get("platforms", {}).get("onebot", {}).get("state"),
              "config_unchanged": True, "qq_runtime_unchanged": unchanged, "backup": str(BACKUP),
              "persona_sha256": manifest["after_sha256"], "no_test_messages_sent": True}
    (STAGE / "deployed.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result), flush=True)
except BaseException as error:
    if stopped:
        if activated:
            run("docker", "stop", "--time", "45", "hermes-cloud")
        for i, path in enumerate(targets):
            atomic(path, (BACKUP / f"persona-{i}.md").read_bytes(), stats[str(path)])
        if revision_attempted:
            maintenance("rollback")
        run("docker", "start", "hermes-cloud")
    (STAGE / "failed.json").write_text(json.dumps({"deployed": False, "error_type": type(error).__name__,
                                                  "resources_restored": stopped, "database_overwritten": False}))
    raise
