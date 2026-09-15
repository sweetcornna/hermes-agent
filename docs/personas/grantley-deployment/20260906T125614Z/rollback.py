"""Restore this deployment's eight files only; config and histories stay untouched."""
from pathlib import Path
import hashlib,json,os,tempfile
backup=Path('/opt/hermes-cloud/data/.deployments/grantley-kc2-20260906T125614Z/backup')
entries=json.loads((backup/'manifest.json').read_text())
for entry in entries:
    data=(backup/entry['label']/entry['name']).read_bytes()
    assert hashlib.sha256(data).hexdigest()==entry['before_sha256']
for entry in entries:
    target=Path(entry['target'])
    data=(backup/entry['label']/entry['name']).read_bytes()
    fd,temporary=tempfile.mkstemp(prefix='.grantley-rollback-',dir=target.parent)
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(data);f.flush();os.fsync(f.fileno())
        os.chmod(temporary,entry['mode']);os.chown(temporary,entry['uid'],entry['gid'])
        os.replace(temporary,target)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)
print(json.dumps({'restored':len(entries),'ok':all(hashlib.sha256(Path(e['target']).read_bytes()).hexdigest()==e['before_sha256'] for e in entries)}))
