"""Command-line picker using an existing gateway and native C++ perception."""
import argparse
import json
import os
from pathlib import Path
import signal
import stat
import sys

from camera_client import CameraClient
from cup_commands import CupSession
from cup_inventory import SelectionError
from language_commands import LanguageController
from local_llm import DisabledIntentClient
from placing_grid import PlacingGrid
from recovery_journal import RecoveryJournal
from robot_client import RobotClient
from robot_controls import RobotControls
from terminal_commands import handle_input, positive, print_cups
from terminal_controller import TerminalController
from .capture import Calibration, CaptureService, CaptureStore
from .model import Model
from .native_perception import NativePerception
from .resources import (advise_capture_cache, advise_loaded_python_cache, advise_packaged_native_cache,
                        trim_unused_heap, wait_for_model_headroom)


HELP = """Commands:
  scan                  Detect cups and check all placement cells; no movement.
  list / show <id>      Inspect the saved cup list or one cup's details.
  start / goto all      Scan/preview a batch, then use confirm <code>.
  goto <id>             Pick, hand over and place using the saved scan.
  hover <id>            Approach above a saved cup without gripping.
  stop / halt          Block further jobs and request a software stop.
  status               Show application state.
  robot status         Refresh gateway/controller status.
  home right|left|both  Preview a supported Home action (both: right, then left).
  confirm-home <code>   Confirm inspection and execute the displayed Home action.
  cancel               Discard proposals; does not stop a dispatched movement.
  reset                Acknowledge inspection after recovery; sends no motion.
  help / quit          Show commands or exit.
Default mode is DRY RUN. --live is required for robot commands.
Picks finish above the destination. Home is a separate operator command.
Close the Mac controller before operating this picker live. After a failure,
inspect both arms, grippers and the paths before reset or confirming Home.
Software Stop depends on the gateway; physical emergency stop remains available.
"""


def terminal_output(*parts, file=None):
    """A lost console must never prevent stop dispatch or resource cleanup."""
    try:
        print(*parts, file=file, flush=True)
    except (OSError, ValueError):
        # Broken pipes, disconnected terminals and already-closed streams are
        # not recoverable by printing another message to the same console.
        pass


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--worker", default="/usr/local/bin/lynx-ncnn-worker")
    parser.add_argument("--gateway", default="http://127.0.0.1:8000")
    parser.add_argument("--live", action="store_true", help="Enable explicit operator motion commands")
    parser.add_argument("--scan-once", action="store_true", help="Save one camera-only scan and exit; never moves")
    parser.add_argument("--max-result-age", type=positive, default=60.0,
                        help="Legacy compatibility option; snapshot picking has no image-age limit")
    parser.add_argument("--depth-frames", type=int, choices=range(1, 10), default=5)
    parser.add_argument("--camera-z", action="store_true", help="Optional measured pick height; disabled by default")
    parser.add_argument("--speed", type=positive)
    parser.add_argument("--conf", type=positive, default=0.5)
    parser.add_argument("--inference-timeout", type=positive, default=30.0)
    parser.add_argument("--controller-profile", type=Path,
                        help="Optional existing read-only controller status profile for legacy gateway controls")
    args = parser.parse_args(argv)
    if args.conf >= 1 or (args.speed is not None and not 1 <= args.speed <= 1500):
        parser.error("confidence must be below 1; speed must be 1..1500 mm/s")
    if args.scan_once and args.live:
        parser.error("--scan-once is camera-only; omit --live")
    return args


def read_token(path):
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("Gateway token must be a regular file.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        opened, current = os.fstat(stream.fileno()), os.lstat(path)
        if (not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode)
                or not os.path.samestat(before, opened) or not os.path.samestat(opened, current)):
            raise ValueError("Gateway token must be a regular file.")
        raw = stream.read(4097)
    token = raw.decode("ascii").strip()
    if len(raw) > 4096 or not token or any(not 33 <= ord(c) <= 126 for c in token):
        raise ValueError("Gateway token file is invalid.")
    return token


class CommandHandler:
    def __init__(self, session, language, controls):
        self.session, self.language, self.controls = session, language, controls

    def __call__(self, line):
        words = line.strip().lower().split()
        if words == ["help"]:
            terminal_output(HELP)
            return True
        if words == ["robot", "status"]:
            self.controls.refresh_status()
            terminal_output(json.dumps(self.controls.status, indent=2))
            return True
        if words and words[0] == "home":
            if len(words) != 2 or words[1] not in ("right", "left", "both"):
                raise SelectionError("Use home right, home left, or home both.")
            self.language.cancel()
            self.controls.propose("home", words[1])
            pending = self.controls.pending
            terminal_output(pending["description"])
            terminal_output(f"After inspecting the robot and return path: confirm-home {pending['token']}")
            return True
        if words and words[0] == "confirm-home":
            if len(words) != 2:
                raise SelectionError("Use confirm-home followed by the displayed code.")
            terminal_output(self.controls.confirm(words[1])["message"])
            return True
        if words not in (["status"], ["list"]) and (not words or words[0] != "show"):
            self.controls.cancel()
        return handle_input(self.session, line, self.language)


