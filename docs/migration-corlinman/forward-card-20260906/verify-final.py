import asyncio,hashlib,json,sys,uuid
from pathlib import Path
import yaml,websockets
from dotenv import dotenv_values
HOME=Path('/opt/hermes/data')
async def main():
 c=yaml.safe_load((HOME/'config.yaml').read_text());e=c['platforms']['onebot']['extra'];token=e.get('access_token') or dotenv_values(HOME/'.env').get('ONEBOT_ACCESS_TOKEN')
 async with websockets.connect(e['ws_url'],additional_headers={'Authorization':'Bearer '+token} if token else None) as ws:
  async def call(action):
   echo=uuid.uuid4().hex;await ws.send(json.dumps({'action':action,'params':{},'echo':echo}))
   while True:
    r=json.loads(await asyncio.wait_for(ws.recv(),30))
    if r.get('echo')==echo:return r
  version=(await call('get_version_info'))['data'];status=(await call('get_status'))['data'];card=(await call('native_forward_self_check'))['data']
  assert 'forward' in version['native_qq']['capabilities'] and status['online']
  expected='Hermes 卡片链路校验\n\n完整代码：\ndef square(_value_):\n    return _value_ ** 2\n\n来源：https://docs.python.org/3/\nHERMES-CARD-E2E-END'
  assert card['readable'] and card['text_sha256']==hashlib.sha256(expected.encode()).hexdigest(),card
  assert card['message_id']==76417699905199
  state=json.loads((HOME/'gateway_state.json').read_text())
  assert state['platforms']['telegram']['state']=='connected' and state['platforms']['onebot']['state']=='connected'
  assert '-1003990634877' in [str(x) for x in c['platforms']['telegram']['extra']['free_response_chats']]
  assert e['reply_debounce_seconds']==3 and e['max_bubbles_per_reply']==3
  assert c['compression']['threshold_tokens']==256000 and c['compression']['tail_tokens']==32000
  print(json.dumps({'version':version['app_version'],'capabilities':version['native_qq']['capabilities'],'onebot':state['platforms']['onebot']['state'],
   'telegram':state['platforms']['telegram']['state'],'telegram_forum_restored':True,'hermes_card_readback':card,'reply_debounce_seconds':3,
   'bubble_ceiling':3,'compression':[256000,32000]},ensure_ascii=False),flush=True)
asyncio.run(main())
