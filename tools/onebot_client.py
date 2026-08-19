"""Shared OneBot v11 client for QQ-backed tools (HTTP or WebSocket).

Several tools need to talk to a running OneBot v11 backend (NapCat /
Lagrange): a QQ voice tool sends synthesized speech as a native voice
message, and the QQ空间 (Qzone) tools borrow the logged-in QQ session —
``get_login_info`` for the uin and ``get_cookies`` for the browser cookie
jar — because Qzone's own API was retired years ago.  All of them need the
same plumbing, so it lives here once.

**This module registers no tools**, so the tool registry's module scan never
imports it as one.  Tools import the functions below.

Transport is picked in this order:

1. **The live gateway adapter's connection.**  When the OneBot platform
   adapter is running in this process it already holds a healthy long-lived
   socket.  Reusing it avoids burning a second connection slot on the
   backend and avoids two clients racing on the same account.
2. ``http(s)://`` — the OneBot HTTP API, one POST per action.
3. ``ws(s)://``  — a short-lived WebSocket per action.  Low-frequency tool
   calls do not justify a persistent connection of their own, and this keeps
   the API synchronous and stateless for tool handlers.

Configuration (environment):

* ``ONEBOT_WS_URL``       — forward WebSocket URL (the usual deployment).
* ``ONEBOT_HTTP_URL``     — HTTP API base; takes precedence when set.
* ``ONEBOT_ACCESS_TOKEN`` — access token.  This is NOT the NapCat WebUI
  token; they are two independent secrets and mixing them up produces a
  permanent 401.

The client never retries.  A tool handler turns the raised ``RuntimeError``
into a tool error the model can read and act on; silently retrying a
send would risk double-posting.
"""

import asyncio
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

#: Default per-request timeout (seconds).  Callers doing heavier work (media
#: upload) may pass a larger value.
ONEBOT_TIMEOUT = 15


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def onebot_base_url() -> str:
    """The configured OneBot endpoint (no trailing slash).

    Prefers ``ONEBOT_HTTP_URL`` and falls back to ``ONEBOT_WS_URL`` so a
    WebSocket-only deployment configures just the one URL it has.
    """
    url = os.getenv("ONEBOT_HTTP_URL", "").strip()
    if not url:
        url = os.getenv("ONEBOT_WS_URL", "").strip()
    return url.rstrip("/")


def onebot_access_token() -> str:
    """The optional OneBot access token."""
    return os.getenv("ONEBOT_ACCESS_TOKEN", "").strip()


def onebot_configured() -> bool:
    """Whether a OneBot endpoint is configured.

    Used as the ``check_fn`` of OneBot-backed tools so they are gated out of
    the model's schema entirely when no backend is set up.
    """
    return bool(onebot_base_url())


def _is_ws_url(url: str) -> bool:
    return url.lower().startswith(("ws://", "wss://"))


# ---------------------------------------------------------------------------
# Response handling
# ---------------------------------------------------------------------------


def _check_onebot_payload(payload: dict, action: str) -> dict:
    """Validate a OneBot response envelope and return its ``data`` object."""
    if not isinstance(payload, dict):
        raise RuntimeError(f"OneBot action '{action}' returned a non-object response.")
    if payload.get("status") == "failed":
        msg = payload.get("message") or payload.get("wording") or "unknown error"
        raise RuntimeError(
            f"OneBot action '{action}' failed: {msg} "
            f"(retcode={payload.get('retcode')})"
        )
    data = payload.get("data")
    if data is None:
        raise RuntimeError(f"OneBot action '{action}' returned no data.")
    return data


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def onebot_call(
    action: str,
    params: dict | None = None,
    *,
    timeout: int = ONEBOT_TIMEOUT,
    prefer_live: bool = True,
) -> dict:
    """Invoke a OneBot v11 action and return its ``data`` object.

    Reuses the gateway adapter's live connection when one exists, otherwise
    picks HTTP or WebSocket from the configured URL scheme.  Raises
    ``RuntimeError`` on a transport error, a ``failed`` status, or a missing
    ``data`` field — never returns a partial result.
    """
    if prefer_live:
        data = _try_live_client(action, params or {}, timeout)
        if data is not None:
            return data
    base = onebot_base_url()
    if not base:
        raise RuntimeError(
            "ONEBOT_WS_URL (or ONEBOT_HTTP_URL) is not configured."
        )
    if _is_ws_url(base):
        return _onebot_call_ws(base, action, params or {}, timeout)
    return _onebot_call_http(base, action, params or {}, timeout)


# ---------------------------------------------------------------------------
# Live-adapter transport (preferred when the gateway runs in this process)
# ---------------------------------------------------------------------------


