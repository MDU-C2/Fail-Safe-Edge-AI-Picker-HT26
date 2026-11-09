# C++ gateway

A port of the Python gateway (`gateway/`) to C++, for devices with
little RAM and storage: the target is the Westermo Lynx (about 500 MB
RAM shared with other processes; WeOS gives all apps together 25 %
of it by default, and web uploads of 100 MB or more fail). The port
keeps the HTTP API, settings, clients file and behaviour of the Python
gateway, so clients do not notice which one they talk to, and the
Python test suite runs against both.

Linux only; built and tested in Docker (WSL on the laptop).

## Build and test

Everything runs in the toolchain container (`docker/Dockerfile`,
target `toolchain`) through `dev.sh`; nothing needs installing in WSL
but Docker. From Windows prefix each command with
`wsl -d Ubuntu-22.04 -u root --`.

```sh
gateway/cpp/dev.sh toolchain          # once: compilers, Boost headers, Python test deps
gateway/cpp/dev.sh build              # dev preset -> gateway/cpp/build/dev/gateway
gateway/cpp/dev.sh test               # C++ unit tests (GoogleTest, ctest)
gateway/cpp/dev.sh pytest             # Python suite against the C++ binary (--impl cpp)
gateway/cpp/dev.sh pytest-python      # the same suite against the Python gateway, on Linux
gateway/cpp/dev.sh sanitize           # unit tests + suite under ASan/UBSan and TSan
gateway/cpp/dev.sh build release-size # the static, size-optimised binary the images ship
gateway/cpp/dev.sh image              # runtime image gateway-cpp (FROM scratch)
```

Presets (`CMakePresets.json`): `dev`, `release-size` (`-Os`, LTO,
section GC, stripped, static), `asan` (address + UB sanitizer),
`tsan` (thread sanitizer). Build trees live in the Docker volume
`gateway-cpp-build`, not on `/mnt/c`, which is slow.

The test suite chooses the implementation with `--impl python|cpp`
(or `GATEWAY_IMPL`) and `--cpp-binary` (or `GATEWAY_CPP_BINARY`);
see `gateway/tests/implementation.py`. With `--impl cpp`, modules
that test Python internals are skipped (their C++ ports are in
`tests/`), and so are the modules of routes not ported yet
(`NOT_PORTED_TO_CPP` in `gateway/tests/conftest.py`; a line goes
when its milestone is done).

Run the binary like the Python gateway, from the repository root
(it reads `gateway/.env` and `gateway/auth_clients.json` relative to
the working directory):

```sh
gateway/cpp/build/dev/gateway --host 127.0.0.1 --port 8000
```

The runtime image holds only the binary, at `/sbin/init` (where WeOS
app hosting starts an app). Give it a clients file:

```sh
docker run --rm -p 8000:8000 -v $PWD/gateway/auth_clients.json:/auth_clients.json:ro \
    -e GATEWAY_AUTH_CLIENTS_FILE=/auth_clients.json \
    -e GATEWAY_CAMERA_PROVIDER=mock -e GATEWAY_ROBOT_PROVIDER=mock gateway-cpp
```

## Layout

| Path | Python counterpart |
|---|---|
| `src/main.cpp` | `uvicorn gateway.main:app` (arguments, signals) |
| `src/app.*` | `gateway/main.py` (wiring, routes) |
| `src/config/` | `config.py` (+ the `.env` parser of python-dotenv) |
| `src/http/` | FastAPI/uvicorn: router, `{"detail"}` errors, 422s, server |
| `src/security/` | `security/` |
| `src/control/` | `control/` |
| `src/events/` | `events/` (bus, permissions, the `/events` WebSocket) |
| `src/camera/` | `camera/` |
| `src/calibration/` | `calibration/` (same file format, so either reads the other's file) |
| `src/robot/` | `robot/` |
| `src/rws/` | `rws/` |
| `src/health.cpp` | `health.py` |
| `openapi/openapi.json` | FastAPI's schema, exported by `tools/export_openapi.py` and served as is |
| `tests/` | GoogleTest; ports of the Python unit tests plus new ones |
| `tools/` | schema export, memory measurement |

Change a Python route or model, then re-export the schema:
`python gateway/cpp/tools/export_openapi.py` (Windows venv is fine).

## Dependencies

| Need | Library | Notes |
|---|---|---|
| HTTP server | Boost.Beast + Asio 1.83 | header-only, from the toolchain's Debian packages |
| RWS client | libcurl 8.11.1 | static, HTTP only, digest auth, cookies; no TLS, proxy, compression |
| JSON | nlohmann/json 3.11.3 | |
| SHA-256 | own, `src/util/sha256.cpp` | checked against hashlib |
| Tests | GoogleTest 1.15.2 | |
| OAK camera | depthai-core 3.10.0 (M6) | built from source with the optional parts off |

Pinned in `cmake/Dependencies.cmake`, fetched into the build tree.

## Design decisions

- **One thread per connection.** A handler may block (a move waits
  up to `GATEWAY_RWS_MOVE_TIMEOUT` for the arm) without holding up
  any other connection, so a lease renewal never waits behind a
  move. Threads get a 256 KB stack; connections are capped at 64.
- **WebSockets never block publishers.** A WebSocket keeps its
  connection thread but runs on a private event loop there;
  `publish()` only queues the message. A client that falls 1000
  messages behind is dropped, and an idle one is pinged and closed
  after 20 s without an answer, so a vanished client frees its
  thread. (The Python gateway awaits every client's send.)
- **The lease runs on the monotonic clock.** Expiry, contention
  window and startup grace cannot be moved by an NTP step on the
  device; the wall clock only gives the reported timestamps.
- **Frames are never copied.** The camera service keeps one shared,
  immutable buffer per frame; responses send from it.
- **Same wire format.** `{"detail": ...}` errors with the same
  texts, 422 bodies in FastAPI's shape, datetimes as
  `2026-11-09T12:00:00.123456Z` (always six digits), and
  `/openapi.json` from the Python app. `/health` adds
  `"implementation": "cpp"`; there is no `/docs`.

## Migration status

| Step | Content | Status |
|---|---|---|
| M0 | CMake, presets, toolchain image, server, settings, `/openapi.json`, `/health` | done |
| M1 | Clients file, auth, control lease and routes | done: `test_auth`, `test_control`, C++ registry/lease tests |
| M2 | Camera service, mock, camera routes; calibration | done: `test_camera`, `test_calibration` (incl. a file written by either implementation read by the other), C++ calibration tests |
| M3 | Events WebSocket | done: `test_events` (+ a new test that 100 closed connections are all released) |
| M4 | `POST /robot/command` (mock + CommServer TCP) | open (also unblocks `test_failover`) |
| M5 | RWS arms, RAPID, motion routes, safe zone | open (client and error parsing done) |
| M6 | OAK producer on a minimal depthai-core build, reconnect | open |
| M7 | arm64/armv7 images, `.wmopkg`, WeOS seccomp check | open |

## Measurements

Linux arm64 Docker, 4 clients pulling frames (`tools/measure_gateway.py`):

| | Python | C++ |
|---|---|---|
| RSS, 640x480 mock | 58 MB | 3.1 MB |
| RSS, real camera path | 262 MB idle, 303 MB peak (`import depthai`) | M6 |
| Image | 860 MB | 2.9 MB at M2 (`FROM scratch`, one static binary: 2.0 MB at M2, 2.4 MB with WebSockets at M3) |
| `.wmopkg` payload (xz squashfs) | 146 MB (104 MB stripped of junk) | 0.7 MB |
