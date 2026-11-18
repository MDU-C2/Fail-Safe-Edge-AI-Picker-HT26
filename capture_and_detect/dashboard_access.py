"""Exact-origin, paired access for a dashboard on a trusted local network.

This does not replace the dashboard's command CSRF check. Call ``check`` first,
then retain that check for every write except the origin-checked pairing form.
No browser cookie or pairing code is sent to an AI provider.
"""

from collections import OrderedDict, deque
import hashlib
import hmac
import html
import ipaddress
import os
from pathlib import Path
import re
import secrets
import ssl
import stat
import threading
import time
from urllib.parse import parse_qs, urlsplit


COOKIE_NAME = "cup_dashboard_pair"
LAN_COOKIE_NAME = "cup_dashboard_lan_pair"
SESSION_SECONDS = 12 * 60 * 60
_PAIR_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def is_loopback(value):
    """A network address, rather than an arbitrary DNS name, is trusted here."""
    if value == "localhost":
        return True
    try:
        address = ipaddress.ip_address(value)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            return address.ipv4_mapped.is_loopback
        return address.is_loopback
    except ValueError:
        return False


def is_private_lan(value):
    """Accept a numeric private LAN address, never a DNS name or wildcard."""
    try:
        address = ipaddress.ip_address(value)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return (address.is_private and not address.is_loopback and not address.is_unspecified
                and not address.is_multicast and not address.is_reserved and "%" not in value)
    except ValueError:
        return False


def validate_origin(origin):
    parsed = urlsplit(origin)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("Dashboard origin has an invalid port.") from error
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment
            or any(character.isspace() for character in origin)
            or origin != f"{parsed.scheme}://{parsed.netloc}"
            or "*" in parsed.netloc or "\\" in parsed.netloc
            or (port is not None and not 1 <= port <= 65535)):
        raise ValueError("Dashboard origin must be one exact http(s) origin without a path or credentials.")
    if parsed.netloc != parsed.netloc.lower():
        raise ValueError("Use a lowercase dashboard host name.")
    return origin


def load_pairing_code(path):
    """Create a private, human-enterable code, or read an existing private file."""
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise ValueError("Pairing code must be an owner-only regular file (permissions 600).")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "r", encoding="ascii") as source:
            code = source.read(129).strip()
        normalized = code.replace("-", "")
        if (len(normalized) < 12 or len(normalized) > 64
                or any(character not in _PAIR_ALPHABET for character in normalized)):
            raise ValueError("Pairing code file is invalid; move it aside and restart to create a new code.")
        return code
    code = "-".join("".join(secrets.choice(_PAIR_ALPHABET) for _ in range(4)) for _ in range(3))
    with os.fdopen(descriptor, "w", encoding="ascii") as target:
        target.write(code + "\n")
    return code


