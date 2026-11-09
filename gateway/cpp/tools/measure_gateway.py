"""
Measure the memory of a gateway process: idle, and while clients pull
camera frames. Works on Windows (ctypes) and Linux (/proc).

The gateway gets the mock camera and robot, strict mode, and RWS on a
closed port; --width/--height only reach gateways that read the frame
size (the Python one through launch_python_1080p.py).

Usage (from the repository root):
    python gateway/cpp/tools/measure_gateway.py [--width 1920 --height 1080] [--seconds 20] [--threads 4]
        [--cmd "<command that starts the gateway on {port}>"]

    # C++ (in the toolchain container)
    python gateway/cpp/tools/measure_gateway.py --width 640 --height 480 \
        --cmd "gateway/cpp/build/release-size/gateway --port {port}"

    # Python at 1080p, a fresh buffer per frame like the OAK producer
    python gateway/cpp/tools/measure_gateway.py \
        --cmd "python gateway/cpp/tools/launch_python_1080p.py {port}"

On Windows, run it with the base interpreter (not .venv's python.exe,
which is a launcher process) and PYTHONPATH=.venv/Lib/site-packages.

Baseline of 2026-11-09 (Linux arm64 Docker, 4 clients): Python 58 MB
RSS at 640x480, 73/89 MB idle/peak at 1080p, 262/303 MB with depthai
imported; C++ (release-size, 640x480 mock) 3.1 MB.
"""

import argparse
import ctypes
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import requests

sys.path.insert(0, os.getcwd())

from gateway.tests.support import token_for, write_auth_clients  # noqa: E402


def memory(pid: int) -> dict[str, float]:
    """Resident and private memory in MB, and the peak resident size."""

    if sys.platform == "win32":
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t),
            ]

        handle = ctypes.windll.kernel32.OpenProcess(0x1000 | 0x0010, False, pid)
        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
        ctypes.windll.kernel32.CloseHandle(handle)

        return {
            "rss": counters.WorkingSetSize / 2**20,
            "private": counters.PrivateUsage / 2**20,
            "peak_rss": counters.PeakWorkingSetSize / 2**20,
        }

    fields = {}

    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        key, _, value = line.partition(":")

        if value.strip().endswith("kB"):
            fields[key] = int(value.split()[0]) / 1024

    return {
        "rss": fields["VmRSS"],
        "private": fields.get("RssAnon", 0.0),
        "peak_rss": fields["VmHWM"],
    }


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))

        return sock.getsockname()[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--seconds", type=float, default=20)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--cmd", default=None)
    args = parser.parse_args()

    port = free_port()
    directory = Path(tempfile.mkdtemp())
    auth = directory / "auth_clients.json"
    write_auth_clients(auth)

    environment = {
        **os.environ,
        "GATEWAY_SECURITY_MODE": "strict",
        "GATEWAY_AUTH_CLIENTS_FILE": str(auth),
        "GATEWAY_CALIBRATION_FILE": str(directory / "calibration.json"),
        "GATEWAY_CAMERA_PROVIDER": "mock",
        "GATEWAY_ROBOT_PROVIDER": "mock",
        "GATEWAY_ROBOT_HOST": "127.0.0.1",
        "GATEWAY_RWS_PORT": str(free_port()),
        "GATEWAY_CAMERA_RGB_WIDTH": str(args.width),
        "GATEWAY_CAMERA_RGB_HEIGHT": str(args.height),
    }

    command = (
        shlex.split(args.cmd.format(port=port))
        if args.cmd
        else [sys.executable, "-m", "uvicorn", "gateway.main:app", "--host", "127.0.0.1", "--port", str(port)]
    )

    process = subprocess.Popen(command, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}/api/v1"

    try:
        for _ in range(300):
            try:
                if requests.get(f"{base}/health", timeout=2).ok:
                    break
            except requests.RequestException:
                time.sleep(0.2)

        time.sleep(5)
        idle = memory(process.pid)

        headers = {"Authorization": f"Bearer {token_for('diagnostics')}"}
        stop = threading.Event()
        frames = [0]
        sizes = set()

        def pull() -> None:
            session = requests.Session()

            while not stop.is_set():
                for path in ("/camera/rgb", "/camera/depth"):
                    response = session.get(f"{base}{path}", headers=headers, timeout=10)

                    if response.ok:
                        frames[0] += 1
                        sizes.add((path, len(response.content)))

        threads = [threading.Thread(target=pull, daemon=True) for _ in range(args.threads)]

        for thread in threads:
            thread.start()

        loaded_max = idle
        deadline = time.monotonic() + args.seconds

        while time.monotonic() < deadline:
            sample = memory(process.pid)

            if sample["rss"] > loaded_max["rss"]:
                loaded_max = sample

            time.sleep(0.25)

        stop.set()

        for thread in threads:
            thread.join()

        final = memory(process.pid)

        print(f"frame size {args.width}x{args.height}, {args.threads} clients, {args.seconds:g} s, {frames[0]} frames served")
        print(f"bytes per response: {sorted(sizes)}")
        print(f"idle:        rss {idle['rss']:7.1f} MB  private {idle['private']:7.1f} MB")
        print(f"under load:  rss {loaded_max['rss']:7.1f} MB  private {loaded_max['private']:7.1f} MB (max sample)")
        print(f"peak rss over run: {final['peak_rss']:7.1f} MB")

    finally:
        process.terminate()
        process.wait(timeout=15)


if __name__ == "__main__":
    main()
