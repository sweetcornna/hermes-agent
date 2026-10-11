import hashlib,json,os,shutil,subprocess,time
from pathlib import Path
from datetime import datetime
home=Path('/opt/hermes-cloud/data'); stage=home/'.deployments/telegram-restore-20260906';stage.mkdir(exist_ok=True)
config=home/'config.yaml';baseline=hashlib.sha256(config.read_bytes()).hexdigest()
for _ in range(90):
 state=json.loads((home/'gateway_state.json').read_text())
 if state.get('active_agents',0)==0 and state['gateway_state']=='running':break
 time.sleep(2)
else:raise RuntimeError('Busy; nothing changed')
backup=stage/'config-before.yaml';assert not backup.exists();shutil.copy2(config,backup);os.chmod(backup,0o600)
def run(*a):return subprocess.check_output(a,stderr=subprocess.STDOUT).decode()
try:
 assert hashlib.sha256(config.read_bytes()).hexdigest()==baseline
 configured=run('docker','exec','hermes-cloud','python','/opt/hermes/data/.deployments/telegram-restore-configure.py','--apply')
 (stage/'configured.json').write_text(configured)
 run('docker','restart','--time','45','hermes-cloud')
 current=json.loads(run('docker','inspect','hermes-cloud'))[0]
 started=datetime.fromisoformat(current['State']['StartedAt'].replace('Z','+00:00')).timestamp()
 for _ in range(30):
  state=json.loads((home/'gateway_state.json').read_text())
  if datetime.fromisoformat(state['updated_at']).timestamp()>=started and state['gateway_state']=='running' and state['platforms']['telegram']['state']=='connected':break
  time.sleep(2)
 else:raise RuntimeError('Telegram readiness timeout')
 result={'restored':True,'chat':'Corn Agents','chat_id':-1003990634877,'telegram':state['platforms']['telegram']['state'],'onebot':state['platforms']['onebot']['state'],'started_at':current['State']['StartedAt'],'history_preserved':True,'no_messages_sent':True}
 (stage/'deployed.json').write_text(json.dumps(result));print(json.dumps(result))
except BaseException:
 shutil.copy2(backup,config);os.chmod(config,0o600);run('docker','restart','--time','45','hermes-cloud');raise
