'use strict';

const { GuardError } = require('./send-guard.cjs');

const FORWARD_ACTIONS = new Set(['send_private_forward_msg', 'send_group_forward_msg']);

function normalizeForward(name, params, selfUin) {
  const grouped = name === 'send_group_forward_msg';
  const target = String(grouped ? params.group_id : params.user_id);
  if (!FORWARD_ACTIONS.has(name) || !/^[1-9]\d{4,9}$/.test(target)
    || !Array.isArray(params.messages) || !params.messages.length || params.messages.length > 24) {
    throw new GuardError('invalid_forward', 'Invalid forward request', { notSent: true });
  }
  const bodies = [];
  for (const node of params.messages) {
    if (node?.type !== 'node' || String(node.data?.uin ?? node.data?.user_id) !== String(selfUin)) {
      throw new GuardError('invalid_forward', 'Generated cards must use the bot identity', { notSent: true });
    }
    const content = typeof node.data.content === 'string'
      ? [{ type: 'text', data: { text: node.data.content } }] : node.data.content;
    if (!Array.isArray(content) || !content.length || content.some(s => s?.type !== 'text' || typeof s.data?.text !== 'string')) {
      throw new GuardError('unsupported_forward_content', 'Only complete text cards are supported', { notSent: true });
    }
    const text = content.map(s => s.data.text).join('');
    if (!text.trim()) throw new GuardError('invalid_forward', 'Empty forward node', { notSent: true });
    bodies.push(text);
  }
  if (Buffer.byteLength(bodies.join('\n\n')) > 60000) {
    throw new GuardError('forward_too_large', 'Forward text exceeds the supported payload', { notSent: true });
  }
  // Keep every Unicode scalar, including code indentation and line breaks.
  // Draft chunks are storage units; the resulting card carries the full text.
  const drafts = [];
  for (const body of bodies) {
    let chunk = '', bytes = 0;
    for (const char of body) {
      const size = Buffer.byteLength(char);
      if (bytes + size > 8000) { drafts.push(chunk); chunk = ''; bytes = 0; }
      chunk += char; bytes += size;
    }
    if (chunk) drafts.push(chunk);
  }
  if (drafts.length > 24) throw new GuardError('forward_too_large', 'Too many forward draft chunks', { notSent: true });
  return { target, grouped, drafts };
}

function confirmed(result) {
  if (result?.native_qq?.not_sent === true) {
    throw new GuardError('forward_prepare_failed', 'QQ could not build the card; recipient was not sent a message', { notSent: true });
  }
  if (!Number.isSafeInteger(result?.message_id) || result.message_id <= 0) {
    throw new GuardError('delivery_unknown', 'Native QQ returned no delivery confirmation');
  }
  return result;
}

async function sendForward({ name, params, selfUin, requestId, signal, guard, sendNative }) {
  const input = normalizeForward(name, params, selfUin);
  // The outer durable record makes the entire operation idempotent. Each
  // real send also has its own reservation, so drafts never bypass pacing.
  return guard.run(requestId, name, params, async () => {
    const ids = [];
    try {
      for (const [index, text] of input.drafts.entries()) {
        const draft = { user_id: Number(selfUin), message: [{ type: 'text', data: { text } }] };
        const result = await guard.run(`${requestId}:draft:${index}`, 'send_private_msg', draft,
          async () => confirmed(await sendNative('send_private_msg', draft)), signal);
        ids.push(result.message_id);
      }
    } catch (error) {
      // An uncertain self-chat draft may pause the guard. The intended
      // recipient still has not received anything; never mask that boundary.
      throw new GuardError(error.code ?? 'forward_draft_failed', 'Forward draft preparation failed; recipient was not sent a message', { notSent: true });
    }
    const finalParams = { [input.grouped ? 'group_id' : 'user_id']: Number(input.target), native_draft_ids: ids };
    return guard.run(`${requestId}:card`, name, finalParams,
      async () => confirmed(await sendNative(name, finalParams)), signal);
  }, signal);
}

module.exports = { FORWARD_ACTIONS, normalizeForward, sendForward };
