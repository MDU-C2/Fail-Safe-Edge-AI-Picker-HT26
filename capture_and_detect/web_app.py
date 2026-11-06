"""Local cup dashboard. Defaults to hardware-free demo; use --gateway for camera mode."""

import argparse
import asyncio
from contextlib import asynccontextmanager
import hmac
import json
import math
from pathlib import Path
import re
import tempfile
import time
import uuid

from web_controller import WebController, WebError

HERE = Path(__file__).resolve().parent


def create_app(controller, *, port=8080, image_root=None):
    # Optional web dependencies are never needed by the original terminal app.
    from fastapi import FastAPI, Request
    from fastapi.responses import FileResponse, JSONResponse

    root = Path(image_root).resolve() if image_root is not None else None
    hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}

    @asynccontextmanager
    async def lifespan(app):
        controller.start()
        try:
            yield
        finally:
            await asyncio.to_thread(controller.close)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = request.headers.get("host", "").lower()
        if host not in hosts:
            return JSONResponse({"detail": "Use the local dashboard address printed in the terminal."}, status_code=403)
        origin = request.headers.get("origin")
        if origin is not None and origin != f"http://{host}":
            return JSONResponse({"detail": "Cross-origin requests are not allowed."}, status_code=403)
        if request.method not in ("GET", "HEAD"):
            token = request.headers.get("x-csrf-token", "")
            if origin != f"http://{host}" or not hmac.compare_digest(token.encode("utf-8"), controller.state()["csrf_token"].encode("utf-8")):
                return JSONResponse({"detail": "Reload the local dashboard before sending a command."}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        return response

    @app.get("/")
    def index():
        return FileResponse(HERE / "web_static" / "index.html")

    @app.get("/static/{asset}")
    def asset(asset: str):
        if asset not in ("app.js", "app.css"):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        return FileResponse(HERE / "web_static" / asset)

    @app.get("/api/state")
    def state():
        return controller.state()

    @app.get("/api/image/{capture_id}")
    def image(capture_id: str):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,150}", capture_id):
            return JSONResponse({"detail": "Unknown scan"}, status_code=404)
        registered = controller.image(capture_id)
        if registered and root is not None:
            candidate = Path(registered).resolve()
            if candidate.is_relative_to(root) and candidate.is_file() and candidate.suffix.lower() in (".jpg", ".jpeg", ".png", ".svg"):
                return FileResponse(candidate)
        return JSONResponse({"detail": "This scan image is no longer available. Scan again."}, status_code=404)

    @app.post("/api/{operation}")
    async def command(operation: str, request: Request):
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            return JSONResponse({"detail": "Send a JSON request."}, status_code=415)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 8192:
                return JSONResponse({"detail": "Request is too large."}, status_code=413)
        try:
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("Duplicate field")
                    result[key] = value
                return result
            payload = json.loads(raw, object_pairs_hook=unique)
        except (ValueError, UnicodeDecodeError, RecursionError):
            return JSONResponse({"detail": "Invalid JSON request."}, status_code=422)
        try:
            return JSONResponse(controller.submit(operation, payload), status_code=202)
        except WebError as error:
            return JSONResponse({"detail": str(error)}, status_code=error.status)

    return app


class NoLanguage:
    def interpret(self, text, scene):
        raise ValueError("Language is disabled. Use the cup selection buttons.")


class DemoLanguage:
    """A visibly labelled, deterministic UI demonstration. Never calls a model."""
    def interpret(self, text, scene):
        lower = text.strip().lower().rstrip("?.!")
        action, selector, cup_id = "clarify", "none", None
        message = "Demo understands ‘pick cup two’, ‘hover above cup one’, and ‘which cups are available?’."
        if lower in ("scan the table", "scan", "find cups"):
            action = "scan"
        elif lower in ("which cups are available", "cups available", "what cups are available", "list cups"):
            action = "list"
        else:
            match = re.fullmatch(r"(pick|transfer|hover above|hover) (?:cup )?(one|two|three|[0-9]+)", lower)
            edge = re.fullmatch(r"(pick|hover above) the (leftmost|rightmost) cup", lower)
            if match:
                number = match[2]
                cup_id = {"one": 1, "two": 2, "three": 3}.get(number)
                if cup_id is None:
                    cup_id = int(number)
                action = "hover" if match[1].startswith("hover") else "goto"
                selector = "id"
            elif edge:
                action = "goto" if edge[1] == "pick" else "hover"
                selector = edge[2]
        return {"action": action, "selector": selector, "cup_id": cup_id, "message": message}


