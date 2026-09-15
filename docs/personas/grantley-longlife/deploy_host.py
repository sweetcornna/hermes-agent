"""Host-side stopped-gateway maintenance transaction; always restart gateway."""
import json
import subprocess
from pathlib import Path

container = 'hermes-cloud'
state = json.loads(Path('/opt/hermes-cloud/data/gateway_state.json').read_text())
assert state['gateway_state'] == 'running' and state.get('active_agents', 0) == 0
info = json.loads(subprocess.check_output(['docker', 'inspect', container]))[0]
assert info['State']['Running']
image = info['Image']
subprocess.run(['docker', 'stop', '--time', '45', container], check=True)
try:
    subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--volumes-from', container,
                    '-e', 'HERMES_HOME=/opt/hermes/data', '-e', 'PYTHONDONTWRITEBYTECODE=1',
                    '-w', '/opt/hermes/repo', '--user', '991:988', '--entrypoint', 'python', image,
                    '/opt/hermes/data/.deployments/grantley-longlife-20260906/apply_deployment.py'], check=True)
finally:
    subprocess.run(['docker', 'start', container], check=True)
