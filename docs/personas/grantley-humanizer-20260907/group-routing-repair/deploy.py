"""CAS-checked plugin routing repair. Preserve current config, QQ and history."""
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
STAGE = DATA / ".deployments/grantley-humanizer-20260907/group-routing-repair"
BACKUP = STAGE / "backup"
manifest = json.loads((STAGE / "manifest.json").read_text())


def run(*args):
    return subprocess.check_output(args, stderr=subprocess.STDOUT, timeout=120).decode()


def inspect(name):
    return json.loads(run("docker", "inspect", name))[0]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic(path, content, stat):
    fd, name = tempfile.mkstemp(prefix=".grantley-routing-", dir=path.parent)
    with os.fdopen(fd, "wb") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    os.chmod(name, stat.st_mode & 0o777)
    os.chown(name, stat.st_uid, stat.st_gid)
    os.replace(name, path)


def maintain(mode):
    result = run("docker", "run", "--rm", "--network", "none", "--volumes-from", "hermes-cloud",
                 "--user", manifest["user"], "-e", "HERMES_HOME=/opt/hermes/data",
                 "-e", "PYTHONDONTWRITEBYTECODE=1", manifest["image"], "python",
                 "/opt/hermes/data/.deployments/grantley-humanizer-20260907/group-routing-repair/maintenance.py", mode)
    (STAGE / f"{mode}-result.txt").write_text(result)
    return next(json.loads(line) for line in result.splitlines() if line.startswith('{"passed":'))


assert not BACKUP.exists(), "An earlier attempt exists; inspect before retry"
assert inspect("hermes-cloud")["State"]["Running"]
assert inspect("hermes-cloud")["Image"] == manifest["image"]
assert sha(DATA / "config.yaml") == manifest["config_sha256"]
assert sha(STAGE / "channel_binding.py") == manifest["after_sha256"]
for row in manifest["files"]:
    assert sha(Path(row["path"])) == row["sha256"]
protected = {}
for name in run("docker", "ps", "-a", "--format", "{{.Names}}").splitlines():
    if "qq" in name.lower() or "snowluma" in name.lower():
        state = inspect(name)["State"]
        protected[name] = {key: state[key] for key in ["StartedAt", "Pid"]}
quiet = 0
for i in range(90):
    state = json.loads((DATA / "gateway_state.json").read_text())
    stamp = datetime.fromisoformat(state["updated_at"].replace("Z", "+00:00")).timestamp()
    quiet = quiet + 1 if state.get("gateway_state") == "running" and not state.get("active_agents") and time.time() - stamp < 30 else 0
    if quiet >= 5:
        break
    if i % 15 == 0:
        print(json.dumps({"phase": "waiting_idle", "active_agents": state.get("active_agents")}), flush=True)
    time.sleep(2)
else:
    raise RuntimeError("Gateway remained busy; no production mutation")
BACKUP.mkdir(mode=0o700)
stats = {}
for i, row in enumerate(manifest["files"]):
    path = Path(row["path"])
    stats[str(path)] = path.stat()
    shutil.copy2(path, BACKUP / f"binding-{i}.py")
shutil.copy2(DATA / "config.yaml", BACKUP / "config.yaml")
stopped = activated = revision_attempted = False
try:
    run("docker", "stop", "--time", "45", "hermes-cloud")
    stopped = True
    assert sha(DATA / "config.yaml") == manifest["config_sha256"]
    source = sqlite3.connect(f"file:{DATA}/state.db?mode=ro", uri=True)
    backup = sqlite3.connect(BACKUP / "state.db")
    source.backup(backup)
    source.close()
    backup.close()
    shutil.copy2(DATA / "sessions/sessions.json", BACKUP / "sessions.json")
    for row in manifest["files"]:
        path = Path(row["path"])
        assert sha(path) == row["sha256"]
        atomic(path, (STAGE / "channel_binding.py").read_bytes(), stats[str(path)])
    verification = maintain("verify")
    assert verification["passed"]
    revision_attempted = True
    revised = maintain("apply")
    run("docker", "start", "hermes-cloud")
    activated = True
    started = datetime.fromisoformat(inspect("hermes-cloud")["State"]["StartedAt"].replace("Z", "+00:00")).timestamp()
    for i in range(45):
        state = json.loads((DATA / "gateway_state.json").read_text())
        stamp = datetime.fromisoformat(state["updated_at"].replace("Z", "+00:00")).timestamp()
        if state.get("gateway_state") == "running" and stamp >= started:
            break
        time.sleep(2)
    else:
        raise RuntimeError("Gateway did not become ready")
    assert sha(DATA / "config.yaml") == manifest["config_sha256"]
    assert sha(DATA / "plugins/grantley/assets/grantley.md") == manifest["persona_sha256"]
    assert all(sha(Path(row["path"])) == manifest["after_sha256"] for row in manifest["files"])
    assert all({key: inspect(name)["State"][key] for key in before} == before for name, before in protected.items())
    result = {"deployed": True, "started_at": inspect("hermes-cloud")["State"]["StartedAt"],
              "binding_sha256": manifest["after_sha256"], "persona_unchanged": True,
              "config_unchanged": True, "qq_runtime_unchanged": True,
              "canonical_session_checks": verification["canonical_sessions"],
              "revisions": revised["revisions"], "unchanged_groups": revised["unchanged_group_sessions"],
              "gateway": state["gateway_state"], "onebot": state.get("platforms", {}).get("onebot", {}).get("state"),
              "no_test_messages_sent": True, "backup": str(BACKUP)}
    (STAGE / "deployed.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result), flush=True)
except BaseException:
    if stopped:
        if activated:
            run("docker", "stop", "--time", "45", "hermes-cloud")
        for i, row in enumerate(manifest["files"]):
            path = Path(row["path"])
            atomic(path, (BACKUP / f"binding-{i}.py").read_bytes(), stats[str(path)])
        if revision_attempted:
            maintain("rollback")
        run("docker", "start", "hermes-cloud")
    raise
