import asyncio,json,hashlib,uuid
from pathlib import Path
import yaml,websockets
from dotenv import dotenv_values
HOME=Path('/opt/hermes/data');STAGE=HOME/'.deployments/forward-card-20260906/results'
async def main():
 e=yaml.safe_load((HOME/'config.yaml').read_text())['platforms']['onebot']['extra'];token=e.get('access_token') or dotenv_values(HOME/'.env').get('ONEBOT_ACCESS_TOKEN')
 async with websockets.connect(e['ws_url'],additional_headers={'Authorization':'Bearer '+token} if token else None) as ws:
  async def call(action):
   echo=uuid.uuid4().hex;await ws.send(json.dumps({'action':action,'params':{},'echo':echo}))
   while True:
    r=json.loads(await asyncio.wait_for(ws.recv(),30))
    if r.get('echo')==echo:return r
  for _ in range(8):
   if (await call('get_status')).get('data',{}).get('online'):break
   await asyncio.sleep(2)
  result=await call('native_forward_self_check');print(json.dumps(result),flush=True)
  expected=json.loads((STAGE/'self-test-attempt-r5.json').read_text())
  digest=hashlib.sha256(expected['body'].encode()).hexdigest()
  assert result.get('data',{}).get('text_sha256')==digest
  (STAGE/'readback-verified.json').write_text(json.dumps({'verified':True,'expected_sha256':digest,'result':result['data']}))
  print(json.dumps({'verified':True,'exact_body_sha256':digest}),flush=True)
asyncio.run(main())
