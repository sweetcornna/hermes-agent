"""Paired direct 3.8 probes. Credentials stay inside the production container."""
import ast
import concurrent.futures
import json
import sys
import urllib.request
from pathlib import Path

import yaml
from dotenv import dotenv_values

sys.path.insert(0, '/opt/hermes/repo')
from plugins.grantley.persona import load_persona_document

stage = Path(__file__).parent
previous = stage.parent / 'search-repair-20260906/upload'
home = Path('/opt/hermes/data')
config = yaml.safe_load((home / 'config.yaml').read_text())
provider = config['providers']['cornna']
key = provider.get('api_key') or dotenv_values(home / '.env')['CORNNA_API_KEY']

def hint(path):
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'PLATFORM_HINT' for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError('Platform hint missing')

prompts = {
    'before': load_persona_document(previous / 'grantley/assets/grantley.md').stable + '\n' + hint(previous / 'onebot/adapter.py'),
    'after': load_persona_document(stage / 'upload/plugins/grantley/assets/grantley.md').stable + '\n' + hint(stage / 'upload/plugins/platforms/onebot/adapter.py'),
}
cases = [
    ('greeting', '在吗'), ('thanks', '谢啦，已经弄好了'), ('hungry', '还没吃饭，好饿'),
    ('tired', '今天有点累'), ('argo', '你觉得我们俩是什么关系？'),
    ('task', '写一个Python函数，返回有序数组中目标第一次出现的下标，找不到返回-1；包括完整代码、循环不变式、复杂度和边界示例。'),
]

def run(item):
    version, case, text = item
    request = urllib.request.Request(provider['base_url'].rstrip('/') + '/chat/completions',
        json.dumps({'model': config['model']['default'], 'temperature': 0.4, 'max_tokens': 1800,
                    'messages': [{'role': 'system', 'content': prompts[version]},
                                 {'role': 'user', 'content': '[Current QQ sender ID: 2104743984]\n[New message]\n' + text}]}).encode(),
        {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=50) as response:
            result = json.load(response)
        output = result['choices'][0]['message'].get('content') or ''
        return {'version': version, 'case': case, 'model': result.get('model'), 'output': output,
                'characters': len(output), 'bubbles': len([p for p in output.split('[MSG_BREAK]') if p.strip()]),
                'requested_card': output.lstrip().startswith('[LONG_REPLY]')}
    except Exception as exc:
        return {'version': version, 'case': case, 'error_type': type(exc).__name__, 'http_status': getattr(exc, 'code', None)}

with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    for result in pool.map(run, [(version, case, text) for case, text in cases for version in prompts]):
        print(json.dumps(result, ensure_ascii=False), flush=True)
