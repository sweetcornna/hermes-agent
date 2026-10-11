"""Advertise native cards only after exact-body readback has passed."""
import hashlib,json,os,subprocess,time
from pathlib import Path
from datetime import datetime
ROOT=Path('/opt/hermes-cloud/data/.deployments/forward-card-20260906');Q='snowluma-native-replacement-r4-20260905'
def run(*a):return subprocess.check_output(a,stderr=subprocess.STDOUT).decode()
verified=json.loads((ROOT/'results/readback-verified.json').read_text());assert verified['verified']
for _ in range(120):
 s=json.loads(Path('/opt/hermes-cloud/data/gateway_state.json').read_text())
 if not s.get('active_agents',0):break
 time.sleep(1)
else:raise RuntimeError('Busy')
private=ROOT/'backup/native-before-enable.json';assert not private.exists()
run('docker','cp',Q+':/app/data/native-onebot.json',str(private));os.chmod(private,0o600)
config=json.loads(private.read_text());assert config.get('forwardEnabled') is not True
config['forwardEnabled']=True
next_file=ROOT/'native-enabled.private.json';next_file.write_text(json.dumps(config,indent=2)+'\n');os.chmod(next_file,0o600)
run('docker','stop','--time','45','hermes-cloud')
try:
 run('docker','exec',Q,'supervisorctl','stop','native-onebot')
 run('docker','cp',str(next_file),Q+':/app/data/native-onebot.json.next')
 run('docker','exec',Q,'python3','-c',"import os;p='/app/data/native-onebot.json';os.chown(p+'.next',1000,1000);os.chmod(p+'.next',0o600);os.replace(p+'.next',p)")
 run('docker','exec',Q,'supervisorctl','start','native-onebot')
finally:run('docker','start','hermes-cloud')
current=json.loads(run('docker','inspect','hermes-cloud'))[0]
started=datetime.fromisoformat(current['State']['StartedAt'].replace('Z','+00:00')).timestamp()
for _ in range(30):
 s=json.loads(Path('/opt/hermes-cloud/data/gateway_state.json').read_text())
 if datetime.fromisoformat(s['updated_at']).timestamp()>=started and s['gateway_state']=='running' and all(s['platforms'][k]['state']=='connected' for k in ['onebot','telegram']):break
 time.sleep(2)
else:raise RuntimeError('Readiness timeout')
result={'enabled':True,'gateway':'running','onebot':'connected','telegram':'connected','qq_restarted_for_enable':False,'started_at':current['State']['StartedAt'],'exact_body_readback':verified['expected_sha256']}
(ROOT/'enabled.json').write_text(json.dumps(result,indent=2));print(json.dumps(result),flush=True)
