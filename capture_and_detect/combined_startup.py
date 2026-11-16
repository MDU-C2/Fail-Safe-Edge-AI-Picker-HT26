"""Retire this installation's idle legacy Mac processes before user startup.

Nothing runs on import. This helper never dispatches a picker command, changes
ownership, removes state files, or force-kills a process. Call it only from the
user's launcher after the replacement dashboard's port has been checked.
"""
import argparse
import ctypes
from dataclasses import dataclass
import errno
import http.client
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


class CombinedStartupError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    argv: tuple
    started: str


@dataclass(frozen=True)
class LegacyProcess:
    name: str
    port: int
    pid_file: Path
    arguments: tuple


def _argv(pid):
    """Read argument boundaries, including paths with spaces, without a shell."""
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2, PID.
        size = ctypes.c_size_t()
        if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0:
            code = ctypes.get_errno()
            if code == errno.ESRCH:
                return None
            raise CombinedStartupError("Cannot inspect a saved process; nothing was signalled.")
        if not 4 < size.value <= 4 * 1024 * 1024:
            raise CombinedStartupError("Saved process arguments are unavailable.")
        buffer = ctypes.create_string_buffer(size.value)
        if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
            if ctypes.get_errno() == errno.ESRCH:
                return None
            raise CombinedStartupError("Cannot inspect a saved process; nothing was signalled.")
        raw = buffer.raw[:size.value]
        count = int.from_bytes(raw[:4], sys.byteorder, signed=True)
        offset = raw.find(b"\0", 4) + 1  # Skip executable path and alignment.
        if not 0 < count < 10000 or offset <= 4:
            raise CombinedStartupError("Saved process arguments are invalid.")
        while offset < len(raw) and raw[offset] == 0:
            offset += 1
        parts = raw[offset:].split(b"\0", count)
        if len(parts) <= count:
            raise CombinedStartupError("Saved process arguments are incomplete.")
        return tuple(os.fsdecode(value) for value in parts[:count])
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise CombinedStartupError("Cannot inspect a saved process; nothing was signalled.") from None
    return tuple(os.fsdecode(value) for value in raw.rstrip(b"\0").split(b"\0")) if raw else None


def _process_start(pid):
    """Distinguish a missing process from a failed or denied status lookup."""
    try:
        result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "lstart=", "-o", "stat="],
                                capture_output=True, text=True, timeout=2, check=False)
    except (OSError, subprocess.SubprocessError):
        raise CombinedStartupError("Cannot verify the saved process start time.") from None
    output = result.stdout.strip()
    if result.returncode == 1 and not output and not result.stderr.strip():
        return None
    fields = output.rsplit(None, 1)
    if result.returncode or result.stderr.strip() or len(fields) != 2:
        raise CombinedStartupError("Cannot verify the saved process start time.")
    return None if fields[1].startswith("Z") else fields[0]


def _identity(pid):
    # Darwin KERN_PROCARGS2 can return EINVAL, not ESRCH, for a PID that
    # already exited. Check existence before reading arguments; do not treat
    # arbitrary argument/permission errors as proof that a process is absent.
    started = _process_start(pid)
    if started is None:
        return None
    try:
        argv = _argv(pid)
    except CombinedStartupError:
        if _process_start(pid) is None:
            return None  # It exited between the status and argument reads.
        raise
    current = _process_start(pid)
    if current is None:
        return None
    if argv is None or current != started:
        raise CombinedStartupError("The saved process changed during inspection; nothing was signalled.")
    return ProcessIdentity(pid, argv, started)


