"""Restore the existing forum's direct dialogue, leaving other groups gated."""
import copy,json,os,sys,tempfile
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime,timezone
import yaml
sys.path.insert(0,'/opt/hermes/repo')
from gateway.config import load_gateway_config,Platform,PlatformConfig
from gateway.platforms.base import MessageType
from gateway.session import build_session_key
from plugins.platforms.telegram.adapter import TelegramAdapter
from telegram import Chat,Message,User
HOME=Path('/opt/hermes/data'); TARGET='-1003990634877'
path=HOME/'config.yaml';original=yaml.safe_load(path.read_text()); changed=copy.deepcopy(original)
extra=changed['platforms']['telegram'].setdefault('extra',{})
free=list(extra.get('free_response_chats') or [])
if TARGET not in [str(v) for v in free]:free.append(TARGET)
extra['free_response_chats']=free
changed.setdefault('telegram',{})['free_response_chats']=free
before=load_gateway_config().platforms[Platform('telegram')]
after=copy.deepcopy(before);after.extra['free_response_chats']=free
ad=TelegramAdapter(before);candidate=TelegramAdapter(after)
for adapter in [ad,candidate]:adapter._bot=SimpleNamespace(id=8720715962,username='sweetcornna2_bot')
checks=[]; keys=[]
for topic in [11,12,13,680]:
 message=Message(9000+topic,datetime.now(timezone.utc),Chat(int(TARGET),'supergroup',title='Corn Agents',is_forum=True),
                 from_user=User(1114483029,'operator',False),text='继续这个话题',is_topic_message=True,message_thread_id=topic)
 before_result=ad._should_process_message(message);after_result=candidate._should_process_message(message)
 assert after_result
 event=candidate._build_message_event(message,MessageType.TEXT)
 assert event.source.thread_id==str(topic)
 keys.append(build_session_key(event.source,group_sessions_per_user=False))
 checks.append({'topic':topic,'before':before_result,'after':after_result})
assert len(set(keys))==4
other=Message(1,datetime.now(timezone.utc),Chat(-1009999999999,'supergroup'),from_user=User(1114483029,'operator',False),text='你好')
assert not candidate._should_process_message(other)
if '--apply' in sys.argv:
 fd,name=tempfile.mkstemp(prefix='.telegram-restore-',dir=HOME)
 with os.fdopen(fd,'w') as f:
  f.write(yaml.safe_dump(changed,allow_unicode=True,sort_keys=False));f.flush();os.fsync(f.fileno())
 os.chmod(name,path.stat().st_mode&0o777);os.replace(name,path)
print(json.dumps({'applied':'--apply' in sys.argv,'target':TARGET,'checks':checks,'separate_topic_sessions':True,'unrelated_group_still_requires_mention':True,'no_messages_sent':True}))