def main(argv=None):
    args = arguments(argv)
    worker = terminal = None
    interrupted = False
    shutting_down = False
    previous_sigterm = None
    signal_installed = False

    def request_shutdown(_signum, _frame):
        nonlocal shutting_down
        # Container/service termination must take the same stop-and-journal
        # path as Ctrl-C. Repeated SIGTERM must not interrupt that cleanup.
        if not shutting_down:
            shutting_down = True
            raise KeyboardInterrupt

    try:
        previous_sigterm = signal.signal(signal.SIGTERM, request_shutdown)
        signal_installed = True
        state = args.state_dir.resolve(strict=True)
        model = Model.load(args.model)
        calibration = Calibration(state / "calibration.json")
        grid = PlacingGrid(state / "placing-grid.json")
        headers = {"Authorization": "Bearer " + read_token(state / "gateway-token")}
        api = args.gateway.rstrip("/")
        if not api.endswith("/api/v1"):
            api += "/api/v1"
        camera = CameraClient(api, headers, max_dimensions=(1920, 1080))
        store = CaptureStore(state / "captures")
        # Linux deployment must expose the shared hosting ancestor counters.
        # This is a startup check, followed by measured full scans on target.
        if sys.platform == "linux":
            terminal_output(advise_capture_cache(store.folder, sync=True).summary())
            terminal_output(advise_packaged_native_cache().summary())
            terminal_output(advise_loaded_python_cache().summary())
            # Imports and model hashing can leave freed transient heap pages.
            # Reclaim them before measurement; never infer headroom from trim.
            terminal_output(trim_unused_heap())
            memory = wait_for_model_headroom(128)
            terminal_output(memory.summary(128))
        worker = NativePerception(args.worker, model, confidence=args.conf, timeout=args.inference_timeout)
        captures = CaptureService(camera, worker, calibration, grid, store,
                                  depth_frames=args.depth_frames, confidence=args.conf)
        robot = RobotClient(api, headers, dry_run=not args.live, require_session=False)
        robot.configure_operator_controls(args.controller_profile)
        recovery = RecoveryJournal(store.folder / "robot_recovery.json",
                                   legacy_inventory_path=store.folder / "cups.json")
        def capture(purpose):
            inventory, started = captures.capture(purpose)
            advise_capture_cache(store.folder, paths=tuple(
                inventory["images"][kind] for kind in ("rgb", "depth")))
            return inventory, started

        session = CupSession(capture, store.save, robot, calibration.digest,
                             tag_offset=calibration.offset, speed=args.speed,
                             recovery=recovery, placing_grid=grid, verification_frames=1,
                             strict_single_verification=True, max_result_age=args.max_result_age,
                             camera_z=args.camera_z)
        if args.scan_once:
            # Do not call session.scan(): a dry camera check must not reconcile
            # or release the deployment's existing occupied/reserved records.
            inventory, _ = capture("scan")
            print_cups(inventory)
            return 0
        language = LanguageController(session, DisabledIntentClient(), audit_dir=store.folder / "language")
        controls = RobotControls(session)
        terminal = TerminalController(session, language, CommandHandler(session, language, controls),
                                      output=terminal_output)
        terminal_output(f"{'LIVE' if args.live else 'DRY RUN'} · native {model.precision} · {model.input_size}px · {api}")
        terminal_output(f"Snapshot picking: one observation per scan; {args.depth_frames} raw depth samples. "
                        "Picks reuse the saved snapshot without further captures or an image-age limit.")
        terminal_output(HELP)
        if session.motion_uncertain:
            terminal_output("Recovery inspection is required before motion; scan is available.")
        while True:
            line = input("cups> ").strip()
            # An empty Enter must not start another expensive camera check.
            if line and not terminal.submit(line):
                break
        return 0
    except (KeyboardInterrupt, EOFError):
        interrupted = True
        terminal_output("Exiting; active work receives a software stop request. No automatic home or resume.")
        return 0
    except Exception as error:
        interrupted = True
        terminal_output(f"Picker stopped: {error}", file=sys.stderr)
        return 1
    finally:
        shutting_down = True
        try:
            if terminal is not None:
                terminal.close(interrupt=interrupted)
        finally:
            try:
                if worker is not None:
                    worker.close()
            finally:
                if signal_installed:
                    signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    raise SystemExit(main())
