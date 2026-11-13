# Mac inference with Raspberry Pi fallback

The Pi runs the gateway, camera client, calibration, cup selection, placement
state, recovery journal and robot coordinator. The Mac runs only a stateless
segmentation service. The Pi keeps a second segmentation model loaded in a
separate local process. A failed or late Mac request falls back to that process.
Lynx is not involved.

Changing inference provider does not replace the robot coordinator or authorize
a new movement. An accepted sequence remains with the Pi. New operations still
need valid perception and the existing confirmation and execution checks.

## Prepare the two hosts

1. Stop the old Mac picker/web controller. Run only one robot coordinator, on
   the Pi. The Mac process in this guide does not require a gateway token.
2. Put the same existing `best.pt` on both hosts. The files' SHA-256 digests must
   match. The model must include `CUP`, `CUP BASE` and `CUP HANDLE`.
3. Keep the current calibration, placement grid and recovery state on the Pi
   before live use. During migration, inspect the physical robot and reconcile
   the existing authoritative state. Do not overwrite it with a repository
   default, clear reservations, or run two writable copies of the live grid.
   Keep these files and the output directory on persistent storage if using
   containers. A dry-run directory is never a substitute for live state.
4. Keep the established gateway configuration and camera/robot networking. Only
   inference traffic travels between the Mac and Pi. The Pi must reach the Mac
   on the worker port; unplugging the Mac must not disconnect the Pi's camera
   or controller network.
5. Install the optional worker dependencies in a dedicated environment on both
   hosts. Run from `capture_and_detect`:

   ```sh
   python3 -m venv .venv-inference
   . .venv-inference/bin/activate
   python -m pip install -r requirements-inference.txt
   ```

   Choose a Python version supported by the installed PyTorch wheels on the Pi.
   The requirements file provides compatible version ranges; it is not a
   reproducibility lock. Resolve and pin the same NumPy, OpenCV, Ultralytics and
   PyTorch versions on both hosts after verifying their platform wheels. Record
   the environment with `python -m pip freeze`. Local development checks used
   Python 3.13, NumPy 2.5.4, OpenCV 5.0.0.93, Ultralytics 8.4.178 and PyTorch
   2.14.1; this does not certify those wheels for your Pi installation.

The worker refuses missing weights; starting it does not download a model.
Inference uses explicit CPU settings on both hosts: 640-pixel square
preprocessing, confidence 0.5, IoU 0.7, full precision, original-image masks and
the three cup classes. If you change confidence or image size, change both
commands together. Model digest/configuration mismatches are rejected.

## Start the Mac worker

From `capture_and_detect` on the Mac, activate its environment and set a new,
long random token shared only with the Pi inference client. Replace the example
value; it must be different from `GATEWAY_TOKEN`.

```sh
export INFERENCE_TOKEN='<shared-random-inference-token>'
python inference_worker.py \
  --model ./best.pt \
  --host 0.0.0.0 --port 8090 \
  --threads 3 --conf 0.5 --imgsz 640 \
  --request-timeout 10
```

The default host is `127.0.0.1`; `--host 0.0.0.0` explicitly permits connections
from the Pi. Permit port 8090 only on the trusted experiment network. HTTP does
not encrypt the token or images: use an isolated trusted LAN, SSH forwarding,
or a TLS reverse proxy. The server exposes authenticated `/health` and `/infer`
only. It has no robot commands, calibration or placement state.

Keep this worker process running when testing a cable disconnection. If the Mac
also has Wi-Fi, a second Ethernet adapter or another route to the Pi, pulling
one cable may leave inference reachable. Disable the alternate experiment
route when the intended fault is total Mac network loss.

## Update the Pi gateway

Live fallback requires the updated gateway's active lease watchdog and execution
session checks. Set `GATEWAY_CONTROL_REQUIRE_SESSION=true` in the gateway's
persistent deployment configuration. With the robot inspected and stationary,
restart the existing single gateway through its normal deployment procedure;
do not launch a second gateway on the same controller.

For an existing Python deployment, the equivalent launch command from the
repository root is:

```sh
GATEWAY_CONTROL_REQUIRE_SESSION=true python3 -m uvicorn gateway.main:app \
  --host 127.0.0.1 --port 8000
```

Use the gateway's own environment, dependencies, authentication and configured
camera/controller settings. For an existing container deployment, set that
environment variable in the gateway container configuration instead. This guide
does not rebuild, replace or start that container automatically.

The updated gateway starts with recovery required. After inspecting both arms,
grippers, work area and pending work, use the existing dashboard **Robot
controls** to perform the explicit inspected RAPID reset/start procedure.
The application recovery acknowledgement (`reset`) and clearing a placement
reservation are separate actions. Neither is an automatic consequence of
reconnection. Do not use a new empty state directory to bypass recovery.