def demo_factory(directory):
    """Use real selection/planning checks and a dry-run robot with synthetic cups."""
    def factory(controller):
        from cup_commands import CupSession
        from cup_inventory import build_inventory
        from language_commands import LanguageController
        from robot_client import RobotClient

        directory.mkdir(parents=True, exist_ok=True)
        image_path = directory / "demo-table.svg"
        image_path.write_text(DEMO_IMAGE, encoding="utf-8")
        sequence = 0
        def capture(purpose):
            nonlocal sequence
            sequence += 1
            controller.event("Generating a sample scan…" if purpose == "scan" else "Checking the sample cup again…")
            objects = []
            for index, (u, v, x, y, color) in enumerate(((240, 310, 580, 100, "teal"),
                                                       (500, 350, 620, 220, "white"),
                                                       (770, 270, 550, 300, "orange")), 1):
                for kind in (["CUP", "CUP BASE"] if index < 3 else ["CUP"]):
                    objects.append({"class": kind, "detection_id": f"{index}-{kind}",
                                    "confidence": 0.94 if index < 3 else 0.68,
                                    "x_px": u, "y_px": v, "robot_x_mm": x,
                                    "robot_y_mm": y, "robot_z_mm": 45,
                                    "bbox_xyxy": [u - 65, v - 105, u + 65, v + 45]})
            result = build_inventory(objects, capture_id=f"demo_{sequence}_{purpose}",
                       frame={"sequence": sequence}, calibration_id="demo-calibration", gateway="demo",
                       images={"annotated": str(image_path)}, model={"demo": True}, tag_offset=(-115, 0, 40))
            return result, time.monotonic()
        session = CupSession(capture, lambda inventory: controller.publish_session(),
                             RobotClient("http://127.0.0.1:1", {}, dry_run=True),
                             "demo-calibration", tag_offset=(-115, 0, 40))
        language = LanguageController(session, DemoLanguage(), audit_dir=directory / "language")
        return session, language
    return factory


def hardware_factory(args):
    def factory(controller):
        # Deferred imports keep demo and tests independent of YOLO/OpenCV.
        from capture_and_detect_custom import (load_token, Calibration, load_model, Detector,
                                               CaptureStore, CaptureService)
        from camera_client import CameraClient
        from cup_commands import CupSession
        from language_commands import LanguageController
        from local_llm import OllamaIntentClient
        from recovery_journal import RecoveryJournal
        from robot_client import RobotClient

        controller.event("Loading calibration and detection model…")
        headers = {"Authorization": f"Bearer {load_token()}"}
        api = args.gateway.rstrip("/") + "/api/v1"
        camera = CameraClient(api, headers)
        calibration = Calibration(args.calibration)
        model, weights = load_model()
        controller.event("Reading camera configuration from the Pi…")
        detector = Detector(model, calibration, camera.intrinsics(), args.conf)
        store = CaptureStore(args.output)
        captures = CaptureService(camera, detector, store, weights)
        recovery = RecoveryJournal(store.folder / "robot_recovery.json", legacy_inventory_path=store.folder / "cups.json")
        def capture(purpose):
            controller.event("Capturing and detecting cups…" if purpose == "scan" else "Rechecking the selected cup with a fresh camera observation…")
            return captures.capture(purpose)
        def save(inventory):
            store.save(inventory)
            controller.publish_session()
        session = CupSession(capture, save, RobotClient(api, headers, dry_run=not args.live),
                     calibration.digest, tag_offset=calibration.offset, speed=args.speed,
                     max_result_age=args.max_result_age, match_mm=args.match_mm, match_px=args.match_px,
                     recovery=recovery)
        interpreter = OllamaIntentClient(model=args.llm_model, url=args.ollama_url) if args.llm else NoLanguage()
        return session, LanguageController(session, interpreter, audit_dir=store.folder / "language")
    return factory


class ProcessLock:
    """Prevent two local web services controlling the same saved session."""
    def __init__(self, directory):
        self.path = Path(directory) / ".cup-web.lock"
        self.file = None

    def __enter__(self):
        import fcntl  # This local app targets the project's Mac/Linux hosts.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a")
        try:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise SystemExit("Another cup web app already owns this output folder. Close it before starting another.")
        return self

    def __exit__(self, *args):
        if self.file is not None:
            self.file.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--demo", action="store_true", help="Sample cups only; no camera, model or robot connection (default without --gateway)")
    parser.add_argument("--gateway", help="Pi gateway base URL; uses camera-connected dry-run unless --live is given")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--live", action="store_true", help="Enable physical robot execution after explicit confirmation")
    modes.add_argument("--dry-run", action="store_true", help="Preview only; the default in camera mode")
    parser.add_argument("--llm", action="store_true", help="Enable typed natural language through local Ollama")
    parser.add_argument("--llm-model", default="qwen3:4b")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--output", default=str(HERE / "Data" / "Detections"))
    parser.add_argument("--calibration", default=str(HERE.parent / "calibration" / "calibration.json"))
    parser.add_argument("--conf", type=float, default=0.5)
    parser.add_argument("--speed", type=float, default=50)
    parser.add_argument("--max-result-age", type=float, default=15)
    parser.add_argument("--match-mm", type=float, default=20)
    parser.add_argument("--match-px", type=float, default=40)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535")
    if args.demo and (args.gateway or args.live):
        parser.error("--demo cannot be combined with --gateway or --live")
    if args.live and not args.gateway:
        parser.error("--live requires --gateway")
    for name in ("conf", "speed", "max_result_age", "match_mm", "match_px"):
        if not math.isfinite(getattr(args, name)) or getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive and finite")
    if args.conf > 1 or not 1 <= args.speed <= 1500:
        parser.error("Confidence must be at most 1; speed must be between 1 and 1500 mm/s")
    try:
        import uvicorn
        import fastapi
    except ImportError:
        parser.exit(1, "Install the web dependencies: python3 -m pip install -r requirements-web.txt\n")
    mode = "demo" if not args.gateway else ("live" if args.live else "dry_run")
    temporary = tempfile.TemporaryDirectory(prefix="cup-dashboard-") if mode == "demo" else None
    directory = Path(temporary.name if temporary else args.output).resolve()
    try:
        factory = demo_factory(directory) if mode == "demo" else hardware_factory(args)
        controller = WebController(factory, mode=mode, model="Scripted demo" if mode == "demo" else args.llm_model,
                                   llm_enabled=mode == "demo" or args.llm)
        app = create_app(controller, port=args.port, image_root=directory)
        with ProcessLock(directory):
            print(f"\nCup dashboard: http://localhost:{args.port}", flush=True)
            print(f"Mode: {mode}. Close the terminal cup controller before using this app.", flush=True)
            print("Closing this server does not stop a movement already dispatched to the robot.\n", flush=True)
            uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1, access_log=False)
    finally:
        if temporary:
            temporary.cleanup()
    return 0


