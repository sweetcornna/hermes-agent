"""Stopped-gateway maintenance command, called only after host-side backup."""
import json
import sys
from pathlib import Path
import yaml

sys.path.insert(0, '/opt/hermes/repo')
from gateway.config import load_gateway_config
from revise_sessions import revise

home = Path('/opt/hermes/data')
config = yaml.safe_load((home / 'config.yaml').read_text())
print(json.dumps({'ok': True, 'revisions': revise(home, load_gateway_config(), config['plugins']['entries']['grantley']['settings']['channels'])}), flush=True)
