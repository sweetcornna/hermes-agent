"""Run only while the gateway is stopped, with the existing home mounted."""
import hashlib
import json
import os
import tempfile
from pathlib import Path

import yaml

home = Path('/opt/hermes/data')
path = home / 'config.yaml'
data = yaml.safe_load(path.read_text())
platform = data['platforms']['onebot']
extra = platform.setdefault('extra', {})
for key, value in {'reply_debounce_seconds': 3, 'max_bubbles_per_reply': 3}.items():
    extra[key] = value
    if key in platform:
        platform[key] = value
assert extra['forward_threshold'] > 0
fd, name = tempfile.mkstemp(prefix='.conversation-', dir=home)
with os.fdopen(fd, 'w') as output:
    output.write(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
    output.flush()
    os.fsync(output.fileno())
os.chmod(name, path.stat().st_mode & 0o777)
os.replace(name, path)
print(json.dumps({'reply_debounce_seconds': 3, 'max_bubbles_per_reply': 3,
                  'forward_threshold': extra['forward_threshold'],
                  'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}))
