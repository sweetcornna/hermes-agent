"""One synthetic card to the authenticated bot itself; never any contact/group."""
import asyncio,json,uuid,sys
from pathlib import Path
import yaml,websockets
from dotenv import dotenv_values
HOME=Path('/opt/hermes/data');STAGE=HOME/'.deployments/forward-card-20260906/results'
async def main():
 c=yaml.safe_load((HOME/'config.yaml').read_text());e=c['platforms']['onebot']['extra'];token=e.get('access_token') or dotenv_values(HOME/'.env').get('ONEBOT_ACCESS_TOKEN')
 async with websockets.connect(e['ws_url'],additional_headers={'Authorization':'Bearer '+token} if token else None) as ws:
  async def call(action,params=None,rid=None):
   echo=uuid.uuid4().hex;request={'action':action,'params':params or {},'echo':echo}
   if rid:request['_hermes_request_id']=rid
   await ws.send(json.dumps(request,ensure_ascii=False))
   while True:
    item=json.loads(await asyncio.wait_for(ws.recv(),120))
    if item.get('echo')==echo:return item
  for _ in range(8):
   status=await call('get_status')
   if status.get('data',{}).get('online') is True:break
   await asyncio.sleep(2)
  assert status.get('data',{}).get('online') is True,status
  identity=await call('get_login_info');self_id=identity['data']['user_id'];assert self_id==2952532060
  check=await call('native_self_check');print(json.dumps({'phase':'self-check','result':check.get('data')},ensure_ascii=False),flush=True)
  assert check.get('data',{}).get('forward_builder_available') is True,check
  text='CARD-BEGIN-6F2A\n这是一条只发送到机器人自身会话的合并转发卡片验证。\n\n'+''.join(f'第{i}段：完整回答保留原文、换行、代码和链接，不为聊天气泡省略内容。\n' for i in range(1,31))+'\n代码校验：\ndef square(_value_):\n    return _value_ ** 2\n\n来源链接：https://docs.python.org/3/\nCARD-END-9C71'
  revision=sys.argv[1] if len(sys.argv)>1 else 'r1'
  assert revision in {'r1','r2','r3','r4','r5','r6'}
  ledger=STAGE/f'self-test-attempt-{revision}.json'
  if ledger.exists():attempt=json.loads(ledger.read_text())
  else:
   attempt={'request_id':uuid.uuid4().hex,'target':self_id,'body':text}
   ledger.write_text(json.dumps(attempt,ensure_ascii=False))
  assert attempt['target']==self_id and attempt['body']==text
  result=await call('send_private_forward_msg',{'user_id':self_id,'messages':[{'type':'node','data':{'uin':str(self_id),'name':'grantly','content':[{'type':'text','data':{'text':text}}]}}]},attempt['request_id'])
  (STAGE/f'self-test-result-{revision}.json').write_text(json.dumps(result,ensure_ascii=False))
  print(json.dumps({'phase':'self-card','status':result.get('status'),'data':result.get('data'),'native_qq':result.get('native_qq'),'body_characters':len(text),'target_is_self':True},ensure_ascii=False),flush=True)
  print(json.dumps({'phase':'diagnostic','data':(await call('native_self_check')).get('data')},ensure_ascii=False),flush=True)
  assert result.get('status')=='ok' and result.get('data',{}).get('message_id'),result
asyncio.run(main())