class DashboardAccess:
    """Small access guard with bounded pairing attempts and expiring sessions."""

    def __init__(self, allowed_origins, *, pairing_code_file=None, certificate_file=None,
                 allow_lan_http=False, clock=time.monotonic):
        self.origins = frozenset(validate_origin(origin) for origin in allowed_origins)
        if not self.origins:
            raise ValueError("At least one dashboard origin is required.")
        if type(allow_lan_http) is not bool:
            raise ValueError("LAN HTTP access must be explicitly enabled or disabled.")
        self.allow_lan_http = allow_lan_http
        if allow_lan_http:
            for origin in self.origins:
                parsed = urlsplit(origin)
                if parsed.scheme == "http" and not (is_loopback(parsed.hostname) or is_private_lan(parsed.hostname)):
                    raise ValueError("LAN HTTP origins must use the Mac's numeric private Wi-Fi address.")
        self.hosts = frozenset(urlsplit(origin).netloc for origin in self.origins)
        self.pairing_code_file = Path(pairing_code_file).expanduser() if pairing_code_file else None
        self.pairing_code = load_pairing_code(self.pairing_code_file) if self.pairing_code_file else None
        self.certificate_file = Path(certificate_file).expanduser().resolve() if certificate_file else None
        self._certificate_data = None
        if self.certificate_file:
            # A mistaken key path must never be exposed as the public CA download.
            data = self.certificate_file.read_bytes()
            if len(data) > 32768 or not re.fullmatch(
                    rb"\s*-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----\s*", data):
                raise ValueError("The phone certificate download must contain only a public PEM certificate.")
            try:
                ssl.PEM_cert_to_DER_cert(data.decode("ascii").strip())
            except (ValueError, UnicodeError) as error:
                raise ValueError("The phone certificate download must contain a valid public PEM certificate.") from error
            # Serve the validated bytes, even if someone later replaces the file.
            self._certificate_data = data
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions = OrderedDict()
        self._attempts = OrderedDict()
        self._global_attempts = deque(maxlen=60)

    def _local(self, request):
        host = urlsplit("//" + request.headers.get("host", "")).hostname or ""
        client = request.client.host if request.client else ""
        return is_loopback(host) and is_loopback(client)

    def is_local_request(self, request):
        """Use for API-key entry and displaying the phone pairing code on Mac."""
        return self._local(request)

    def _lan_http(self, request):
        host = request.headers.get("host", "").lower()
        hostname = urlsplit("//" + host).hostname or ""
        peer = request.client.host if request.client else ""
        return (self.allow_lan_http and request.url.scheme == "http"
                and "http://" + host in self.origins and is_private_lan(hostname)
                and (is_private_lan(peer) or is_loopback(peer)))

    def _valid_origin(self, request):
        origin = request.headers.get("origin")
        return (origin in self.origins and urlsplit(origin).netloc == request.headers.get("host", "").lower()
                and urlsplit(origin).scheme == request.url.scheme)

    @staticmethod
    def _cookie_name(request):
        # HTTP must not try to overwrite a Secure cookie left by the earlier
        # HTTPS dashboard: browsers scope cookies by host, not by port.
        return COOKIE_NAME if request.url.scheme == "https" else LAN_COOKIE_NAME

    def _authenticated(self, request):
        token = request.cookies.get(self._cookie_name(request), "")
        if not token or len(token) > 128:
            return False
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        with self._lock:
            self._expire_sessions()
            return digest in self._sessions

    def _expire_sessions(self):
        now = self._clock()
        expired = [key for key, deadline in self._sessions.items() if deadline <= now]
        for key in expired:
            self._sessions.pop(key, None)

    @staticmethod
    def csrf_exempt(request):
        return request.method == "POST" and request.url.path == "/pair"

    def check(self, request):
        """Return a denial/redirect response, or None when middleware may proceed."""
        from fastapi.responses import JSONResponse, RedirectResponse

        host = request.headers.get("host", "").lower()
        if host not in self.hosts:
            return JSONResponse({"detail": "Use the dashboard address printed in the terminal."}, status_code=403)
        origin = request.headers.get("origin")
        if ((origin is not None and not self._valid_origin(request))
                or (request.method not in ("GET", "HEAD") and not self._valid_origin(request))):
            return JSONResponse({"detail": "Cross-origin requests are not allowed."}, status_code=403)
        if self._local(request):
            return None
        if request.url.scheme != "https" and not self._lan_http(request):
            return JSONResponse({"detail": "Use the configured phone address on the same private Wi-Fi network."}, status_code=403)
        if not self.pairing_code:
            return JSONResponse({"detail": "Phone access is not enabled. Start the dashboard with a pairing code file."}, status_code=403)
        if request.url.path == "/pair" and request.method in ("GET", "HEAD", "POST"):
            return None
        if self.certificate_file and request.url.path == "/phone-ca.crt" and request.method in ("GET", "HEAD"):
            return None
        if self._authenticated(request):
            return None
        if request.method in ("GET", "HEAD") and not request.url.path.startswith("/api/"):
            return RedirectResponse("/pair", status_code=303)
        return JSONResponse({"detail": "Pair this phone with the dashboard before continuing.", "pairing_required": True}, status_code=401)

    def _allow_attempt(self, address):
        now = self._clock()
        with self._lock:
            while self._global_attempts and self._global_attempts[0] <= now - 60:
                self._global_attempts.popleft()
            attempts = self._attempts.setdefault(address, deque(maxlen=5))
            self._attempts.move_to_end(address)
            while len(self._attempts) > 256:
                self._attempts.popitem(last=False)
            while attempts and attempts[0] <= now - 60:
                attempts.popleft()
            if len(attempts) >= 5 or len(self._global_attempts) >= 60:
                return False
            attempts.append(now)
            self._global_attempts.append(now)
        return True

    def install_routes(self, app):
        from fastapi import Request
        from fastapi.responses import HTMLResponse, RedirectResponse, Response

        @app.get("/pair")
        def pairing_page():
            return HTMLResponse(_pair_page())

        @app.post("/pair")
        async def pair(request: Request):
            # Keep this check inside the route too, so an integration mistake
            # cannot turn a third-party form submission into a paired session.
            if not self._valid_origin(request):
                return HTMLResponse(_pair_page("Open the pairing page again before submitting."), status_code=403)
            address = request.client.host if request.client else "unknown"
            if not self._allow_attempt(address):
                return HTMLResponse(_pair_page("Too many attempts. Wait one minute and try again."), status_code=429,
                                    headers={"Retry-After": "60"})
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 512:
                    return HTMLResponse(_pair_page("Pairing request is too large."), status_code=413)
            if request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
                return HTMLResponse(_pair_page("Use the pairing form below."), status_code=415)
            try:
                values = parse_qs(raw.decode("ascii"), strict_parsing=True, max_num_fields=2)
                supplied = values["code"]
                if len(supplied) != 1 or set(values) != {"code"}:
                    raise ValueError("Invalid pairing form")
                normalized = supplied[0].strip().upper().replace("-", "").replace(" ", "")
                expected = (self.pairing_code or "").replace("-", "")
                matched = bool(expected) and hmac.compare_digest(normalized.encode("utf-8"), expected.encode("ascii"))
            except (ValueError, KeyError, UnicodeError):
                matched = False
            if not matched:
                return HTMLResponse(_pair_page("That pairing code did not match. Check the code shown on your Mac."), status_code=403)
            token = secrets.token_urlsafe(32)
            with self._lock:
                self._expire_sessions()
                self._sessions[hashlib.sha256(token.encode("ascii")).digest()] = self._clock() + SESSION_SECONDS
                while len(self._sessions) > 128:
                    self._sessions.popitem(last=False)
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(self._cookie_name(request), token, max_age=SESSION_SECONDS,
                                secure=request.url.scheme == "https", httponly=True, samesite="strict", path="/")
            return response

        if self.certificate_file:
            @app.get("/phone-ca.crt")
            def public_certificate():
                return Response(self._certificate_data, media_type="application/x-x509-ca-cert",
                                headers={"Content-Disposition": 'attachment; filename="cup-dashboard-local-ca.crt"'})


