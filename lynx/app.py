"""Host the gateway and cup coordinator together, with one native NCNN worker."""
import argparse
import asyncio
import base64
from contextlib import asynccontextmanager
import hmac
import math
import os
from pathlib import Path
import shutil
import sys


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--dashboard-origin", help="Exact browser origin; remote access uses the dashboard-token password")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--live", action="store_true")
    modes.add_argument("--mock", action="store_true", help="Synthetic gateway camera; block all robot mutations")
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--conf", type=float, default=.5)
    parser.add_argument("--inference-timeout", type=float, default=10.)
    parser.add_argument("--model-headroom-mib", type=int, default=48,
                        help="Additional visible cgroup and host memory required before real-camera model startup (32..128 MiB)")
    parser.add_argument("--camera-width", type=int, default=960)
    parser.add_argument("--camera-height", type=int, default=540)
    parser.add_argument("--camera-fps", type=float, default=2.)
    parser.add_argument("--capture-budget-mib", type=int, default=256)
    parser.add_argument("--grid-table-z", type=float, default=0.)
    parser.add_argument("--grid-cell-size", type=float, default=80.)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535 or not 1 <= args.threads <= 4:
        parser.error("Use port 1024..65535 and 1..4 inference threads.")
    if args.host not in ("127.0.0.1", "localhost", "0.0.0.0"):
        parser.error("Use --host 127.0.0.1 or 0.0.0.0 so the coordinator can reach its local gateway.")
    if args.host == "localhost":
        args.host = "127.0.0.1"
    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.dashboard_origin:
        parser.error("Remote binding requires --dashboard-origin http://LYNX_ADDRESS:PORT.")
    if (args.camera_width, args.camera_height) not in ((640, 360), (960, 540), (1280, 720), (1920, 1080)):
        parser.error("Camera dimensions must be 640x360, 960x540, 1280x720 or 1920x1080.")
    if not all(math.isfinite(v) for v in (args.conf, args.inference_timeout, args.camera_fps, args.grid_table_z, args.grid_cell_size)):
        parser.error("Numeric settings must be finite.")
    if not 0 < args.conf < 1 or not 0 < args.inference_timeout <= 15 or not .5 <= args.camera_fps <= 4:
        parser.error("Use confidence (0,1), inference timeout (0,15] seconds and camera FPS .5..4.")
    if not 32 <= args.capture_budget_mib <= 4096 or not 20 <= args.grid_cell_size <= 200:
        parser.error("Use capture budget 32..4096 MiB and grid cell 20..200 mm.")
    if not 32 <= args.model_headroom_mib <= 128:
        parser.error("Model startup headroom must be 32..128 MiB.")
    return args


def configure(args):
    """Call before any gateway import; do not inherit a checkout's .env file."""
    if "gateway.main" in sys.modules:
        raise RuntimeError("Start the combined app in a fresh process, not inside an existing gateway.")
    for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[key] = "1"
    os.environ.setdefault("MALLOC_ARENA_MAX", "2")
    state = args.state_dir.resolve()
    if not state.is_dir():
        raise ValueError("Prepare a persistent state directory with python -m lynx.prepare_state first.")
    if sys.platform.startswith("linux") and not args.mock and not os.path.ismount(state):
        raise ValueError("Mount persistent storage at --state-dir; WeOS writable overlay uses RAM.")
    if shutil.disk_usage(state).free < 64 * 1024 * 1024:
        raise ValueError("Persistent state storage needs at least 64 MiB free.")
    for name in ("calibration.json", "placing-grid.json", "auth_clients.json", "gateway-token", "dashboard-token"):
        if not (state / name).is_file():
            raise ValueError(f"State file is missing: {name}")
    values = {"API_PREFIX": "", "SECURITY_MODE": "strict", "CONTROL_REQUIRE_SESSION": "true",
        "AUTH_CLIENTS_FILE": str(state / "auth_clients.json"),
        "CALIBRATION_FILE": str(state / "gateway-calibration.json"),
        "CAMERA_PROVIDER": "mock" if args.mock else "oak", "ROBOT_PROVIDER": "mock" if args.mock else "tcp",
        "CAMERA_RGB_WIDTH": str(args.camera_width), "CAMERA_RGB_HEIGHT": str(args.camera_height),
        "CAMERA_FPS": str(args.camera_fps)}
    if args.mock:
        values.update(ROBOT_HOST="127.0.0.1", RWS_PORT="1", ROBOT_PORT="1")
    if args.live and not os.environ.get("GATEWAY_ROBOT_HOST"):
        raise ValueError("Set GATEWAY_ROBOT_HOST explicitly before enabling --live.")
    os.environ.update({"GATEWAY_" + key: value for key, value in values.items()})
    from gateway.config import Settings, get_settings
    Settings.model_config["env_file"] = None
    get_settings.cache_clear()
    from gateway.security.client_registry import ClientRegistry
    token = (state / "gateway-token").read_text().strip()
    identity = ClientRegistry(str(state / "auth_clients.json")).authenticate(token)
    if len(token) < 32 or identity is None or not {"camera.read", "robot.read", "robot.control"}.issubset(identity.permissions):
        raise ValueError("The private coordinator token does not match the required registry permissions.")
    dashboard_token = (state / "dashboard-token").read_text().strip()
    if len(dashboard_token) < 32 or dashboard_token == token:
        raise ValueError("Use a distinct dashboard token with at least 32 characters.")
    return state, token, dashboard_token


