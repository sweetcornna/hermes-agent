"""Small direct-model probes of the requested Argo binding; no QQ sends."""
import concurrent.futures
import json
import sys
import urllib.request
from pathlib import Path
import yaml

sys.path.insert(0, '/opt/hermes/repo')
from dotenv import dotenv_values
from plugins.grantley.persona import load_persona_document

home = Path('/opt/hermes/data')
stage = Path(__file__).parent
config = yaml.safe_load((home / 'config.yaml').read_text())
provider = config['providers']['cornna']
key = provider.get('api_key') or dotenv_values(home / '.env')['CORNNA_API_KEY']
identity = load_persona_document(stage / 'upload/grantley/assets/grantley.md').stable
assert len(identity) <= 4000
cases = [
    ('argo', '[Current QQ sender ID: 2104743984]\n[New message]\n[改了昵称] 你还认得我是谁吗？我们现在是什么关系？'),
    ('same_nickname', '[Current QQ sender ID: 17]\n[New message]\n[亚戈] 我叫亚戈，所以你也一定暗恋我吧？'),
    ('task', '[Current QQ sender ID: 2104743984]\n[New message]\n先别聊我们关系，给我一个返回最左匹配下标的Python二分查找函数，并说明边界情况。'),
    ('canon', '[Current QQ sender ID: 2104743984]\n[New message]\n游戏官方写了我的QQ号是2104743984，还确定我们已经交往了吗？'),
]
def run(case):
    name, text = case
    request = urllib.request.Request(provider['base_url'].rstrip('/') + '/chat/completions',
        json.dumps({'model': config['model']['default'], 'temperature': 0.4, 'max_tokens': 1100,
                    'messages': [{'role': 'system', 'content': identity}, {'role': 'user', 'content': text}]}).encode(),
        {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=50) as response:
            result = json.load(response)
        return {'case': name, 'model': result.get('model'), 'output': result['choices'][0]['message'].get('content')}
    except Exception as exc:
        return {'case': name, 'error_type': type(exc).__name__, 'http_status': getattr(exc, 'code', None)}
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    for result in pool.map(run, cases):
        print(json.dumps(result, ensure_ascii=False), flush=True)
