"""Dry-run the exact migration on a private production SQLite snapshot."""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
import yaml
from configure import desired_config

sys.path.insert(0, '/opt/hermes/repo')
production = Path('/opt/hermes/data')
config = desired_config(yaml.safe_load((production / 'config.yaml').read_text()))
groups = [str(key) for key, value in config['plugins']['entries']['grantley']['settings']['channels'].items()
          if value.get('group') and value.get('persona') == 'grantley']
with tempfile.TemporaryDirectory(prefix='longlife-migration-') as temporary:
    home = Path(temporary)
    os.environ['HERMES_HOME'] = str(home)
    (home / 'config.yaml').write_text(yaml.safe_dump(config, allow_unicode=True))
    (home / 'sessions').mkdir()
    shutil.copy2(production / 'sessions/sessions.json', home / 'sessions/sessions.json')
    source = sqlite3.connect(f'file:{production}/state.db?mode=ro', uri=True)
    destination = sqlite3.connect(home / 'state.db')
    source.backup(destination)
    source.close()
    destination.close()
    from gateway.config import load_gateway_config
    from migrate_groups import migrate
    cfg = load_gateway_config()
    assert cfg.group_sessions_per_user is False and cfg.default_reset_policy.mode == 'none'
    print(json.dumps({'phase': 'dry_run_apply', 'groups': migrate(home, cfg, groups, apply=True)}), flush=True)
    print(json.dumps({'phase': 'dry_run_idempotence', 'groups': migrate(home, cfg, groups, apply=True)}), flush=True)
