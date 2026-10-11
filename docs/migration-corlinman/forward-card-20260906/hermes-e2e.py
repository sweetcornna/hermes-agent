"""Real installed Hermes adapter -> authenticated bridge -> self-only card."""
import asyncio,json,sys
from pathlib import Path
sys.path.insert(0,'/opt/hermes/repo')
from gateway.config import load_gateway_config,Platform
from plugins.platforms.onebot.adapter import _standalone_send
async def main():
 home=Path('/opt/hermes/data');out=home/'.deployments/forward-card-20260906/results/hermes-e2e.json'
 assert not out.exists(),'Do not repeat a completed or uncertain send'
 out.write_text(json.dumps({'started':True,'target':2952532060}))
 cfg=load_gateway_config().platforms[Platform('onebot')]
 body='[LONG_REPLY]Hermes 卡片链路校验\n\n完整代码：\ndef square(_value_):\n    return _value_ ** 2\n\n来源：https://docs.python.org/3/\nHERMES-CARD-E2E-END'
 result=await _standalone_send(cfg,'2952532060',body)
 out.write_text(json.dumps({'target_is_self':True,'result':result},ensure_ascii=False))
 print(json.dumps({'target_is_self':True,'result':result},ensure_ascii=False),flush=True)
 assert result.get('success') and result.get('message_id'),result
asyncio.run(main())
