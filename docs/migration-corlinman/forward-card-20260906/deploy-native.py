"""Install tested native card support; leave advertisement off until self-test."""
import hashlib,json,os,shutil,subprocess,time
from pathlib import Path
ROOT=Path('/opt/hermes-cloud'); HOME=ROOT/'data'; STAGE=HOME/'.deployments/forward-card-20260906'; BACKUP=STAGE/'backup'
QQ='snowluma-native-replacement-r4-20260905'; NATIVE='/opt/QQ/resources/app/snowluma-native-replacement'
def run(*a):return subprocess.check_output(a,stderr=subprocess.STDOUT).decode()
def inspect(name):return json.loads(run('docker','inspect',name))[0]
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
manifest=json.loads((STAGE/'manifest.json').read_text())
native_before={r['path']:r['sha256'] for r in map(json.loads,(STAGE/'native-baseline.jsonl').read_text().splitlines()) if 'path' in r}
hermes_before={r['name']:r['sha256'] for r in map(json.loads,(STAGE/'hermes-baseline.jsonl').read_text().splitlines())}
assert inspect('hermes-cloud')['State']['Running'] and inspect(QQ)['State']['Running']
for relative,digest in manifest.items():assert sha(STAGE/'upload'/relative)==digest
for name,digest in hermes_before.items():assert sha(ROOT/'repo/plugins/platforms/onebot'/name)==digest
probe="import json,hashlib;from pathlib import Path;print(json.dumps({str(p):hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None for p in [Path(x) for x in "+repr(list(native_before))+ "]}))"
assert json.loads(run('docker','exec',QQ,'python3','-c',probe))==native_before
for _ in range(120):
 state=json.loads((HOME/'gateway_state.json').read_text())
 if state['gateway_state']=='running' and not state.get('active_agents',0):break
 time.sleep(2)
else:raise RuntimeError('Gateway busy; nothing changed')
BACKUP.mkdir(mode=0o700)
for path,digest in native_before.items():
 if digest:run('docker','cp',QQ+':'+path,str(BACKUP/Path(path).name))
run('docker','cp',QQ+':/app/data/native-onebot.json',str(BACKUP/'native-onebot.json'))
os.chmod(BACKUP/'native-onebot.json',0o600)
for name in hermes_before:shutil.copy2(ROOT/'repo/plugins/platforms/onebot'/name,BACKUP/('hermes-'+name))
# Persist full current plugin entrypoint/support files for reconstructing this
# container independently of its writable layer. No QQ account data is copied.
run('docker','cp',QQ+':'+NATIVE,str(BACKUP/'native-runtime'))
activated=False
try:
 run('docker','stop','--time','45','hermes-cloud')
 run('docker','exec',QQ,'supervisorctl','stop','native-onebot')
 for relative in manifest:
  if not relative.startswith('native/'):continue
  target=NATIVE+'/'+relative.removeprefix('native/')
  run('docker','cp',str(STAGE/'upload'/relative),QQ+':'+target+'.next')
  run('docker','exec',QQ,'python3','-c',"import os; p="+repr(target)+"; os.chown(p+'.next',0,0);os.chmod(p+'.next',0o644);os.replace(p+'.next',p)")
 for name in hermes_before:
  p=ROOT/'repo/plugins/platforms/onebot'/name;s=p.stat();tmp=p.with_name(p.name+'.forward-next')
  tmp.write_bytes((STAGE/'upload/hermes'/name).read_bytes());os.chown(tmp,s.st_uid,s.st_gid);os.chmod(tmp,s.st_mode&0o777);os.replace(tmp,p)
 # Stop only the existing QQ main process, identified from its actual argv.
 qq_restart="""import os,signal,time,json
from pathlib import Path
pids=[]
for p in Path('/proc').iterdir():
 if not p.name.isdigit():continue
 try:args=(p/'cmdline').read_bytes().split(b'\\0')
 except OSError:continue
 if args and args[0]==b'/opt/QQ/qq' and not any(a.startswith(b'--type=') for a in args):pids.append(int(p.name))
assert len(pids)==1,pids
pid=pids[0];os.kill(pid,signal.SIGTERM)
for _ in range(50):
 try:
  state=Path('/proc',str(pid),'stat').read_text().rsplit(')',1)[1].strip().split()[0]
  if state=='Z':break
 except FileNotFoundError:break
 time.sleep(.2)
else:raise RuntimeError('QQ did not exit gracefully; no forced kill')
print(json.dumps({'restarted_qq_pid':pid}))
"""
 (STAGE/'qq-stop.json').write_text(run('docker','exec',QQ,'python3','-c',qq_restart))
 run('docker','exec',QQ,'supervisorctl','start','qq')
 activated=True
 run('docker','exec',QQ,'supervisorctl','start','native-onebot')
 run('docker','start','hermes-cloud')
 print(json.dumps({'installed':True,'qq_restarted':True,'forward_advertised':False,'self_test_required':True,'backup':str(BACKUP)}),flush=True)
except BaseException:
 # Keep Telegram available even when QQ needs login or further diagnosis.
 subprocess.run(['docker','exec',QQ,'supervisorctl','start','native-onebot'],capture_output=True)
 subprocess.run(['docker','start','hermes-cloud'],capture_output=True)
 print(json.dumps({'installed':False,'qq_restarted':activated,'backup':str(BACKUP),'gateway_start_requested':True}),flush=True)
 raise
