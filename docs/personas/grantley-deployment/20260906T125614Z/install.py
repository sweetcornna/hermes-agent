"""Install only the reviewed persona files; compare before hashes and keep rollback."""
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

import yaml

stage=Path('/opt/hermes-cloud/data/.deployments/grantley-kc2-20260906T125614Z')
home=Path('/opt/hermes-cloud/data')
roots={'repo':Path('/opt/hermes-cloud/repo/plugins/grantley'),'installed':home/'plugins/grantley'}
manifest=json.loads((stage/'upload/manifest.json').read_text())
expected=json.loads((stage/'before-hashes.json').read_text())
config=yaml.safe_load((home/'config.yaml').read_text())
assert config['model']['default']=='gemini-3.8-flash-high'
assert config['providers']['cornna']['default_model']=='gemini-3.8-flash-high'
state=json.loads((home/'gateway_state.json').read_text())
assert state['gateway_state']=='running'
assert state.get('active_agents',0)==0,'Gateway is busy; wait for idle before installing'
for label,root in roots.items():
    for name in manifest:
        target=root/name
        assert hashlib.sha256(target.read_bytes()).hexdigest()==expected[label+'/'+name],str(target)+' changed after staging'
backup=stage/'backup'
backup.mkdir(mode=0o700,exist_ok=False)
entries=[]
for label,root in roots.items():
    for name in manifest:
        target=root/name
        saved=backup/label/name
        saved.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(target,saved)
        st=target.stat()
        entries.append({'label':label,'name':name,'target':str(target),'uid':st.st_uid,'gid':st.st_gid,'mode':st.st_mode&0o777,'before_sha256':hashlib.sha256(saved.read_bytes()).hexdigest(),'after_sha256':manifest[name]})
for name in ('config.yaml','SOUL.md'):
    shutil.copy2(home/name,backup/name)
(backup/'manifest.json').write_text(json.dumps(entries,indent=2)+'\n')


def replace(target,data,entry):
    fd,temporary=tempfile.mkstemp(prefix='.grantley-deploy-',dir=target.parent)
    try:
        with os.fdopen(fd,'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary,entry['mode'])
        os.chown(temporary,entry['uid'],entry['gid'])
        os.replace(temporary,target)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


written=[]
try:
    for entry in entries:
        target=Path(entry['target'])
        source=stage/'upload/plugin'/entry['name']
        data=source.read_bytes()
        assert hashlib.sha256(data).hexdigest()==entry['after_sha256']
        replace(target,data,entry)
        written.append(entry)
    assert all(hashlib.sha256(Path(e['target']).read_bytes()).hexdigest()==e['after_sha256'] for e in entries)
except Exception:
    for entry in reversed(written):
        replace(Path(entry['target']),(backup/entry['label']/entry['name']).read_bytes(),entry)
    raise
result={'ok':True,'updated_files':len(entries),'backup':str(backup),'config_changed':False,'model':config['model']['default'],'active_agents_before':state.get('active_agents',0),'platforms_before':{k:v.get('state') for k,v in state.get('platforms',{}).items() if isinstance(v,dict)}}
(stage/'installed.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result,ensure_ascii=False))
