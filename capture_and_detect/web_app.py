"""Local cup dashboard. Defaults to hardware-free demo; use --gateway for camera mode."""

import argparse
import asyncio
from contextlib import asynccontextmanager
import hmac
import json
import math
from pathlib import Path
import platform
import re
import tempfile
import time
import uuid

from web_controller import WebController, WebError
from handover_plan import DEFAULT_SPEED, HOME_SPEED

HERE = Path(__file__).resolve().parent


def create_app(controller, *, port=8080, image_root=None, allowed_origin=None,
               assistant=None, assistant_settings=None, access=None, tls=False, extra_origins=(),
               device_monitor=None):
    # Optional web dependencies are never needed by terminal mode.
    from fastapi import FastAPI, Request
    from fastapi.responses import FileResponse, JSONResponse, Response

    root = Path(image_root).resolve() if image_root is not None else None
    hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}
    origins = {f"{'https' if tls else 'http'}://{host}" for host in hosts}
    for origin_value in ([allowed_origin] if allowed_origin is not None else []) + list(extra_origins):
        from urllib.parse import urlsplit
        parsed = urlsplit(origin_value)
        if (parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username
                or parsed.password or parsed.path or parsed.query or parsed.fragment):
            raise ValueError("Dashboard origin must be an exact http(s) origin without a path or credentials.")
        hosts.add(parsed.netloc.lower())
        origins.add(origin_value)

    from robot_assistant import RobotAssistant
    from assistant_api import AssistantError, AssistantSettings
    assistant = assistant if assistant is not None else RobotAssistant(
        controller, image_root=root, settings=AssistantSettings(assistant_settings))
    # Direct and in-process managed Mac pickers share the same client. The
    # credentials never cross a remote device-service boundary.
    if (getattr(getattr(controller, "factory", None), "astra_named_matching", False) is True
            or getattr(controller, "astra_named_matching", False) is True):
        controller.configure_astra(assistant.client, assistant.settings)

    @asynccontextmanager
    async def lifespan(app):
        try:
            if device_monitor is not None:
                device_monitor.start()
            controller.start()
            yield
        finally:
            try:
                await asyncio.to_thread(controller.close)
            finally:
                if device_monitor is not None:
                    await asyncio.to_thread(device_monitor.close)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if access is not None:
            rejected = access.check(request)
            if rejected is not None:
                return rejected
        host = request.headers.get("host", "").lower()
        if host not in hosts:
            return JSONResponse({"detail": "Use the local dashboard address printed in the terminal."}, status_code=403)
        origin = request.headers.get("origin")
        valid_origin = origin in origins and origin.rsplit("://", 1)[-1].lower() == host if origin else False
        if origin is not None and not valid_origin:
            return JSONResponse({"detail": "Cross-origin requests are not allowed."}, status_code=403)
        if request.method not in ("GET", "HEAD") and not (access is not None and access.csrf_exempt(request)):
            token = request.headers.get("x-csrf-token", "")
            if not valid_origin or not hmac.compare_digest(token.encode("utf-8"), controller.state()["csrf_token"].encode("utf-8")):
                return JSONResponse({"detail": "Reload the local dashboard before sending a command."}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Keep Origin on the same-origin pairing form POST; no-referrer can
        # turn it into "null" in browsers. External links still get no referrer.
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "microphone=(), camera=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        return response

    if access is not None:
        access.install_routes(app)

    @app.get("/")
    def index():
        return FileResponse(HERE / "web_static" / "index.html")

    @app.get("/static/{asset}")
    def asset(asset: str):
        if asset not in ("app.js", "app.css", "assistant.js"):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        return FileResponse(HERE / "web_static" / asset)

    # 3D view: vendored three.js bundle, viewer module and the YuMi model.
    viewer_assets = {"viewer3d.js": "text/javascript", "pose_interpolation.js": "text/javascript",
                     "cup_holding.js": "text/javascript",
                     "three-bundle.min.js": "text/javascript",
                     "yumi_model.json": "application/json", "yumi_model.bin": "application/octet-stream"}

    @app.get("/static/3d/{asset}")
    def viewer_asset(asset: str):
        if asset not in viewer_assets:
            return JSONResponse({"detail": "Not found"}, status_code=404)
        return FileResponse(HERE / "web_static" / "3d" / asset, media_type=viewer_assets[asset])

    @app.get("/api/robot-pose")
    def robot_pose():
        # Read-only telemetry. Controllers without it (e.g. a remote device
        # proxy) report it as unavailable instead of failing the page.
        read = getattr(controller, "robot_pose", None)
        if not callable(read):
            return {"source": "unavailable", "arms": {"left": None, "right": None}}
        return read()

    @app.get("/api/state")
    def state():
        return controller.state()

    @app.get("/api/active_device")
    def active_device():
        from active_device_status import empty_status
        return device_monitor.snapshot() if device_monitor is not None else empty_status()

    @app.get("/api/status")
    def operator_status():
        return controller.status()

    @app.get("/api/help")
    def operator_help():
        return controller.help()

    @app.get("/api/saved-image/{cup_id}")
    def saved_image(cup_id: str):
        data = controller.saved_image(cup_id)
        if data is None:
            return JSONResponse({"detail": "Saved cup image unavailable."}, status_code=404)
        return Response(content=data, media_type="image/jpeg")

    @app.get("/api/image/{capture_id}")
    def image(capture_id: str):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,150}", capture_id):
            return JSONResponse({"detail": "Unknown scan"}, status_code=404)
        remote_image = getattr(controller, "image_bytes", None)
        if callable(remote_image):
            value = remote_image(capture_id)
            if value is not None:
                data, media_type = value
                return Response(content=data, media_type=media_type)
            return JSONResponse({"detail": "Selected device image unavailable."}, status_code=404)
        registered = controller.image(capture_id)
        if registered and root is not None:
            candidate = Path(registered).resolve()
            if candidate.is_relative_to(root) and candidate.is_file() and candidate.suffix.lower() in (".jpg", ".jpeg", ".png", ".svg"):
                return FileResponse(candidate)
        return JSONResponse({"detail": "This scan image is no longer available. Scan again."}, status_code=404)

    def settings_editable(request):
        # Enter/manage provider credentials on the Mac. A paired HTTP phone
        # can use the assistant without sending API credentials over Wi-Fi.
        from dashboard_access import is_loopback
        from urllib.parse import urlsplit
        return bool(request.client and is_loopback(request.client.host)
                    and is_loopback(urlsplit("//" + request.headers.get("host", "")).hostname or ""))

    @app.get("/api/assistant/status")
    def assistant_status(request: Request):
        result = assistant.status()
        result["settings_editable"] = settings_editable(request)
        if access is not None and allowed_origin and access.pairing_code:
            result["phone"] = {"origin": allowed_origin,
                               "pairing_code": access.pairing_code if settings_editable(request) else None}
        return result

    async def bounded_body(request, limit):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > limit:
                raise WebError("Request is too large.", 413)
        return bytes(raw)

    @app.post("/api/assistant/{operation}")
    async def assistant_command(operation: str, request: Request):
        try:
            if operation not in ("settings", "forget", "test", "ask", "prepare"):
                raise WebError("Assistant operation not found.", 404)
            if operation in ("settings", "forget", "test") and not settings_editable(request):
                raise WebError("Manage API access in Settings on the Mac. This phone uses the same configured key.", 403)
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                raise WebError("Send a JSON request.", 415)
            raw = await bounded_body(request, 8192)
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("Duplicate field")
                    result[key] = value
                return result
            try:
                payload = json.loads(raw, object_pairs_hook=unique)
            except (ValueError, UnicodeDecodeError, RecursionError):
                raise WebError("Invalid JSON request.", 422) from None
            if not isinstance(payload, dict):
                raise WebError("Send a JSON object.", 422)
            expected = {"ask": {"text"}, "prepare": {"proposal_id"}, "forget": set(), "test": set()}
            expected_context = None
            if operation in ("ask", "prepare"):
                snapshot = controller.state()
                if snapshot.get("devices"):
                    expected[operation] = expected[operation] | {"_device_id", "_device_epoch"}
                    expected_context = {"device_id": payload.get("_device_id"),
                                        "device_epoch": payload.get("_device_epoch")}
                    if (not isinstance(expected_context["device_id"], str)
                            or not isinstance(expected_context["device_epoch"], str)
                            or not expected_context["device_epoch"]
                            or expected_context["device_id"] != snapshot["devices"].get("active_id")
                            or expected_context["device_epoch"] != snapshot.get("device_epoch")):
                        raise WebError("The selected device changed. Refresh before asking the Assistant.", 409)
            if operation != "settings" and set(payload) != expected[operation]:
                raise WebError("Unknown assistant fields.", 422)
            if operation == "settings":
                result = await asyncio.to_thread(assistant.update, payload)
            elif operation == "forget":
                result = await asyncio.to_thread(assistant.forget)
            elif operation == "test":
                result = await asyncio.to_thread(assistant.test)
            elif operation == "ask":
                result = await asyncio.to_thread(assistant.ask, payload["text"],
                                                **({"expected_context": expected_context} if expected_context else {}))
            else:
                result = await asyncio.to_thread(assistant.prepare, payload["proposal_id"],
                                                **({"expected_context": expected_context} if expected_context else {}))
            return JSONResponse(result, status_code=202 if operation == "prepare" else 200)
        except WebError as error:
            return JSONResponse({"detail": str(error)}, status_code=error.status)
        except AssistantError as error:
            return JSONResponse({"detail": str(error)}, status_code=422)

    @app.post("/api/{operation}")
    async def command(operation: str, request: Request):
        if operation == "pick-height":
            operation = "pick_height"
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
            # A selected-device proxy may wait on HTTP. Keep the event loop
            # available for Stop and state reads while that request is pending.
            result = await asyncio.to_thread(controller.submit, operation, payload)
            return JSONResponse(result, status_code=202)
        except WebError as error:
            return JSONResponse({"detail": str(error)}, status_code=error.status)

    return app