The watchdog inhibits new commands when the coordinator lease expires and
requests a software stop. If completion cannot be confirmed, it stays blocked.
A software stop is not a safety-rated emergency stop.

## Start with camera-connected dry-run on the Pi

Use an independent test output directory and a copy of the current grid. The
commands below preserve an existing test copy and never overwrite the live grid:

```sh
mkdir -p Data/DegradationDryRun
cp -n placing_grid.json Data/DegradationDryRun/placing_grid.json
export GATEWAY_TOKEN='<existing-pi-operator-token>'
export INFERENCE_TOKEN='<same-inference-token-as-the-mac>'
python capture_and_detect.py --web \
  --gateway http://127.0.0.1:8000 --dry-run \
  --mac-inference-url http://MAC_IP:8090 \
  --inference-model ./best.pt \
  --inference-threads 3 --inference-image-size 640 --conf 0.5 \
  --mac-timeout 2 --pi-timeout 5 \
  --mac-probe-interval 3 --mac-recovery-successes 3 \
  --output ./Data/DegradationDryRun \
  --placing-grid ./Data/DegradationDryRun/placing_grid.json \
  --calibration ../calibration/calibration.json
```

Replace `MAC_IP` with the Mac address reachable from the Pi. Keep the process
running in a Pi terminal session that survives an SSH disconnect (for example,
an existing `tmux` session) or use your established service manager. Running the
coordinator inside a disposable SSH session would test coordinator failure as
well as Mac inference loss.

The dashboard binds to Pi loopback. From the Mac, open a separate tunnel:

```sh
ssh -N -L 8080:127.0.0.1:8080 kasun@rpi5.local
```

Open `http://localhost:8080`. `rpi5.local` is the discovered Pi hostname and
`kasun` is its login; enable the intended SSH key on the Pi before using that
key for the tunnel. The Pi process loads and warms the local model before it is ready.
The dashboard initially has no accepted inference source; use **Scan
workspace** to produce one. A local development run on a Mac is labelled
`local`, not `Pi`. Live Mac/Pi fallback is permitted only on a Linux ARM64
coordinator and requires the updated gateway capability check.

The same inference flags are available on `capture_and_detect.py` for
terminal operation. Use `--dry-run` there too during development.

For a camera-only cable experiment, `check_inference_switching.py` reads fresh
paired RGB/depth frames and exercises the two inference workers directly. It
imports no robot controller, calibration or placement code, and its gateway
transport permits only camera status/RGB/depth GETs. From a persistent Pi
terminal session, with both tokens set and the Mac worker running:

```sh
python check_inference_switching.py \
  --gateway http://127.0.0.1:8000 \
  --mac-inference-url http://MAC_IP:8090 --model ./best.pt \
  --output ./Data/CameraSwitchTest-01 \
  --observations 30 --interval 2 \
  --mac-timeout 2 --pi-timeout 5 --threads 3 --probe-interval 3
```

Use a new or empty output directory. Each accepted observation prints one
flushed JSON record with its sequence, provider, duration, detection count,
counters and recovery progress. Unplug and reconnect the Mac during this run.
The Pi saves `observations.jsonl`, durable `inference-events.jsonl`, and a final
`summary.json`, including failures or interruption. This checker requires no
web server and never acquires robot control or sends robot commands.

## Demonstrate switching and return

1. **Baseline:** scan while the Mac worker is reachable. Check that the accepted
   source is **Mac**, the last result has an observation ID and duration, and
   the Mac accepted-result counter increases.
2. **Disconnect:** pull the Mac network cable while the Pi, camera and gateway
   remain powered and connected. Request another scan from a device still
   connected to the Pi, or run an already planned dry-run operation from a Pi
   terminal. A preapproved live batch can request its next observation without
   a browser connection, once separate live validation is complete.
3. **Confirm fallback:** the next Mac request fails or reaches its total
   deadline. The state changes to switching, then **Pi active** only after a
   local result is validated and accepted. The Pi accepted-result counter
   increases, and the history records the failure and accepted Pi observation.
4. **Reconnect:** restore the Mac link and keep its worker running. Pi inference
   remains selected while the recovery display records successful test
   predictions (default three, separated by the probe interval).
5. **Confirm return:** after successful probes, the next clean observation group
   selects the Mac. **Mac active** and its accepted-result counter change only
   when a normal Mac prediction is accepted. Probe successes alone do not
   increment the accepted-result counter or prove that robot perception has returned.
