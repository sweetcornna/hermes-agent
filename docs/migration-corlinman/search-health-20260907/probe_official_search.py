"""Probe provider-hosted search through the existing Hermes proxy credential."""
import concurrent.futures
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml
from dotenv import dotenv_values

root = Path('/opt/hermes/data')
cfg = yaml.safe_load((root / 'config.yaml').read_text())
provider = cfg['providers']['cornna']
secrets = dotenv_values(root / '.env')
key = provider.get('api_key') or secrets[provider.get('api_key_env', 'CORNNA_API_KEY')]
prompt = ('Search the public web now for the official Python documentation tutorial. '
          'Use the built-in search tool. Return at most 3 results with title, URL and a short description. '
          'Do not answer from memory or invent links.')
jobs = [
    ('gpt', provider['base_url'].rstrip('/') + '/responses', {
        'model': 'gpt-6-astra', 'input': prompt, 'store': False,
        'tools': [{'type': 'web_search', 'external_web_access': True}],
        'tool_choice': 'required', 'include': ['web_search_call.action.sources'],
        'reasoning': {'effort': 'low'},
    }),
    ('gemini', 'https://api.cornna.xyz/antigravity/v1beta/models/gemini-3.8-flash-high:generateContent', {
        'contents': [{'role': 'user', 'parts': [{'text': prompt}]}],
        'tools': [{'googleSearch': {}}], 'generationConfig': {'maxOutputTokens': 3000},
    }),
]


def call(job):
    label, url, body = job
    started = time.monotonic()
    request = urllib.request.Request(url, json.dumps(body).encode(),
                                     {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=85) as response:
            data = json.load(response)
        return {'provider': label, 'endpoint': url, 'seconds': round(time.monotonic() - started, 2),
                'request': body, 'response': data}
    except urllib.error.HTTPError as error:
        # Remove the only credential this request can contain before recording an error.
        message = error.read(5000).decode(errors='replace').replace(key, '[REDACTED]')
        return {'provider': label, 'endpoint': url, 'seconds': round(time.monotonic() - started, 2),
                'request': body, 'http_status': error.code, 'error': message}
    except Exception as error:
        return {'provider': label, 'endpoint': url, 'seconds': round(time.monotonic() - started, 2),
                'error_type': type(error).__name__}


with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    for result in pool.map(call, jobs):
        print(json.dumps(result, ensure_ascii=False), flush=True)
