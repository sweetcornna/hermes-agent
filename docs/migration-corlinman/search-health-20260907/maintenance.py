"""Private snapshot migration, installed provider verification, and recovery."""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import yaml

HOME_PATH = Path('/opt/hermes/data')
STAGE = HOME_PATH / '.deployments/official-search-20260907'
sys.path.insert(0, '/opt/hermes/repo')
sys.path.insert(0, str(STAGE))
from revise_sessions import revise


def perform(home, rollback=False):
    os.environ['HERMES_HOME'] = str(home)
    from gateway.config import load_gateway_config
    return revise(home,load_gateway_config(),rollback=rollback)


mode = sys.argv[1]
if mode == 'preflight':
    with tempfile.TemporaryDirectory(prefix='official-search-preflight-') as temporary:
        home = Path(temporary)
        (home/'sessions').mkdir()
        shutil.copy2(HOME_PATH/'config.yaml',home/'config.yaml')
        shutil.copy2(HOME_PATH/'sessions/sessions.json',home/'sessions/sessions.json')
        src = sqlite3.connect(f'file:{HOME_PATH}/state.db?mode=ro',uri=True)
        dst = sqlite3.connect(home/'state.db')
        src.backup(dst)
        src.close()
        dst.close()
        first = perform(home)
        assert all(row['already_revised'] for row in perform(home))
        rollback = perform(home,True)
        print(json.dumps({'passed':True,'revisions':first,'idempotent':True,'rollback_count':len(rollback)}),flush=True)
elif mode in {'revise','rollback'}:
    print(json.dumps({'passed':True,'revisions':perform(HOME_PATH,mode=='rollback')}),flush=True)
elif mode == 'verify':
    os.environ['HERMES_HOME'] = str(HOME_PATH)
    from tools import web_tools
    web_tools._ensure_web_plugins_loaded()
    from agent.web_search_registry import get_provider
    checks = {}
    for name in ['official-search','codex-search','gemini-search']:
        provider = get_provider(name)
        assert provider is not None and provider.is_available()
        checks[name] = {'available':True,'class_module':type(provider).__module__}
    assert web_tools.check_web_api_key()
    assert web_tools._get_search_backend() == 'official-search'
    print(json.dumps({'passed':True,'active_search_backend':'official-search','providers':checks,'no_chat_messages_sent':True}),flush=True)
else:
    raise SystemExit('Unknown mode')
