"""Share one Python runtime between the durable coordinator and armed Lynx picker.

The native model remains in its existing isolated process. This launcher never
starts a batch, acquires control, captures an image or sends a robot command.
The picker retains its 128 MiB startup check and finite, explicitly admitted runs.
"""

import argparse
import os
from pathlib import Path
import signal
import threading

from failover_coordinator import (CoordinatorError, CoordinatorServer,
                                  CoordinatorStore, _read_token)

RETRY_DELAY = 5
SHUTDOWN_TIMEOUT = 2


def output(message):
    try:
        print(message, flush=True)
    except (OSError, ValueError):
        pass


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--worker", default="/usr/local/bin/lynx-ncnn-worker")
    parser.add_argument("--gateway", default="http://127.0.0.1:8000")
    parser.add_argument("--token-file", type=Path, help="Private tertiary gateway credential")
    parser.add_argument("--coordinator-token-file", type=Path, required=True)
    parser.add_argument("--coordinator-gateway-token-file", type=Path, required=True,
                        help="Private read-only gateway credential for queue receipts")
    parser.add_argument("--coordinator-state-file", type=Path)
    parser.add_argument("--coordinator-url", help="Defaults to http://127.0.0.1:PORT")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8092)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--depth-frames", type=int, default=5)
    parser.add_argument("--conf", type=float, default=0.5)
    parser.add_argument("--speed", type=float)
    parser.add_argument("--inference-timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("--live is required to arm the automatic failover worker")
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1..65535")
    return args


def picker_arguments(args):
    values = ["--live", "--armed", "--state-dir", str(args.state_dir),
              "--model", str(args.model), "--worker", args.worker,
              "--gateway", args.gateway, "--coordinator-url",
              args.coordinator_url or f"http://127.0.0.1:{args.port}",
              "--coordinator-token-file", str(args.coordinator_token_file),
              "--depth-frames", str(args.depth_frames), "--conf", str(args.conf),
              "--inference-timeout", str(args.inference_timeout)]
    if args.token_file is not None:
        values.extend(("--token-file", str(args.token_file)))
    if args.speed is not None:
        values.extend(("--speed", str(args.speed)))
    return values


def close_server(server, serving):
    """Bound listener shutdown even if its serving thread failed unexpectedly.

    Request handlers are already daemon threads with bounded socket timeouts.
    Never wait indefinitely for HTTPServer.shutdown when serve_forever failed
    before entering its loop. Neither shutdown path touches the robot.
    """
    if serving.is_alive():
        shutdown = threading.Thread(target=server.shutdown,
                                    name="failover-coordinator-shutdown", daemon=True)
        shutdown.start()
        shutdown.join(SHUTDOWN_TIMEOUT)
    server.server_close()
    serving.join(SHUTDOWN_TIMEOUT)


def main(argv=None):
    args = arguments(argv)
    from . import picker
    command = picker_arguments(args)
    # Reject invalid native options before creating a listener or state file.
    picker.arguments(command)
    stopping = threading.Event()
    failed = threading.Event()
    previous = {}
    server = serving = None

    def shutdown(_signum, _frame):
        stopping.set()
        # Interrupt credential/startup IO or the retry delay. While picker.main
        # runs it installs its own SIGTERM handler and performs its own cleanup.
        raise KeyboardInterrupt

    def serve():
        try:
            server.serve_forever(poll_interval=0.2)
        except Exception:
            pass
        finally:
            if stopping.is_set():
                return
            failed.set()
            output("Failover coordinator listener failed; disarming the local worker.")
            # Interrupt only this process's receipt wait. Accepted gateway work
            # remains at the gateway; this sends no physical Stop.
            os.kill(os.getpid(), signal.SIGTERM)

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, shutdown)
        state = args.state_dir.resolve(strict=True)
        store = CoordinatorStore(args.coordinator_state_file or state / "failover-coordinator.json",
                                 args.gateway, _read_token(args.coordinator_gateway_token_file))
        server = CoordinatorServer((args.host, args.port), store,
                                   _read_token(args.coordinator_token_file))
        serving = threading.Thread(target=serve, name="failover-coordinator", daemon=True)
        serving.start()
        output("Failover coordinator is listening; the native worker will arm after its memory and model checks.")
        while not stopping.is_set() and not failed.is_set():
            result = picker.main(command)
            if result == 0:
                return 1 if failed.is_set() else 0
            output("Native worker is unavailable; coordinator remains available. Retrying arming in 5 seconds.")
            if stopping.wait(RETRY_DELAY):
                break
        return 1 if failed.is_set() else 0
    except KeyboardInterrupt:
        return 1 if failed.is_set() else 0
    except (CoordinatorError, OSError, ValueError):
        output("Armed service could not start or access its configured state.")
        return 1
    finally:
        stopping.set()
        try:
            if server is not None:
                if serving is not None and serving.ident is not None:
                    close_server(server, serving)
                else:
                    server.server_close()
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
