"""Exercise the complete shared app with a real model and synthetic camera.

Run as python -m lynx.tests.runtime_smoke --model DIR --worker EXECUTABLE.
Requires development FastAPI/httpx dependencies; never contacts hardware.
"""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time

from fastapi.testclient import TestClient

from lynx.app import arguments, create_combined
from lynx.prepare_state import prepare


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--worker", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="lynx-runtime-smoke-") as temporary:
        root = Path(temporary)
        calibration = root / "calibration.json"
        calibration.write_text(json.dumps({"scale":1,"R":[[1,0,0],[0,1,0],[0,0,1]],"t":[0,0,-500]}))
        grid = root / "grid.json"
        grid.write_text(json.dumps({"locations":[{"id":1,"x":400,"y":-200,"z":230,"occupied":True}]}))
        state = prepare(root / "state", calibration, grid)
        settings = arguments(["--state-dir",str(state),"--model",str(args.model.resolve()),
                              "--worker",str(args.worker.resolve()),"--mock"])
        with TestClient(create_combined(settings), base_url="http://127.0.0.1:8080") as client:
            def idle():
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    value = client.get("/api/state").json()
                    if not value["busy"]:
                        assert value["ready"] and value["error"] is None, value
                        return value
                    time.sleep(.05)
                raise AssertionError("Combined application did not become idle before deadline")
            view = idle()
            assert view["mode"] == "dry_run" and view["recovery_required"]
            auth = {"Authorization": "Bearer " + (state / "gateway-token").read_text().strip()}
            assert client.get("/api/v1/camera/status", headers=auth).status_code == 200
            assert client.get("/api/v1/camera/status").status_code == 401
            assert client.post("/api/v1/control/acquire", headers=auth, json={}).status_code == 403
            headers = {"Origin":"http://127.0.0.1:8080","X-CSRF-Token":view["csrf_token"]}
            response = client.post("/api/scan", headers=headers, json={"request_id":"runtime_smoke_scan_01"})
            assert response.status_code == 202, response.text
            view = idle()
            inventory = json.loads((state / "captures/cups.json").read_text())
            assert inventory["placement_observation"]["frame_count"] == 3
            assert "torch" not in sys.modules and "ultralytics" not in sys.modules
            print(json.dumps({"ready":view["ready"],"mode":view["mode"],"scan_frames":3,
                "cup_candidates":len(inventory["cups"]), "inference":inventory["model"]["inference"],
                "torch_loaded":False,"ultralytics_loaded":False,"gateway_mutations_blocked":True}, indent=2))


if __name__ == "__main__":
    main()
