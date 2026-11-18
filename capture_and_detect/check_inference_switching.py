"""Camera-only Mac/Pi switching experiment. No robot or placement operations."""

import argparse
import json
import math
import os
import platform
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np
import requests

from camera_client import CameraClient, CameraError
from inference_backends import ProcessBackend, RemoteBackend
from inference_protocol import (decode_detections, make_request, make_settings,
                                validate_response)
from inference_supervisor import InferenceSupervisor


class ReadOnlyCameraSession:
    """Only exact camera GETs; neither redirects nor proxy credentials escape."""
    def __init__(self, api):
        self.allowed = {api + suffix for suffix in ("/camera/status", "/camera/rgb", "/camera/depth")}
        self._session = requests.Session()
        self._session.trust_env = False

    def get(self, url, **kwargs):
        if url not in self.allowed:
            raise CameraError("Switching check permits only camera status/RGB/depth reads")
        return self._session.get(url, allow_redirects=False, **kwargs)

    def close(self):
        self._session.close()


def one_observation(camera, supervisor, model_hash, settings):
    """Retain one fresh, matching depth peer until exact-frame masks validate."""
    started = time.monotonic()
    rgb, depth, frame = camera.capture()
    captured = time.monotonic()
    if (not isinstance(depth, np.ndarray) or depth.dtype != np.uint16 or
            depth.ndim != 2 or depth.shape != rgb.shape[:2]):
        raise CameraError("Camera RGB/depth dimensions or depth format do not match")
    request = make_request(rgb, model_sha256=model_hash, settings=settings,
                           observation_id=f"{uuid.uuid4().hex}:{frame['sequence']}")
    response = supervisor.predict(request)
    validate_response(response, request)
    detections = decode_detections(response)
    if any(detection["mask"].shape != depth.shape for detection in detections):
        raise CameraError("Inference mask does not match the retained depth observation")
    metadata = response["_inference"]
    snapshot = supervisor.snapshot()
    return {"type": "accepted", "at": datetime.now(timezone.utc).isoformat(),
        "sequence": frame["sequence"], "camera_timestamp": frame.get("timestamp"),
        "request_id": request["request_id"], "observation_id": request["observation_id"],
        "provider": metadata["provider"], "duration_ms": metadata["duration_ms"],
        "capture_duration_ms": round((captured - started) * 1000, 2),
        "total_duration_ms": round((time.monotonic() - started) * 1000, 2),
        "detection_count": len(detections), "width": request["width"], "height": request["height"],
        "depth_shape": list(depth.shape), "valid_depth_pixels": int(np.count_nonzero(depth)),
        "counters": snapshot["counters"], "recovery": snapshot["recovery"]}


def _emit(value):
    print(json.dumps(value, allow_nan=False, separators=(",", ":")), flush=True)


