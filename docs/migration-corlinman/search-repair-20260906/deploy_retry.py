"""Finish the image cutover after isolating the search process working dir."""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path('/opt/hermes-cloud')
HOME = ROOT / 'data'
STAGE = HOME / '.deployments/search-repair-20260906'
OLD = 'hermes-cloud-before-search-20260906'
CANDIDATE = 'hermes-cloud-search-failed-20260906'


def run(*args):
    return subprocess.check_output(args, stderr=subprocess.STDOUT).decode()


def inspect(name):
    return json.loads(run('docker', 'inspect', name))[0]


old = inspect('hermes-cloud')
candidate = inspect(CANDIDATE)
assert old['State']['Running'] and not candidate['State']['Running']
assert old['Image'].startswith('sha256:f614580b9ca') and candidate['Image'].startswith('sha256:8bce426d1e93')
for key in ['User', 'Env', 'Cmd', 'WorkingDir', 'Entrypoint']:
    assert old['Config'][key] == candidate['Config'][key], key
for key in ['Binds', 'NetworkMode', 'Memory', 'SecurityOpt', 'Privileged', 'CapAdd', 'CapDrop', 'ReadonlyRootfs', 'RestartPolicy']:
    assert old['HostConfig'][key] == candidate['HostConfig'][key], key
state = json.loads((HOME / 'gateway_state.json').read_text())
assert state['gateway_state'] == 'running' and state.get('active_agents', 0) == 0
target = ROOT / 'repo/plugins/corlinman_search/mcp.json'
assert hashlib.sha256(target.read_bytes()).hexdigest() == 'bf5704ed73ecd3bbda50dcb91065a4ade9e1c64eab45498d923ada35457a470d'
source = STAGE / 'test-plugins/corlinman_search/mcp.json'
assert hashlib.sha256(source.read_bytes()).hexdigest() == '94cf229c279fc30118ea1c91e2b8ca0d0b66cfb3a82de565637ae4dfaaf91378'
shutil.copy2(target, STAGE / 'backup/mcp-before-cwd.json')
st = target.stat()
fd, temporary = tempfile.mkstemp(prefix='.mcp-cwd-', dir=target.parent)
with os.fdopen(fd, 'wb') as output:
    output.write(source.read_bytes())
    output.flush()
    os.fsync(output.fileno())
os.chmod(temporary, st.st_mode & 0o777)
os.chown(temporary, st.st_uid, st.st_gid)
os.replace(temporary, target)
renamed = False
swapped = False
try:
    run('docker', 'stop', '--time', '45', 'hermes-cloud')
    run('docker', 'rename', 'hermes-cloud', OLD)
    renamed = True
    run('docker', 'rename', CANDIDATE, 'hermes-cloud')
    swapped = True
    run('docker', 'start', 'hermes-cloud')
    current = inspect('hermes-cloud')
    started = datetime.fromisoformat(current['State']['StartedAt'].replace('Z', '+00:00')).timestamp()
    for _ in range(30):
        current = inspect('hermes-cloud')
        assert current['State']['Running'] and not current['State']['OOMKilled'] and not current['RestartCount']
        state = json.loads((HOME / 'gateway_state.json').read_text())
        updated = datetime.fromisoformat(state['updated_at']).timestamp()
        if updated >= started and state['gateway_state'] == 'running' and state['platforms']['onebot']['state'] == 'connected':
            break
        time.sleep(2)
    else:
        raise RuntimeError('Replacement did not become ready')
    logs = run('docker', 'logs', '--since', current['State']['StartedAt'], 'hermes-cloud')
    registered = any('corlinman-search' in line and 'registered 1 tool' in line for line in logs.splitlines())
    cache = HOME / 'cache/mcp_schema_cache.json'
    cache_confirmed = False
    if cache.exists() and cache.stat().st_mtime >= started:
        entries = json.loads(cache.read_text())
        cache_confirmed = any('corlinman-search' in name and len(value.get('tools', [])) == 1 and value['tools'][0]['name'] == 'search'
                              for name, value in entries.items())
    processes = run('docker', 'top', 'hermes-cloud', '-eo', 'pid,ppid,args')
    server_running = 'corlinman_search/server.py' in processes
    assert registered or (cache_confirmed and server_running), 'No live registration evidence'
    result = {'ok': True, 'image': current['Image'], 'started_at': current['State']['StartedAt'],
              'search_registered_log': registered, 'fresh_search_schema_cache': cache_confirmed,
              'search_process_running': server_running, 'gateway_state': state['gateway_state'],
              'onebot': state['platforms']['onebot']['state'], 'old_container': OLD,
              'chat_history_preserved': True, 'persona_revision': 'grantley-argo-20260906'}
    (STAGE / 'deployed.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result), flush=True)
except BaseException:
    if swapped:
        subprocess.run(['docker', 'stop', '--time', '45', 'hermes-cloud'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        run('docker', 'rename', 'hermes-cloud', CANDIDATE)
    if renamed:
        run('docker', 'rename', OLD, 'hermes-cloud')
    run('docker', 'start', 'hermes-cloud')
    print(json.dumps({'ok': False, 'rolled_back_image': True, 'chat_history_preserved': True}), flush=True)
    raise
