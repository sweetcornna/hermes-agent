"""Try the persona revision on a private snapshot of the production database."""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
import yaml

sys.path.insert(0, '/opt/hermes/repo')
production = Path('/opt/hermes/data')
config = yaml.safe_load((production / 'config.yaml').read_text())
with tempfile.TemporaryDirectory(prefix='argo-revision-dry-') as temporary:
    home = Path(temporary)
    os.environ['HERMES_HOME'] = str(home)
    (home / 'config.yaml').write_text(yaml.safe_dump(config, allow_unicode=True))
    (home / 'sessions').mkdir()
    shutil.copy2(production / 'sessions/sessions.json', home / 'sessions/sessions.json')
    source = sqlite3.connect(f'file:{production}/state.db?mode=ro', uri=True)
    target = sqlite3.connect(home / 'state.db')
    source.backup(target)
    source.close()
    target.close()
    from gateway.config import load_gateway_config
    from revise_sessions import revise
    result = revise(home, load_gateway_config(), config['plugins']['entries']['grantley']['settings']['channels'])
    print(json.dumps({'ok': True, 'revisions': result}), flush=True)