class NoLanguage:
    def interpret(self, text, scene):
        raise ValueError("Use the Assistant tab for GPT-6 Astra requests, or the cup selection buttons.")


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


def demo_factory(directory, *, speed=None, camera_z=False):
    """Use real selection/planning checks and a dry-run robot with synthetic cups."""
    def factory(controller):
        from cup_commands import CupSession
        from cup_inventory import atomic_json, build_inventory
        from demo_named_cups import annotate_demo_inventory, DemoNamedLanguageController
        from placing_grid import PlacingGrid
        from robot_client import RobotClient

        directory.mkdir(parents=True, exist_ok=True)
        image_path = directory / "demo-table.svg"
        image_path.write_text(DEMO_IMAGE, encoding="utf-8")
        # This synthetic grid belongs to the demo's temporary directory. Never
        # load, reset, reserve or mark the project's real placement file here.
        grid_path = directory / "demo-placing-grid.json"
        atomic_json(grid_path, {"locations": [
            {"id": index + 1, "x": 400 + (index % 4) * 80,
             "y": -200 - (index // 4) * 80, "z": 230, "occupied": index < 9}
            for index in range(12)]})
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
            annotate_demo_inventory(result, {"1-CUP": "teal", "2-CUP": "white", "3-CUP": "orange"})
            return result, time.monotonic()
        session = CupSession(capture, lambda inventory: controller.publish_session(),
                             RobotClient("http://127.0.0.1:1", {}, dry_run=True),
                             "demo-calibration", tag_offset=(-115, 0, 40), speed=speed,
                             placing_grid=PlacingGrid(grid_path), camera_z=camera_z)
        language = DemoNamedLanguageController(session, DemoLanguage(), directory=directory,
                                               audit_dir=directory / "language")
        return session, language
    return factory


def hardware_factory(args):
    def factory(controller):
        # Deferred imports keep demo and tests independent of YOLO/OpenCV.
        from picker_workflow import (load_token, Calibration,
                                               CaptureStore, CaptureService)
        from inference_integration import create_detector, create_robot
        from camera_client import CameraClient
        from cup_commands import CupSession
        from language_commands import LanguageController
        from named_cups import NamedCupStore
        from placing_grid import PlacingGrid
        from recovery_journal import RecoveryJournal
        from robot_client import RobotClient

        if not getattr(args, "execution_lock_held", False):
            from device_agent import ExecutionLock
            grid_path = Path(args.placing_grid)
            controller.attach_resource(ExecutionLock(grid_path.with_name("." + grid_path.name + ".controller.lock")))

        controller.event("Loading gateway credentials and calibration…")
        headers = {"Authorization": f"Bearer {load_token()}"}
        api = args.gateway.rstrip("/") + "/api/v1"
        camera = CameraClient(api, headers)
        calibration = Calibration(args.calibration)
        controller.event("Reading camera configuration from the gateway…")
        intrinsics = camera.intrinsics()
        controller.event("Loading detection model…")
        detector, weights = create_detector(args, calibration, intrinsics,
            on_update=controller.publish_inference, register_resource=controller.attach_resource)
        store = CaptureStore(args.output)
        grid = PlacingGrid(args.placing_grid)
        captures = CaptureService(camera, detector, store, weights, placing_grid=grid,
                                  grid_table_z=args.grid_table_z, grid_cell_size=args.grid_cell_size,
                                  depth_frames=getattr(args, "depth_frames", 5),
                                  scan_observations=getattr(args, "scan_observations", 1))
        recovery = RecoveryJournal(store.folder / "robot_recovery.json", legacy_inventory_path=store.folder / "cups.json")
        failover = bool(getattr(args, "live", False) and getattr(args, "coordinator_url", None))
        def capture(purpose):
            controller.event("Capturing and detecting cups…" if purpose == "scan" else "Capturing a requested workspace observation…")
            executing = failover and getattr(getattr(session, "failover", None), "executing", False)
            if executing:
                session.robot.check_lease()
            result = captures.capture(purpose)
            if executing:
                session.robot.check_lease()
            return result
        def save(inventory):
            store.save(inventory)
            controller.publish_session()
        between_cup_check = failover or getattr(args, "between_cup_check", platform.system() == "Darwin")
        right_park_pose = None
        if failover:
            from handover_plan import right_via_pose
            right_park_pose = right_via_pose()
        elif between_cup_check:
            from camera_parking import load_right_park_pose
            right_park_pose = load_right_park_pose(Path(args.placing_grid).with_suffix(".right-park.json"),
                                                  args.gateway)
        session = CupSession(capture, save, create_robot(args, api, headers),
                     calibration.digest, tag_offset=calibration.offset, speed=args.speed,
                     max_result_age=args.max_result_age, match_mm=args.match_mm, match_px=args.match_px,
                     recovery=recovery, placing_grid=grid,
                     verification_frames=getattr(args, "verification_frames", 1),
                     strict_single_verification=getattr(args, "strict_single_verification",
                                                        getattr(args, "verification_frames", 1) == 1),
                     camera_z=getattr(args, "camera_z", False),
                     between_cup_check=between_cup_check, right_park_pose=right_park_pose,
                     between_cup_grid_check=between_cup_check,
                     continuous_batch=None)
        if failover:
            from device_service import read_gateway_token
            from failover_coordinator import CoordinatorClient
            from failover_workflow import FailoverRunner
            coordinator = CoordinatorClient(args.coordinator_url,
                                            read_gateway_token(args.coordinator_token_file), "primary")
            session.failover = FailoverRunner(session, coordinator, role="primary", state_dir=args.state_dir,
                                              output=controller.event)
        library = NamedCupStore(store.folder / "saved_cups.json", image_root=store.folder)
        return session, LanguageController(session, NoLanguage(), audit_dir=store.folder / "language",
                                           named_cups=library)
    factory.astra_named_matching = True
    return factory


class ProcessLock:
    """Compatibility context for the single-user app; no OS lock or file IO."""
    def __init__(self, directory):
        self.path = Path(directory) / ".cup-web.lock"
        self.file = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def main(argv=None):
    from inference_integration import add_inference_arguments, validate_inference_arguments
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1", help="Bind address; LAN access uses an exact origin and pairing")
    parser.add_argument("--lan-http", action="store_true", help="Allow paired phone access over HTTP on a private Wi-Fi network")
    parser.add_argument("--local-port", type=int, help="Optional localhost HTTP port sharing the same phone HTTPS dashboard")
    parser.add_argument("--allowed-origin", help="Exact phone origin, including its port")
    parser.add_argument("--tls-cert", type=Path, help="Optional HTTPS server certificate")
    parser.add_argument("--tls-key", type=Path, help="Private HTTPS server key")
    parser.add_argument("--pairing-code-file", type=Path, help="Private phone pairing code file")
    parser.add_argument("--phone-ca", type=Path, help="Public local CA certificate for the phone download")
    parser.add_argument("--assistant-settings", type=Path, help="Private settings file outside Git; written only if Remember is selected")
    parser.add_argument("--demo", action="store_true", help="Sample cups only; no camera, model or robot connection (default without --gateway)")
    parser.add_argument("--gateway", help="Robot gateway base URL; this computer runs detection and picking")
    parser.add_argument("--device-config", type=Path,
                        help="Local computer/Pi/Lynx picker configuration; the local computer supports Astra Assistant")
    parser.add_argument("--controller-profile", default=str(HERE / "robot_controller.json"),
                        help="Legacy compatibility option; status is read through the gateway")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--live", action="store_true", help="Enable physical robot execution after explicit confirmation")
    modes.add_argument("--dry-run", action="store_true", help="Preview only; the default in camera mode")
    parser.add_argument("--output", help="Capture directory, or separate dashboard cache in managed-device mode")
    parser.add_argument("--calibration", default=str(HERE.parent / "calibration" / "calibration_grid.json"))
    parser.add_argument("--placing-grid", default=str(HERE / "placing_grid_outside_tray.json"),
                        help="Placement JSON shared with terminal mode; occupancy is preserved")
    parser.add_argument("--grid-table-z", type=float, default=0.0, help="Physical robot Z of the table in mm (default 0)")
    parser.add_argument("--grid-cell-size", type=float, default=80.0, help="Fallback cell width in mm (default 80; 20..200); the grid file's cell_size_mm takes precedence")
    parser.add_argument("--conf", type=float, default=0.5)
    parser.add_argument("--depth-frames", type=int, choices=range(1, 10), default=5,
                        help="Fresh depth samples per observation (default 5); grid checks retain raw aligned depth")
    parser.add_argument("--camera-z", action="store_true",
                        help="Pick at measured cup-base Z + 20 mm, with a 40 mm minimum TCP height")
    parser.add_argument("--speed", type=float, default=None,
                        help=f"Override motion speed, including home (1..1500 mm/s). Without this option: "
                             f"normal {DEFAULT_SPEED}, automatic home {HOME_SPEED}; cup flips are capped at 100")
    parser.add_argument("--max-result-age", type=float, default=30,
                        help="Legacy compatibility option; snapshot picking has no image-age limit")
    parser.add_argument("--match-mm", type=float, default=20,
                        help="Legacy compatibility option; snapshot picking does not rematch cup positions")
    parser.add_argument("--match-px", type=float, default=40,
                        help="Legacy compatibility option; snapshot picking does not rematch cup pixels")
    add_inference_arguments(parser)
    args = parser.parse_args(argv)
    if args.output is None:
        args.output = str(args.device_config.resolve().parent / "dashboard-cache"
                          if args.device_config else HERE / "Data" / "Detections")
    validate_inference_arguments(args, parser)
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535")
    from dashboard_access import DashboardAccess, is_loopback, validate_origin
    from urllib.parse import urlsplit
    import ipaddress
    try:
        if args.host != "localhost":
            ipaddress.ip_address(args.host)
        if bool(args.tls_cert) != bool(args.tls_key):
            raise ValueError("Use --tls-cert and --tls-key together.")
        tls = bool(args.tls_cert)
        if args.lan_http and (tls or not args.allowed_origin or not args.pairing_code_file):
            raise ValueError("--lan-http needs an HTTP --allowed-origin and --pairing-code-file, without TLS options.")
        if args.local_port is not None and (not tls or not 1024 <= args.local_port <= 65535 or args.local_port == args.port):
            raise ValueError("--local-port needs HTTPS and a different port between 1024 and 65535.")
        scheme = "https" if tls else "http"
        origins = {f"{scheme}://localhost:{args.port}", f"{scheme}://127.0.0.1:{args.port}"}
        local_origins = {f"http://localhost:{args.local_port}", f"http://127.0.0.1:{args.local_port}"} if args.local_port else set()
        origins.update(local_origins)
        if args.allowed_origin:
            validate_origin(args.allowed_origin)
            if urlsplit(args.allowed_origin).scheme != scheme:
                raise ValueError("The allowed origin must match the server's HTTP or HTTPS scheme.")
            origins.add(args.allowed_origin)
        if not is_loopback(args.host) and not ((tls or args.lan_http) and args.allowed_origin and args.pairing_code_file):
            raise ValueError("Phone access requires --lan-http (or TLS), --allowed-origin and --pairing-code-file. Run phone_setup.py first.")
        for path in (args.tls_cert, args.tls_key):
            if path and not path.is_file():
                raise ValueError("The HTTPS certificate or key is missing. Run phone_setup.py first.")
        access = DashboardAccess(origins, pairing_code_file=args.pairing_code_file,
                                 certificate_file=args.phone_ca, allow_lan_http=args.lan_http)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    if args.device_config and (args.demo or args.gateway or args.mac_inference_url or args.isolated_inference):
        parser.error("--device-config configures each picker; omit --demo, --gateway and inference-only flags")
    if args.demo and (args.gateway or args.live):
        parser.error("--demo cannot be combined with --gateway or --live")
    if args.live and not args.gateway and not args.device_config:
        parser.error("--live requires --gateway")
    for name in ("conf", "max_result_age", "match_mm", "match_px"):
        if not math.isfinite(getattr(args, name)) or getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive and finite")
    if args.conf > 1 or (args.speed is not None and (not math.isfinite(args.speed) or not 1 <= args.speed <= 1500)):
        parser.error("Confidence must be at most 1; speed must be between 1 and 1500 mm/s")
    if not math.isfinite(args.grid_table_z) or not math.isfinite(args.grid_cell_size) or not 20 <= args.grid_cell_size <= 200:
        parser.error("--grid-table-z must be finite; --grid-cell-size must be 20..200 mm")
    try:
        import uvicorn
        import fastapi
    except ImportError:
        parser.exit(1, "Install the web dependencies: python3 -m pip install -r requirements-web.txt\n")
    mode = "demo" if not args.gateway and not args.device_config else ("live" if args.live else "dry_run")
    temporary = tempfile.TemporaryDirectory(prefix="cup-dashboard-") if mode == "demo" else None
    directory = Path(temporary.name if temporary else args.output).resolve()
    controller = device_monitor = None
    try:
        if args.device_config:
            from device_dashboard import DeviceDashboard
            controller = DeviceDashboard.from_config(args.device_config, mode=mode)
        else:
            factory = demo_factory(directory, speed=args.speed, camera_z=args.camera_z) if mode == "demo" else hardware_factory(args)
            controller = WebController(factory, mode=mode, model="Scripted demo" if mode == "demo" else "gpt-6-astra",
                                       llm_enabled=mode == "demo")
        from active_device_status import monitor_from_options
        device_monitor = monitor_from_options(args, mode=mode)
        app = create_app(controller, port=args.port, image_root=directory,
                         allowed_origin=args.allowed_origin, assistant_settings=args.assistant_settings,
                         access=access, tls=tls, extra_origins=local_origins,
                         device_monitor=device_monitor)
        with ProcessLock(directory):
            print(f"\nCup dashboard: {scheme}://localhost:{args.port}", flush=True)
            if args.allowed_origin:
                print(f"Phone dashboard: {args.allowed_origin}", flush=True)
            if args.local_port:
                print(f"Computer dashboard (same session): http://localhost:{args.local_port}", flush=True)
            if access.pairing_code:
                print(f"Phone pairing code: {access.pairing_code}", flush=True)
            print(f"Mode: {mode}. Close the terminal cup controller before using this app.", flush=True)
            print("Closing this server does not stop a movement already dispatched to the robot.\n", flush=True)
            # Both listeners share the same controller, settings and CSRF state.
            # Only the main HTTPS listener owns application startup/shutdown.
            local_server = local_thread = None
            if args.local_port:
                import threading
                local_server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.local_port,
                    lifespan="off", access_log=False, proxy_headers=False))
                local_thread = threading.Thread(target=local_server.run, name="dashboard-local", daemon=True)
                local_thread.start()
            try:
                uvicorn.run(app, host=args.host, port=args.port, workers=1, access_log=False,
                            proxy_headers=False,
                            ssl_certfile=str(args.tls_cert) if tls else None,
                            ssl_keyfile=str(args.tls_key) if tls else None)
            finally:
                if local_server is not None:
                    local_server.should_exit = True
                    local_thread.join(timeout=5)
    finally:
        try:
            # Local managed clients acquire state locks before ASGI lifespan
            # begins. Release them even if app creation, ProcessLock or server
            # startup fails. Normal lifespan shutdown may have closed already.
            if controller is not None:
                controller.close()
        finally:
            try:
                if device_monitor is not None:
                    device_monitor.close()
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