def _pair_page(message=""):
    return """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Pair your phone · Cup dashboard</title>
<style>body{margin:0;background:#f4f6f8;color:#15232b;font:17px system-ui,sans-serif;display:grid;min-height:100dvh;place-items:center}main{box-sizing:border-box;width:min(90vw,430px);background:white;padding:28px;border-radius:20px;box-shadow:0 12px 40px #20334018}h1{font-size:26px;margin:0 0 12px}p{line-height:1.5;color:#4a5b65}label{display:block;font-weight:650;margin:24px 0 8px}input,button{box-sizing:border-box;width:100%;font:inherit;padding:14px;border-radius:10px}input{border:1px solid #aab8c2;text-transform:uppercase;letter-spacing:.08em}button{margin-top:14px;background:#17645b;color:white;border:0;font-weight:650}.notice{color:#984521;font-size:15px}</style>
</head><body><main><h1>Pair your phone</h1><p>On your Mac, open Settings in the dashboard to find the phone pairing code. Enter it below to use this browser for 12 hours.</p>""" + (f'<p class="notice" role="alert">{html.escape(message)}</p>' if message else "") + """
<form method="post" action="/pair"><label for="code">Pairing code</label><input id="code" name="code" maxlength="80" autocomplete="off" autocapitalize="characters" spellcheck="false" required placeholder="ABCD-EFGH-JKLM"><button type="submit">Open dashboard</button></form>
<p>Keep your phone and Mac on the same trusted Wi-Fi network.</p></main></body></html>"""
