"""Apply the reviewed, hash-checked removal of obsolete QQ bridge code."""
from pathlib import Path
import hashlib
import json
import os
import py_compile
import shutil
import subprocess
import tarfile
import yaml

assert os.geteuid() == 0
root = Path('/opt/hermes-cloud/repo')
home = Path('/opt/hermes-cloud/data')
backup = home / '.deployments/llbot-20260908/backup'
stage = Path('/opt/llbot/private/hermes-update')
manifest = json.loads(Path('/tmp/production-cleanup-manifest.json').read_text())
state = json.loads((home / 'gateway_state.json').read_text())
assert state['active_agents'] == 0, 'Wait for ongoing agents'
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
for rel, info in manifest.items():
    assert sha(root / rel) == info['before_sha256'], f'Changed since review: {rel}'
stage.mkdir(mode=0o700, exist_ok=True)
with tarfile.open('/tmp/llbot-hermes-cleanup.tgz') as tar:
    for member in tar.getmembers():
        assert member.name in manifest and member.isfile()
        target = stage / member.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(tar.extractfile(member).read())
for rel, info in manifest.items():
    assert sha(stage / rel) == info['after_sha256']
    if rel.endswith('.py'):
        py_compile.compile(str(stage / rel), doraise=True)
config_path = home / 'config.yaml'
before = yaml.safe_load(config_path.read_text())
after = yaml.safe_load(config_path.read_text())
extra = after['platforms']['onebot']['extra']
extra.pop('native_qq', None)
extra.pop('desktop_qq', None)
assert extra['ws_url'] == 'ws://127.0.0.1:3301'
after['plugins']['enabled'] = [p for p in after['plugins']['enabled'] if p != 'qq-native']
assert after['plugins'].get('entries') == before['plugins'].get('entries')
subprocess.run(['docker', 'stop', '--time', '30', 'hermes-cloud'], check=True)
removed = []
try:
    for rel in manifest:
        dest = backup / 'pre-cleanup' / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / rel, dest)
        (root / rel).write_bytes((stage / rel).read_bytes())
    shutil.copy2(config_path, backup / 'config-before-cleanup.yaml')
    config_path.write_text(yaml.safe_dump(after, allow_unicode=True, sort_keys=False))
    for name in ['native_backend', 'native_cli', 'desktop_backend', 'desktop_cli', 'desktop_media', 'desktop_inbox']:
        path = root / 'plugins/platforms/onebot' / (name + '.py')
        if path.exists():
            path.unlink()
            removed.append(str(path))
        for cache in (path.parent / '__pycache__').glob(name + '.*.pyc'):
            cache.unlink()
    plugin = home / 'plugins/qq-native'
    if plugin.exists():
        shutil.rmtree(plugin)
        removed.append(str(plugin))
    for name in ['test_onebot_native_backend.py', 'test_onebot_desktop_backend.py',
                 'test_onebot_desktop_cli.py', 'test_onebot_desktop_media.py']:
        path = root / 'tests/gateway' / name
        if path.exists():
            path.unlink()
            removed.append(str(path))
finally:
    subprocess.run(['docker', 'start', 'hermes-cloud'], check=True)
result = {'changed_files': list(manifest), 'removed': removed,
          'native_config_removed': True, 'persona_bindings_preserved': True,
          'ws_url': extra['ws_url'], 'qq_online': False,
          'remaining': 'Official LLBot Auth Token approval and QQ login'}
out = Path('/opt/llbot/private/hermes-deployment.json')
out.write_text(json.dumps(result, indent=2) + '\n')
out.chmod(0o600)
print(json.dumps(result, indent=2))