def run_check(args, *, camera=None, local=None, remote=None,
              supervisor_factory=InferenceSupervisor, emit=_emit, sleep=time.sleep):
    """Injectable runner; every started resource closes on capture/startup failure."""
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Use a new or empty output directory for this camera-only experiment")
    records, supervisor, failure, outcome = [], None, None, "complete"
    started = datetime.now(timezone.utc).isoformat()
    settings = make_settings()
    try:
        if camera is None:
            api = args.gateway.rstrip("/") + "/api/v1"
            camera = CameraClient(api, {"Authorization": "Bearer " + os.environ["GATEWAY_TOKEN"]},
                                  session=ReadOnlyCameraSession(api))
        if local is None:
            local = ProcessBackend(args.model, settings, threads=args.threads)
        if remote is None:
            remote = RemoteBackend(args.mac_inference_url, os.environ["INFERENCE_TOKEN"])
        local_provider = "pi" if platform.system() == "Linux" and platform.machine().lower() in ("aarch64", "arm64") else "local"
        supervisor = supervisor_factory(local, remote, validate_response=validate_response,
            remote_timeout_s=args.mac_timeout, local_timeout_s=args.pi_timeout,
            probe_interval_s=args.probe_interval, event_path=output / "inference-events.jsonl",
            local_provider=local_provider)
        local.start()
        supervisor.start()
        emit({"type": "ready", "scope": "camera-only", "local_provider": local_provider,
              "observations_requested": args.observations, "model_sha256": local.model_hash})
        with (output / "observations.jsonl").open("a", encoding="utf-8") as journal:
            for index in range(args.observations):
                record = one_observation(camera, supervisor, local.model_hash, settings)
                record["index"] = index + 1
                journal.write(json.dumps(record, allow_nan=False) + "\n")
                journal.flush()
                os.fsync(journal.fileno())
                records.append(record)
                emit(record)
                if index + 1 < args.observations:
                    sleep(args.interval)
    except KeyboardInterrupt:
        outcome, failure = "interrupted", {"type": "KeyboardInterrupt", "message": "Experiment interrupted"}
    except Exception as error:
        outcome, failure = "failed", {"type": type(error).__name__, "message": str(error)[:300]}
        emit({"type": "error", **failure})
    finally:
        snapshot = supervisor.snapshot() if supervisor is not None else None
        if supervisor is not None:
            supervisor.close()
        else:
            for backend in (remote, local):
                if backend is not None:
                    backend.close()
        session = getattr(camera, "session", None)
        if session is not None and hasattr(session, "close"):
            session.close()
        summary = {"scope": "camera-only", "outcome": outcome, "started_at": started,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "observations_requested": args.observations, "observations_accepted": len(records),
            "providers_observed": list(dict.fromkeys(item["provider"] for item in records)),
            "failure": failure, "status_before_close": snapshot, "observations": records}
        temporary = output / ".summary.json.tmp"
        temporary.write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        temporary.replace(output / "summary.json")
        emit({"type": "summary", "outcome": outcome, "observations_accepted": len(records),
              "providers_observed": summary["providers_observed"], "path": str(output / "summary.json")})
    return 0 if outcome == "complete" else 130 if outcome == "interrupted" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", required=True, help="Gateway base URL; only camera GETs are permitted")
    parser.add_argument("--mac-inference-url", required=True)
    parser.add_argument("--model", default=str(Path(__file__).parent / "best.pt"))
    parser.add_argument("--output", required=True, help="New or empty experiment directory, separate from live state")
    parser.add_argument("--observations", type=int, default=12)
    parser.add_argument("--interval", type=float, default=2)
    parser.add_argument("--mac-timeout", type=float, default=2)
    parser.add_argument("--pi-timeout", type=float, default=5)
    parser.add_argument("--threads", type=int, default=3)
    parser.add_argument("--probe-interval", type=float, default=3)
    args = parser.parse_args(argv)
    if not 1 <= args.observations <= 1000:
        parser.error("--observations must be between 1 and 1000")
    if not 1 <= args.threads <= 32:
        parser.error("--threads must be between 1 and 32")
    for name in ("interval", "mac_timeout", "pi_timeout", "probe_interval"):
        value = getattr(args, name)
        if not math.isfinite(value) or not 0 < value <= 300:
            parser.error(f"--{name.replace('_', '-')} must be finite, greater than zero and at most 300")
    try:
        address = urlsplit(args.gateway)
        if address.scheme not in ("http", "https") or not address.hostname or address.username or address.password or address.query or address.fragment or address.path not in ("", "/"):
            raise ValueError()
    except ValueError:
        parser.error("--gateway must be an HTTP(S) base URL without credentials, path, query or fragment")
    if not os.environ.get("GATEWAY_TOKEN") or not os.environ.get("INFERENCE_TOKEN"):
        parser.error("Set GATEWAY_TOKEN for camera access and a separate INFERENCE_TOKEN for the Mac worker")
    if os.environ["GATEWAY_TOKEN"] == os.environ["INFERENCE_TOKEN"]:
        parser.error("GATEWAY_TOKEN and INFERENCE_TOKEN must be different")
    try:
        return run_check(args)
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
