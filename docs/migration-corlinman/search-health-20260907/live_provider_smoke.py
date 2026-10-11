"""Install into a private temporary profile and call native Hermes dispatch."""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import yaml
from dotenv import dotenv_values

payload = json.load(sys.stdin)
production = Path('/opt/hermes/data')
production_cfg = yaml.safe_load((production / 'config.yaml').read_text())
provider_cfg = production_cfg['providers']['cornna']
env_name = provider_cfg.get('api_key_env', 'CORNNA_API_KEY')
key = provider_cfg.get('api_key') or dotenv_values(production / '.env')[env_name]
with tempfile.TemporaryDirectory(prefix='official-search-live-') as temporary:
    home = Path(temporary)
    os.environ['HERMES_HOME'] = str(home)
    os.environ[env_name] = key
    sys.path.insert(0, '/opt/hermes/repo')
    plugin = home / 'plugins/official-search'
    plugin.mkdir(parents=True)
    for name, text in payload['files'].items():
        assert Path(name).name == name
        (plugin / name).write_text(text)
    cfg = {'plugins': {'enabled': ['official-search']}, 'providers': {'cornna': provider_cfg},
           'web': {'search_backend': 'official-search', 'official_search': payload['settings']}}
    (home / 'config.yaml').write_text(yaml.safe_dump(cfg))
    from hermes_cli.plugins import discover_plugins
    discover_plugins()
    from model_tools import get_tool_definitions
    from tools.registry import registry
    tools = get_tool_definitions(enabled_toolsets=['web'], quiet_mode=True, skip_tool_search_assembly=True)
    assert any(t.get('function', {}).get('name') == 'web_search' for t in tools)
    for backend, query in [('codex-search', 'Python official documentation tutorial'),
                           ('gemini-search', '骑士学院2 官网')]:
        cfg['web']['search_backend'] = backend
        (home / 'config.yaml').write_text(yaml.safe_dump(cfg))
        start = time.monotonic()
        raw = registry.dispatch('web_search', {'query': query, 'limit': 3})
        result = json.loads(raw) if isinstance(raw, str) else raw
        print(json.dumps({'backend': backend, 'seconds': round(time.monotonic() - start, 2),
                          'result': result}, ensure_ascii=False), flush=True)
        assert result['success'] and result['data']['web'], result
        assert result['provider'] == backend.removesuffix('-search')
        assert result['search_evidence']['queries']
    print(json.dumps({'passed': True, 'native_web_search_visible': True, 'isolated_profile': True,
                      'no_chat_messages_sent': True}), flush=True)
