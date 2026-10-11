import json,time,uuid,urllib.request,urllib.error
from pathlib import Path
import yaml
from dotenv import dotenv_values
r=Path('/opt/hermes/data');c=yaml.safe_load((r/'config.yaml').read_text());p=c['providers']['cornna'];k=p.get('api_key') or dotenv_values(r/'.env')[p.get('api_key_env','CORNNA_API_KEY')]
url=p['base_url'].rstrip('/')+'/alpha/search'
body={'id':str(uuid.uuid4()),'model':'gpt-6-astra','commands':{'search_query':[{'q':'Python official documentation tutorial'}],'response_length':'short'},'settings':{'allowed_callers':['direct'],'external_web_access':True},'max_output_tokens':3000}
headers={'Authorization':'Bearer '+k,'Content-Type':'application/json','Accept':'application/json','Session_id':str(uuid.uuid4()),'X-Client-Request-Id':str(uuid.uuid4())}
start=time.monotonic()
try:
 with urllib.request.urlopen(urllib.request.Request(url,json.dumps(body).encode(),headers),timeout=60) as x:d=json.load(x)
 d.pop('encrypted_output',None)
 print(json.dumps({'endpoint':url,'seconds':round(time.monotonic()-start,2),'request':body,'response':d},ensure_ascii=False))
except urllib.error.HTTPError as e:print(json.dumps({'endpoint':url,'http_status':e.code,'error':e.read(2000).decode(errors='replace').replace(k,'[REDACTED]')}))
except Exception as e:print(json.dumps({'endpoint':url,'error_type':type(e).__name__}))
