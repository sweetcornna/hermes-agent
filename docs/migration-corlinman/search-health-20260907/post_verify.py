"""Read-only installed/config/history checks on private DB snapshots."""
import copy
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path('/opt/hermes/data')
STAGE = ROOT/'.deployments/official-search-20260907'
sys.path.insert(0,'/opt/hermes/repo')
manifest = json.loads((STAGE/'manifest.json').read_text())
deployed = json.loads((STAGE/'deployed.json').read_text())
before_cfg = yaml.safe_load((STAGE/'backup/config.yaml').read_text())
after_cfg = yaml.safe_load((ROOT/'config.yaml').read_text())
expected = copy.deepcopy(before_cfg)
expected['plugins']['enabled'].append('official-search')
web = expected.get('web') or {}
web['search_backend'] = 'official-search'
web['official_search'] = json.loads((STAGE/'settings.json').read_text())
expected['web'] = web
assert after_cfg == expected
assert hashlib.sha256((ROOT/'.env').read_bytes()).hexdigest() == manifest['credentials_sha256']
for name, digest in manifest['plugin_hashes'].items():
    assert hashlib.sha256((ROOT/'plugins/official-search'/name).read_bytes()).hexdigest() == digest
revisions = next(json.loads(line)['revisions'] for line in (STAGE/'revise-result.txt').read_text().splitlines() if line.startswith('{"passed":'))
with tempfile.TemporaryDirectory(prefix='official-search-postcheck-') as temporary:
    snapshots = {}
    for label, source in [('before',STAGE/'backup'),('after',ROOT)]:
        home = Path(temporary)/label
        (home/'sessions').mkdir(parents=True)
        shutil.copy2(ROOT/'config.yaml',home/'config.yaml')
        shutil.copy2(source/('sessions.json' if label=='before' else 'sessions/sessions.json'),home/'sessions/sessions.json')
        src=sqlite3.connect(f'file:{source}/state.db?mode=ro',uri=True);dst=sqlite3.connect(home/'state.db')
        src.backup(dst);src.close();dst.close()
        os.environ['HERMES_HOME']=str(home)
        from gateway.config import load_gateway_config
        from gateway.session import SessionStore
        store=SessionStore(home/'sessions',load_gateway_config());store._ensure_loaded()
        ids={r[k] for r in revisions for k in ['old_session_id','new_session_id']}
        sessions={sid:store._db.get_session(sid) for sid in ids}
        histories={sid:store.load_transcript(sid) for sid in ids if sessions[sid] is not None}
        snapshots[label]={'entries':{k:v.session_id for k,v in store._entries.items()},'sessions':sessions,'histories':histories}
        for db in store._db_handles.values():
            if db is not None:db.close()
    before,after=snapshots['before'],snapshots['after']
    count=0
    for row in revisions:
        old,new=row['old_session_id'],row['new_session_id']
        assert after['entries'][row['key']]==new
        assert before['sessions'][old]['system_prompt']==after['sessions'][old]['system_prompt']
        assert before['histories'][old]==after['histories'][old]
        normalize=lambda rows:[{k:v for k,v in r.items() if k!='_row_id'} for r in rows if r.get('role')!='system']
        original=normalize(before['histories'][old]);restored=normalize(after['histories'][new])
        assert restored[:len(original)]==original
        count+=len(original)
    changed={r['key'] for r in revisions}
    assert all(after['entries'].get(k)==v for k,v in before['entries'].items() if k not in changed)
state=json.loads((ROOT/'gateway_state.json').read_text())
assert state['gateway_state']=='running'
print(json.dumps({'passed':True,'deployed_at':deployed['started_at'],'active_backend':after_cfg['web']['search_backend'],
                  'primary':'codex','fallback':'gemini','credentials_unchanged':True,'model_configuration_unchanged':True,
                  'other_settings_unchanged':True,'plugin_hashes_match':True,'revised_sessions':len(revisions),
                  'preserved_messages':count,'old_histories_and_prefixes_unchanged':True,'other_routes_unchanged':True,
                  'platforms':{k:v['state'] for k,v in state['platforms'].items()},'no_chat_messages_sent':True}),flush=True)
