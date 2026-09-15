"""Read effective deployment and instantiate bound/unbound group/DM policies."""
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
import yaml

sys.path.insert(0, '/opt/hermes/repo')
home = Path('/opt/hermes/data')
config = yaml.safe_load((home / 'config.yaml').read_text())
config_hash = hashlib.sha256((home / 'config.yaml').read_bytes()).hexdigest()
from gateway.config import load_gateway_config, Platform
from gateway.session import SessionSource, build_session_key
from agent.image_routing import decide_image_input_mode
cfg = load_gateway_config()
assert cfg.group_sessions_per_user is False
assert cfg.default_reset_policy.mode == 'none'
assert all(p.mode == 'none' for p in cfg.reset_by_type.values())
assert all(p.mode == 'none' for p in cfg.reset_by_platform.values())
assert decide_image_input_mode('custom', config['model']['default'], config, requested_provider='cornna') == 'native'
group_keys = [build_session_key(SessionSource(platform=Platform('onebot'), chat_id='g183287894', chat_type='group', user_id=user),
                               group_sessions_per_user=cfg.group_sessions_per_user) for user in ('a', 'b', 'proactive')]
assert len(set(group_keys)) == 1
dm_keys = [build_session_key(SessionSource(platform=Platform('onebot'), chat_id=user, chat_type='dm', user_id=user),
                            group_sessions_per_user=cfg.group_sessions_per_user) for user in ('a', 'b')]
assert len(set(dm_keys)) == 2
connection = sqlite3.connect(f'file:{home}/state.db?mode=ro', uri=True)
rows = connection.execute("SELECT id,session_key FROM sessions WHERE id LIKE 'grantley-longlife-20260906-%'").fetchall()
assert len(rows) == 5
for sid, key in rows:
    assert connection.execute('SELECT COUNT(*) FROM messages WHERE session_id=? AND active=1', (sid,)).fetchone()[0] >= 2
    routing = connection.execute('SELECT entry_json FROM gateway_routing WHERE session_key=?', (key,)).fetchall()
    assert any(json.loads(r[0])['session_id'] == sid for r in routing)
connection.close()
policies = []
with tempfile.TemporaryDirectory(prefix='longlife-verify-') as temporary:
    test_home = Path(temporary)
    os.environ['HERMES_HOME'] = str(test_home)
    config['plugins']['enabled'] = []
    config['plugins']['entries'] = {'grantley': config['plugins']['entries']['grantley']}
    (test_home / 'config.yaml').write_text(yaml.safe_dump(config, allow_unicode=True))
    shutil.copytree(home / 'plugins/grantley', test_home / 'plugins/grantley', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    from run_agent import AIAgent
    for chat_type, chat in [('group', '183287894'), ('dm', '2104743984'), ('dm', 'unbound-verification-contact')]:
        with patch('run_agent.get_tool_definitions', return_value=[]), patch('run_agent.check_toolset_requirements', return_value={}):
            agent = AIAgent(api_key='offline-check', base_url=config['providers']['cornna']['base_url'],
                            provider='custom', requested_provider='cornna', api_mode='chat_completions',
                            model=config['model']['default'], enabled_toolsets=[], quiet_mode=True,
                            skip_background_review=True, skip_context_files=True, platform='onebot',
                            chat_type=chat_type, chat_id=chat, user_id='verify', session_id='verify-' + chat)
        c = agent.context_compressor
        assert c.threshold_tokens == 256000 and c.tail_token_budget == 32000
        assert not c.should_compress(255999) and c.should_compress(256000)
        assert agent.compression_enabled and agent.compression_in_place and c.abort_on_summary_failure
        assert not c._micro_compact_enabled
        policies.append({'chat_type': chat_type, 'bound': chat != 'unbound-verification-contact',
                         'trigger_tokens': c.threshold_tokens, 'tail_tokens': c.tail_token_budget,
                         'context_length': c.context_length, 'in_place': agent.compression_in_place})
state = json.loads((home / 'gateway_state.json').read_text())
assert state['gateway_state'] == 'running'
assert hashlib.sha256((home / 'config.yaml').read_bytes()).hexdigest() == config_hash
print(json.dumps({'ok': True, 'model': config['model']['default'], 'image_mode': 'native', 'policies': policies,
                  'group_shared_with_proactive': True, 'dm_contacts_isolated': True, 'restored_shared_groups': len(rows),
                  'session_auto_reset': 'none', 'gateway_state': state['gateway_state'],
                  'platforms': {key: value.get('state') for key, value in state.get('platforms', {}).items() if isinstance(value, dict)}}, ensure_ascii=False))
