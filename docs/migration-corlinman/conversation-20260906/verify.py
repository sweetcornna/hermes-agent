"""Read-only verification of the installed runtime; no message sending."""
import asyncio
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, '/opt/hermes/repo')
from gateway.config import load_gateway_config, Platform
from plugins.platforms.onebot.adapter import OneBotAdapter, LONG_REPLY_MARKER, PLATFORM_HINT
from plugins.platforms.onebot.reply_batch import quiet_seconds
from plugins.grantley.persona import load_persona_document

config = load_gateway_config()
ad = OneBotAdapter(config.platforms[Platform('onebot')])
assert quiet_seconds(ad.config.extra) == 3
assert ad.max_bubbles_per_reply == 3
assert ad.forward_threshold > 0 and LONG_REPLY_MARKER in PLATFORM_HINT
doc = load_persona_document()
assert len(doc.stable) <= 4000
assert '2104743984' in doc.stable and '[LONG_REPLY]' in doc.stable
source = Path('/opt/hermes/repo/plugins/grantley/assets/grantley.md').read_bytes()
assert source == Path('/opt/hermes/data/plugins/grantley/assets/grantley.md').read_bytes()
print(json.dumps({'configured_seconds': quiet_seconds(ad.config.extra),
                  'bubble_ceiling': ad.max_bubbles_per_reply,
                  'forward_threshold': ad.forward_threshold,
                  'persona_characters': len(doc.stable),
                  'persona_sha256': hashlib.sha256(source).hexdigest(),
                  'installed_persona_matches_source': True,
                  'native_forward_verified': False,
                  'no_messages_sent': True}))
