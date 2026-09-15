"""Apply inside a maintenance container while the real gateway is stopped."""
import hashlib
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
home = Path('/opt/hermes/data')
stage = Path(__file__).parent
backup = stage / 'backup'
backup.mkdir(mode=0o700, exist_ok=False)
for name in ('config.yaml', 'sessions/sessions.json'):
    target = backup / name
    target.parent.mkdir(exist_ok=True)
    shutil.copy2(home / name, target)
source = sqlite3.connect(home / 'state.db')
destination = sqlite3.connect(backup / 'state.db')
source.backup(destination)
source.close()
destination.close()


def original_digest(path, old_only=False):
    connection = sqlite3.connect(path)
    digest = hashlib.sha256()
    for table, key in [('sessions', 'id'), ('messages', 'session_id')]:
        where = f" WHERE {key} NOT LIKE 'grantley-longlife-20260906-%'" if old_only else ''
        for row in connection.execute(f'SELECT * FROM {table}' + where + ' ORDER BY id'):
            digest.update(repr(row).encode())
    connection.close()
    return digest.hexdigest()


before = original_digest(backup / 'state.db')
config_bytes = (home / 'config.yaml').read_bytes()
config = desired_config(yaml.safe_load(config_bytes))
def replace_config(data):
    fd, temporary = tempfile.mkstemp(prefix='.longlife-', dir=home)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, (home / 'config.yaml').stat().st_mode & 0o777)
        os.replace(temporary, home / 'config.yaml')
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


try:
    replace_config(yaml.safe_dump(config, allow_unicode=True, sort_keys=False).encode())
    from gateway.config import load_gateway_config
    from migrate_groups import migrate
    groups = [str(key) for key, value in config['plugins']['entries']['grantley']['settings']['channels'].items()
              if value.get('group') and value.get('persona') == 'grantley']
    reports = migrate(home, load_gateway_config(), groups, apply=True)
    assert original_digest(home / 'state.db', old_only=True) == before
    result = {'ok': True, 'groups': reports, 'original_rows_unchanged': True,
              'config_before_sha256': hashlib.sha256(config_bytes).hexdigest(),
              'config_after_sha256': hashlib.sha256((home / 'config.yaml').read_bytes()).hexdigest(),
              'backup': str(backup)}
    (stage / 'applied.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result), flush=True)
except BaseException:
    # No concurrent writers: the production gateway is still stopped.
    for suffix in ('-wal', '-shm', '-journal'):
        (home / ('state.db' + suffix)).unlink(missing_ok=True)
    for name in ('state.db', 'sessions/sessions.json'):
        shutil.copy2(backup / name, home / name)
    replace_config(config_bytes)
    raise