def _listener(port):
    try:
        result = subprocess.run(["/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                                capture_output=True, text=True, timeout=2, check=False)
    except (OSError, subprocess.SubprocessError):
        raise CombinedStartupError("Cannot verify the legacy server's listening process.") from None
    values = result.stdout.split()
    if result.returncode or not values or any(not value.isdecimal() for value in values):
        raise CombinedStartupError("The legacy server's listening process is unavailable.")
    return set(map(int, values))


def _get_json(port, path, token=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        headers = {"Authorization": "Bearer " + token} if token else {}
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        raw = response.read(1024 * 1024 + 1)
        if response.status != 200 or len(raw) > 1024 * 1024:
            raise ValueError()
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (OSError, http.client.HTTPException, ValueError):
        raise CombinedStartupError(f"Legacy server on port {port} is unreachable or returned invalid state; it was not stopped.") from None
    finally:
        connection.close()


def _read_token(private):
    path = private / "mac-service.token"
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024:
            raise ValueError()
        token = path.read_text().strip()
        if not 32 <= len(token) <= 512 or any(not 33 <= ord(value) <= 126 for value in token):
            raise ValueError()
        return token
    except (OSError, ValueError):
        raise CombinedStartupError("The private Mac service token is unavailable or invalid.") from None


def _check_idle(target, private):
    if target.name == "dashboard":
        state = _get_json(target.port, "/api/state")
        if (state.get("mode") != "live" or state.get("busy") is not False
                or state.get("stop_in_flight") is not False or not isinstance(state.get("devices"), dict)
                or state["devices"].get("switching") is not False):
            raise CombinedStartupError("The legacy dashboard is busy, switching, or has unverified idle state; it was not stopped.")
    else:
        document = _get_json(target.port, "/v1/state", _read_token(private))
        health, state = document.get("health"), document.get("state")
        if (not isinstance(health, dict) or health.get("device_id") != "mac" or health.get("mode") != "live"
                or health.get("busy") is not False or health.get("transfer_incomplete") is not False
                or health.get("handoff_blocked") is not False):
            raise CombinedStartupError("The legacy Mac service is busy or has an unfinished handoff; it was not stopped.")
        if state is None and health.get("controller_present") is False:
            return  # Inactive service; no controller or picker worker exists.
        if (not isinstance(state, dict) or health.get("controller_present") is not True
                or state.get("busy") is not False or state.get("stop_in_flight") is not False):
            raise CombinedStartupError("The legacy Mac controller is busy or its idle state is unverified; it was not stopped.")


def _verify(target, identity):
    current = _identity(identity.pid)
    if current != identity:
        raise CombinedStartupError(f"The saved {target.name} process changed; nothing further was signalled.")
    if _listener(target.port) != {identity.pid}:
        raise CombinedStartupError(f"Port {target.port} is not owned solely by the verified {target.name}; it was not stopped.")


def _wait_exit(identity, timeout):
    deadline = time.monotonic() + timeout
    while _identity(identity.pid) == identity:
        if time.monotonic() >= deadline:
            raise CombinedStartupError("A legacy process did not exit after graceful shutdown. No force-kill was sent; all files were preserved.")
        time.sleep(min(.2, max(0, deadline - time.monotonic())))


def retire_idle_legacy(installation, repo, *, check_only=False, timeout=10):
    """Check saved legacy processes and gracefully stop only verified idle ones.

    Missing/dead PID files are harmless. Any foreign PID, busy state, unfinished
    handoff or unreachable live server aborts. Recheck the Mac service after the
    dashboard has exited, to catch jobs admitted before the dashboard closed.
    PID files, owner records, placement state and credentials are preserved.
    """
    if not 0 < timeout <= 10:
        raise ValueError("Shutdown timeout must be between zero and ten seconds.")
    installation, repo = Path(installation).resolve(), Path(repo).resolve()
    private = installation / "private"
    targets = [
        LegacyProcess("dashboard", 8086, private / "dashboard.pid", (
            str(repo / "capture_and_detect/web_app.py"), "--device-config", str(private / "devices.json"),
            "--live", "--output", str(private / "dashboard-cache"), "--port", "8086")),
        LegacyProcess("Mac service", 8091, private / "mac-service.pid", (
            "-m", "device_service", "--device-id", "mac", "--backend", "python",
            "--state-dir", str(private / "mac-state"), "--model", str(repo / "capture_and_detect/best.pt"),
            "--token-file", str(private / "mac-service.token"), "--robot-id", "mdu-yumi-lynx-9113c0",
            "--gateway", "http://192.168.125.20:8000", "--controller-profile", str(private / "mac-state/controller-status.json"),
            "--listen", "127.0.0.1", "--port", "8091", "--live")),
    ]
    found, absent = [], []
    for target in targets:
        if not target.pid_file.exists() and not target.pid_file.is_symlink():
            absent.append(target.name)
            continue
        try:
            raw = target.pid_file.read_text().strip() if not target.pid_file.is_symlink() and target.pid_file.stat().st_size < 64 else ""
            if not raw.isdecimal() or int(raw) <= 1:
                raise ValueError()
        except (OSError, ValueError):
            raise CombinedStartupError(f"The saved {target.name} PID is invalid; nothing was signalled.") from None
        identity = _identity(int(raw))
        if identity is None:
            absent.append(target.name)
            continue
        if (not identity.argv or Path(identity.argv[0]).resolve() != Path(sys.executable).resolve()
                or identity.argv[1:] != target.arguments):
            raise CombinedStartupError(f"The saved {target.name} PID belongs to an unexpected process; nothing was signalled.")
        _verify(target, identity)
        found.append((target, identity))
    # Validate every live target before terminating either one.
    for target, _ in found:
        _check_idle(target, private)
    stopped = []
    if not check_only:
        for target, identity in found:
            try:
                _check_idle(target, private)
                _verify(target, identity)
                os.kill(identity.pid, signal.SIGTERM)
                _wait_exit(identity, timeout)
                stopped.append(target.name)
            except (OSError, CombinedStartupError) as error:
                prefix = f"Already closed: {', '.join(stopped)}. " if stopped else ""
                detail = str(error) if isinstance(error, CombinedStartupError) else "Graceful shutdown could not be sent; no force-kill was attempted."
                raise CombinedStartupError(prefix + detail) from None
    return {"checked": [target.name for target, _ in found], "stopped": stopped, "absent": absent}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check and gracefully close only this installation's idle legacy dashboard and Mac service. Busy or unreachable servers abort; state is preserved.")
    parser.add_argument("--installation", required=True, type=Path)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--check-only", action="store_true", help="Read process identity and idle state; send no signal.")
    args = parser.parse_args(argv)
    try:
        result = retire_idle_legacy(args.installation, args.repo, check_only=args.check_only)
    except CombinedStartupError as error:
        print(str(error), file=sys.stderr)
        return 1
    print("Legacy startup check passed." if args.check_only else "Legacy startup preparation complete.")
    if result["stopped"]:
        print("Gracefully closed: " + ", ".join(result["stopped"]) + ".")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
