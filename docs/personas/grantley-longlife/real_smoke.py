"""Isolated real Gemini API + Hermes compactor/SQLite continuity probe.

Production credentials stay in memory on the host. No messaging adapters run.
Only general tool discovery is disabled; model and summary calls are real.
Test thresholds are scaled down; production boundary is checked separately.
"""
import copy
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import yaml
from configure import desired_config

root = Path('/opt/hermes/repo')
production = Path('/opt/hermes/data')
sys.path.insert(0, str(root))
config = desired_config(yaml.safe_load((production / 'config.yaml').read_text()))
p = config['providers']['cornna']
from dotenv import dotenv_values
key = p.get('api_key') or dotenv_values(production / '.env')['CORNNA_API_KEY']
model = config['model']['default']
base_url = p['base_url']

with tempfile.TemporaryDirectory(prefix='grantley-longlife-smoke-') as temporary:
    home = Path(temporary)
    os.environ['HERMES_HOME'] = str(home)
    for name in list(os.environ):
        if any(s in name for s in ('API_KEY', 'TOKEN', 'SECRET', 'PASSWORD')):
            os.environ.pop(name)
    config['plugins']['enabled'] = []
    config['plugins']['entries'] = {'grantley': config['plugins']['entries']['grantley']}
    config['model']['context_length'] = 1048576
    config['compression'] = {'enabled': False, 'context_timeout_seconds': 0}
    (home / 'config.yaml').write_text(yaml.safe_dump(config, allow_unicode=True))
    shutil.copytree(production / 'plugins/grantley', home / 'plugins/grantley',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))

    from agent.image_routing import decide_image_input_mode, build_native_content_parts
    from agent.context_compressor import is_compaction_summary_message
    from hermes_state import SessionDB
    from run_agent import AIAgent

    assert decide_image_input_mode('custom', model, config, requested_provider='cornna') == 'native'
    db_path = home / 'state.db'
    session_id = 'isolated-longlife-smoke'
    def make_agent(db):
        with patch('run_agent.get_tool_definitions', return_value=[]), patch('run_agent.check_toolset_requirements', return_value={}):
            return AIAgent(api_key=key, base_url=base_url, provider='custom', requested_provider='cornna',
                           api_mode='chat_completions', model=model, session_db=db, session_id=session_id,
                           platform='onebot', chat_id='183287894', chat_type='group', user_id='smoke',
                           gateway_session_key='agent:main:onebot:group:g183287894', quiet_mode=True,
                           skip_context_files=True, skip_background_review=True, enabled_toolsets=[], max_iterations=3,
                           max_tokens=2000)
    db = SessionDB(db_path=db_path)
    agent = make_agent(db)
    c = agent.context_compressor
    assert agent.compression_in_place and agent.compression_enabled and c.abort_on_summary_failure
    assert c.threshold_tokens == 256000 and c.tail_token_budget == 32000
    assert not c.should_compress(255999) and c.should_compress(256000)
    # Scale only this isolated test, avoiding a 256k-token synthetic API bill.
    c.threshold_tokens = 12000
    c.tail_token_budget = 1200
    system = agent._build_system_prompt('This is an isolated continuity test. Answer only the latest message. Do not use tools.')
    agent._cached_system_prompt = system
    messages = [{'role': 'system', 'content': system}]
    messages.extend([{'role': 'user', 'content': '[Alice] 长期项目约定：项目代号 SABLE-619，最终交付日期是10月12日。请保留，后面会核对。'},
                     {'role': 'assistant', 'content': '记下了，项目代号 SABLE-619，10月12日交付。'}])
    for epoch in range(2):
        for i in range(20):
            messages.extend([{'role': 'user', 'content': f'[Bob] 第{epoch}-{i}轮训练记录：' + ('训练后在食堂吃饭，再去图书馆整理笔记。这只是日常。' * 70)},
                             {'role': 'assistant', 'content': f'这轮记录已经收到：{epoch}-{i}。'}])
        agent._ensure_db_session()
        assert agent._flush_messages_to_session_db(messages, [])
        before = copy.deepcopy(messages)
        messages, returned_system = agent._compress_context(messages, system, approx_tokens=30000)
        assert returned_system == system and agent.session_id == session_id
        assert messages != before and any(is_compaction_summary_message(m) for m in messages)
        assert 'SABLE-619' in json.dumps(messages, ensure_ascii=False)
        print(json.dumps({'phase': 'real_compaction', 'epoch': epoch + 1, 'remaining_messages': len(messages), 'fact_preserved': True}), flush=True)
        # Close and reopen both real agent and SQLite between compactions.
        db.close()
        db = SessionDB(db_path=db_path)
        messages = db.get_messages_as_conversation(session_id)
        assert any(is_compaction_summary_message(m) for m in messages)
        agent = make_agent(db)
        agent._cached_system_prompt = system
        agent.context_compressor.threshold_tokens = 12000
        agent.context_compressor.tail_token_budget = 1200
    agent.context_compressor.threshold_tokens = 256000
    result = agent.run_conversation('Alice之前约定的项目代号和交付日期是什么？只回答这两项，不用工具。',
                                    system_message=system, conversation_history=messages)
    output = result.get('final_response', '')
    assert 'SABLE-619' in output and '12' in output, output
    print(json.dumps({'phase': 'recall_after_restart', 'output': output}, ensure_ascii=False), flush=True)
    parts, skipped = build_native_content_parts('读取这张图：文字代码是什么，左边和右边分别是什么颜色与形状？不用工具。',
                                                [str(Path(__file__).with_name('vision-fixture.png'))])
    assert not skipped
    result = agent.run_conversation(parts, system_message=system, conversation_history=result['messages'])
    output = result.get('final_response', '')
    assert 'ZX49-K7' in output and '红' in output and '蓝' in output, output
    print(json.dumps({'phase': 'native_image_core_loop', 'output': output}, ensure_ascii=False), flush=True)
    db.close()
