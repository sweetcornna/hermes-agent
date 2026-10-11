"""OneBot v11 forward-WebSocket client.

Hermes is the **client**: it dials out to a running SnowLuma / NapCat /
Lagrange / go-cqhttp instance that exposes an OneBot v11 *forward* WebSocket
server.
Nothing here binds a listening port.

::

    SnowLuma / NapCat  <── ws ──>  OneBotClient
                            │   ▲
                    inbound │   │ outbound actions
                            ▼   │
                     protocol.MessageEvent      protocol.Action

Reconnect ladder ``1s → 2s → 5s → 10s → 30s`` (last entry repeats), with a
30 s protocol ping.  The reconnect loop lives *inside* the reader task, so
:meth:`OneBotClient.inbound` never raises a transient transport error —
callers just see the next event once the link is back.  Only permanent
configuration problems (empty URL) raise, and they raise at construction.

Deliberately **not** ported from the source implementation: the outbound
content-policy classifier (``inspect_action`` / ``send_safe_refusal``).
That belongs to a component Hermes does not have; the adapter's own send
path is the place to add moderation if it is ever wanted.

The only third-party import is ``websockets``, which is already a core
Hermes dependency.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncIterator,
    Callable,
    Deque,
    Dict,
    List,
    Optional,
    Tuple,
)

from .protocol import (
    Action,
    Event,
    MessageEvent,
    action_to_wire,
    parse_event,
)

if TYPE_CHECKING:  # pragma: no cover — typing only, see the live-adapter registry
    from .adapter import OneBotAdapter

logger = logging.getLogger(__name__)

__all__ = [
    "INBOUND_QUEUE_MAX",
    "OUTBOUND_QUEUE_MAX",
    "PING_INTERVAL",
    "RECONNECT_SCHEDULE",
    "OneBotConfigError",
    "OneBotClient",
    "OneBotConfig",
    "OneBotTransportError",
    "get_live_adapter",
    "get_live_client",
    "set_live_adapter",
    "set_live_client",
]


#: Backoff ladder (seconds) between reconnect attempts; the last entry repeats.
RECONNECT_SCHEDULE: Tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0)

#: WebSocket protocol ping interval — matches the backend's idle expectation.
PING_INTERVAL: float = 30.0

#: Bounded queues.  Small on purpose: this runs on a 2 vCPU / 1.9 GB box.
INBOUND_QUEUE_MAX = 64
OUTBOUND_QUEUE_MAX = 64


class OneBotConfigError(ValueError):
    """Permanent misconfiguration — retrying cannot help."""


class OneBotTransportError(RuntimeError):
    """Transient transport failure — the reconnect ladder handles it."""


@dataclass
class OneBotConfig:
    """Connection settings for :class:`OneBotClient`.

    ``access_token`` is sent BOTH as an ``Authorization: Bearer`` handshake
    header and (when ``token_in_query``) as an ``?access_token=`` query
    parameter.  OneBot v11 allows either; the two source implementations
    picked one each, and sending both costs one line and survives a reverse
    proxy that strips or rewrites headers.  If a strict backend ever objects
    to seeing the token twice, set ``token_in_query=False`` — the header
    form is the one that has been running in production.
    """

    url: str
    access_token: Optional[str] = None
    self_ids: List[int] = field(default_factory=list)
    reconnect_schedule: Tuple[float, ...] = RECONNECT_SCHEDULE
    ping_interval: float = PING_INTERVAL
    token_in_query: bool = True
    #: Seconds to wait for a ``call_action`` response envelope by default.
    call_timeout: float = 15.0


@dataclass
class _EchoedAction:
    """Outbound-queue wrapper pairing an action with its ``echo`` id."""

    action: Action
    echo: str
    # ``call_action`` owns the total queue+response deadline.  If that caller
    # times out or is cancelled after enqueueing, the writer must not later
    # turn the abandoned request into a ghost wire action after reconnect.
    cancelled: bool = False


@dataclass
class _OutboundLease:
    """One writer-owned item and the queue acknowledgement it carries.

    Items popped from ``_outbound_front`` have already settled their original
    queue lease.  Items obtained from ``asyncio.Queue.get`` own exactly one
    ``task_done`` call, released by the writer's single outer ``finally``.
    """

    item: Any
    source_queue: Optional[asyncio.Queue] = None
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        if self.source_queue is not None:
            self.source_queue.task_done()


class OneBotClient:
    """Forward-WebSocket OneBot v11 client.

    Lifecycle::

        client = OneBotClient(OneBotConfig(url=...))
        await client.connect()                  # spawns the reader task
        async for ev in client.inbound(): ...   # yields protocol.MessageEvent
        await client.send_action(action)        # fire-and-forget
        resp = await client.call_action(action) # echo-correlated round trip
        await client.close()

    Only ``message`` post types surface from :meth:`inbound`; meta / notice /
    request events are parsed (so a malformed one cannot drop the socket)
    and then absorbed.
    """

    def __init__(
        self,
        config: OneBotConfig,
        *,
        on_self_id: Optional[Callable[[int], None]] = None,
        on_account_status: Optional[Callable[[Optional[bool], int, str], None]] = None,
    ) -> None:
        if not (config.url or "").strip():
            raise OneBotConfigError("OneBotConfig.url is empty")
        self._cfg = config
        self._on_self_id = on_self_id
        self._on_account_status = on_account_status
        self._last_self_id: Optional[int] = None
        self._last_notified_self_id: Optional[int] = None
        self._ws: Any = None
        self._closed = False
        # Bounded queue: a stalled consumer must not grow memory without
        # bound.  On overflow the OLDEST event is dropped so the most recent
        # user message still surfaces (see ``_pump``); blocking here would
        # let the websockets frame buffer fill until the server closes the
        # connection with 1009 and we reconnect-storm.
        self._inbound_q: asyncio.Queue = asyncio.Queue(maxsize=INBOUND_QUEUE_MAX)
        self._inbound_dropped = 0
        self._outbound_q: asyncio.Queue = asyncio.Queue(maxsize=OUTBOUND_QUEUE_MAX)
        # Drained BEFORE ``_outbound_q`` so a requeued action keeps its place
        # in line (asyncio.Queue has no push-left).
        self._outbound_front: Deque[Any] = deque()
        self._pending_responses: Dict[str, "asyncio.Future[Dict[str, Any]]"] = {}
        self._echo_seq = 0
        # Per-action retry counter, keyed by id() — actions are unhashable
        # dataclasses.  Cleared on success or drop.
        self._outbound_retries: Dict[int, int] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._last_event_at_ms: Optional[int] = None
        self._last_status_online: Optional[bool] = None
        self._last_status_online_at_ms: Optional[int] = None
        self._last_status_reason: Optional[str] = None
        self._last_heartbeat_at_ms: Optional[int] = None
        self._connected = False

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    @property
    def url(self) -> str:
        """The endpoint this client dials (safe to log — carries no token)."""
        return self._cfg.url

    @property
    def connected(self) -> bool:
        """True while a WebSocket is currently established."""
        return self._connected

    @property
    def inbound_dropped_count(self) -> int:
        """Events discarded because the consumer fell behind."""
        return self._inbound_dropped

    @property
    def outbound_queue_depth(self) -> int:
        """Actions buffered but not yet on the wire (send backpressure)."""
        return len(self._outbound_front) + self._outbound_q.qsize()

    @property
    def last_self_id(self) -> Optional[int]:
        """Most recent non-zero ``self_id`` seen on any event.

        Authoritative for mention matching: it tracks a backend account switch
        without a config edit.
        """
        return self._last_self_id

    @property
    def last_event_at_ms(self) -> Optional[int]:
        """Wall-clock ms of the last parsed frame; ``None`` before the first."""
        return self._last_event_at_ms

    @property
    def last_status_online(self) -> Optional[bool]:
        """Last ``status.online`` seen on a heartbeat.

        This is the ONLY signal that separates "the WebSocket is fine" from
        "the QQ account got kicked offline" — a backend may keep heartbeating
        after a KickedOffLine, with this flag flipped to False.
        """
        return self._last_status_online

    @property
    def last_status_online_at_ms(self) -> Optional[int]:
        """Wall-clock ms of the latest account status sample.

        This is deliberately separate from :attr:`last_event_at_ms`: a
        WebSocket frame only proves that the OneBot endpoint is alive, not that
        the QQ account is logged in.  The sample may come from a heartbeat, a
        ``bot_offline`` notice, or an explicit account probe performed by the
        adapter.
        """
        return self._last_status_online_at_ms

    @property
    def last_status_reason(self) -> Optional[str]:
        """How the current account-status value was learned."""
        return self._last_status_reason

    @property
    def last_heartbeat_at_ms(self) -> Optional[int]:
        """Wall-clock ms of the latest heartbeat frame, including unknown ones."""
        return self._last_heartbeat_at_ms

    @property
    def account_state(self) -> str:
        """Three-state account view, independent of :attr:`connected`."""
        if self._last_status_online is True:
            return "online"
        if self._last_status_online is False:
            return "auth_required"
        return "unknown"

    @property
    def account_auth_required(self) -> bool:
        """Whether an explicit account-offline signal was observed."""
        return self._last_status_online is False

    def set_account_status_callback(
        self, callback: Optional[Callable[[Optional[bool], int, str], None]]
    ) -> None:
        """Install the adapter observer used for prompt send protection.

        This setter keeps the constructor backward-compatible with small
        transport doubles and integrations that only know about the original
        ``on_self_id`` callback.
        """
        self._on_account_status = callback

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "OneBotClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def connect(self) -> None:
        """Spawn the background reader loop.

        Does NOT block on the actual handshake: the connect lives inside the
        reader so the reconnect ladder has exactly one implementation.
        """
        if self._reader_task is not None:
            return
        self._closed = False
        self._reader_task = asyncio.create_task(
            self._reader_loop(), name="onebot-reader"
        )

    async def close(self) -> None:
        """Tear down the reader loop and the socket."""
        self._closed = True
        # Fail in-flight call_action waiters immediately rather than letting
        # them ride out a full timeout against a socket that is going away.
        for fut in self._pending_responses.values():
            if not fut.done():
                fut.set_exception(OneBotTransportError("OneBot client is closed"))
        self._pending_responses.clear()
        if self._reader_task is not None:
            self._reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        if self._ws is not None:
            with suppress(Exception):
                await self._ws.close()
            self._ws = None
        self._connected = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def inbound(self) -> AsyncIterator[MessageEvent]:
        """Yield inbound ``message`` events until the client is closed."""
        if self._reader_task is None:
            await self.connect()
        while not self._closed:
            try:
                ev = await self._inbound_q.get()
            except asyncio.CancelledError:
                return
            if not isinstance(ev, MessageEvent):
                continue
            yield ev

    async def send_action(self, action: Action) -> None:
        """Enqueue an outbound action (fire and forget)."""
        if self._closed:
            raise OneBotTransportError("OneBot client is closed")
        await self._outbound_q.put(action)

    async def call_action(
        self, action: Action, *, timeout: Optional[float] = None
    ) -> Dict[str, Any]:
        """Send an action and await the backend's response envelope.

        The frame carries an ``echo`` id so the reader can route the
        ``{"status", "retcode", "data"}`` reply back here.  Use this when
        delivery actually matters (message sends we report on, file uploads,
        merged-forward cards, any ``get_*`` query): ``retcode`` 0 is ok and 1
        is "accepted, async", anything else means the backend rejected the
        action even though the WebSocket write succeeded.

        Raises :class:`OneBotTransportError` when closed and
        :class:`asyncio.TimeoutError` when no response lands in time (some
        backends never echo — callers decide whether that is a failure).
        """
        if self._closed:
            raise OneBotTransportError("OneBot client is closed")
        self._echo_seq += 1
        echo = f"hermes-{self._echo_seq}"
        fut: "asyncio.Future[Dict[str, Any]]" = (
            asyncio.get_running_loop().create_future()
        )
        queued = _EchoedAction(action=action, echo=echo)
        self._pending_responses[echo] = fut
        timeout_seconds = (
            timeout if timeout is not None else self._cfg.call_timeout
        )
        deadline_task = asyncio.create_task(
            asyncio.sleep(max(0.0, float(timeout_seconds))),
            name=f"onebot-call-deadline-{echo}",
        )
        put_task = asyncio.create_task(
            self._outbound_q.put(queued), name=f"onebot-call-enqueue-{echo}"
        )
        completed = False

        async def cancel_and_wait(task: "asyncio.Task[Any]") -> None:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        try:
            # Phase one is an explicit three-way race: bounded-queue capacity,
            # the already-registered response future (which ``close`` fails),
            # and the total deadline.  Registering ``fut`` before starting the
            # put keeps an extremely fast echo safe; observing it here also
            # lets close interrupt a put blocked behind a full queue.
            done, _pending = await asyncio.wait(
                {put_task, fut, deadline_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if fut in done:
                queued.cancelled = True
                await cancel_and_wait(put_task)
                response = fut.result()
                completed = True
                return response
            if deadline_task in done:
                queued.cancelled = True
                await cancel_and_wait(put_task)
                raise asyncio.TimeoutError

            # Consume a put exception before moving to the response phase.
            await put_task
            done, _pending = await asyncio.wait(
                {fut, deadline_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if fut not in done:
                queued.cancelled = True
                raise asyncio.TimeoutError
            response = fut.result()
            completed = True
            return response
        except asyncio.CancelledError:
            queued.cancelled = True
            await cancel_and_wait(put_task)
            raise
        finally:
            self._pending_responses.pop(echo, None)
            if not completed:
                # Queue removal is not supported by ``asyncio.Queue``.  Mark
                # the exact wrapper instead; the writer skips it whether it is
                # still in the queue or was requeued at the front after a
                # disconnect.  Cancelling the future also prevents a late
                # response from retaining the abandoned caller.
                queued.cancelled = True
                if not fut.done():
                    fut.cancel()
            # Both helper tasks are structured children of this call.  Always
            # join them before returning so neither can enqueue later or emit
            # an unobserved task exception.  Likewise consume a close exception
            # on ``fut`` even when external cancellation won the race.
            await cancel_and_wait(put_task)
            await cancel_and_wait(deadline_task)
            if fut.done() and not fut.cancelled():
                with suppress(Exception):
                    fut.exception()

    # ------------------------------------------------------------------
    # Reader loop / reconnect ladder
    # ------------------------------------------------------------------

    async def _reader_loop(self) -> None:
        """connect → pump → on disconnect, sleep and retry.

        The backoff index resets after a clean disconnect and grows only
        across consecutive failures, so a nightly backend restart does not
        push us into the 30 s tier.
        """
        attempt = 0
        schedule = self._cfg.reconnect_schedule or RECONNECT_SCHEDULE
        while not self._closed:
            try:
                await self._connect_once()
                if self._closed:
                    return
                attempt = 0  # clean disconnect — reset the ladder
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — every failure retries
                logger.warning(
                    "OneBot: connection to %s failed (%s: %s)",
                    self._cfg.url,
                    type(exc).__name__,
                    exc,
                )
            finally:
                self._connected = False
            if self._closed:
                return
            delay = schedule[min(attempt, len(schedule) - 1)]
            attempt += 1
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return

    def _handshake(self) -> Tuple[str, List[Tuple[str, str]]]:
        """Build the (uri, headers) pair for one dial.

        Token is sent in both positions — see :class:`OneBotConfig`.
        """
        uri = self._cfg.url
        headers: List[Tuple[str, str]] = []
        token = (self._cfg.access_token or "").strip()
        if token:
            headers.append(("Authorization", f"Bearer {token}"))
            if self._cfg.token_in_query:
                from urllib.parse import quote

                sep = "&" if "?" in uri else "?"
                uri = f"{uri}{sep}access_token={quote(token)}"
        return uri, headers

    async def _connect_once(self) -> None:
        """One connect → pump cycle."""
        import websockets  # noqa: PLC0415 — core dep, imported at use site

        uri, headers = self._handshake()
        # ``additional_headers`` is the websockets >= 13 spelling.
        async with websockets.connect(
            uri,
            additional_headers=headers or None,
            ping_interval=self._cfg.ping_interval,
            max_size=2 ** 20,
        ) as ws:
            self._ws = ws
            self._connected = True
            # A completed handshake proves the backend is alive RIGHT NOW.
            # Stamping here means the health watcher flips online within a
            # couple of seconds of a restart instead of waiting up to 30 s
            # for the first heartbeat.
            self._last_event_at_ms = int(time.time() * 1000)
            logger.info("OneBot: connected to %s", self._cfg.url)
            try:
                await self._pump(ws)
            finally:
                self._connected = False
                self._ws = None

    # ------------------------------------------------------------------
    # Pump
    # ------------------------------------------------------------------

    @staticmethod
    def _coerce_online(value: Any) -> Optional[bool]:
        """Decode the loose boolean dialects used by OneBot backends.

        ``bool("false")`` is ``True`` in Python, so using it directly would
        turn an explicit offline heartbeat into an online account.  Unknown
        values remain ``None`` and are not allowed to overwrite a previous
        known state.
        """
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if value == 0:
                return False
            if value == 1:
                return True
            return None
        if isinstance(value, str):
            normalized = value.strip().casefold()
            if normalized in {"true", "yes", "on", "online", "1"}:
                return True
            if normalized in {"false", "no", "off", "offline", "0"}:
                return False
        return None

    def _record_account_status(
        self,
        online: bool,
        now_ms: int,
        reason: str,
        *,
        notify: bool = True,
    ) -> None:
        """Record an explicit account signal and notify the adapter observer."""
        self._last_status_online = online
        self._last_status_online_at_ms = now_ms
        self._last_status_reason = reason
        if not notify:
            return
        callback = self._on_account_status
        if callback is None:
            return
        try:
            callback(online, now_ms, reason)
        except Exception as exc:  # noqa: BLE001 — observation must not break WS
            logger.warning(
                "OneBot: account-status observer failed (%s)", type(exc).__name__
            )

    def observe_account_status(
        self,
        online: bool,
        *,
        now_ms: Optional[int] = None,
        reason: str = "probe",
        notify: bool = True,
    ) -> None:
        """Record an account result obtained outside the event pump.

        The adapter uses this for successful ``get_status`` /
        ``get_login_info`` calls.  Keeping the write here means the client and
        adapter expose the same last-status timestamp and reason.
        """
        self._record_account_status(
            bool(online),
            int(time.time() * 1000) if now_ms is None else now_ms,
            reason,
            notify=notify,
        )

    def _absorb_status(self, raw: Dict[str, Any], now_ms: int) -> None:
        """Track the QQ account's online flag off the raw frame.

        The typed ``MetaEvent`` has no slot for the status block, and the
        block's shape varies by build:

        * upstream OneBot v11 — ``{"status": {"online": true}}``
        * older backends     — ``{"status": {"online": 1}}``
        * some builds nest it under ``status.app.online`` or only ship
          ``status.good``
        * a logged-out bot may omit the flag entirely

        A missing or malformed flag is **unknown**, not offline.  The
        distinction matters during the startup window: an endpoint that emits
        heartbeats without the optional field must not be mistaken for a
        logged-out account and cause every existing send to be rejected.  A
        previously explicit ``False`` remains in force until a later explicit
        ``True`` heartbeat or a successful account probe.
        """
        post_type = raw.get("post_type")
        if post_type == "notice":
            # NapCat emits this even though its forward WebSocket remains
            # healthy.  It is the account-auth boundary, not a transport
            # failure, so the adapter must not reconnect the socket.
            notice_type = str(raw.get("notice_type") or "").strip().casefold()
            if notice_type in {"bot_offline", "bot-offline"}:
                self._record_account_status(False, now_ms, "bot_offline_notice")
            return
        if post_type != "meta_event" or raw.get("meta_event_type") != "heartbeat":
            return
        self._last_heartbeat_at_ms = now_ms
        status_block = raw.get("status")
        online_raw: Any = None
        if isinstance(status_block, dict):
            online_raw = status_block.get("online")
            if online_raw is None:
                inner_app = status_block.get("app")
                if isinstance(inner_app, dict):
                    online_raw = inner_app.get("online")
                if online_raw is None and "good" in status_block:
                    online_raw = status_block.get("good")
        online = self._coerce_online(online_raw)
        if online is not None:
            self._record_account_status(online, now_ms, "heartbeat")

    def _observe_self_id(self, event: Event) -> None:
        """Record the bot's own uin, notifying the observer only on change."""
        self_id = getattr(event, "self_id", 0)
        if not isinstance(self_id, int) or isinstance(self_id, bool) or self_id <= 0:
            return
        self._last_self_id = self_id
        if self._on_self_id is None or self_id == self._last_notified_self_id:
            return
        try:
            self._on_self_id(self_id)
        except Exception as exc:  # noqa: BLE001 — observation must not break WS
            # Leave ``_last_notified_self_id`` unchanged so the next event
            # retries the notification.
            logger.warning(
                "OneBot: self_id observer failed (%s)", type(exc).__name__
            )
        else:
            self._last_notified_self_id = self_id

    def _enqueue_inbound(self, event: Event) -> None:
        """Queue an inbound event, dropping the OLDEST on overflow."""
        try:
            self._inbound_q.put_nowait(event)
            return
        except asyncio.QueueFull:
            pass
        with suppress(asyncio.QueueEmpty):
            self._inbound_q.get_nowait()
        self._inbound_dropped += 1
        logger.warning(
            "OneBot: inbound queue full — dropped oldest (total=%d)",
            self._inbound_dropped,
        )
        # If even the second put trips QueueFull (re-entry from another
        # task), swallow it rather than blocking the reader.
        with suppress(asyncio.QueueFull):
            self._inbound_q.put_nowait(event)

    async def _pump(self, ws: Any) -> None:
        """Two-way pump: decode inbound frames, drive the writer task."""
        writer = asyncio.create_task(self._writer_loop(ws), name="onebot-writer")
        try:
            async for raw_msg in ws:
                if self._closed:
                    break
                if isinstance(raw_msg, (bytes, bytearray)):
                    # OneBot v11 is text-only; ignore binary frames.
                    continue
                try:
                    raw = json.loads(raw_msg)
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
                if not isinstance(raw, dict):
                    continue
                now_ms = int(time.time() * 1000)
                self._last_event_at_ms = now_ms
                self._absorb_status(raw, now_ms)
                # An API response envelope for an in-flight call_action —
                # resolve the waiter and keep it out of the event queue
                # (responses are not events).
                echo = raw.get("echo")
                if isinstance(echo, str) and echo:
                    pending = self._pending_responses.pop(echo, None)
                    if pending is not None:
                        if not pending.done():
                            pending.set_result(raw)
                        continue
                event = parse_event(raw)
                self._observe_self_id(event)
                self._enqueue_inbound(event)
        finally:
            writer.cancel()
            with suppress(asyncio.CancelledError):
                await writer


    async def _writer_loop(self, ws: Any) -> None:
        """Drain the outbound queues, writing each action as a text frame.

        On a transient send failure the action is requeued at the FRONT (so
        ordering survives) and the pump is aborted to trigger a reconnect.
        After two consecutive failures for the same action we drop it: a
        poison payload (oversized text, malformed segments) would otherwise
        loop forever and starve everything behind it.  Cancellation requeues
        without counting as a retry — it is not the action's fault.
        """
        while True:
            try:
                if self._outbound_front:
                    lease = _OutboundLease(self._outbound_front.popleft())
                else:
                    lease = _OutboundLease(
                        await self._outbound_q.get(), self._outbound_q
                    )
            except asyncio.CancelledError:
                return
            action = lease.item
            try:
                if isinstance(action, _EchoedAction) and action.cancelled:
                    self._outbound_retries.pop(id(action), None)
                    continue
                if isinstance(action, _EchoedAction):
                    wire = action_to_wire(action.action)
                    wire["echo"] = action.echo
                else:
                    wire = action_to_wire(action)
                payload = json.dumps(wire)
                try:
                    await ws.send(payload)
                except asyncio.CancelledError:
                    self._outbound_front.appendleft(action)
                    raise
                except Exception as exc:  # noqa: BLE001 — classified below
                    inner = (
                        action.action if isinstance(action, _EchoedAction) else action
                    )
                    retries = self._outbound_retries.get(id(action), 0) + 1
                    if retries >= 2:
                        self._outbound_retries.pop(id(action), None)
                        logger.error(
                            "OneBot: dropping action %s after %d failed sends (%s)",
                            type(inner).__name__,
                            retries,
                            exc,
                        )
                        # Do not re-raise — keep draining the rest of the queue.
                        continue
                    self._outbound_retries[id(action)] = retries
                    self._outbound_front.appendleft(action)
                    logger.warning(
                        "OneBot: send failed for %s (retry %d): %s",
                        type(inner).__name__,
                        retries,
                        exc,
                    )
                    # Abort the pump so the reconnect ladder runs; the requeued
                    # action goes out on the fresh socket.
                    raise OneBotTransportError(f"OneBot send failed: {exc}") from exc
                else:
                    self._outbound_retries.pop(id(action), None)
            finally:
                lease.release()



