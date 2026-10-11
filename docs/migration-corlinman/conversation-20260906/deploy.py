"""CAS-checked in-place gateway deployment, preserving QQ and chat history."""
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

ROOT = Path('/opt/hermes-cloud')
HOME = ROOT / 'data'
STAGE = HOME / '.deployments/conversation-20260906'
BACKUP = STAGE / 'backup'
IMAGE = 'sha256:8bce426d1e93c851cd676dc1807b822a7094d7f753139d7b9467e50dbfd79366'
QQ = 'snowluma-native-replacement-r4-20260905'

def run(*args):
    return subprocess.check_output(args, stderr=subprocess.STDOUT).decode()

def inspect(name):
    return json.loads(run('docker', 'inspect', name))[0]

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None

def replace(path, data, stat):
    fd, temp = tempfile.mkstemp(prefix='.conversation-', dir=path.parent)
    with os.fdopen(fd, 'wb') as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    os.chmod(temp, stat.st_mode & 0o777)
    os.chown(temp, stat.st_uid, stat.st_gid)
    os.replace(temp, path)

info = inspect('hermes-cloud')
qq_before = inspect(QQ)['State']['StartedAt']
assert info['State']['Running'] and info['Image'] == IMAGE
manifest = json.loads((STAGE / 'manifest.json').read_text())
for row in manifest:
    assert sha(Path(row['target'])) == row['before'], row['target']
    assert sha(STAGE / row['source']) == row['after']
config_hash = sha(HOME / 'config.yaml')
assert config_hash == '86064581ec071bc44e5e25a209faea74f44221e6d1b3a2c8cd9f51e57b3f6015'
for _ in range(120):
    state = json.loads((HOME / 'gateway_state.json').read_text())
    if state['gateway_state'] == 'running' and state.get('active_agents', 0) == 0:
        break
    time.sleep(2)
else:
    raise RuntimeError('Gateway did not become idle; nothing changed')
BACKUP.mkdir(mode=0o700)
shutil.copy2(HOME / 'config.yaml', BACKUP / 'config.yaml')
stats = {}
for index, row in enumerate(manifest):
    path = Path(row['target'])
    stats[str(path)] = path.stat() if path.exists() else (ROOT / 'repo/plugins/platforms/onebot/adapter.py').stat()
    if path.exists():
        shutil.copy2(path, BACKUP / f'file-{index}')
activated = False
stopped = False
def maintenance(script):
    return run('docker', 'run', '--rm', '--network', 'none', '--volumes-from', 'hermes-cloud',
               '--user', '991:988', '-e', 'HERMES_HOME=/opt/hermes/data',
               '-e', 'PYTHONDONTWRITEBYTECODE=1', IMAGE, 'python',
               '/opt/hermes/data/.deployments/conversation-20260906/' + script)
try:
    run('docker', 'stop', '--time', '45', 'hermes-cloud')
    stopped = True
    assert sha(HOME / 'config.yaml') == config_hash
    source = sqlite3.connect(HOME / 'state.db')
    destination = sqlite3.connect(BACKUP / 'state.db')
    source.backup(destination)
    source.close()
    destination.close()
    shutil.copy2(HOME / 'sessions/sessions.json', BACKUP / 'sessions.json')
    for row in manifest:
        path = Path(row['target'])
        assert sha(path) == row['before']
        replace(path, (STAGE / row['source']).read_bytes(), stats[str(path)])
    for script in ['configure.py', 'apply_revision.py', 'verify.py']:
        (STAGE / (script + '.result.txt')).write_text(maintenance(script))
    run('docker', 'start', 'hermes-cloud')
    activated = True
    current = inspect('hermes-cloud')
    started = datetime.fromisoformat(current['State']['StartedAt'].replace('Z', '+00:00')).timestamp()
    for _ in range(30):
        state = json.loads((HOME / 'gateway_state.json').read_text())
        updated = datetime.fromisoformat(state['updated_at']).timestamp()
        if updated >= started and state['gateway_state'] == 'running' and state['platforms']['onebot']['state'] == 'connected':
            break
        time.sleep(2)
    else:
        raise RuntimeError('Gateway readiness timeout')
    assert inspect(QQ)['State']['StartedAt'] == qq_before
    current = inspect('hermes-cloud')
    assert current['State']['Running'] and not current['State']['OOMKilled']
    assert all(sha(Path(row['target'])) == row['after'] for row in manifest)
    result = {'deployed': True, 'started_at': current['State']['StartedAt'],
              'gateway': state['gateway_state'], 'onebot': state['platforms']['onebot']['state'],
              'qq_not_restarted': True, 'history_preserved': True,
              'persona_revision': 'grantley-conversation-20260906',
              'native_forward_available': False, 'backup': str(BACKUP)}
    (STAGE / 'deployed.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result), flush=True)
except BaseException:
    if stopped:
        if activated:
            run('docker', 'stop', '--time', '45', 'hermes-cloud')
        for index, row in enumerate(manifest):
            path = Path(row['target'])
            if row['before'] is None:
                path.unlink(missing_ok=True)
            else:
                replace(path, (BACKUP / f'file-{index}').read_bytes(), stats[str(path)])
        shutil.copy2(BACKUP / 'config.yaml', HOME / 'config.yaml')
        # Once activated, never roll back the DB over newly received messages.
        if not activated and (BACKUP / 'state.db').exists():
            for suffix in ['-wal', '-shm', '-journal']:
                (HOME / ('state.db' + suffix)).unlink(missing_ok=True)
            shutil.copy2(BACKUP / 'state.db', HOME / 'state.db')
            shutil.copy2(BACKUP / 'sessions.json', HOME / 'sessions/sessions.json')
        run('docker', 'start', 'hermes-cloud')
    print(json.dumps({'deployed': False, 'rollback': stopped, 'new_history_preserved': activated}), flush=True)
    raise
