# Fail-safe edge-AI picker

Project repository for the **DVA490 / DVA474 – Projektkurs i Robotik** course at Mälardalen University (MDU), autumn semester 2026 (HT2026).

Course center: [Projects @ Collaborative Center (C2) MDU](https://github.com/MDU-C2)

## Robot gateway

The gateway that every AI tier drives the YuMi and the OAK camera
through is the **C++ gateway** in [gateway/cpp/](gateway/cpp/README.md),
running on the Westermo Lynx ([gateway/cpp/LYNX.md](gateway/cpp/LYNX.md)).
The Python gateway in [gateway/](gateway/README_START.md) is **legacy**:
kept as the reference implementation and for its test suite, but no
longer updated. New gateway work goes into the C++ one.

## Cup picker entry points

Run from the repository root with the existing gateway token set:

```sh
# V4 capture workflow with smooth handover; Enter runs the physical pick-and-place cycle.
python capture_and_detect/capture_and_detect.py --gateway http://192.168.125.20:8000

# Existing browser UI and its separate enhanced workflow.
python capture_and_detect/web_app.py --gateway http://192.168.125.20:8000 --live

# UI preview without camera, gateway or robot.
python capture_and_detect/web_app.py --demo
```

`capture_and_detect.py` keeps the V4 capture/detection and terminal workflow,
with the same smooth dual-arm handover plan as the UI and no automatic Home.
`depth_average.py` remains unchanged from V4. UI services use
`picker_workflow.py` and `picker_depth.py`. The terminal has no dry-run mode or UI
reservations/recovery checks; it uses its original calibration and placement grid.
Both dual-arm workflows require the gateway's `/api/v1/robot/rws/path` endpoint
and the controller's loaded `RunPath` routine. Run only one controller at a time.
Use a separate
`--output capture_and_detect/Data/TerminalDetections` for terminal captures when
switching between workflows. Full commands and state differences are in the
[cup picker guide](capture_and_detect/README.md).
