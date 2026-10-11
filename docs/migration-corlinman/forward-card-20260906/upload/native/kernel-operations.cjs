'use strict';

const { createHash } = require('node:crypto');

// Native message-service contracts, independently implemented from the interfaces:
// NapNeko/NapCatQQ eecb02140f77b58cb70a3ec8c0b2e816a346f3fe
// packages/napcat-core/{services/NodeIKernelMsgService.ts,types/{msg,element}.ts}
const isUin = value => /^[1-9]\d{4,9}$/.test(String(value));
const isUid = value => typeof value === 'string' && /^u_[A-Za-z0-9_-]{8,64}$/.test(value);
const isId = value => typeof value === 'string' && /^[1-9]\d{0,24}$/.test(value);
const failure = message => new Error(message);

function createKernelOperations({ getState, emit, now = Date.now, sendTimeoutMs = 20000 }) {
  let current = null;
  let started = 0;
  const messages = new Map();
  const identities = new Map();
  const delivered = new Map();
  const sending = new Map();
  const stats = { received: 0, sent: 0 };

  function bound(map, limit) {
    while (map.size > limit) map.delete(map.keys().next().value);
  }
  function rememberIdentity(uin, uid) {
    if (!isUin(uin) || !isUid(uid)) return;
    identities.set(String(uin), uid);
    bound(identities, 2000);
  }
  function setSession(session) {
    for (const pending of sending.values()) pending.finish(failure('QQ session changed; send was not retried'));
    sending.clear();
    current = session;
    started = Math.floor(now() / 1000);
    messages.clear();
    identities.clear();
    if (session) rememberIdentity(session.uin, session.uid);
  }
  function setEpoch(epoch) {
    if (!current || current.epoch === epoch) return;
    for (const pending of sending.values()) pending.finish(failure('QQ session changed; send was not retried'));
    sending.clear();
    current = { ...current, epoch };
  }
  function requireSession() {
    const state = getState();
    if (!current || !state?.kernelReady || current.epoch !== state.epoch || current.uin !== state.uin) {
      throw failure('Authenticated QQ kernel session unavailable');
    }
    return current;
  }
  function unchanged(snapshot) {
    if (requireSession() !== snapshot) throw failure('QQ session changed during native operation');
  }
  function key(message) {
    return `${current.uin}:${message.chatType}:${message.peerUid}:${message.msgId}`;
  }
  function remember(message) {
    const identity = key(message);
    let id = createHash('sha256').update(identity).digest().readUIntBE(0, 6) || 1;
    while (messages.has(id) && messages.get(id).identity !== identity) id = id === 0xffffffffffff ? 1 : id + 1;
    // Keep native snowflake IDs as strings. OneBot gets a separate safe integer.
    messages.set(id, { ...message, identity });
    bound(messages, 10000);
    return id;
  }
  function basic(raw) {
    if (!raw || ![1, 2].includes(raw.chatType) || !isId(raw.msgId)
      || !isUin(raw.senderUin) || !isUid(raw.senderUid)
      || (raw.chatType === 1 ? !isUid(raw.peerUid) : !isUin(raw.peerUid))
      || !/^[0-9]{1,12}$/.test(raw.msgTime) || !Array.isArray(raw.elements) || raw.elements.length > 512) return null;
    rememberIdentity(raw.senderUin, raw.senderUid);
    if (raw.chatType === 1) rememberIdentity(raw.peerUin, raw.peerUid);
    return { msgId: raw.msgId, msgSeq: String(raw.msgSeq ?? ''), msgTime: raw.msgTime,
      clientSeq: String(raw.clientSeq ?? ''), chatType: raw.chatType, peerUid: raw.peerUid,
      senderUid: raw.senderUid, senderUin: String(raw.senderUin) };
  }
  function segments(raw) {
    const out = [];
    for (const element of raw.elements) {
      if (element.elementType === 1 && typeof element.textElement?.content === 'string') {
        const text = element.textElement;
        if (text.atType === 1) out.push({ type: 'at', data: { qq: 'all' } });
        else if ([2, 4].includes(text.atType) && isUin(text.atUid)) out.push({ type: 'at', data: { qq: String(text.atUid) } });
        else if ([2, 4].includes(text.atType) && text.atNtUid === current.uid) out.push({ type: 'at', data: { qq: current.uin } });
        else out.push({ type: 'text', data: { text: text.content } });
      } else if (element.elementType === 6 && Number.isInteger(element.faceElement?.faceIndex)) {
        out.push({ type: 'face', data: { id: String(element.faceElement.faceIndex) } });
      } else if (element.elementType === 7 && isId(element.replyElement?.replayMsgId)) {
        const id = remember({ ...basic(raw), msgId: element.replyElement.replayMsgId,
          msgSeq: element.replyElement.replayMsgSeq, senderUin: element.replyElement.senderUin,
          senderUid: element.replyElement.senderUidStr ?? '', msgTime: element.replyElement.replyMsgTime ?? '' });
        out.push({ type: 'reply', data: { id: String(id) } });
      } else if (element.elementType !== 8) {
        // Unsupported media is explicit, never silently represented as text.
        out.push({ type: 'native_unsupported', data: { element_type: element.elementType } });
      }
    }
    return out;
  }
  function observe({ list, kind, epoch, uin }) {
    if (!current || current.epoch !== epoch || current.uin !== uin || !Array.isArray(list)) return;
    try { requireSession(); } catch { return; }
    for (const raw of list.slice(0, 1000)) {
      const message = basic(raw);
      if (!message) continue;
      const sendIdentity = `${current.uin}:${raw.chatType}:${raw.peerUid}:${raw.guildId}`;
      const pending = raw.senderUin === current.uin && isId(raw.guildId) ? sending.get(sendIdentity) : null;
      if (pending && [0, 2, 3].includes(raw.sendStatus)) {
        if (raw.sendStatus === 0) {
          sending.delete(sendIdentity);
          pending.finish(failure('QQ reported message delivery failure'));
        } else if (!pending.delivered) {
          pending.delivered = { message_id: remember(message) };
          stats.sent++;
          pending.complete();
        }
      }
      if (kind !== 'receive' || raw.isOnlineMsg !== true || raw.sourceType === 3
        || Number(raw.msgTime) < started || raw.senderUin === current.uin
        || (raw.recallTime && raw.recallTime !== '0')) continue;
      const identity = key(message);
      if (delivered.has(identity)) continue;
      const content = segments(raw);
      if (!content.length) continue;
      const event = { time: Number(raw.msgTime), self_id: Number(current.uin), post_type: 'message',
        message_type: raw.chatType === 1 ? 'private' : 'group', sub_type: raw.chatType === 1 ? 'friend' : 'normal',
        message_id: remember(message), user_id: Number(raw.senderUin), message: content,
        raw_message: content.filter(item => item.type === 'text').map(item => item.data.text).join(''),
        sender: { user_id: Number(raw.senderUin), nickname: String(raw.sendNickName ?? '').slice(0, 256),
          card: String(raw.sendMemberName ?? '').slice(0, 256) } };
      if (raw.chatType === 2) event.group_id = Number(raw.peerUid);
      if (Buffer.byteLength(JSON.stringify(event)) > 256 * 1024) continue;
      delivered.set(identity, true);
      bound(delivered, 10000);
      stats.received++;
      emit(event);
    }
  }
  async function uidFor(uin, snapshot) {
    uin = String(uin);
    if (!isUin(uin)) throw failure('Invalid QQ number');
    if (identities.has(uin)) return identities.get(uin);
    const result = await snapshot.session.getUixConvertService().getUid([uin]);
    unchanged(snapshot);
    const uid = result?.uidInfo?.get(uin);
    if (!isUid(uid)) throw failure('QQ could not resolve recipient UID');
    rememberIdentity(uin, uid);
    return uid;
  }
  async function send(action, params, snapshot) {
    const chatType = action === 'send_group_msg' || (action === 'send_msg' && params.message_type === 'group') ? 2 : 1;
    const target = String(chatType === 2 ? params.group_id : params.user_id);
    if (!isUin(target)) throw failure('Invalid message recipient');
    const peer = { chatType, peerUid: chatType === 2 ? target : await uidFor(target, snapshot), guildId: '' };
    const input = typeof params.message === 'string' ? [{ type: 'text', data: { text: params.message } }] : params.message;
    if (typeof params.message === 'string' && params.message.includes('[CQ:') && params.auto_escape !== true) {
      throw failure('Use structured segments or auto_escape for literal CQ text');
    }
    if (!Array.isArray(input) || !input.length || input.length > 128) throw failure('Invalid message segments');
    const elements = [];
    for (const segment of input) {
      if (segment?.type === 'text' && typeof segment.data?.text === 'string') {
        elements.push({ elementType: 1, elementId: '', textElement: { content: segment.data.text,
          atType: 0, atUid: '', atTinyId: '', atNtUid: '' } });
      } else if (segment?.type === 'at' && chatType === 2) {
        const all = segment.data?.qq === 'all';
        const uin = String(segment.data?.qq);
        const uid = all ? '' : await uidFor(uin, snapshot);
        elements.push({ elementType: 1, elementId: '', textElement: { content: all ? '@全体成员' : `@${uin}`,
          atType: all ? 1 : 2, atUid: all ? '0' : uin, atTinyId: '0', atNtUid: uid } });
      } else if (segment?.type === 'reply') {
        const id = Number(segment.data?.id);
        const original = messages.get(id);
        if (!Number.isSafeInteger(id) || !original || original.peerUid !== peer.peerUid || original.chatType !== chatType
          || !isUin(original.senderUin) || !/^\d+$/.test(original.msgSeq)) throw failure('Reply target is unavailable for this conversation');
        elements.push({ elementType: 7, elementId: '', replyElement: { replayMsgId: original.msgId,
          replayMsgSeq: original.msgSeq, senderUin: original.senderUin, senderUidStr: original.senderUid,
          replyMsgTime: original.msgTime, replyMsgClientSeq: original.clientSeq } });
      } else throw failure('Native text bridge does not support this outbound segment');
    }
    return sendElements(peer, elements, snapshot);
  }
  async function sendElements(peer, elements, snapshot) {
    const chatType = peer.chatType;
    if (Buffer.byteLength(JSON.stringify(elements)) > 64000) throw failure('Message exceeds limit');
    unchanged(snapshot);
    if (sending.size >= 8) throw failure('Too many unresolved QQ sends');
    const service = snapshot.session.getMsgService();
    const serverTime = snapshot.session.getMSFService().getServerTime();
    if (!isId(serverTime)) throw failure('QQ returned an invalid server clock');
    const correlation = service.generateMsgUniqueId(chatType, serverTime);
    if (!isId(correlation)) throw failure('QQ returned an invalid correlation ID');
    // Current desktop callers pass 0 as the native message ID and carry the
    // generated correlation value in Peer.guildId. The actual message ID is
    // supplied by onMsgInfoListUpdate; it is never replaced with our token.
    // Contract: napcat-core/apis/msg.ts:220-254 at the pinned interface revision.
    peer.guildId = correlation;
    const identity = `${snapshot.uin}:${chatType}:${peer.peerUid}:${correlation}`;
    if (sending.has(identity)) throw failure('Duplicate native message ID');
    let timer;
    let entry;
    const confirmed = new Promise((resolve, reject) => {
      let settled = false;
      entry = { accepted: false, delivered: null, complete() {
        if (entry.accepted && entry.delivered) {
          sending.delete(identity);
          entry.finish(null);
        }
      }, finish(error) {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        if (error) reject(error); else resolve(entry.delivered);
        // Timed-out sends retain their reservation until a native completion
        // or a session change, so an uncertain send cannot be duplicated.
        if (!error || error.message !== 'QQ delivery confirmation timed out; not retried') sending.delete(identity);
      } };
      sending.set(identity, entry);
      timer = setTimeout(() => entry.finish(failure('QQ delivery confirmation timed out; not retried')), sendTimeoutMs);
    });
    // Attach rejection handling before native callbacks can fire synchronously.
    const completion = confirmed.then(result => { unchanged(snapshot); return result; });
    try {
      Promise.resolve(service.sendMsg('0', peer, elements, new Map())).then(result => {
        if (!result || result.result !== 0) { sending.delete(identity); entry.finish(failure('Native QQ send was rejected')); }
        else { entry.accepted = true; entry.complete(); }
      }, () => { sending.delete(identity); entry.finish(failure('Native QQ send failed')); });
    } catch { sending.delete(identity); entry.finish(failure('Native QQ send failed')); }
    return completion;
  }
  async function forward(action, params, snapshot) {
    let peer, elements;
    let stage = 'recipient';
    // Everything before sendElements is preparation. A failed preparation
    // proves nothing was sent to the recipient and permits a text fallback.
    try {
      const chatType = action === 'send_group_forward_msg' ? 2 : 1;
      const target = String(chatType === 2 ? params.group_id : params.user_id);
      if (!isUin(target)) throw failure('Invalid forward recipient');
      peer = { chatType, peerUid: chatType === 2 ? target : await uidFor(target, snapshot), guildId: '' };
      stage = 'drafts';
      if (!Array.isArray(params.native_draft_ids) || !params.native_draft_ids.length || params.native_draft_ids.length > 24) {
        throw failure('Invalid forward drafts');
      }
      const ids = params.native_draft_ids.map(id => {
        const original = Number.isSafeInteger(id) ? messages.get(id) : null;
        // Only our own confirmed self-chat drafts may become generated cards.
        if (!original || original.chatType !== 1 || original.peerUid !== snapshot.uid || original.senderUin !== snapshot.uin) {
          throw failure('Forward draft is not an own self-chat message');
        }
        return original.msgId;
      });
      unchanged(snapshot);
      const service = snapshot.session.getMsgService();
      if (typeof service.buildMultiForwardMsg !== 'function') throw failure('Forward builder unavailable');
      stage = 'build';
      const result = await service.buildMultiForwardMsg({ srcMsgIds: ids,
        srcContact: { chatType: 1, peerUid: snapshot.uid, guildId: '' } });
      unchanged(snapshot);
      stage = 'card';
      elements = result?.rspInfo?.elements;
      if (result?.result !== 0 || !Array.isArray(elements) || elements.length !== 1) throw failure('Forward builder rejected drafts');
      const ark = elements[0]?.arkElement;
      const card = JSON.parse(ark?.bytesData ?? '');
      if (elements[0].elementType !== 10 || card.app !== 'com.tencent.multimsg' || !card.meta?.detail?.resid) {
        throw failure('Forward builder returned no usable card');
      }
      if (Buffer.byteLength(JSON.stringify(elements)) > 64000) throw failure('Forward card exceeds limit');
    } catch {
      console.warn('[SnowLuma native forward]', JSON.stringify({ failed_stage: stage }));
      return { native_qq: { code: 'forward_prepare_failed', not_sent: true } };
    }
    // Use the same correlation and actual native delivery callback as text.
    // A timeout after this point is unknown, never a safe-to-repeat failure.
    return sendElements(peer, elements, snapshot);
  }
  async function execute(action, body) {
    if (body.length > 65536) throw failure('Native action payload exceeds limit');
    const params = JSON.parse(body.toString('utf8'));
    if (!params || typeof params !== 'object' || Array.isArray(params)) throw failure('Invalid action parameters');
    const snapshot = requireSession();
    if (action === 'get_login_info') return { user_id: Number(snapshot.uin), nickname: '' };
    if (action === 'get_status') return { online: true, good: true, stat: { packet_received: stats.received, packet_sent: stats.sent },
      native_kernel: true, raw_packet_transport: false };
    if (action === 'get_version_info') return { app_name: 'SnowLuma native text bridge', app_version: '0.1.0', protocol_version: 'v11' };
    if (action === 'native_self_check') {
      const result = await snapshot.session.getMsgService().getLatestDbMsgs({ chatType: 1, peerUid: snapshot.uid, guildId: '' }, 1);
      unchanged(snapshot);
      if (!result || result.result !== 0 || !Array.isArray(result.msgList)) throw failure('Native QQ message service self-check failed');
      return { native_message_service: true, local_query: true, rows: result.msgList.length,
        forward_builder_available: typeof snapshot.session.getMsgService().buildMultiForwardMsg === 'function', network_send_verified: stats.sent > 0,
        live_receive_verified: stats.received > 0 };
    }
    if (['send_private_msg', 'send_group_msg', 'send_msg'].includes(action)) return send(action, params, snapshot);
    if (['send_private_forward_msg', 'send_group_forward_msg'].includes(action)) return forward(action, params, snapshot);
    if (action === 'delete_msg') {
      const original = messages.get(Number(params.message_id));
      if (!original || original.senderUin !== snapshot.uin) throw failure('Own message recall target unavailable');
      const result = await snapshot.session.getMsgService().recallMsg({ chatType: original.chatType, peerUid: original.peerUid, guildId: '' }, [original.msgId]);
      unchanged(snapshot);
      if (!result || result.result !== 0) throw failure('QQ recall failed');
      return {};
    }
    throw failure('Unsupported native text bridge action');
  }
  return { setSession, setEpoch, observe, execute };
}

module.exports = { createKernelOperations };
