"""Shared QQ空间 (QZone) primitives: auth, transport, and response parsing.

Every QZone tool in this package needs the same four things — the logged-in
uin, the browser cookie jar, the ``g_tk`` CSRF token derived from it, and a
way to speak form-urlencoded HTTP to Tencent's reverse-engineered web CGIs.
They live here once so ``publish`` and ``feed`` cannot drift apart; the
corlinman original made the same call (``comment.py`` imports its auth
helpers straight out of ``publish.py``).

QQ retired the QZone OpenAPI ``emotion`` interface years ago, so there is no
supported way to post a 说说. These tools drive the *web* endpoints, which
need a logged-in session. Rather than running their own QR login they
**borrow** the session from a running OneBot v11 backend (NapCat /
Lagrange): ``get_login_info`` yields the uin and ``get_cookies`` yields the
``*.qq.com`` jar including ``p_skey``. ``g_tk`` is then computed locally.

Transport
---------
Network access goes through an injectable callable (:data:`Transport`) so
every HTTP path in this package is unit-testable without touching the
network — corlinman got this property from ``httpx``'s pluggable transports;
here it is a plain function parameter, because hermes tool handlers are
synchronous and adding ``httpx`` would mean a new runtime dependency the
target install does not have. The default implementation is stdlib
``urllib``.

HTTP failures never echo the response body. A Tencent risk-control
interstitial or captive-portal error page landing verbatim in the model's
context is both a prompt-injection surface and a waste of tokens; the status
code is the only part that helps.

Compliance note (inherited verbatim from both source implementations):
automated posting violates Tencent's Terms of Service and carries an
account-ban risk. Nothing here retries silently — a failure is surfaced to
the caller, which is the only safe behaviour when the write may have landed.
"""

from __future__ import annotations

import html as _html
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "HttpResponse",
    "QZoneAuth",
    "QZoneError",
    "Transport",
    "compute_gtk",
    "default_transport",
    "extract_cookie_value",
    "extract_json_object",
    "parse_callback_json",
    "qzone_auth",
    "qzone_get",
    "qzone_post",
    "strip_html_lite",
    "unescape_js",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Cookie domain to ask OneBot for. NapCat / Lagrange return the whole
#: ``*.qq.com`` jar (uin / skey / p_skey / ...) when asked for this one.
QZONE_COOKIE_DOMAIN: str = "user.qzone.qq.com"

#: A desktop UA. QZone serves a different (mobile) flow to mobile UAs, and
#: the mobile flow needs a token this borrowed cookie jar does not carry.
DESKTOP_UA: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

#: Per-request timeouts (seconds). Publishing is quick; an image upload is
#: not, so it gets its own budget.
QZONE_TIMEOUT: int = 20
QZONE_UPLOAD_TIMEOUT: int = 60


class QZoneError(RuntimeError):
    """Any failure of the QZone primitives.

    Carries a wire-stable ``code`` so a caller — in particular an unattended
    cron job — can branch on the failure *kind* rather than string-matching a
    human message. The tool handlers fold this into ``tool_error(msg,
    code=...)``.
    """

    def __init__(self, message: str, code: str = "qzone_request_failed") -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Transport seam
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HttpResponse:
    """A minimal HTTP response: everything these CGIs actually need."""

    status: int
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


#: A transport takes ``(method, url, headers, body)`` and returns a
#: :class:`HttpResponse`. ``body`` is already form-encoded bytes for POST and
#: ``None`` for GET. Raising :class:`QZoneError` signals a transport-level
#: failure (unreachable / timed out) as opposed to an HTTP error status.
Transport = Callable[[str, str, Mapping[str, str], Optional[bytes], int], HttpResponse]


def default_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: Optional[bytes],
    timeout: int,
) -> HttpResponse:
    """The stdlib ``urllib`` transport used outside tests.

    ``urllib`` raises on a 4xx/5xx rather than returning it, so the error
    status is normalised back into a :class:`HttpResponse` and the response
    body is deliberately discarded — see the module docstring.
    """
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HttpResponse(status=getattr(response, "status", 200) or 200, body=response.read())
    except urllib.error.HTTPError as exc:
        # Drain and drop: an error page from Tencent's risk control must not
        # reach the model's context.
        try:
            exc.read()
        except Exception:  # noqa: BLE001 — best effort, the body is discarded anyway
            pass
        return HttpResponse(status=exc.code, body=b"")
    except urllib.error.URLError as exc:
        raise QZoneError(f"Cannot reach QZone: {exc.reason}", "qzone_request_failed") from exc
    except TimeoutError as exc:
        raise QZoneError("QZone request timed out.", "qzone_request_failed") from exc


