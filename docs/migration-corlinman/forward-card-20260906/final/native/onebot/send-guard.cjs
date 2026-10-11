'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { createHash, randomUUID } = require('node:crypto');
const { DatabaseSync } = require('node:sqlite');

const WRITE_ACTIONS = new Set(['send_private_msg', 'send_group_msg', 'send_msg', 'delete_msg', 'send_private_forward_msg', 'send_group_forward_msg']);
const DEFAULT_POLICY = Object.freeze({ global_min_interval_ms: 1000, peer_min_interval_ms: 2000,
  max_sends_per_minute: 30, max_queue_wait_ms: 4000, max_pending_sends: 8,
  failure_threshold: 3, cooldown_seconds: 300, idempotency_hours: 24 });
const RANGES = { global_min_interval_ms: [0, 60000], peer_min_interval_ms: [0, 60000],
  max_sends_per_minute: [1, 600], max_queue_wait_ms: [0, 4000], max_pending_sends: [1, 8],
  failure_threshold: [1, 20], cooldown_seconds: [30, 86400], idempotency_hours: [1, 168] };

class GuardError extends Error {
  constructor(code, message, { notSent = false, retryAfter = 0 } = {}) {
    super(message);
    this.code = code;
    this.notSent = notSent;
    this.retryAfter = retryAfter;
  }
}
function validatePolicy(input = {}) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) throw new Error('Invalid native send policy');
  const result = { ...DEFAULT_POLICY };
  for (const [key, value] of Object.entries(input)) {
    if (!RANGES[key] || !Number.isSafeInteger(value) || value < RANGES[key][0] || value > RANGES[key][1]) {
      throw new Error(`Invalid native send policy: ${key}`);
    }
    result[key] = value;
  }
  return result;
}
function startId(pid) {
  if (process.platform !== 'linux') return 'unknown';
  try {
    const stat = fs.readFileSync(`/proc/${pid}/stat`, 'utf8');
    return fs.readFileSync('/proc/sys/kernel/random/boot_id', 'utf8').trim()
      + ':' + stat.slice(stat.lastIndexOf(')') + 2).split(' ')[19];
  } catch { return null; }
}
function alive(owner) {
  try { process.kill(owner.pid, 0); }
  catch (error) { return error.code === 'EPERM'; }
  return owner.start === 'unknown' || startId(owner.pid) === owner.start;
}
const hash = value => createHash('sha256').update(value).digest('hex');

