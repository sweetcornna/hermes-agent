'use strict';

const { createHash } = require('node:crypto');

// Native message-service contracts, independently implemented from the interfaces:
// NapNeko/NapCatQQ eecb02140f77b58cb70a3ec8c0b2e816a346f3fe
// packages/napcat-core/{services/NodeIKernelMsgService.ts,types/{msg,element}.ts}
const isUin = value => /^[1-9]\d{4,9}$/.test(String(value));
const isUid = value => typeof value === 'string' && /^u_[A-Za-z0-9_-]{8,64}$/.test(value);
const isId = value => typeof value === 'string' && /^[1-9]\d{0,24}$/.test(value);
const failure = message => new Error(message);
function isUploadedForward(raw) {
  try {
    const card = JSON.parse(raw.elements?.find(e => e.elementType === 10)?.arkElement?.bytesData ?? '');
    return card.app === 'com.tencent.multimsg' && Boolean(card.meta?.detail?.resid);
  } catch { return false; }
}

function createKernelOperations({ getState, emit, now = Date.now, sendTimeoutMs = 20000 }) {
  let current = null;
  let started = 0;
  const messages = new Map();
  const identities = new Map();
  const delivered = new Map();
  const sending = new Map();
  const stats = { received: 0, sent: 0 };
  let lastForwardDiagnostic = null;

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
      let sendIdentity = `${current.uin}:${raw.chatType}:${raw.peerUid}:${raw.guildId}`;
      let pending = raw.senderUin === current.uin && isId(raw.guildId) ? sending.get(sendIdentity) : null;
      if (!pending && kind === 'update' && raw.senderUin === current.uin && isUploadedForward(raw)) {
        // multiForwardMsgWithComment does not guarantee a guildId echo.
        // Serialize cards per peer, then accept only a fresh own uploaded
        // card from the matching peer and current authenticated session.
        const candidates = [...sending.entries()].filter(([, entry]) => entry.forward
          && entry.chatType === raw.chatType && entry.peerUid === raw.peerUid
          && Number(raw.msgTime) >= entry.started && !entry.knownIds.has(raw.msgId));
        if (candidates.length === 1) [sendIdentity, pending] = candidates[0];
      }
      if (pending?.forward && [2, 3].includes(raw.sendStatus) && !isUploadedForward(raw)) continue;
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
  async function sendElements(peer, elements, snapshot, forwardOperation = null) {
    const chatType = peer.chatType;
    if (Buffer.byteLength(JSON.stringify(elements)) > 64000) throw failure('Message exceeds limit');
    unchanged(snapshot);
    if (sending.size >= 8) throw failure('Too many unresolved QQ sends');
    if (forwardOperation && [...sending.values()].some(entry => entry.forward && entry.chatType === chatType && entry.peerUid === peer.peerUid)) {
      throw failure('A forward card for this recipient is still unresolved');
    }
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
      entry = { forward: Boolean(forwardOperation), chatType, peerUid: peer.peerUid,
        started: Math.floor(now() / 1000), knownIds: new Set([...messages.values()].map(message => message.msgId)),
        accepted: false, delivered: null, complete() {
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
      Promise.resolve(forwardOperation ? forwardOperation(service, peer) : service.sendMsg('0', peer, elements, new Map())).then(result => {
        const rejected = forwardOperation ? result?.result != null && result.result !== 0 : !result || result.result !== 0;
        if (rejected) { sending.delete(identity); entry.finish(failure('Native QQ send was rejected')); }
        else { entry.accepted = true; entry.complete(); }
      }, () => { sending.delete(identity); entry.finish(failure('Native QQ send failed')); });
    } catch { sending.delete(identity); entry.finish(failure('Native QQ send failed')); }
    return completion;
  }
  async function forward(action, params, snapshot) {
    let peer, ids;
    try {
      const chatType = action === 'send_group_forward_msg' ? 2 : 1;
      const target = String(chatType === 2 ? params.group_id : params.user_id);
      if (!isUin(target)) throw failure('Invalid forward recipient');
      peer = { chatType, peerUid: chatType === 2 ? target : await uidFor(target, snapshot), guildId: '' };
      if (!Array.isArray(params.native_draft_ids) || !params.native_draft_ids.length || params.native_draft_ids.length > 24) {
        throw failure('Invalid forward drafts');
      }
      ids = params.native_draft_ids.map(id => {
        const original = Number.isSafeInteger(id) ? messages.get(id) : null;
        if (!original || original.chatType !== 1 || original.peerUid !== snapshot.uid || original.senderUin !== snapshot.uin) {
          throw failure('Forward draft is not an own self-chat message');
        }
        return original.msgId;
      });
      unchanged(snapshot);
      if (typeof snapshot.session.getMsgService().multiForwardMsgWithComment !== 'function') throw failure('Native forward sender unavailable');
      if ([...sending.values()].some(entry => entry.forward && entry.chatType === peer.chatType && entry.peerUid === peer.peerUid)) {
        throw failure('A forward for this recipient is still unresolved');
      }
    } catch {
      return { native_qq: { code: 'forward_prepare_failed', not_sent: true } };
    }
    // buildMultiForwardMsg produces only an empty-resource preview on Linux.
    // This native operation uploads the source records and sends the card.
    lastForwardDiagnostic = { method: 'multiForwardMsgWithComment', drafts: ids.length };
    return sendElements(peer, [], snapshot, (service, destination) => service.multiForwardMsgWithComment(
      ids.map(msgId => ({ msgId, senderShowName: String(params.native_sender_name || snapshot.uin).slice(0, 64) })),
      { chatType: 1, peerUid: snapshot.uid, guildId: '' }, destination, [], new Map(),
    ));
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
    if (action === 'native_forward_self_check') {
      const peer = { chatType: 1, peerUid: snapshot.uid, guildId: '' };
      const service = snapshot.session.getMsgService();
      const latest = await service.getLatestDbMsgs(peer, 1);
      unchanged(snapshot);
      const message = latest?.msgList?.[0];
      if (latest?.result !== 0 || message?.senderUin !== snapshot.uin || message?.peerUid !== snapshot.uid) {
        return { readable: false, reason: 'no_own_message' };
      }
      const element = message.elements?.find(e => e.elementType === 10);
      if (!element?.arkElement?.bytesData) return { readable: false, reason: 'not_a_card' };
      const card = JSON.parse(element.arkElement.bytesData);
      if (card.app !== 'com.tencent.multimsg') return { readable: false, reason: 'not_a_forward' };
      const metadata = basic(message);
      if (!metadata) return { readable: false, reason: 'invalid_native_message' };
      const id = remember(metadata);
      const result = await service.getMultiMsg(peer, message.msgId, message.msgId);
      unchanged(snapshot);
      if (result?.result !== 0 || !Array.isArray(result.msgList)) return {
        readable: false, reason: 'fetch_failed', result: result?.result, message_id: id,
        has_resource: Boolean(card.meta?.detail?.resid),
      };
      const text = result.msgList.map(row => (row.elements ?? []).map(e => e.textElement?.content ?? '').join('')).join('');
      return { readable: Boolean(text), message_id: id, nodes: result.msgList.length,
        has_resource: Boolean(card.meta?.detail?.resid), characters: text.length,
        text_sha256: createHash('sha256').update(text).digest('hex'),
        begins_correctly: text.startsWith('CARD-BEGIN-6F2A'), ends_correctly: text.endsWith('CARD-END-9C71') };
    }
    if (action === 'native_self_check') {
      const result = await snapshot.session.getMsgService().getLatestDbMsgs({ chatType: 1, peerUid: snapshot.uid, guildId: '' }, 1);
      unchanged(snapshot);
      if (!result || result.result !== 0 || !Array.isArray(result.msgList)) throw failure('Native QQ message service self-check failed');
      return { native_message_service: true, local_query: true, rows: result.msgList.length,
        forward_builder_available: typeof snapshot.session.getMsgService().multiForwardMsgWithComment === 'function', network_send_verified: stats.sent > 0,
        forward_diagnostic: lastForwardDiagnostic,
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