def _resolve(transport: Optional[Transport]) -> Transport:
    return transport if transport is not None else default_transport


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


def _base_headers(cookie: str, referer_uin: str) -> Dict[str, str]:
    return {
        "Cookie": cookie,
        "Referer": f"https://user.qzone.qq.com/{referer_uin}",
        "User-Agent": DESKTOP_UA,
    }


def qzone_post(
    url: str,
    form: Mapping[str, str],
    cookie: str,
    referer_uin: str,
    timeout: int = QZONE_TIMEOUT,
    *,
    transport: Optional[Transport] = None,
) -> bytes:
    """POST a form-urlencoded body to a QZone CGI and return the raw body."""
    headers = _base_headers(cookie, referer_uin)
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    body = urllib.parse.urlencode(dict(form)).encode("utf-8")
    response = _resolve(transport)("POST", url, headers, body, timeout)
    if response.status >= 400:
        raise QZoneError(f"QZone HTTP {response.status}", "qzone_request_failed")
    return response.body


def qzone_get(
    url: str,
    params: Mapping[str, str],
    cookie: str,
    referer_uin: str,
    timeout: int = QZONE_TIMEOUT,
    *,
    transport: Optional[Transport] = None,
) -> str:
    """GET a QZone CGI with query params and return the decoded body text."""
    headers = _base_headers(cookie, referer_uin)
    headers["Accept"] = "*/*"
    headers["Accept-Language"] = "zh-CN,zh;q=0.9"
    full_url = f"{url}?{urllib.parse.urlencode(dict(params))}"
    response = _resolve(transport)("GET", full_url, headers, None, timeout)
    if response.status >= 400:
        raise QZoneError(f"QZone HTTP {response.status}", "qzone_read_failed")
    return response.text


# ---------------------------------------------------------------------------
# Auth — borrowed from the OneBot backend
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QZoneAuth:
    """Everything a QZone CGI call needs to be accepted."""

    uin: str
    cookie: str
    gtk: int
    skey: str = ""
    p_skey: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


def compute_gtk(p_skey: str) -> int:
    """Compute the QZone ``g_tk`` CSRF token from the ``p_skey`` cookie.

    The long-standing QZone DJB-style hash. Both source implementations
    carry this function byte-for-byte identically, and corlinman's copy is
    annotated "verified bit-for-bit against the live endpoint".

    Note it is ``p_skey``, not ``skey``: computing it from the wrong cookie
    yields a plausible-looking number that every CGI rejects.
    """
    h = 5381
    for ch in p_skey:
        h += (h << 5) + ord(ch)
    return h & 0x7FFFFFFF


def extract_cookie_value(cookie_str: str, key: str) -> Optional[str]:
    """Return the value of ``key`` from a ``k=v; k2=v2`` cookie string."""
    for part in cookie_str.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name == key:
            return value
    return None


def _login_uin(onebot_call: Callable[..., dict]) -> str:
    """Resolve the QQ number this session is logged in as.

    Prefers the ``self_id`` the running platform adapter has *observed* on a
    live event (disagreement S11: the event's own ``self_id`` is
    authoritative and survives a NapCat account switch without a config
    change), and falls back to asking the backend directly. The fallback is
    what makes these tools work from ``hermes cron`` or the CLI, where no
    gateway is running.
    """
    try:
        from plugins.platforms.onebot.client import get_live_client

        client, _loop = get_live_client()
        observed = getattr(client, "last_self_id", None) if client is not None else None
        if isinstance(observed, int) and not isinstance(observed, bool) and observed > 0:
            return str(observed)
    except Exception as exc:  # noqa: BLE001 — the adapter is optional here
        logger.debug("qzone: no live adapter self_id (%s); asking OneBot", exc)

    data = onebot_call("get_login_info")
    uin = data.get("user_id")
    if not uin:
        raise QZoneError(
            "OneBot get_login_info returned no user_id.", "onebot_failed"
        )
    return str(uin)


def _cookie_string(onebot_call: Callable[..., dict]) -> str:
    data = onebot_call("get_cookies", {"domain": QZONE_COOKIE_DOMAIN})
    cookies = (data.get("cookies") or "").strip()
    if not cookies:
        raise QZoneError(
            "OneBot get_cookies returned an empty cookie string — the QQ login "
            "state may be stale; re-login the NapCat/Lagrange client.",
            "qzone_cookie_stale",
        )
    return cookies