DEMO_IMAGE = '''<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="620" viewBox="0 0 1000 620">
<defs><pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse"><path d="M40 0H0V40" fill="none" stroke="#c9d3cc" stroke-width=".6"/></pattern>
<linearGradient id="table" x2="1" y2="1"><stop stop-color="#e4e9df"/><stop offset="1" stop-color="#d3ddd5"/></linearGradient>
<linearGradient id="cup"><stop stop-color="#e9eee6"/><stop offset=".5" stop-color="#fcfdf8"/><stop offset="1" stop-color="#c2cfc7"/></linearGradient>
<filter id="shadow"><feGaussianBlur stdDeviation="8"/></filter></defs>
<path fill="url(#table)" d="M0 0h1000v620H0z"/><path fill="url(#grid)" d="M0 0h1000v620H0z"/>
<path d="M80 70h840v480H80z" fill="none" stroke="#e7a956" stroke-width="3" stroke-dasharray="15 9"/>
<g font-family="Arial,sans-serif" font-size="12" fill="#657c70"><text x="92" y="55">SAMPLE WORKSPACE</text><text x="660" y="580">ILLUSTRATION · NO CAMERA CONNECTED</text></g>
<g transform="translate(240 310)"><ellipse cy="45" rx="85" ry="25" fill="#567364" opacity=".2" filter="url(#shadow)"/><path d="M48-55c74-18 76 78 4 68" fill="none" stroke="#498f82" stroke-width="18"/><path d="M-55-75h110l-8 111q-46 28-94 0z" fill="#498f82"/><ellipse cy="-75" rx="55" ry="19" fill="#a1c6b5"/><ellipse cy="-75" rx="44" ry="12" fill="#29685e"/><path d="M-65-105h130V45H-65z" fill="none" stroke="#217b64" stroke-width="2"/><rect x="-65" y="-130" width="74" height="25" rx="4" fill="#217b64"/><text x="-54" y="-112" fill="white" font-family="Arial" font-size="13">CUP 01</text></g>
<g transform="translate(500 350)"><ellipse cy="45" rx="85" ry="25" fill="#567364" opacity=".2" filter="url(#shadow)"/><path d="M48-55c74-18 76 78 4 68" fill="none" stroke="#e8eee7" stroke-width="18"/><path d="M-55-75h110l-8 111q-46 28-94 0z" fill="url(#cup)"/><ellipse cy="-75" rx="55" ry="19" fill="#fafcf5"/><ellipse cy="-75" rx="44" ry="12" fill="#b4c4b6"/><path d="M-65-105h130V45H-65z" fill="none" stroke="#217b64" stroke-width="2"/><rect x="-65" y="-130" width="74" height="25" rx="4" fill="#217b64"/><text x="-54" y="-112" fill="white" font-family="Arial" font-size="13">CUP 02</text></g>
<g transform="translate(770 270)"><ellipse cy="45" rx="85" ry="25" fill="#567364" opacity=".2" filter="url(#shadow)"/><path d="M48-55c74-18 76 78 4 68" fill="none" stroke="#c98352" stroke-width="18"/><path d="M-55-75h110l-8 111q-46 28-94 0z" fill="#c98352"/><ellipse cy="-75" rx="55" ry="19" fill="#ebc7a2"/><ellipse cy="-75" rx="44" ry="12" fill="#995c36"/><path d="M-65-105h130V45H-65z" fill="none" stroke="#ac692c" stroke-width="2" stroke-dasharray="6 4"/><rect x="-65" y="-130" width="130" height="25" rx="4" fill="#ac692c"/><text x="-54" y="-112" fill="white" font-family="Arial" font-size="13">CUP 03 · BLOCKED</text></g></svg>'''


if __name__ == "__main__":
    raise SystemExit(main())
