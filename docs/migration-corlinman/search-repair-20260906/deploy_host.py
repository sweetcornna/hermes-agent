"""Deploy verified search dependencies and Argo identity without restarting QQ.

Clones the current Docker configuration through the engine API, preserving
credentials in a private server-side backup and keeping them out of argv/logs.
Before activation, failures restore files and DB. After activation, rollback
keeps all newly received messages and persona changes while reverting the image.
"""
import copy
import hashlib
import http.client
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path('/opt/hermes-cloud')
HOME = ROOT / 'data'
STAGE = HOME / '.deployments/search-repair-20260906'
BACKUP = STAGE / 'backup'
OLD_NAME = 'hermes-cloud-before-search-20260906'
EXPECTED_IMAGE = 'sha256:f614580b9ca33379c09f68dc6b23578f1a05df411ce02d2b3e3ae8c30280418b'


class DockerConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect('/var/run/docker.sock')


def docker_api(method, path, payload=None):
    connection = DockerConnection('localhost', timeout=60)
    try:
        body = json.dumps(payload).encode() if payload is not None else None
        connection.request(method, path, body=body, headers={'Content-Type': 'application/json'})
        response = connection.getresponse()
        data = response.read()
        if response.status >= 400:
            raise RuntimeError(f'Docker API {method} {path} failed ({response.status})')
        return json.loads(data) if data else None
    finally:
        connection.close()