class SendGuard {
  constructor({ stateFile, account, policy, now = Date.now }) {
    if (!path.isAbsolute(stateFile) || !/^[1-9]\d{4,9}$/.test(String(account))) throw new Error('Invalid native guard storage');
    this.now = now;
    this.account = String(account);
    this.closed = false;
    this.controllers = new Set();
    this.owner = { pid: process.pid, start: startId(process.pid), token: randomUUID() };
    const parent = fs.lstatSync(path.dirname(stateFile));
    if (!parent.isDirectory() || parent.uid !== process.getuid() || (parent.mode & 0o022)) throw new Error('Untrusted native guard directory');
    try { const fd = fs.openSync(stateFile, 'wx', 0o600); fs.closeSync(fd); }
    catch (error) { if (error.code !== 'EEXIST') throw error; }
    const stat = fs.lstatSync(stateFile);
    if (!stat.isFile() || stat.uid !== process.getuid() || (stat.mode & 0o077)) throw new Error('Private native guard file required');
    this.db = new DatabaseSync(stateFile);
    try {
      const appId = this.db.prepare('PRAGMA application_id').get().application_id;
      if (stat.size > 0 && appId !== 0x484e5151) throw new Error('Not a native QQ guard database');
      const version = this.db.prepare('PRAGMA user_version').get().user_version;
      if (version > 1) throw new Error('Unsupported native guard database version');
      this.db.exec('PRAGMA application_id=1213092177; PRAGMA user_version=1;');
      this.db.exec(`PRAGMA busy_timeout=3000; PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS attempts (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
          peer TEXT NOT NULL, scheduled INTEGER NOT NULL, expires INTEGER NOT NULL,
          state TEXT NOT NULL, result TEXT, code TEXT);
        CREATE INDEX IF NOT EXISTS attempts_schedule ON attempts(scheduled);
        CREATE INDEX IF NOT EXISTS attempts_peer_schedule ON attempts(peer, scheduled);`);
      this.transaction(() => {
        const previous = this.get('owner', null);
        if (previous && (!Number.isSafeInteger(previous.pid) || previous.pid <= 0 || typeof previous.start !== 'string'
          || typeof previous.token !== 'string')) throw new Error('Invalid native guard owner record');
        if (previous && alive(previous)) throw new Error('Native send guard already owned by a live process');
        if (this.get('account', this.account) !== this.account) throw new Error('Native guard account mismatch');
        this.put('account', this.account);
        this.policy = validatePolicy(policy ?? this.get('policy', DEFAULT_POLICY));
        this.put('policy', this.policy);
        const interrupted = this.db.prepare("SELECT count(*) AS n FROM attempts WHERE state IN ('queued','dispatched')").get().n;
        if (interrupted) {
          this.db.exec("UPDATE attempts SET state='unknown',code='process_interrupted' WHERE state IN ('queued','dispatched')");
          this.put('paused_until', this.now() + this.policy.cooldown_seconds * 1000);
          this.put('pause_reason', 'process_interrupted');
        }
        this.put('owner', this.owner);
      });
    } catch (error) { this.db.close(); throw error; }
  }
  transaction(fn) {
    this.db.exec('BEGIN IMMEDIATE');
    try { const value = fn(); this.db.exec('COMMIT'); return value; }
    catch (error) { this.db.exec('ROLLBACK'); throw error; }
  }
  get(key, fallback) {
    const row = this.db.prepare('SELECT value FROM meta WHERE key=?').get(key);
    return row ? JSON.parse(row.value) : fallback;
  }
  put(key, value) {
    this.db.prepare('INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value').run(key, JSON.stringify(value));
  }
  status() {
    const until = this.get('paused_until', 0);
    const manual = this.get('manual_pause', false);
    return { durable: true, policy: this.policy, paused: manual || until > this.now(),
      reason: manual ? 'manual_pause' : until > this.now() ? this.get('pause_reason', 'cooldown') : null,
      retry_after_seconds: manual ? null : Math.max(0, Math.ceil((until - this.now()) / 1000)),
      consecutive_failures: this.get('failures', 0),
      counts: Object.fromEntries(this.db.prepare('SELECT state,count(*) AS n FROM attempts GROUP BY state').all().map(row => [row.state, row.n])) };
  }
  setPolicy(input) {
    const next = validatePolicy(input);
    this.transaction(() => this.put('policy', next));
    this.policy = next;
    return this.status();
  }
  pause() { this.transaction(() => this.put('manual_pause', true)); return this.status(); }
  resume() {
    this.transaction(() => { this.put('manual_pause', false); this.put('paused_until', 0); this.put('failures', 0); });
    return this.status();
  }
  reserve(id, name, params) {
    if (this.closed) throw new GuardError('guard_closed', 'Native send guard is closed', { notSent: true });
    if (typeof id !== 'string' || id.length < 1 || id.length > 256) throw new GuardError('invalid_request_id', 'Invalid request identity', { notSent: true });
    const grouped = name === 'send_group_msg' || name === 'send_group_forward_msg' || (name === 'send_msg' && params.message_type === 'group');
    const target = name === 'delete_msg' ? `recall:${params.message_id}` : `${grouped ? 'g' : 'u'}:${grouped ? params.group_id : params.user_id}`;
    const peer = hash(`${this.account}:${target}`);
    const fingerprint = hash(JSON.stringify({ name, params }));
    const key = hash(`${this.account}:${id}`);
    return this.transaction(() => {
      const existing = this.db.prepare('SELECT * FROM attempts WHERE id=?').get(key);
      if (existing) {
        if (existing.fingerprint !== fingerprint) throw new GuardError('request_conflict', 'Request ID was already used for different parameters', { notSent: true });
        if (existing.state === 'succeeded') return { replay: true, result: JSON.parse(existing.result) };
        const uncertain = ['unknown', 'dispatched', 'queued'].includes(existing.state);
        throw new GuardError(uncertain ? 'delivery_unknown' : 'request_already_recorded',
          'This operation is already recorded; it was not sent again', { notSent: existing.state === 'cancelled' });
      }
      const state = this.status();
      if (state.paused) throw new GuardError('sending_paused', 'Native sending is paused', { notSent: true, retryAfter: state.retry_after_seconds });
      const now = this.now();
      this.db.prepare("DELETE FROM attempts WHERE expires<=? AND state IN ('succeeded','failed','cancelled')").run(now);
      if (this.db.prepare('SELECT count(*) AS n FROM attempts').get().n >= 10000) throw new GuardError('journal_full', 'Native journal needs review', { notSent: true });
      if (this.db.prepare("SELECT count(*) AS n FROM attempts WHERE state IN ('queued','dispatched')").get().n >= this.policy.max_pending_sends) {
        throw new GuardError('queue_full', 'Native send queue is full', { notSent: true });
      }
      const last = this.db.prepare('SELECT max(scheduled) AS n FROM attempts').get().n;
      const lastPeer = this.db.prepare('SELECT max(scheduled) AS n FROM attempts WHERE peer=?').get(peer).n;
      const scheduled = Math.max(now, last === null ? now : last + this.policy.global_min_interval_ms,
        lastPeer === null ? now : lastPeer + this.policy.peer_min_interval_ms);
      if (scheduled - now > this.policy.max_queue_wait_ms) throw new GuardError('rate_limited', 'Native send spacing budget exceeded', { notSent: true, retryAfter: Math.ceil((scheduled - now) / 1000) });
      if (this.db.prepare('SELECT count(*) AS n FROM attempts WHERE scheduled>?').get(scheduled - 60000).n >= this.policy.max_sends_per_minute) {
        throw new GuardError('rate_limited', 'Native send minute budget exceeded', { notSent: true, retryAfter: 60 });
      }
      this.db.prepare('INSERT INTO attempts VALUES (?,?,?,?,?,?,NULL,NULL)').run(key, fingerprint, peer, scheduled,
        now + this.policy.idempotency_hours * 3600000, 'queued');
      return { key, scheduled, replay: false };
    });
  }
  async run(id, name, params, operation, signal) {
    const reservation = this.reserve(id, name, params);
    if (reservation.replay) return reservation.result;
    const controller = new AbortController();
    this.controllers.add(controller);
    const abort = () => controller.abort();
    signal?.addEventListener('abort', abort, { once: true });
    if (signal?.aborted) controller.abort();
    let dispatched = false;
    try {
      const wait = Math.max(0, reservation.scheduled - this.now());
      if (wait) await new Promise((resolve, reject) => {
        if (controller.signal.aborted) return reject(new GuardError('cancelled_before_send', 'Caller disconnected before dispatch', { notSent: true }));
        const cancel = () => { clearTimeout(timer); reject(new GuardError('cancelled_before_send', 'Caller disconnected before dispatch', { notSent: true })); };
        const timer = setTimeout(() => { controller.signal.removeEventListener('abort', cancel); resolve(); }, wait);
        controller.signal.addEventListener('abort', cancel, { once: true });
      });
      if (controller.signal.aborted || this.closed) throw new GuardError('cancelled_before_send', 'Caller disconnected before dispatch', { notSent: true });
      this.transaction(() => {
        const state = this.status();
        if (state.paused) throw new GuardError('sending_paused', 'Sending paused before dispatch', { notSent: true, retryAfter: state.retry_after_seconds });
        this.db.prepare("UPDATE attempts SET state='dispatched' WHERE id=?").run(reservation.key);
      });
      dispatched = true;
      const result = await operation();
      if (!this.closed) this.transaction(() => {
        this.db.prepare("UPDATE attempts SET state='succeeded',result=? WHERE id=?").run(JSON.stringify(result), reservation.key);
        this.put('failures', 0);
      });
      return result;
    } catch (error) {
      const unknown = dispatched && !error.notSent && error.code !== 'native_rejected';
      if (!this.closed) this.transaction(() => {
        this.db.prepare('UPDATE attempts SET state=?,code=? WHERE id=?').run(unknown ? 'unknown' : dispatched ? 'failed' : 'cancelled',
          unknown ? 'delivery_unknown' : (error.code || 'native_rejected'), reservation.key);
        if (dispatched && !error.notSent) {
          const failures = this.get('failures', 0) + 1;
          this.put('failures', failures);
          if (unknown || failures >= this.policy.failure_threshold) {
            this.put('paused_until', this.now() + this.policy.cooldown_seconds * 1000);
            this.put('pause_reason', unknown ? 'delivery_unknown' : 'repeated_failures');
          }
        }
      });
      if (unknown) throw new GuardError('delivery_unknown', 'Delivery outcome is unknown; sending paused and no retry performed');
      throw error instanceof GuardError ? error : new GuardError('native_rejected', 'Native QQ rejected the operation');
    } finally {
      signal?.removeEventListener('abort', abort);
      this.controllers.delete(controller);
    }
  }
  close() {
    if (this.closed) return;
    this.closed = true;
    for (const controller of this.controllers) controller.abort();
    this.transaction(() => {
      const pending = this.db.prepare("SELECT count(*) AS n FROM attempts WHERE state='dispatched'").get().n;
      this.db.exec("UPDATE attempts SET state='cancelled',code='shutdown_before_dispatch' WHERE state='queued'");
      this.db.exec("UPDATE attempts SET state='unknown',code='shutdown_during_dispatch' WHERE state='dispatched'");
      if (pending) { this.put('paused_until', this.now() + this.policy.cooldown_seconds * 1000); this.put('pause_reason', 'delivery_unknown'); }
      if (this.get('owner', null)?.token === this.owner.token) this.db.prepare("DELETE FROM meta WHERE key='owner'").run();
    });
    this.db.close();
  }
}

module.exports = { SendGuard, GuardError, WRITE_ACTIONS, DEFAULT_POLICY, validatePolicy };
