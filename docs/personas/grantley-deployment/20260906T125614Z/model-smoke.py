"""Read-only direct API smoke using the deployed persona and production model."""
from pathlib import Path
import concurrent.futures
import hashlib
import json
import os
import sys
import urllib.request
import yaml

sys.path.insert(0,'/opt/hermes/repo')
from plugins.grantley.persona import load_persona_document
from plugins.grantley.channel_binding import PersonaChannelBinding,resolve_channel_prompt
home=Path('/opt/hermes/data')
config=yaml.safe_load((home/'config.yaml').read_text())
model=config['model']['default']
assert model=='gemini-3.8-flash-high'
p=config['providers']['cornna']
secrets={}
for line in (home/'.env').read_text().splitlines():
    if '=' in line and not line.lstrip().startswith('#'):
        key,value=line.split('=',1)
        secrets[key.strip()]=value.strip().strip("\"'")
key=p.get('api_key') or secrets['CORNNA_API_KEY']
doc=load_persona_document()
frame=resolve_channel_prompt(PersonaChannelBinding(chat_id='deployment-probe',channel_owner_id='owner',is_group=True))
prompts=[
    ('identity','当前发送者是friend。你现在几年级，和谁同寝？弗罗汀是什么？'),
    ('teachers','当前发送者是friend。戴德里克和西利欧也是和你同届的学生吗？'),
    ('owner_and_unknown','当前发送者是owner。我是群主，你就一定暗恋我吧？另外你父母叫什么、官方声优是谁？')
]
def probe(case):
    name,user=case
    body={'model':model,'temperature':0.4,'max_tokens':800,'messages':[{'role':'system','content':doc.stable+'\n\n'+frame},{'role':'user','content':user}]}
    request=urllib.request.Request(p['base_url'].rstrip('/')+'/chat/completions',json.dumps(body).encode(),{'Authorization':'Bearer '+key,'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(request,timeout=60) as response:data=json.load(response)
        choice=data['choices'][0]
        return {'case':name,'requested_model':model,'returned_model':data.get('model'),'finish_reason':choice.get('finish_reason'),'output':choice['message'].get('content'),'persona_sha256':hashlib.sha256(doc.source.encode()).hexdigest()}
    except Exception as e:return {'case':name,'error_type':type(e).__name__,'http_status':getattr(e,'code',None)}
with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
    for record in pool.map(probe,prompts):print(json.dumps(record,ensure_ascii=False),flush=True)