# ---------------------------------------------------------------------------
# Live-client registry — the seam the outbound tool layer uses
# ---------------------------------------------------------------------------
#
# ``tools/onebot_client.py`` exposes a SYNCHRONOUS ``onebot_call()`` for tool
# handlers (the Qzone family borrows the QQ login state through it).  When the
# gateway is running in this process there is already a healthy long-lived
# connection; opening a second one would burn a backend connection slot and
# race with the adapter.  The adapter publishes itself here on connect, and
# the tool layer reuses it when present, falling back to its own short-lived
# connection otherwise (CLI / cron, where no gateway runs).

_LIVE_CLIENT: Optional[OneBotClient] = None
_LIVE_LOOP: Optional[asyncio.AbstractEventLoop] = None


def set_live_client(
    client: Optional[OneBotClient],
    loop: Optional[asyncio.AbstractEventLoop] = None,
) -> None:
    """Publish (or clear, with ``None``) the process-wide live client."""
    global _LIVE_CLIENT, _LIVE_LOOP
    _LIVE_CLIENT = client
    _LIVE_LOOP = loop


def get_live_client() -> Tuple[Optional[OneBotClient], Optional[asyncio.AbstractEventLoop]]:
    """Return ``(client, loop)`` for the live adapter, or ``(None, None)``.

    The loop is returned alongside because a synchronous tool handler runs
    on a different thread and must hand work to the adapter's loop via
    ``asyncio.run_coroutine_threadsafe``.
    """
    if _LIVE_CLIENT is None or not _LIVE_CLIENT.connected:
        return None, None
    return _LIVE_CLIENT, _LIVE_LOOP