def _try_live_client(action: str, params: dict, timeout: int) -> dict | None:
    """Route through the running platform adapter, or return ``None``.

    ``None`` means "no usable live connection" and the caller falls back to
    its own transport — that fallback is what makes the tools work from
    ``hermes cron`` or the CLI, where no gateway is running.
    """
    try:
        from plugins.platforms.onebot.client import get_live_client
        from plugins.platforms.onebot.protocol import RawAction
    except Exception:  # noqa: BLE001 — plugin absent / not importable here
        return None

    client, loop = get_live_client()
    if client is None or loop is None or loop.is_closed():
        return None

    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is not None and running is loop:
        # We are ON the adapter's own loop: blocking on it would deadlock.
        # Let the caller fall back to the thread-isolated transport.
        return None

    try:
        future = asyncio.run_coroutine_threadsafe(
            client.call_action(RawAction(action, dict(params)), timeout=float(timeout)),
            loop,
        )
        payload = future.result(timeout=timeout + 5)
    except Exception as exc:  # noqa: BLE001 — fall back rather than fail hard
        logger.debug("OneBot: live-adapter call failed (%s); using direct transport", exc)
        return None
    return _check_onebot_payload(payload, action)


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------


def _onebot_call_http(base: str, action: str, params: dict, timeout: int) -> dict:
    """Invoke a OneBot action over the HTTP API."""
    url = f"{base}/{action}"
    body = json.dumps(params).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    token = onebot_access_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        # Deliberately no response body: an error page from a captive portal
        # or a risk-control interstitial would otherwise land in the model's
        # context verbatim.
        raise RuntimeError(
            f"OneBot HTTP {e.code} for action '{action}'."
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Cannot reach OneBot at {base} — is NapCat/Lagrange running? ({e.reason})"
        ) from e

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"OneBot action '{action}' returned non-JSON: {raw[:200]}"
        ) from e

    return _check_onebot_payload(payload, action)


# ---------------------------------------------------------------------------
# WebSocket transport
# ---------------------------------------------------------------------------


def _onebot_call_ws(base: str, action: str, params: dict, timeout: int) -> dict:
    """Invoke a OneBot action over WebSocket (synchronous wrapper)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No event loop on this thread — safe to drive one directly.
        return asyncio.run(_ws_roundtrip(base, action, params, timeout))

    # Already inside a running loop: isolate the round trip in a worker
    # thread so we neither block that loop nor nest two of them.
    import concurrent.futures  # noqa: PLC0415 — only needed on this path

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            lambda: asyncio.run(_ws_roundtrip(base, action, params, timeout))
        )
        return future.result(timeout=timeout + 15)


async def _ws_roundtrip(base: str, action: str, params: dict, timeout: int) -> dict:
    """Open a connection, send one action, and return its ``data``.

    The backend pushes unsolicited events on the same socket, so this reads
    until the reply carrying our ``echo`` arrives and skips everything else.
    """
    try:
        import websockets  # noqa: PLC0415 — core dep, imported on demand
    except ImportError as e:
        raise RuntimeError(
            "OneBot WebSocket transport needs the 'websockets' package — "
            "run `pip install websockets`, or point ONEBOT_HTTP_URL at an "
            "http:// endpoint instead."
        ) from e

    uri = base
    headers = []
    token = onebot_access_token()
    if token:
        # Send the token BOTH ways: OneBot v11 allows either, backends accept
        # both, and a reverse proxy that strips or rewrites headers would
        # otherwise turn into a mystery 401.
        headers.append(("Authorization", f"Bearer {token}"))
        sep = "&" if "?" in uri else "?"
        uri = f"{uri}{sep}access_token={urllib.parse.quote(token)}"

    echo = f"hermes-{action}-{os.urandom(4).hex()}"
    request = json.dumps({"action": action, "params": params, "echo": echo})

    try:
        async with websockets.connect(
            uri,
            additional_headers=headers or None,
            max_size=None,
            open_timeout=timeout,
        ) as ws:
            await ws.send(request)
            # Skip pushed events; stop when our echo comes back.
            for _ in range(500):
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
                if isinstance(msg, dict) and msg.get("echo") == echo:
                    return _check_onebot_payload(msg, action)
    except RuntimeError:
        raise
    except (OSError, asyncio.TimeoutError, TimeoutError) as e:
        raise RuntimeError(
            f"Cannot reach OneBot at {base} — is NapCat/Lagrange running? ({e})"
        ) from e
    except Exception as e:  # noqa: BLE001 — surface one clear message
        raise RuntimeError(f"OneBot WebSocket action '{action}' failed: {e}") from e

    raise RuntimeError(
        f"OneBot action '{action}' got no matching reply (echo timeout)."
    )
