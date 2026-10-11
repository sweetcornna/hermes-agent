"""Real Hermes loader, MCP handshake and native tool dispatch in a temp home.

No gateway adapters, model API calls, or messages to contacts are involved.
The optional public searches exercise the same registry handler the agent uses.
"""
import argparse
import hashlib
import importlib.metadata
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

parser = argparse.ArgumentParser()
parser.add_argument('--plugin-root', type=Path, required=True)
parser.add_argument('--search', action='store_true')
args = parser.parse_args()
sys.path.insert(0, '/opt/hermes/repo')
with tempfile.TemporaryDirectory(prefix='hermes-search-smoke-') as directory:
    home = Path(directory)
    os.environ['HERMES_HOME'] = str(home)
    os.environ['HERMES_BUNDLED_PLUGINS'] = str(args.plugin_root.parent)
    (home / 'config.yaml').write_text('plugins:\n  enabled:\n    - corlinman-search\n')
    import hermes_cli.plugins as plugins
    import tools.mcp_tool as mcp
    from tools.registry import registry
    plugins._plugin_manager = None
    assert mcp._ensure_mcp_sdk(), 'Gateway MCP SDK missing'
    try:
        names = mcp.discover_mcp_tools()
        statuses = mcp.get_mcp_status()
        assert len(statuses) == 1, statuses
        status = statuses[0]
        assert status['connected'] and status['tools'] == 1, status
        assert len(names) == 1 and names[0].endswith('__search'), names
        schema = registry.get_schema(names[0])
        assert {'query', 'max_results'} <= set(schema['parameters']['required'])
        bound = schema['parameters']['properties']['max_results']
        assert bound['minimum'] == 1 and bound['maximum'] == 5
        print(json.dumps({'phase': 'handshake', 'status': status, 'tools': names,
                          'mcp_sdk': importlib.metadata.version('mcp'),
                          'manifest_sha256': hashlib.sha256((args.plugin_root / 'mcp.json').read_bytes()).hexdigest()}), flush=True)
        if args.search:
            for query, expected_hosts in (
                ('Python official documentation tutorial', {'python.org', 'www.python.org', 'docs.python.org'}),
                ('骑士学院2 官网', {'kaiju09.com', 'www.kaiju09.com', 'store.steampowered.com'}),
            ):
                start = time.monotonic()
                raw = registry.dispatch(names[0], {'query': query, 'max_results': 3})
                result = json.loads(raw) if isinstance(raw, str) else raw
                assert not result.get('error'), result
                content = result.get('structuredContent', result.get('result'))
                if isinstance(content, str):
                    content = json.loads(content)
                assert isinstance(content, dict) and content.get('results'), result
                assert len(content['results']) <= 3
                assert all(row['url'].startswith(('http://', 'https://')) for row in content['results'])
                assert any(urlsplit(row['url']).hostname in expected_hosts for row in content['results']), content
                print(json.dumps({'phase': 'search', 'query': query,
                                  'seconds': round(time.monotonic() - start, 2), 'payload': content}, ensure_ascii=False), flush=True)
        print(json.dumps({'phase': 'complete', 'ok': True}), flush=True)
    finally:
        mcp.shutdown_mcp_servers()
