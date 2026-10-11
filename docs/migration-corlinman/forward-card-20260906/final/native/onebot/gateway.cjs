'use strict';

const fs = require('node:fs');
const net = require('node:net');
const http = require('node:http');
const path = require('node:path');
const { timingSafeEqual, randomUUID, createHash } = require('node:crypto');
const { WebSocketServer, WebSocket } = require('ws');
const { encodeFrame, FrameReader, OP } = require('../pipe-server.cjs');
const { SendGuard, GuardError, WRITE_ACTIONS } = require('./send-guard.cjs');
const { FORWARD_ACTIONS, sendForward } = require('./forward.cjs');

function startGateway({ runtimeDir, token, selfUin, host = '127.0.0.1', port = 3001, requestTimeoutMs = 24000, forwardEnabled = false, guard: guardOptions = {} }) {
  if (!path.isAbsolute(runtimeDir) || typeof token !== 'string' || token.length < 16
    || !/^[1-9]\d{4,9}$/.test(String(selfUin)) || !Number.isInteger(port) || port < 0 || port > 65535
    || !['127.0.0.1', '0.0.0.0'].includes(host)) throw new Error('Invalid native gateway configuration');
  const identity = String(selfUin);
  if (typeof forwardEnabled !== 'boolean') throw new Error('Invalid forwardEnabled setting');
  const capabilities = ['text', 'at', 'reply', 'recall', ...(forwardEnabled ? ['forward'] : [])];
  const guard = new SendGuard({ stateFile: guardOptions.state_file ?? path.join(runtimeDir, 'native-onebot-guard.sqlite'),
    account: identity, policy: guardOptions.policy });
  const sockets = new Set();
  const pending = new Map();
  let link = null, closed = false, timer, requestId = 1;
  const log = event => console.info('[SnowLuma native OneBot]', JSON.stringify({ event }));
  const online = () => Boolean(link?.controlReady && link?.recvReady && link.controlLogin && link.recvLogin);
  const send = (ws, value) => {
    if (ws.readyState !== WebSocket.OPEN) return;
    if (ws.bufferedAmount > 1024 * 1024) return ws.terminate();
    ws.send(JSON.stringify(value));
  };
  function rejectPending() {
    for (const entry of pending.values()) { clearTimeout(entry.timer); entry.reject(new GuardError('delivery_unknown', 'QQ connection changed; action was not retried')); }
    pending.clear();
  }
  function disconnect(expected) {
    if (link !== expected) return;
    link = null;
    expected.control?.destroy();
    expected.recv?.destroy();
    rejectPending();
    log('kernel-disconnected');
  }
  function connect(pid) {
    const expected = { pid, controlReady: false, recvReady: false, controlLogin: false, recvLogin: false };
    link = expected;
    for (const kind of ['control', 'recv']) {
      const socketPath = path.join(runtimeDir, `kernel.${pid}.${kind}.sock`);
      const stat = fs.lstatSync(socketPath);
      if (!stat.isSocket() || stat.uid !== process.getuid() || (stat.mode & 0o777) !== 0o600) throw new Error('Untrusted kernel socket');
      const socket = net.createConnection(socketPath);
      expected[kind] = socket;
      const helloTimer = setTimeout(() => socket.destroy(), 5000);
      const reader = new FrameReader(frame => {
        if (link !== expected) return;
        if (frame.op === OP.hello) {
          if (expected[`${kind}Ready`] || frame.value0 !== BigInt(pid)) return disconnect(expected);
          expected[`${kind}Ready`] = true;
          clearTimeout(helloTimer);
          return;
        }
        if (!expected[`${kind}Ready`]) return disconnect(expected);
        if (frame.op === OP.login) {
          const before = online();
          expected[`${kind}Login`] = frame.flags === 4 && frame.msg === identity && frame.value0 === BigInt(identity);
          if (before && !online()) rejectPending();
          if (!before && online()) log('kernel-authenticated');
          return;
        }
        if (kind === 'recv' && frame.op === OP.kernelEvent && online()) {
          if (frame.body.length > 256 * 1024) return disconnect(expected);
          const event = JSON.parse(frame.body.toString('utf8'));
          if (event.post_type !== 'message' || String(event.self_id) !== identity) return;
          for (const ws of sockets) send(ws, event);
          log('native-message-received');
          return;
        }
        if (kind !== 'control' || ![OP.reply, OP.error].includes(frame.op)) return;
        const entry = pending.get(frame.requestId);
        if (!entry) return;
        pending.delete(frame.requestId);
        clearTimeout(entry.timer);
        // The current kernel pipe collapses both delivery timeouts and native
        // rejections into OP.error. It cannot prove a write was never sent.
        if (frame.op === OP.error || frame.status !== 0) entry.reject(new GuardError(
          entry.writeAction ? 'delivery_unknown' : 'native_rejected', 'QQ native action did not confirm completion'));
        else {
          try { entry.resolve(JSON.parse(frame.body.toString('utf8'))); }
          catch { entry.reject(new GuardError('delivery_unknown', 'Native reply could not be decoded')); disconnect(expected); }
        }
      });
      socket.on('data', bytes => { try { reader.push(bytes); } catch { disconnect(expected); } });
      socket.on('error', () => disconnect(expected));
      socket.on('close', () => { clearTimeout(helloTimer); disconnect(expected); });
    }
  }
  function discover() {
    if (closed) return;
    if (!link) {
      try {
        const stat = fs.lstatSync(runtimeDir);
        if (!stat.isDirectory() || stat.uid !== process.getuid() || (stat.mode & 0o777) !== 0o700) throw new Error('Untrusted runtime directory');
        const candidates = fs.readdirSync(runtimeDir).filter(name => /^kernel\.[1-9]\d*\.control\.sock$/.test(name));
        const alive = candidates.map(name => Number(name.split('.')[1])).filter(pid => {
          try { process.kill(pid, 0); return true; } catch { return false; }
        });
        if (alive.length === 1) connect(alive[0]);
      } catch { if (link) disconnect(link); }
    }
    timer = setTimeout(discover, 1000);
  }
  function sendNative(name, params) {
    if (!online()) return Promise.reject(new GuardError('authentication_required', 'QQ not authenticated', { notSent: true }));
    if (pending.size >= 8) return Promise.reject(new GuardError('queue_full', 'Native action capacity exceeded', { notSent: true }));
    const body = Buffer.from(JSON.stringify(params));
    if (body.length > 65536 || link.control.writableLength > 1024 * 1024) return Promise.reject(new GuardError('queue_full', 'Native action exceeds limit', { notSent: true }));
    while (pending.has(requestId)) requestId = requestId % 0xffffffff + 1;
    const id = requestId;
    requestId = requestId % 0xffffffff + 1;
    return new Promise((resolve, reject) => {
      const entry = { resolve, reject, writeAction: WRITE_ACTIONS.has(name), timer: setTimeout(() => {
        pending.delete(id);
        reject(new GuardError('delivery_unknown', 'Native action timed out; not retried'));
      }, FORWARD_ACTIONS.has(name) ? Math.max(requestTimeoutMs, 60000) : requestTimeoutMs) };
      pending.set(id, entry);
      try { link.control.write(encodeFrame({ op: OP.sendRequest, requestId: id, flags: 1, cmd: name, body })); }
      catch { pending.delete(id); clearTimeout(entry.timer); reject(new GuardError('delivery_unknown', 'Native socket write failed')); }
    });
  }
  async function action(name, params, { requestId = randomUUID(), signal } = {}) {
    if (name === 'get_version_info') return { app_name: 'SnowLuma native text bridge', app_version: '0.3.0', protocol_version: 'v11',
      native_qq: { schema: 1, delivery_confirmation: true, idempotency: true,
        capabilities, guard: true } };
    if (name === 'get_native_status') return { online: online(), guard: guard.status(),
      capabilities, requires_human_login: !online() };
    if (name === 'set_native_policy') return guard.setPolicy(params.policy);
    if (name === 'pause_native_sending') return guard.pause();
    if (name === 'resume_native_sending') return guard.resume();
    if (FORWARD_ACTIONS.has(name)) {
      // Before production enablement permit only an authenticated self-test.
      if (!forwardEnabled && !(name === 'send_private_forward_msg' && String(params.user_id) === identity)) {
        throw new GuardError('forward_disabled', 'Forward cards are not enabled', { notSent: true });
      }
      return sendForward({ name, params, selfUin: identity, requestId, signal, guard, sendNative });
    }
    if (WRITE_ACTIONS.has(name)) return guard.run(requestId, name, params, async () => {
      const result = await sendNative(name, params);
      if (name !== 'delete_msg' && (!Number.isSafeInteger(result?.message_id) || result.message_id <= 0)) {
        throw new GuardError('delivery_unknown', 'Native QQ returned no valid delivery confirmation');
      }
      return result;
    }, signal);
    return sendNative(name, params);
  }
  const server = http.createServer((_req, res) => { res.writeHead(404); res.end(); });
  const wss = new WebSocketServer({ noServer: true, maxPayload: 65536, perMessageDeflate: false });
  server.on('upgrade', (req, socket, head) => {
    const supplied = Buffer.from(req.headers.authorization ?? '');
    const expected = Buffer.from(`Bearer ${token}`);
    let url;
    try { url = new URL(req.url, 'http://localhost'); }
    catch { socket.destroy(); return; }
    const queryTokens = url.searchParams.getAll('access_token');
    // Hermes sends the credential in both places. The Authorization header
    // remains mandatory; a query value can only repeat that same credential.
    const validQuery = [...url.searchParams.keys()].every(key => key === 'access_token')
      && (queryTokens.length === 0 || (queryTokens.length === 1 && queryTokens[0] === token));
    if (url.pathname !== '/' || !validQuery || req.headers.origin || sockets.size >= 4
      || supplied.length !== expected.length || !timingSafeEqual(supplied, expected)) {
      socket.end('HTTP/1.1 401 Unauthorized\r\nConnection: close\r\n\r\n');
      return;
    }
    wss.handleUpgrade(req, socket, head, ws => wss.emit('connection', ws));
  });
  wss.on('connection', ws => {
    const connectionId = randomUUID();
    const controller = new AbortController();
    sockets.add(ws);
    ws.on('error', () => {});
    ws.on('close', () => { controller.abort(); sockets.delete(ws); });
    ws.on('message', async (bytes, binary) => {
      if (binary) return ws.close(1003);
      let request;
      try {
        request = JSON.parse(bytes.toString('utf8'));
        if (!request || typeof request.action !== 'string' || !/^[a-z_]{1,48}$/.test(request.action)
          || (request.params !== undefined && (!request.params || typeof request.params !== 'object' || Array.isArray(request.params)))) throw new Error();
      } catch { return ws.close(1007); }
      try {
        const explicitId = request._hermes_request_id;
        if (explicitId !== undefined && (typeof explicitId !== 'string' || !/^[a-f0-9]{32}$/.test(explicitId))) {
          throw new GuardError('invalid_request_id', 'Invalid Hermes operation ID', { notSent: true });
        }
        const id = explicitId ?? `legacy:${connectionId}:${createHash('sha256').update(JSON.stringify(request.echo ?? randomUUID())).digest('hex')}`;
        const data = request.action === 'get_status' && !online() ? { online: false, good: false }
          : await action(request.action, request.params ?? {}, { requestId: id, signal: controller.signal });
        send(ws, { status: 'ok', retcode: 0, data, echo: request.echo });
      } catch (error) {
        send(ws, { status: 'failed', retcode: 1400, data: null,
          message: error instanceof GuardError ? error.message : 'Native QQ action unavailable or failed; no retry was performed',
          native_qq: { code: error instanceof GuardError ? error.code : 'native_action_failed', retryable: false,
            not_sent: error instanceof GuardError ? error.notSent : false,
            retry_after_seconds: error instanceof GuardError ? error.retryAfter : 0 }, echo: request.echo });
      }
    });
    send(ws, { time: Math.floor(Date.now() / 1000), self_id: Number(identity), post_type: 'meta_event',
      meta_event_type: 'lifecycle', sub_type: 'connect' });
  });
  server.listen(port, host, () => log('listening'));
  discover();
  return { action, online, address: () => server.address(), close: async () => {
    closed = true;
    clearTimeout(timer);
    guard.close();
    if (link) disconnect(link);
    for (const ws of sockets) ws.terminate();
    wss.close();
    await new Promise(resolve => server.close(resolve));
  } };
}

if (require.main === module) {
  const filename = process.argv[2];
  if (!filename || !path.isAbsolute(filename)) throw new Error('Absolute private configuration path required');
  const stat = fs.lstatSync(filename);
  if (!stat.isFile() || stat.uid !== process.getuid() || (stat.mode & 0o077) !== 0) throw new Error('Private configuration permissions required');
  const config = JSON.parse(fs.readFileSync(filename, 'utf8'));
  config.guard = { ...config.guard, state_file: config.guard?.state_file ?? path.join(path.dirname(filename), 'native-onebot-guard.sqlite') };
  const gateway = startGateway(config);
  for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, () => { void gateway.close().then(() => process.exit(0)); });
}

module.exports = { startGateway };
