"""One-time LLBot deployment on cornna-cn; run as root, no credentials printed."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import yaml

IMAGE = "linyuchen/llbot:8.1.10@sha256:067159e742b529dedba5f0b2987290fb34b4627a1f605b83119e36ef76a81467"
BASE = Path("/opt/llbot")
HERMES = Path("/opt/hermes-cloud/data")
BACKUP = HERMES / ".deployments/llbot-20260908/backup"


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def private_write(path, data, uid=0, gid=0):
    path.write_text(data)
    path.chmod(0o600)
    os.chown(path, uid, gid)


assert os.geteuid() == 0
assert not BASE.exists(), "Existing LLBot install: inspect before changing it"
state = json.loads((HERMES / "gateway_state.json").read_text())
assert state.get("active_agents") == 0, "Wait for active agents before cutover"
config = yaml.safe_load((HERMES / "config.yaml").read_text())
assert config["platforms"]["onebot"]["extra"]["ws_url"] == "ws://127.0.0.1:3301"
BACKUP.mkdir(parents=True, mode=0o700)
for name in ("config.yaml", ".env"):
    shutil.copy2(HERMES / name, BACKUP / name)
adapter = Path("/opt/hermes-cloud/repo/plugins/platforms/onebot/adapter.py")
shutil.copy2(adapter, BACKUP / "adapter.py")
token = run("docker", "exec", "hermes-cloud", "python", "-c",
            'from dotenv import dotenv_values; print(dotenv_values("/opt/hermes/data/.env")["ONEBOT_ACCESS_TOKEN"])')
assert token
default = json.loads(run("docker", "run", "--rm", "--network", "none", "--entrypoint", "cat", IMAGE,
                         "/app/llbot/default_config.json"))
default["webui"] = {"enable": True, "host": "", "port": 3080}
default["ffmpeg"] = "/usr/bin/ffmpeg"
default["enableLocalFile2Url"] = True
default["ob11"] = {"enable": True, "connect": [{
    "name": "Hermes", "type": "ws", "enable": True, "host": "", "port": 3001,
    "heartInterval": 30000, "token": token, "reportSelfMessage": False,
    "reportOfflineMessage": False, "messageFormat": "array", "debug": False,
}]}
BASE.mkdir(mode=0o755)
(BASE / "data").mkdir(mode=0o700)
os.chown(BASE / "data", 991, 988)
(BASE / "config").mkdir(mode=0o755)
(BASE / "private").mkdir(mode=0o700)
import secrets
password = secrets.token_urlsafe(24)
private_write(BASE / "data/webui_token.txt", password + "\n", 991, 988)
private_write(BASE / "private/webui-password.txt", password + "\n")
private_write(BASE / "config/default_config.json", json.dumps(default, indent=2) + "\n", 991, 988)
private_write(BASE / "data/config_2952532060.json", json.dumps(default, indent=2) + "\n", 991, 988)
args = ["docker", "run", "-d", "--name", "luckylilliabot", "--init", "--restart", "on-failure:3",
        "--user", "991:988", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "--memory", "512m", "--memory-swap", "1g", "--pids-limit", "128",
        "--log-opt", "max-size=10m", "--log-opt", "max-file=3",
        "-p", "127.0.0.1:6099:3080", "-p", "127.0.0.1:3301:3001",
        "-v", "/opt/llbot/data:/app/llbot/data",
        "-v", "/opt/llbot/config/default_config.json:/app/llbot/default_config.json:ro",
        "--entrypoint", "node", IMAGE, "--enable-source-maps", "./llbot.js"]
container_id = run(*args)
# No automatic QQ account login; the user supplies LLBot Auth Token and scans the QR in WebUI.
result = {"image": IMAGE, "container": container_id, "webui": "https://qq.cornna.xyz/",
          "ws_url": "ws://127.0.0.1:3301", "backup": str(BACKUP),
          "qq_account_expected": "2952532060", "account_online": False,
          "remaining": "LLBot Auth Token and QQ QR login", "hermes_config_changed": False}
private_write(BASE / "private/deployment.json", json.dumps(result, indent=2) + "\n")
print(json.dumps(result, indent=2))
