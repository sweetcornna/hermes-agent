"""Validate deployed/staged persona with real production imports in a temp home."""
import argparse
import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
from datetime import date
from pathlib import Path

import yaml

parser = argparse.ArgumentParser()
parser.add_argument('--plugin-root', type=Path, required=True)
args = parser.parse_args()
production_home = Path('/opt/hermes/data')
config_path = production_home / 'config.yaml'
config_bytes = config_path.read_bytes()
config = yaml.safe_load(config_bytes)
settings = config['plugins']['entries']['grantley']['settings']
assert config['model']['default'] == 'gemini-3.8-flash-high'
assert config['providers']['cornna']['default_model'] == 'gemini-3.8-flash-high'
assert settings['inject_identity_section'] is True
assert config['memory']['provider'] == 'grantley'
channels = settings['channels']
chat_id = next(str(k) for k,v in channels.items() if v.get('group') and v.get('persona')=='grantley')
for name in list(os.environ):
    if any(s in name for s in ('TOKEN','API_KEY','SECRET','PASSWORD')):
        os.environ.pop(name)
with tempfile.TemporaryDirectory(prefix='grantley-deploy-smoke-') as temporary:
    test_home = Path(temporary)
    os.environ['HERMES_HOME'] = str(test_home)
    (test_home/'config.yaml').write_text(yaml.safe_dump({
        'model': {'provider':'openrouter','default':'test/model'},
        'plugins': {'enabled':[], 'entries':{'grantley':{'settings':settings}}},
        'memory': {'provider':None},
    }))
    spec=importlib.util.spec_from_file_location('_deployment_grantley',args.plugin_root/'__init__.py',submodule_search_locations=[str(args.plugin_root)])
    plugin=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=plugin
    spec.loader.exec_module(plugin)
    from hermes_cli import plugins as runtime
    from hermes_cli.plugins import PluginContext,PluginManager,PluginManifest
    manager=PluginManager()
    manager._discovered=True
    runtime._plugin_manager=manager
    plugin.register(PluginContext(PluginManifest(name='grantley',key='grantley',source='bundled'),manager))
    from _deployment_grantley.persona import load_persona_document
    from _deployment_grantley.channel_binding import PersonaChannelBinding,resolve_channel_prompt
    from _deployment_grantley.memory_provider import GrantleyMemoryProvider
    from _deployment_grantley.state import PersonaState
    from _deployment_grantley.life import resolve_seed_library,draw_life_beat
    from agent.system_prompt import build_system_prompt
    from agent.turn_context import compose_user_api_content
    from run_agent import AIAgent
    document=load_persona_document()
    document.assert_cache_safe()
    assert len(document.stable)<=4000
    assert '二代新学期' in document.stable and '弗罗汀共和国' in document.stable
    counts=[]
    for platform,channel,expected in [('onebot',chat_id,1),('onebot','unbound-deploy-check',0),('cli',chat_id,0)]:
        agent=AIAgent(api_key='test-key',provider='openrouter',model='test/model',base_url='https://openrouter.ai/api/v1',quiet_mode=True,skip_memory=True,skip_context_files=True,enabled_toolsets=[],platform=platform,chat_id=channel,chat_type='group',session_id='deployment-'+platform+'-'+channel)
        prompt=build_system_prompt(agent)
        count=prompt.count(document.stable)
        assert count==expected,(platform,count)
        counts.append(count)
    binding=PersonaChannelBinding(chat_id=chat_id,channel_owner_id='deploy-owner',is_group=True)
    a=resolve_channel_prompt(binding,on=date(2026,9,6))
    b=resolve_channel_prompt(binding,on=date(2026,9,7))
    assert a==b and '今天你在' not in a and '必须走' not in a
    connection=sqlite3.connect(':memory:',check_same_thread=False)
    provider=GrantleyMemoryProvider(connection=connection,data_dir=test_home,config={'channels':channels})
    provider.initialize('deploy-state',platform='onebot',chat_id=chat_id,chat_type='group')
    provider._ensure_store().upsert_state(PersonaState(persona_id='grantley',mood='认真',state_json={'life_activity':'在食堂吃饭'}))
    sidecar=provider.prefetch('今天在做什么')
    composed=compose_user_api_content('今天在做什么',sidecar,'')
    assert '<persona-state>' in composed and '在食堂吃饭' in composed
    assert provider.system_prompt_block()==''
    assert load_persona_document().stable==document.stable
    seeds=resolve_seed_library('grantley',test_home)
    assert set(seeds['mentor']).isdisjoint(seeds['companion'])
    assert '亚戈' in seeds['companion']
    assert not any('失踪案' in x or '镇压' in x for x in seeds['mission_scenario'])
    assert config_path.read_bytes()==config_bytes
    print(json.dumps({'ok':True,'plugin_root':str(args.plugin_root),'stable_chars':len(document.stable),'persona_sha256':hashlib.sha256(document.source.encode()).hexdigest(),'identity_counts':counts,'channel_stable_across_days':a==b,'state_sidecar_verified':True,'model':config['model']['default'],'production_config_unchanged':True},ensure_ascii=False))