class AccessGate:
    """Authenticate remote dashboard and block gateway mutations in dry-run."""
    def __init__(self, app, *, live, dashboard_token=None):
        self.app, self.live = app, live
        self.authorization = ("Basic " + base64.b64encode(("lynx:" + dashboard_token).encode()).decode()).encode() if dashboard_token else None

    async def __call__(self, scope, receive, send):
        from starlette.responses import JSONResponse
        if scope["type"] in ("http", "websocket"):
            path = scope["path"]
            gateway = path == "/api/v1" or path.startswith("/api/v1/")
            if gateway and not self.live and scope["type"] == "http" and scope["method"] not in ("GET", "HEAD", "OPTIONS"):
                await JSONResponse({"detail": "Robot mutations are disabled in Lynx dry-run mode."}, status_code=403)(scope, receive, send)
                return
            if not gateway and self.authorization:
                auth = dict(scope.get("headers", [])).get(b"authorization", b"")
                if not hmac.compare_digest(auth, self.authorization):
                    if scope["type"] == "websocket":
                        await send({"type": "websocket.close", "code": 1008})
                    else:
                        await JSONResponse({"detail": "Sign in with user lynx and the dashboard-token password."}, status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="Lynx picker"', "Cache-Control": "no-store"})(scope, receive, send)
                    return
        await self.app(scope, receive, send)


def compose(gateway, dashboard, *, live=False, dashboard_token=None):
    from starlette.applications import Starlette
    from starlette.routing import Mount
    @asynccontextmanager
    async def lifespan(app):
        # Mounted ASGI applications do not automatically get lifespan events.
        async with gateway.router.lifespan_context(gateway):
            async with dashboard.router.lifespan_context(dashboard):
                yield
    app = Starlette(routes=[Mount("/api/v1", app=gateway), Mount("/", app=dashboard)], lifespan=lifespan)
    return AccessGate(app, live=live, dashboard_token=dashboard_token)


def hardware_factory(args, state, token, model, service, loop_getter):
    def factory(controller):
        import cv2
        from capture_and_detect import Calibration, CaptureService, CaptureStore
        from cup_commands import CupSession
        from language_commands import LanguageController
        from placing_grid import PlacingGrid
        from recovery_journal import RecoveryJournal
        from robot_client import RobotClient
        from web_app import NoLanguage
        from .camera import LocalCameraClient
        from .inference import LynxDetector, NativeWorker
        cv2.setNumThreads(1)
        cv2.ocl.setUseOpenCL(False)
        api = f"http://127.0.0.1:{args.port}/api/v1"
        camera = LocalCameraClient(service, loop_getter(), api=api)
        calibration = Calibration(state / "calibration.json")
        grid = PlacingGrid(state / "placing-grid.json")
        grid.snapshot()
        if sys.platform.startswith("linux") and not args.mock:
            from .resources import require_model_headroom, trim_unused_heap
            controller.event(trim_unused_heap())
            snapshot = require_model_headroom(args.model_headroom_mib)
            controller.event(snapshot.summary(args.model_headroom_mib))
        controller.event("Starting the Lynx native model…")
        worker = NativeWorker(args.worker, model, threads=args.threads, confidence=args.conf, timeout=args.inference_timeout)
        try:
            controller.attach_resource(worker)
            detector = LynxDetector(worker, calibration, camera.intrinsics(), args.conf)
            class BudgetStore(CaptureStore):
                def save_capture(self, inventory, rgb, depth, annotated, objects, K, purpose):
                    used = sum(p.stat().st_size for p in self.folder.rglob("*") if p.is_file())
                    reserve = rgb.nbytes + depth.nbytes + annotated.nbytes + 1024 * 1024
                    if used + reserve > args.capture_budget_mib * 1024 * 1024 or shutil.disk_usage(self.folder).free < reserve + 32 * 1024 * 1024:
                        raise OSError("Capture storage budget reached. Archive old captures before scanning again; robot state was preserved.")
                    super().save_capture(inventory, rgb, depth, annotated, objects, K, purpose)
            store = BudgetStore(state / "captures")
            captures = CaptureService(camera, detector, store, model.weights, placing_grid=grid,
                                      grid_table_z=args.grid_table_z, grid_cell_size=args.grid_cell_size)
            recovery = RecoveryJournal(store.folder / "robot_recovery.json", legacy_inventory_path=store.folder / "cups.json")
            def capture(purpose):
                controller.event("Capturing and detecting cups on Lynx…")
                return captures.capture(purpose)
            def save(inventory):
                store.save(inventory)
                controller.publish_session()
            session = CupSession(capture, save, RobotClient(api, {"Authorization": "Bearer " + token},
                         dry_run=not args.live, require_session=True), calibration.digest,
                         tag_offset=calibration.offset, recovery=recovery, placing_grid=grid)
            return session, LanguageController(session, NoLanguage(), audit_dir=store.folder / "language")
        except BaseException:
            # Startup errors are shown in the still-running dashboard. Release
            # model memory now rather than waiting for that dashboard to close.
            worker.close()
            raise
    return factory


def create_combined(args):
    state, token, dashboard_token = configure(args)
    from .model import Model
    model = Model.load(args.model)
    if not args.worker.is_file() or not os.access(args.worker, os.X_OK):
        raise ValueError("The native inference executable is missing or not executable.")
    from gateway.main import app as gateway
    from gateway.camera.service import camera_service
    from web_app import create_app
    from web_controller import WebController
    loop_holder = {}
    original = gateway.router.lifespan_context
    @asynccontextmanager
    async def gateway_lifespan(app):
        loop_holder["loop"] = asyncio.get_running_loop()
        async with original(app):
            yield
    gateway.router.lifespan_context = gateway_lifespan
    controller = WebController(hardware_factory(args, state, token, model, camera_service, lambda: loop_holder["loop"]),
                               mode="live" if args.live else "dry_run", model="Lynx NCNN", llm_enabled=False)
    dashboard = create_app(controller, port=args.port, image_root=state / "captures", allowed_origin=args.dashboard_origin)
    return compose(gateway, dashboard, live=args.live, dashboard_token=dashboard_token if args.dashboard_origin else None)


def main(argv=None):
    args = arguments(argv)
    try:
        from web_app import ProcessLock
        if not args.state_dir.is_dir():
            raise ValueError("Prepare a persistent state directory with python -m lynx.prepare_state first.")
        with ProcessLock(args.state_dir):
            app = create_combined(args)
            import uvicorn
            print(f"Lynx picker: {'LIVE' if args.live else 'DRY RUN'}; dashboard port {args.port}; gateway /api/v1", flush=True)
            uvicorn.run(app, host=args.host, port=args.port, workers=1, access_log=False, loop="asyncio",
                        http="h11", ws="none", timeout_keep_alive=5)
    except (ValueError, OSError, RuntimeError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
