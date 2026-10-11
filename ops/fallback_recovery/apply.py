"""Apply a reviewed prepare.py stage during an explicitly approved restart."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def systemctl(*args):
    subprocess.run(["systemctl", *args], check=True)


def write(target, data):
    before = target.stat() if target.exists() else None
    fd, temp = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        os.fchmod(handle.fileno(), (before.st_mode & 0o777) if before else 0o644)
        if before:
            os.fchown(handle.fileno(), before.st_uid, before.st_gid)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, target)


def apply(stage):
    manifest = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
    for row in manifest:
        path = Path(row["path"])
        actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        if actual != row["before_sha256"]:
            raise RuntimeError(f"Production changed since preparation: {path}")
        content = (stage / (row["slot"] + ".after")).read_bytes()
        assert hashlib.sha256(content).hexdigest() == row["after_sha256"]
    state = json.loads(Path("/opt/hermes/data/gateway_state.json").read_text(encoding="utf-8"))
    assert state["gateway_state"] == "running" and state.get("active_agents") == 0, "Wait until the gateway is idle"
    backup = Path("/opt/hermes/backups") / stage.name
    assert not backup.exists(), "Backup destination already exists"
    shutil.copytree(stage, backup)
    systemctl("stop", "hermes.service")
    try:
        for row in manifest:
            write(Path(row["path"]), (stage / (row["slot"] + ".after")).read_bytes())
        systemctl("daemon-reload")
        systemctl("start", "hermes.service")
        systemctl("enable", "--now", "hermes-fallback-recovery.timer")
        systemctl("is-active", "hermes.service", "hermes-fallback-recovery.timer")
    except BaseException:
        subprocess.run(["systemctl", "disable", "--now", "hermes-fallback-recovery.timer"], check=False)
        systemctl("stop", "hermes.service")
        for row in manifest:
            old = stage / (row["slot"] + ".before")
            if old.exists():
                write(Path(row["path"]), old.read_bytes())
        systemctl("daemon-reload")
        systemctl("start", "hermes.service")
        raise
    print(f"Applied; backup: {backup}")


if __name__ == "__main__":
    apply(Path(sys.argv[1]))