def qzone_auth(onebot_call: Optional[Callable[..., dict]] = None) -> QZoneAuth:
    """Borrow the QQ login state and derive everything QZone needs.

    Raises :class:`QZoneError` with ``onebot_unavailable`` / ``onebot_failed``
    when the backend cannot be reached or is not logged in, and
    ``qzone_cookie_stale`` when the jar came back without ``p_skey`` — which
    is what a lapsed login looks like from here, and the single most common
    real-world failure of these tools.
    """
    if onebot_call is None:
        from tools.onebot_client import onebot_call as _call

        onebot_call = _call

    try:
        uin = _login_uin(onebot_call)
        cookie = _cookie_string(onebot_call)
    except QZoneError:
        raise
    except Exception as exc:  # noqa: BLE001 — one clean message for the model
        raise QZoneError(
            f"Could not borrow QQ login state from OneBot: {exc}", "onebot_unavailable"
        ) from exc

    p_skey = extract_cookie_value(cookie, "p_skey")
    if not p_skey:
        raise QZoneError(
            "p_skey not found in OneBot cookies — the QQ login may be stale, or "
            "NapCat has not fetched the QZone cookie jar yet.",
            "qzone_cookie_stale",
        )
    # skey is optional: several NapCat builds omit it and the CGIs accept "".
    skey = extract_cookie_value(cookie, "skey") or ""
    return QZoneAuth(uin=uin, cookie=cookie, gtk=compute_gtk(p_skey), skey=skey, p_skey=p_skey)


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _as_text(raw: Any) -> str:
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw).decode("utf-8", errors="replace").strip()
    return (raw or "").strip()


def extract_json_object(raw: Any) -> Optional[Dict[str, Any]]:
    """Locate and parse the first ``{...}`` object in a JSONP-ish body.

    QZone wraps payloads in shims (``_Callback({...})``,
    ``frameElement.callback({...})``). Greedy match, because the payload is
    the outermost object in these responses.
    """
    text = _as_text(raw)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def parse_callback_json(body: str) -> Optional[Dict[str, Any]]:
    """Extract the JSON inside a ``frameElement.callback({...})`` shim.

    The comment CGI's response is a full ``<script>`` block whose wrapper
    contains a ``try{...}`` — a naive ``{.*}`` search matches *that* instead
    of the payload, so this anchors on the ``callback(`` token. Learned in
    the source implementation; do not "simplify" it back to
    :func:`extract_json_object`.
    """
    m = re.search(r"callback\(\s*(\{.*\})\s*\)\s*;?\s*</script>", body, re.DOTALL)
    if not m:
        m = re.search(r"callback\(\s*(\{.*?\})\s*\)", body, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


# feeds3 embeds rendered HTML as a JS string literal carrying JS escapes:
# ``\xNN`` / ``\uNNNN`` byte escapes AND the simple two-char escapes ``\/``
# ``\"`` ``\t`` ``\n`` ``\\`` etc. Tags arrive as ``<\/div>`` — decoding only
# the \xNN form leaves every closing tag mangled, so every downstream regex
# misses and the parser returns an empty list with no error anywhere. Decode
# the lot in one pass.
_JS_ESCAPE_RE = re.compile(r"""\\(x[0-9A-Fa-f]{2}|u[0-9A-Fa-f]{4}|[/"'\\tnrbf0])""")

_SIMPLE_JS_ESCAPES = {
    "/": "/",
    '"': '"',
    "'": "'",
    "\\": "\\",
    "t": "\t",
    "n": "\n",
    "r": "\r",
    "b": "",
    "f": "",
    "0": "",
}


def unescape_js(s: str) -> str:
    """Decode the JS string escapes in a feeds3 ``html:'…'`` payload."""

    def _repl(m: "re.Match[str]") -> str:
        g = m.group(1)
        if g[0] in ("x", "u"):
            return chr(int(g[1:], 16))
        return _SIMPLE_JS_ESCAPES.get(g, g)

    return _JS_ESCAPE_RE.sub(_repl, s)


def strip_html_lite(text: str) -> str:
    """Reduce QZone inline HTML to readable plain text (lossy, on purpose)."""
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return _html.unescape(text).strip()


def coerce_uin(value: Any) -> Tuple[str, bool]:
    """Normalise a QQ number argument to ``(text, is_valid)``."""
    text = str(value or "").strip()
    return text, bool(text) and text.isdigit()
