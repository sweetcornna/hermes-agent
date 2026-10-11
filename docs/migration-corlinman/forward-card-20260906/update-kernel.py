import hashlib,json,subprocess,time
from pathlib import Path
ROOT=Path('/opt/hermes-cloud/data/.deployments/forward-card-20260906');Q='snowluma-native-replacement-r4-20260905';P='/opt/QQ/resources/app/snowluma-native-replacement/kernel-operations.cjs'
def run(*a):return subprocess.check_output(a,stderr=subprocess.STDOUT).decode()
for _ in range(90):
 state=json.loads(Path('/opt/hermes-cloud/data/gateway_state.json').read_text())
 if not state.get('active_agents',0):break
 time.sleep(2)
else:raise RuntimeError('Busy')
expected=json.loads((ROOT/'manifest.json').read_text())['native/kernel-operations.cjs']
actual=run('docker','exec',Q,'python3','-c',"import hashlib; print(hashlib.sha256(open("+repr(P)+",'rb').read()).hexdigest())").strip();assert actual==expected
run('docker','cp',Q+':'+P,str(ROOT/'backup/kernel-before-diagnostic.cjs'))
run('docker','stop','--time','45','hermes-cloud')
try:
 run('docker','exec',Q,'supervisorctl','stop','native-onebot')
 run('docker','cp',str(ROOT/'kernel-diagnostic.cjs'),Q+':'+P+'.next')
 run('docker','exec',Q,'python3','-c',"import os;p="+repr(P)+";os.chown(p+'.next',0,0);os.chmod(p+'.next',0o644);os.replace(p+'.next',p)")
 code="""import os,signal,time
from pathlib import Path
pids=[]
for p in Path('/proc').iterdir():
 if not p.name.isdigit():continue
 try:a=(p/'cmdline').read_bytes().split(b'\\0')
 except:continue
 if a and a[0]==b'/opt/QQ/qq' and not any(s.startswith(b'--type=') for s in a):pids.append(int(p.name))
assert len(pids)==1,pids
os.kill(pids[0],signal.SIGTERM)
for _ in range(50):
 try:
  if Path('/proc',str(pids[0]),'stat').read_text().rsplit(')',1)[1].strip().split()[0]=='Z':break
 except FileNotFoundError:break
 time.sleep(.2)
else:raise RuntimeError('QQ did not stop')
"""
 run('docker','exec',Q,'python3','-c',code)
 run('docker','exec',Q,'supervisorctl','start','qq')
 run('docker','exec',Q,'supervisorctl','start','native-onebot')
 print(json.dumps({'updated':True,'sha256':hashlib.sha256((ROOT/'kernel-diagnostic.cjs').read_bytes()).hexdigest()}))
finally:run('docker','start','hermes-cloud')