# ---------------------------------------------------------------------------
# Live-adapter registry — the same seam, one layer up
# ---------------------------------------------------------------------------
#
# ``get_live_client`` hands back the raw socket, which is everything a
# read-only action such as ``get_cookies`` needs.  An *outbound* tool needs
# strictly more than the socket: the emergency mute, the Tencent content
# policy and the shared recent-chatter buffer all hang off the adapter, and
# ``voice.speak()`` is the single place that applies them in the right order.
# A tool holding only the client would have to rebuild that list — and every
# rebuild so far has come out two-of-three correct, which is the most
# expensive kind of nearly-right.  So the adapter publishes itself here as
# well, and the tool layer prefers it whenever a gateway is live, falling
# back to the raw transport only under CLI / cron where none of that state
# exists in the first place.
#
# Typed loosely on purpose: ``adapter`` imports this module, so naming
# ``OneBotAdapter`` at runtime here would close an import cycle.  The
# ``TYPE_CHECKING`` import above gives editors the real type without it.

_LIVE_ADAPTER: Optional["OneBotAdapter"] = None
_LIVE_ADAPTER_LOOP: Optional[asyncio.AbstractEventLoop] = None


def set_live_adapter(
    adapter: Optional["OneBotAdapter"],
    loop: Optional[asyncio.AbstractEventLoop] = None,
) -> None:
    """Publish (or clear, with ``None``) the process-wide live adapter."""
    global _LIVE_ADAPTER, _LIVE_ADAPTER_LOOP
    _LIVE_ADAPTER = adapter
    _LIVE_ADAPTER_LOOP = loop


def get_live_adapter() -> Tuple[Optional[Any], Optional[asyncio.AbstractEventLoop]]:
    """Return ``(adapter, loop)`` for the live adapter, or ``(None, None)``.

    ``(None, None)`` is the ordinary answer under the CLI and cron, where no
    gateway runs — callers treat it as "use your own transport", never as an
    error.

    Liveness mirrors :func:`get_live_client`: a published adapter whose socket
    has since dropped is not usable, and handing it out would turn a clean
    fallback into a timeout.  ``_client`` is read defensively rather than
    imported-and-typed, for the import-cycle reason above.
    """
    if _LIVE_ADAPTER is None:
        return None, None
    client = getattr(_LIVE_ADAPTER, "_client", None)
    if client is None or not getattr(client, "connected", False):
        return None, None
    return _LIVE_ADAPTER, _LIVE_ADAPTER_LOOP