def run(*args):
    return subprocess.check_output(args, stderr=subprocess.STDOUT).decode()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replace(path, data, stat):
    fd, temp = tempfile.mkstemp(prefix='.search-repair-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temp, stat.st_mode & 0o777)
        os.chown(temp, stat.st_uid, stat.st_gid)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


info = docker_api('GET', '/containers/hermes-cloud/json')
assert info['State']['Running'] and info['Image'] == EXPECTED_IMAGE
assert subprocess.run(['docker', 'inspect', OLD_NAME], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0
state = json.loads((HOME / 'gateway_state.json').read_text())
assert state['gateway_state'] == 'running' and state.get('active_agents', 0) == 0
manifest = json.loads((STAGE / 'manifest.json').read_text())
for entry in manifest:
    assert sha(Path(entry['target'])) == entry['before'], entry['target'] + ' changed after review'
    assert sha(STAGE / entry['source']) == entry['after']
image_info = docker_api('GET', '/images/hermes-cloud:search-20260906/json')
new_image = image_info['Id']
BACKUP.mkdir(mode=0o700)
(BACKUP / 'container.json').write_text(json.dumps(info))
os.chmod(BACKUP / 'container.json', 0o600)
file_stats = {}
for index, entry in enumerate(manifest):
    path = Path(entry['target'])
    file_stats[str(path)] = path.stat()
    shutil.copy2(path, BACKUP / f'file-{index}')
for name in ['requirements.txt', 'Dockerfile']:
    shutil.copy2(ROOT / name, BACKUP / name)
shutil.copy2(HOME / 'config.yaml', BACKUP / 'config.yaml')
config_hash = sha(HOME / 'config.yaml')
activated = False
renamed = False
created = False
stopped = False
try:
    run('docker', 'stop', '--time', '45', 'hermes-cloud')
    stopped = True
    source = sqlite3.connect(HOME / 'state.db')
    destination = sqlite3.connect(BACKUP / 'state.db')
    source.backup(destination)
    source.close()
    destination.close()
    shutil.copy2(HOME / 'sessions/sessions.json', BACKUP / 'sessions.json')
    for entry in manifest:
        path = Path(entry['target'])
        replace(path, (STAGE / entry['source']).read_bytes(), file_stats[str(path)])
    dependencies = json.loads(run('docker', 'run', '--rm', '--network', 'none', new_image,
                                 'python', '-c', 'from pathlib import Path; print(Path("/opt/hermes/search-dependencies.json").read_text())'))
    additions = sorted(f"{item['metadata']['name']}=={item['metadata']['version']}" for item in dependencies['install'])
    requirements = ROOT / 'requirements.txt'
    data = requirements.read_text().rstrip() + '\n\n# Search MCP runtime (verified 2026-09-06)\n' + '\n'.join(additions) + '\n'
    replace(requirements, data.encode(), requirements.stat())
    revision_output = run('docker', 'run', '--rm', '--network', 'none', '--volumes-from', 'hermes-cloud',
                         '--user', '991:988', '-e', 'HERMES_HOME=/opt/hermes/data',
                         '-e', 'PYTHONDONTWRITEBYTECODE=1', new_image, 'python',
                         '/opt/hermes/data/.deployments/search-repair-20260906/apply_revision.py')
    (STAGE / 'revision-applied.txt').write_text(revision_output)
    assert sha(HOME / 'config.yaml') == config_hash
    run('docker', 'rename', 'hermes-cloud', OLD_NAME)
    renamed = True
    config = copy.deepcopy(info['Config'])
    config['Image'] = new_image
    config['HostConfig'] = copy.deepcopy(info['HostConfig'])
    docker_api('POST', '/containers/create?name=hermes-cloud', config)
    created = True
    docker_api('POST', '/containers/hermes-cloud/start')
    activated = True
    started_at = datetime.now(timezone.utc)
    ready = None
    for _ in range(30):
        current = docker_api('GET', '/containers/hermes-cloud/json')
        if not current['State']['Running'] or current['RestartCount']:
            raise RuntimeError('Replacement gateway did not remain running')
        ready = json.loads((HOME / 'gateway_state.json').read_text())
        updated = datetime.fromisoformat(ready['updated_at'])
        if updated >= started_at and ready['gateway_state'] == 'running' and ready['platforms'].get('onebot', {}).get('state') == 'connected':
            break
        time.sleep(2)
    else:
        raise RuntimeError('Gateway/QQ readiness timeout')
    logs = run('docker', 'logs', '--since', current['State']['StartedAt'], 'hermes-cloud')
    search_registered = any('corlinman-search' in line and 'registered 1 tool' in line for line in logs.splitlines())
    if not search_registered:
        for filename in ['agent.log', 'gateway.log']:
            path = HOME / 'logs' / filename
            if path.exists():
                recent = path.read_text(errors='replace').splitlines()[-3000:]
                search_registered = search_registered or any('corlinman-search' in line and 'registered 1 tool' in line for line in recent)
    assert search_registered, 'Live gateway did not confirm search tool registration'
    result = {'ok': True, 'image_before': EXPECTED_IMAGE, 'image_after': new_image,
              'started_at': current['State']['StartedAt'], 'old_container': OLD_NAME,
              'search_registered': True, 'gateway_state': ready['gateway_state'],
              'onebot': ready['platforms']['onebot']['state'], 'config_unchanged': True,
              'runtime_files_updated': len(manifest), 'dependency_pins': additions,
              'backup': str(BACKUP)}
    (STAGE / 'deployed.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result), flush=True)
except BaseException:
    if created:
        subprocess.run(['docker', 'stop', '--time', '45', 'hermes-cloud'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(['docker', 'rename', 'hermes-cloud', 'hermes-cloud-search-failed-20260906'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not activated and stopped:
        for index, entry in enumerate(manifest):
            path = Path(entry['target'])
            replace(path, (BACKUP / f'file-{index}').read_bytes(), file_stats[str(path)])
        shutil.copy2(BACKUP / 'requirements.txt', ROOT / 'requirements.txt')
        if (BACKUP / 'state.db').exists():
            for suffix in ['-wal', '-shm', '-journal']:
                (HOME / ('state.db' + suffix)).unlink(missing_ok=True)
            shutil.copy2(BACKUP / 'state.db', HOME / 'state.db')
            shutil.copy2(BACKUP / 'sessions.json', HOME / 'sessions/sessions.json')
    if renamed:
        run('docker', 'rename', OLD_NAME, 'hermes-cloud')
    if stopped:
        run('docker', 'start', 'hermes-cloud')
    print(json.dumps({'ok': False, 'rolled_back_image': True, 'retained_new_history': activated}), flush=True)
    raise