6. **Fail both providers in a dry-run test:** stop the Mac worker and exercise
   a deliberately unavailable local test backend through the automated tests.
   The supervisor must report unavailable and reject perception-dependent work.
   Do not kill or overload the production Pi as a substitute for this test.

A grid scan uses three observations. If fallback changes provider mid-scan,
the scan discards its partial observations and restarts with the selected
provider. Return to the Mac waits for a clean observation-group boundary. This
avoids combining grid observations from different providers.

The counters count validated normal inference results, including a frame later
discarded when a grid scan restarts. They exclude recovery probes. They do not
count completed scans, picked cups, or robot actions; use the scan/task records
to establish those outcomes.

Unplugging the Mac also disconnects its dashboard tunnel. That browser must
show a disconnected/stale state; it cannot prove the Pi's current state while
offline. Watch from a second computer that stays connected to the Pi (with its
own SSH tunnel), use the Pi terminal, or reconnect and inspect the durable
history. The history is stored on the Pi, by default at:

```text
OUTPUT/inference-events.jsonl
```

For the dry-run command above, `OUTPUT` is `Data/DegradationDryRun`. Override
the path with `--inference-events PATH`. A Pi terminal can follow it directly:

```sh
tail -f Data/DegradationDryRun/inference-events.jsonl
```

Read the event's accepted provider, observation/request identity and time;
distinguish accepted normal results from recovery probes. Saved captures also
record accepted inference metadata. Retain/copy an experiment log after the
run for the report; the application bounds its retained event history.

## Live operation after dry-run validation

Stop the dry-run coordinator, inspect and reconcile the real robot and its
authoritative state, then run the same Pi web command with `--live` instead of
`--dry-run`. Set `--output`, `--placing-grid` and `--calibration` to the inspected
authoritative Pi paths, not the dry-run copies. Keep the current state files
and reservations intact. Complete the explicit gateway/application recovery
procedure and take a fresh scan before confirming a task.

The new inference mode requires the existing explicit task confirmation. It
does not retry robot commands, replay an interrupted job, clear recovery, or
open a gripper after a disconnection. Mac inference loss alone does not cancel
an already accepted robot sequence that can finish without new perception.
If new perception is required and neither provider succeeds, that operation
is blocked.

The core scan/numbered-cup workflow does not need Ollama. Leave `--llm` off
initially. Saved-cup identity matching that depends on an unavailable model
does not acquire a weaker fallback identity check.

## Timing, limitations and tests

`--mac-timeout 2` and `--pi-timeout 5` are initial full-request budgets, not
measured guarantees. Measure capture, image encoding/transfer, prediction,
validation and the three-frame grid scan with both hosts running the full
application. Keep the existing 15-second pre-grip observation-age checks.
Do not raise that limit simply to hide slow fallback. Tune inference threads
and deadlines while measuring gateway and stop responsiveness under load.
The worker's `--request-timeout` and the local/remote backend deadline values
must be finite, greater than zero and at most 300 seconds. The example worker
limit of 10 seconds includes request-body reception and local prediction;
the Pi's shorter Mac deadline can still discard that request first.

Each backend accepts one outstanding job, with no queue of stale images. A
late response cannot replace an accepted result. HTTP redirects and
environment proxies are disabled. Both blocking network I/O and local model
execution run in killable child processes; deadline expiry discards their
results. A timed-out local model must reload before another result, within the
next request's budget. Persistent timeout is reported as unavailable, rather
than retried indefinitely inside a single perception operation.

Switching covers Mac inference failure. Losing Pi power also loses the
coordinator and gateway; controller-side behavior for that case must be
established separately. Camera/depth failure, stale observations and uncertain
robot command completion are not repaired by changing the inference provider.

From `capture_and_detect`, run the worker's hardware-free regression checks:

```sh
python -m pip install httpx
python -m unittest test_inference_workers -v
```

These checks use synthetic images, fake model processes and a temporary
loopback HTTP server. They cover exact-frame/configuration validation,
authentication, redirects, malformed/oversized responses, stalled inference,
slow-drip network responses, process restart and closing an active request.
They do not contact robot hardware or download weights. The loopback transport
checks need permission to bind a local ephemeral port.

A saved-image integration check also ran the existing weights through the real
HTTP worker, stopped that worker to trigger local inference, restarted it,
verified three recovery probes, and accepted Mac inference again. All three
accepted results contained the same eight detections. Both processes ran on
one Mac during that check; it is evidence of protocol/fallback behavior, not a
measurement of Pi performance or physical network disconnection.

Implementation and automated checks do not deploy software to the Pi or
exercise the real robot. A supervised lab trial is still required to establish
actual switch latency, full-path performance and physical execution behavior.
