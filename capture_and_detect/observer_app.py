"""Shared observer routes and independent, loopback-only Windows/Mac viewer."""

import argparse
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from gateway_observer import DemoObserver, GatewayObserver, diagnostics_token, unavailable_snapshot

STATIC = Path(__file__).resolve().parent / "web_static"


def observer_page():
    # One component serves the main dashboard tab and standalone viewer.
    source = (STATIC / "index.html").read_text(encoding="utf-8")
    panel = source.split("<!-- OBSERVER START -->", 1)[1].split("<!-- OBSERVER END -->", 1)[0]
    panel = panel.replace('tabindex="0" hidden>', 'tabindex="0">', 1).replace('role="tabpanel"', 'role="region"', 1)
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Observer · Picker Lab</title><link rel="stylesheet" href="/static/observer.css">'
            '<script src="/static/observer.js" defer></script></head>'
            '<body class="observer-standalone"><div class="observer-shell">'
            '<header class="observer-brand"><span>PICKER / LAB</span><span>GATEWAY OBSERVER</span></header>'
            '<main>' + panel + '</main></div></body></html>')


def install_observer_routes(app, monitor):
    from fastapi.responses import HTMLResponse

    @app.get("/api/observer")
    def observer_snapshot():
        return monitor.snapshot() if monitor is not None else unavailable_snapshot()

    @app.get("/observer", response_class=HTMLResponse)
    def focused_observer():
        return observer_page()


def create_observer_app(monitor, port=8088):
    from fastapi import FastAPI, Request
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

    @asynccontextmanager
    async def lifespan(app):
        monitor.start()
        try:
            yield
        finally:
            await asyncio.to_thread(monitor.close)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    @app.middleware("http")
    async def readonly_local(request: Request, call_next):
        host = request.headers.get("host", "").lower()
        if host not in hosts or request.client is None or request.client.host not in ("127.0.0.1", "::1"):
            return JSONResponse({"detail": "Open this observer on localhost."}, status_code=403)
        if request.headers.get("origin") not in (None, "http://" + host):
            return JSONResponse({"detail": "Cross-origin requests are not allowed."}, status_code=403)
        if request.method not in ("GET", "HEAD"):
            return JSONResponse({"detail": "This observer is read-only."}, status_code=405)
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer",
                                 "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"})
        return response

    install_observer_routes(app, monitor)

    @app.get("/", response_class=HTMLResponse)
    def index():
        return observer_page()

    @app.get("/static/{asset}")
    def static_asset(asset: str):
        if asset not in ("observer.js", "observer.css"):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        return FileResponse(STATIC / asset)

    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description="Open a read-only gateway observer. No picker or robot controller is started.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", help="Gateway address, e.g. http://192.168.125.20:8000")
    source.add_argument("--demo", action="store_true", help="Show explicitly simulated readings without a gateway")
    parser.add_argument("--token-file", type=Path, help="Diagnostics token file; otherwise GATEWAY_DIAG_TOKEN / client_tokens.json")
    parser.add_argument("--port", type=int, default=8088)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("Port must be between 1024 and 65535.")
    try:
        monitor = DemoObserver() if args.demo else GatewayObserver(args.url, diagnostics_token(args.token_file))
    except (OSError, ValueError):
        parser.error("Cannot configure the observer. Check the gateway URL and diagnostics token file.")
    import uvicorn
    print(f"Observer: http://127.0.0.1:{args.port}", flush=True)
    print("Read-only monitoring. No picker, model or control lease is started.", flush=True)
    uvicorn.run(create_observer_app(monitor, args.port), host="127.0.0.1", port=args.port,
                access_log=False, proxy_headers=False)


if __name__ == "__main__":
    main()
