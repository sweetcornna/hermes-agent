"""Host-side one-time deployment; preserves model, credentials and QQ runtime."""
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
DATA = ROOT/'data'
STAGE = DATA/'.deployments/official-search-20260907'
BACKUP = STAGE/'backup'
PLUGIN = DATA/'plugins/official-search'
manifest = json.loads((STAGE/'manifest.json').read_text())


def run(*args):
    return subprocess.check_output(args,stderr=subprocess.STDOUT,timeout=120).decode()


def inspect(name):
    return json.loads(run('docker','inspect',name))[0]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic(path, data, stat):
    fd, name = tempfile.mkstemp(prefix='.official-search-',dir=path.parent)
    with os.fdopen(fd,'wb') as out:
        out.write(data);out.flush();os.fsync(out.fileno())
    os.chmod(name,stat.st_mode & 0o777)
    os.chown(name,stat.st_uid,stat.st_gid)
    os.replace(name,path)


def maintenance(mode):
    output = run('docker','run','--rm','--network','none','--volumes-from','hermes-cloud',
                 '--user',info['Config']['User'],'-e','HERMES_HOME=/opt/hermes/data',
                 '-e','PYTHONDONTWRITEBYTECODE=1',info['Image'],'python',
                 '/opt/hermes/data/.deployments/official-search-20260907/maintenance.py',mode)
    (STAGE/f'{mode}-result.txt').write_text(output)
    return output


assert manifest['live_search_verified'] and manifest['tests_passed']
assert not BACKUP.exists() and not PLUGIN.exists()
assert sha(DATA/'config.yaml') == manifest['before_config_sha256']
assert sha(DATA/'.env') == manifest['credentials_sha256']
for name,expected in manifest['plugin_hashes'].items():
    assert sha(STAGE/'plugin'/name) == expected
info = inspect('hermes-cloud')
assert info['State']['Running'] and info['Image'] == manifest['image']
qq = inspect('snowluma-native-replacement-r4-20260905')['State']
qq_identity = {k:qq[k] for k in ['Pid','StartedAt']}
quiet = 0
for index in range(300):
    state = json.loads((DATA/'gateway_state.json').read_text())
    updated = datetime.fromisoformat(state['updated_at']).timestamp()
    idle = state['gateway_state']=='running' and state.get('active_agents',0)==0 and time.time()-updated < 30
    quiet = quiet+1 if idle else 0
    if quiet >= 5:
        break
    if index % 15 == 0:
        print(json.dumps({'phase':'waiting_for_idle','active_agents':state.get('active_agents')}),flush=True)
    time.sleep(2)
else:
    raise SystemExit('Gateway remained busy; no production changes')
assert sha(DATA/'config.yaml') == manifest['before_config_sha256']
BACKUP.mkdir(mode=0o700)
shutil.copy2(DATA/'config.yaml',BACKUP/'config.yaml')
stat = (DATA/'config.yaml').stat()
stopped = False
revised = False
activated = False
try:
    run('docker','stop','--time','45','hermes-cloud')
    stopped = True
    assert sha(DATA/'config.yaml') == manifest['before_config_sha256']
    src = sqlite3.connect(f'file:{DATA}/state.db?mode=ro',uri=True)
    dst = sqlite3.connect(BACKUP/'state.db');src.backup(dst);src.close();dst.close()
    # Host-side read-only SQLite connections can create WAL sidecars. Keep
    # their ownership identical to the database before the gateway reopens it.
    database_stat = (DATA/'state.db').stat()
    for suffix in ['-wal','-shm','-journal']:
        sidecar = DATA/('state.db'+suffix)
        if sidecar.exists() and not sidecar.is_symlink():
            os.chown(sidecar,database_stat.st_uid,database_stat.st_gid)
    shutil.copy2(DATA/'sessions/sessions.json',BACKUP/'sessions.json')
    shutil.copytree(STAGE/'plugin',PLUGIN)
    uid,gid = map(int,info['Config']['User'].split(':'))
    os.chown(PLUGIN,uid,gid);os.chmod(PLUGIN,0o755)
    for path in PLUGIN.iterdir():
        os.chown(path,uid,gid);os.chmod(path,0o644)
    # Use the container's YAML runtime; no new dependency on the host.
    update = """import json,os,tempfile,yaml
from pathlib import Path
p=Path('/opt/hermes/data/config.yaml');stat=p.stat();cfg=yaml.safe_load(p.read_text());settings=json.loads(Path('/opt/hermes/data/.deployments/official-search-20260907/settings.json').read_text())
enabled=cfg.setdefault('plugins',{}).setdefault('enabled',[])
if 'official-search' not in enabled: enabled.append('official-search')
web=cfg.setdefault('web',{})
if web is None: web={};cfg['web']=web
web['search_backend']='official-search';web['official_search']=settings
fd,name=tempfile.mkstemp(dir=p.parent,prefix='.official-search-')
with os.fdopen(fd,'w') as out: yaml.safe_dump(cfg,out,allow_unicode=True,sort_keys=False);out.flush();os.fsync(out.fileno())
os.chmod(name,stat.st_mode & 0o777);os.replace(name,p)
"""
    run('docker','run','--rm','--network','none','--volumes-from','hermes-cloud','--user',info['Config']['User'],info['Image'],'python','-c',update)
    maintenance('verify')
    revised = True
    maintenance('revise')
    run('docker','start','hermes-cloud')
    activated = True
    started = datetime.fromisoformat(inspect('hermes-cloud')['State']['StartedAt']).timestamp()
    for _ in range(45):
        state = json.loads((DATA/'gateway_state.json').read_text())
        if datetime.fromisoformat(state['updated_at']).timestamp() >= started and state['gateway_state']=='running':
            break
        time.sleep(2)
    else:
        raise RuntimeError('Gateway readiness timed out')
    assert sha(DATA/'.env') == manifest['credentials_sha256']
    assert all(sha(PLUGIN/name)==expected for name,expected in manifest['plugin_hashes'].items())
    assert {k:inspect('snowluma-native-replacement-r4-20260905')['State'][k] for k in qq_identity} == qq_identity
    result = {'deployed':True,'started_at':inspect('hermes-cloud')['State']['StartedAt'],
              'plugin_path':str(PLUGIN),'backup':str(BACKUP),'credentials_unchanged':True,'qq_runtime_unchanged':True,
              'config_sha256':sha(DATA/'config.yaml'),'no_chat_messages_sent':True}
    (STAGE/'deployed.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result),flush=True)
except BaseException as error:
    if stopped:
        if activated:
            run('docker','stop','--time','45','hermes-cloud')
        atomic(DATA/'config.yaml',(BACKUP/'config.yaml').read_bytes(),stat)
        if revised:
            maintenance('rollback')
        if PLUGIN.exists():
            shutil.move(str(PLUGIN),str(STAGE/'failed-plugin'))
        run('docker','start','hermes-cloud')
    (STAGE/'failed.json').write_text(json.dumps({'error_type':type(error).__name__,'database_overwritten':False}))
    raise
