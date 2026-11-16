"""Export/install private portable dashboard state; never activate or move a device."""
import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
from urllib.parse import urlsplit
import zipfile

from device_agent import digest, validate_motion
from placing_grid import _unique_object, _invalid_constant, validate_place

HERE = Path(__file__).resolve().parent
MAX_FILE = 2 * 1024 * 1024
MAX_BUNDLE = 16 * 1024 * 1024
BASE_FILES = {"devices.json", "dashboard-owner.json", "tokens/pi.token", "tokens/lynx.token",
              "local-state/calibration.json", "local-state/placing-grid.json",
              "local-state/placing-grid.table-reference.json", "local-state/gateway-token",
              "local-state/controller-status.json", "local-state/captures/robot_recovery.json"}
OPTIONAL_FILES = {"local-state/device-owner.json", "local-state/device-motion.json"}
LOCAL_OPTIONS = {"conf", "depth_frames", "max_result_age", "inference_timeout", "threads", "speed", "camera_z"}


class SetupError(ValueError):
    pass


def _fail(message):
    raise SetupError(message)


def _json(raw):
    try:
        return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        _fail("A setup document contains invalid JSON.")


def _encode(value):
    return (json.dumps(value, indent=2, allow_nan=False) + "\n").encode()


def _read(path, *, optional=False):
    """Bounded regular-file read, including a post-open check on Windows."""
    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode):
            _fail("Setup inputs must be regular files, not links.")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(path, flags), "rb") as source:
            opened, current = os.fstat(source.fileno()), os.lstat(path)
            if (not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode)
                    or not os.path.samestat(before, opened) or not os.path.samestat(opened, current)):
                _fail("A setup input changed while being read.")
            raw = source.read(MAX_FILE + 1)
        if len(raw) > MAX_FILE:
            _fail("A setup input exceeds its size limit.")
        return raw
    except FileNotFoundError:
        if optional:
            return None
        _fail("A required setup file is missing; check the selected device configuration.")
    except OSError:
        _fail("A setup input could not be read.")


def _outside_repository(path):
    resolved = Path(path).resolve()
    if any((parent / ".git").exists() for parent in (resolved, *resolved.parents)):
        _fail("Choose a private destination outside every Git repository; this setup contains credentials.")
    return resolved


def _path(base, value):
    if not isinstance(value, str) or not value.strip():
        _fail("A configured setup path is invalid.")
    return base / value


def _origin(value):
    try:
        parsed = urlsplit(value)
        if (not isinstance(value, str) or len(value) > 2048 or any(c.isspace() for c in value)
                or "\\" in value or parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ("", "/") or (parsed.port is not None and parsed.port < 1)):
            raise ValueError
    except (TypeError, ValueError, AttributeError):
        _fail("A device or gateway address is invalid; use an HTTP(S) origin without credentials.")


def _token(raw, *, service=False):
    try:
        value = raw.decode("ascii").strip()
        if len(raw) > 4096 or not (32 if service else 1) <= len(value) <= (512 if service else 4096) or any(not 33 <= ord(c) <= 126 for c in value):
            raise ValueError
        return value
    except (ValueError, UnicodeError):
        _fail("A private access token is invalid.")


def _owner(value, dashboard_owner):
    if (not isinstance(value, dict) or set(value) != {"owner_id", "highest_generation", "active_generation"}
            or value["owner_id"] not in (None, dashboard_owner)
            or type(value["highest_generation"]) is not int or value["highest_generation"] < 0
            or ((value["owner_id"] is None) != (value["highest_generation"] == 0))
            or (value["active_generation"] is not None and (type(value["active_generation"]) is not int
                or value["active_generation"] <= 0 or value["active_generation"] != value["highest_generation"]))):
        _fail("Device ownership is invalid or belongs to another dashboard.")


def _record(value):
    if (not isinstance(value, dict) or set(value) != {"owner_id", "generation", "phase", "device_id", "target_id", "context", "handoff"}
            or not re.fullmatch(r"[0-9a-f]{32}", str(value.get("owner_id", "")))
            or type(value.get("generation")) is not int or value["generation"] < 1):
        _fail("The persisted dashboard ownership record is invalid.")
    if value["device_id"] == "mac":
        _fail("Select Raspberry Pi or Lynx and wait until it is ready before exporting; the local computer must be inactive.")
    context = value["context"]
    if (value["phase"] != "active" or value["device_id"] not in ("pi", "lynx")
            or value["target_id"] is not None or value["handoff"] is not None
            or not isinstance(context, dict) or set(context) != {"boot_id", "owner_id", "generation"}
            or not re.fullmatch(r"[0-9a-f]{32}", str(context.get("boot_id", "")))
            or context.get("owner_id") != value["owner_id"] or type(context.get("generation")) is not int
            or context.get("generation") != value["generation"]):
        _fail("Finish the current device handoff before exporting.")


def _clean_recovery(raw):
    value = _json(raw)
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1 or value.get("recovery_required") is not False
            or ("context" in value and not isinstance(value["context"], dict))):
        _fail("Resolve local robot recovery before exporting.")


def _clean_legacy(raw):
    if raw is None:
        return
    value = _json(raw)
    cups = value.get("cups") if isinstance(value, dict) else value
    if not isinstance(cups, list):
        _fail("The local legacy recovery inventory is invalid.")
    for cup in cups:
        if (not isinstance(cup, dict) or (cup.get("status") is not None and not isinstance(cup["status"], str))
                or cup.get("status") in ("movement_unconfirmed", "command_pending")
                or ("execution" in cup and (not isinstance(cup["execution"], dict)
                    or cup["execution"].get("state") != "completed"))):
            _fail("Resolve local legacy recovery before exporting.")


def _identity(files, local):
    grid = _json(files["local-state/placing-grid.json"])
    if not isinstance(grid, dict) or not isinstance(grid.get("locations"), list) or not 1 <= len(grid["locations"]) <= 64:
        _fail("The local placement grid is invalid.")
    ids, coordinates = set(), set()
    for place in grid["locations"]:
        try:
            validate_place(place)
        except ValueError:
            _fail("The local placement grid is invalid.")
        if place.get("reserved") or place["id"] in ids or tuple(place[k] for k in ("x", "y", "z")) in coordinates:
            _fail("Resolve placement reservations or invalid grid entries before exporting.")
        ids.add(place["id"])
        coordinates.add(tuple(place[k] for k in ("x", "y", "z")))
    size = grid.get("cell_size_mm")
    if size is not None and (type(size) not in (int, float) or not 20 <= size <= 200):
        _fail("The placement cell size is invalid.")
    calibration = files["local-state/calibration.json"]
    reference = _json(files["local-state/placing-grid.table-reference.json"])
    if (not isinstance(_json(calibration), dict) or not isinstance(reference, dict)
            or reference.get("schema_version") != 1 or reference.get("kind") != "placement_table_reference"
            or not isinstance(reference.get("identity"), dict)
            or reference["identity"].get("calibration_sha256") != hashlib.sha256(calibration).hexdigest()):
        _fail("Calibration bytes do not match the saved table reference; repair or recalibrate before exporting.")
    return {"robot_id": local["robot_id"], "geometry": digest({"locations": [
        {k: p[k] for k in ("id", "x", "y", "z")} for p in grid["locations"]], "cell_size_mm": size}),
        "calibration": hashlib.sha256(calibration).hexdigest(),
        "table_reference": hashlib.sha256(files["local-state/placing-grid.table-reference.json"]).hexdigest()}


def _validate_files(files):
    if not BASE_FILES.issubset(files) or set(files) - BASE_FILES - OPTIONAL_FILES:
        _fail("The setup bundle has missing or unexpected files.")
    config, record = _json(files["devices.json"]), _json(files["dashboard-owner.json"])
    _record(record)
    if (not isinstance(config, dict) or set(config) != {"schema_version", "initial_device", "state_file", "devices"}
            or config["schema_version"] != 1 or config["initial_device"] != record["device_id"]
            or config["state_file"] != "dashboard-owner.json" or not isinstance(config["devices"], dict)
            or set(config["devices"]) != {"mac", "pi", "lynx"}):
        _fail("The portable device configuration is invalid.")
    local = config["devices"]["mac"]
    required = {"backend", "state_dir", "model", "gateway", "robot_id", "controller_profile"}
    if (not isinstance(local, dict) or not required.issubset(local) or set(local) - required - LOCAL_OPTIONS
            or local["backend"] != "local" or local["state_dir"] != "local-state" or local["model"] != "best.pt"
            or local["controller_profile"] != "local-state/controller-status.json"
            or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", str(local["robot_id"]))):
        _fail("The portable local-computer configuration is invalid.")
    _origin(local["gateway"])
    options = {"conf": .5, "depth_frames": 5, "max_result_age": 30, "inference_timeout": 30,
               "threads": 3, "speed": None, "camera_z": False}
    options.update({key: local[key] for key in LOCAL_OPTIONS if key in local})
    def number(value, minimum, maximum, *, strict_min=False):
        return (type(value) in (int, float) and math.isfinite(value)
                and (minimum < value if strict_min else minimum <= value) and value <= maximum)
    if (not number(options["conf"], 0, 1, strict_min=True) or options["conf"] == 1
            or any(type(options[key]) is not int or not 1 <= options[key] <= limit
                   for key, limit in (("depth_frames", 9), ("threads", 32)))
            or any(not number(options[key], 0, 300, strict_min=True) for key in ("max_result_age", "inference_timeout"))
            or type(options["camera_z"]) is not bool
            or (options["speed"] is not None and not number(options["speed"], 1, 1500))):
        _fail("The portable local perception or movement settings are invalid.")
    for name in ("pi", "lynx"):
        remote = config["devices"][name]
        if not isinstance(remote, dict) or set(remote) != {"url", "token_file"} or remote["token_file"] != f"tokens/{name}.token":
            _fail("The portable remote-device configuration is invalid.")
        _origin(remote["url"])
        _token(files[f"tokens/{name}.token"], service=True)
    _token(files["local-state/gateway-token"])
    profile = _json(files["local-state/controller-status.json"])
    if (not isinstance(profile, dict) or set(profile) != {"schema_version", "gateway_api", "controller_url"}
            or profile["schema_version"] != 1 or profile["gateway_api"] != local["gateway"].rstrip("/") + "/api/v1"):
        _fail("The controller profile must match the configured gateway.")
    _origin(profile["controller_url"])
    _clean_recovery(files["local-state/captures/robot_recovery.json"])
    if "local-state/device-owner.json" in files:
        owner = _json(files["local-state/device-owner.json"])
        _owner(owner, record["owner_id"])
        if owner["active_generation"] is not None:
            _fail("The local computer still owns an active generation; select Pi or Lynx first.")
    if "local-state/device-motion.json" in files:
        try:
            validate_motion(_json(files["local-state/device-motion.json"]))
        except ValueError:
            _fail("The saved local movement settings are invalid.")
    return config, record, _identity(files, local)


def _probe(client, name, record, identity):
    try:
        result = client.request("/v1/state", timeout=3)
    except Exception:
        _fail(f"Could not verify {name}; check its connection and export again. No command was sent.")
    health = result.get("health") if isinstance(result, dict) else None
    if (not isinstance(health, dict) or health.get("device_id") != name or health.get("identity") != identity
            or health.get("mode") not in ("live", "dry_run")
            or not re.fullmatch(r"[0-9a-f]{32}", str(health.get("boot_id", "")))
            or any(health.get(key) is not False for key in ("busy", "transfer_incomplete", "handoff_blocked"))):
        _fail(f"{name} is busy, blocked, or has different calibration/geometry; resolve it before exporting.")
    owner = health.get("owner")
    _owner(owner, record["owner_id"])
    active = name == record["device_id"]
    if active:
        if (health.get("ready") is not True or health.get("controller_present") is not True
                or health["boot_id"] != record["context"]["boot_id"]
                or owner["owner_id"] != record["owner_id"] or owner["active_generation"] != record["generation"]):
            _fail("The active device no longer matches saved dashboard ownership; refresh the dashboard before exporting.")
        state = result.get("state")
        controls = state.get("robot_controls") if isinstance(state, dict) else None
        grid = state.get("placement_grid") if isinstance(state, dict) else None
        if (not isinstance(state, dict) or state.get("ready") is not True
                or any(state.get(key) is not False for key in ("busy", "recovery_required", "stop_requested", "stop_in_flight"))
                or state.get("proposal") or not isinstance(controls, dict) or controls.get("proposal")
                or not isinstance(grid, dict) or not isinstance(grid.get("locations"), list)
                or any(not isinstance(p, dict) or p.get("reserved") for p in grid["locations"])):
            _fail("Finish or cancel pending work and resolve recovery before exporting.")
    elif owner["active_generation"] is not None or health.get("controller_present") is not False or result.get("state") is not None:
        _fail("The other remote device is still active; resolve device ownership before exporting.")
    return copy.deepcopy({key: health[key] for key in ("boot_id", "owner", "identity", "mode")})


def export_bundle(config_path, output_path, *, client_factory=None):
    output = _outside_repository(output_path)
    if output.exists():
        _fail("The output bundle already exists; choose a new filename.")
    snapshots = {}

    def read(path, optional=False):
        path = Path(path).absolute()
        raw = _read(path, optional=optional)
        snapshots[path] = raw
        return raw

    path = Path(config_path).absolute()
    source = _json(read(path))
    if (not isinstance(source, dict) or set(source) != {"schema_version", "initial_device", "state_file", "devices"}
            or source["schema_version"] != 1 or not isinstance(source["devices"], dict)
            or set(source["devices"]) != {"mac", "pi", "lynx"}):
        _fail("Choose a combined dashboard configuration with an in-process local computer.")
    local = source["devices"]["mac"]
    if not isinstance(local, dict) or local.get("backend") != "local":
        _fail("This export requires the combined dashboard's local computer backend.")
    files = {"dashboard-owner.json": read(_path(path.parent, source["state_file"]))}
    record = _json(files["dashboard-owner.json"])
    _record(record)
    root = _path(path.parent, local.get("state_dir"))
    runtime = local.get("runtime_paths", {})
    if not isinstance(runtime, dict) or set(runtime) - {"placing_grid", "calibration", "output", "table_reference"}:
        _fail("The local runtime paths are invalid.")
    grid = _path(path.parent, runtime["placing_grid"]) if "placing_grid" in runtime else root / "placing-grid.json"
    calibration = _path(path.parent, runtime["calibration"]) if "calibration" in runtime else root / "calibration.json"
    output_root = _path(path.parent, runtime["output"]) if "output" in runtime else root / "captures"
    reference = _path(path.parent, runtime["table_reference"]) if "table_reference" in runtime else grid.with_suffix(".table-reference.json")
    if reference.absolute() != grid.with_suffix(".table-reference.json").absolute():
        _fail("The table reference must be paired with the placement grid.")
    if read(root / "device-handoff-pending.json", True) is not None:
        _fail("Resolve the unfinished local state transfer before exporting.")
    for name, original in {
        "calibration.json": calibration,
        "placing-grid.json": grid, "placing-grid.table-reference.json": reference,
        "gateway-token": root / "gateway-token",
        "controller-status.json": _path(path.parent, local["controller_profile"]) if "controller_profile" in local else root / "controller-status.json"}.items():
        files["local-state/" + name] = read(original)
    for name in ("device-owner.json", "device-motion.json"):
        raw = read(root / name, True)
        if raw is not None:
            files["local-state/" + name] = raw
    recovery = read(output_root / "robot_recovery.json", True)
    if recovery is None:
        _clean_legacy(read(output_root / "cups.json", True))
        recovery = _encode({"schema_version": 1, "recovery_required": False})
    files["local-state/captures/robot_recovery.json"] = recovery
    portable = {"backend": "local", "state_dir": "local-state", "model": "best.pt",
                "gateway": local.get("gateway"), "robot_id": local.get("robot_id"),
                "controller_profile": "local-state/controller-status.json"}
    portable.update({key: local[key] for key in LOCAL_OPTIONS if key in local})
    config = {"schema_version": 1, "initial_device": record["device_id"], "state_file": "dashboard-owner.json",
              "devices": {"mac": portable}}
    for name in ("pi", "lynx"):
        remote = source["devices"][name]
        if not isinstance(remote, dict) or set(remote) != {"url", "token_file"}:
            _fail("A remote service configuration is invalid.")
        files[f"tokens/{name}.token"] = read(_path(path.parent, remote["token_file"]))
        config["devices"][name] = {"url": remote["url"], "token_file": f"tokens/{name}.token"}
    files["devices.json"] = _encode(config)
    _, _, identity = _validate_files(files)
    resolved = {"grid": str(grid.resolve()), "output": str(output_root.resolve()),
                "calibration": str(calibration.resolve()), "table_reference": str(reference.resolve())}
    standard = {"grid": str((root / "placing-grid.json").resolve()), "output": str((root / "captures").resolve()),
                "calibration": str((root / "calibration.json").resolve()),
                "table_reference": str((root / "placing-grid.table-reference.json").resolve())}
    if resolved != standard:
        binding = read(root / "device-runtime-binding.json", True)
        occupancy = [[p["id"], bool(p.get("occupied"))]
                     for p in _json(files["local-state/placing-grid.json"])["locations"]]
        expected = {"schema_version": 1, "paths": digest(resolved), "occupancy": digest(occupancy)}
        if binding is None or _json(binding) != expected:
            _fail("Local runtime records need reconciliation. Use the source dashboard's normal handoff to the local computer and back to Pi or Lynx, then export again.")
    if client_factory is None:
        from device_dashboard import DeviceClient
        client_factory = DeviceClient
    clients = {name: client_factory(config["devices"][name]["url"], _token(files[f"tokens/{name}.token"], service=True))
               for name in ("pi", "lynx")}
    first = {name: _probe(clients[name], name, record, identity) for name in clients}
    second = {name: _probe(clients[name], name, record, identity) for name in clients}
    if first != second or len({value["mode"] for value in second.values()}) != 1:
        _fail("Device state changed during export; wait until idle and export again.")
    for original, raw in snapshots.items():
        if _read(original, optional=raw is None) != raw:
            _fail("Source state changed during export; wait until idle and export again.")
    manifest = {"schema_version": 1, "identity": identity, "mode": second[record["device_id"]]["mode"],
                "sha256": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}}
    if sum(map(len, files.values())) + len(_encode(manifest)) > MAX_BUNDLE:
        _fail("The portable setup exceeds its total size limit.")
    # An exclusive private output prevents overwriting an earlier portable copy.
    try:
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        _fail("The bundle could not be created; choose a new file in an existing private directory.")
    try:
        with os.fdopen(descriptor, "wb") as target:
            with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, raw in {**files, "manifest.json": _encode(manifest)}.items():
                    archive.writestr(name, raw)
            target.flush()
            os.fsync(target.fileno())
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return {"bundle": str(output), "active_device": record["device_id"], "mode": manifest["mode"]}


def install_bundle(bundle_path, directory, *, model_path=None):
    destination = _outside_repository(directory)
    if destination.exists():
        _fail("The installation directory already exists; choose a new directory.")
    model = Path(model_path or HERE / "best.pt").resolve()
    if not model.is_file():
        _fail("The local model is missing; supply --model with this clone's best.pt file.")
    files = {}
    try:
        if Path(bundle_path).stat().st_size > MAX_BUNDLE:
            _fail("The setup bundle exceeds its size limit.")
        with zipfile.ZipFile(bundle_path) as archive:
            entries = archive.infolist()
            if (len(entries) > len(BASE_FILES | OPTIONAL_FILES) + 1
                    or sum(entry.file_size for entry in entries) > MAX_BUNDLE):
                _fail("The setup bundle exceeds its size limit.")
            for entry in entries:
                if (entry.filename not in BASE_FILES | OPTIONAL_FILES | {"manifest.json"}
                        or entry.filename in files or entry.file_size > MAX_FILE
                        or entry.flag_bits & 1 or stat.S_IFMT(entry.external_attr >> 16) not in (0, stat.S_IFREG)):
                    _fail("The setup bundle contains an unsafe, duplicate, or oversized entry.")
                with archive.open(entry) as source:
                    raw = source.read(MAX_FILE + 1)
                if len(raw) != entry.file_size or len(raw) > MAX_FILE:
                    _fail("A setup bundle entry exceeds its size limit.")
                files[entry.filename] = raw
    except (OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError):
        _fail("The setup bundle could not be read.")
    manifest = _json(files.pop("manifest.json", b"null"))
    if (not isinstance(manifest, dict) or set(manifest) != {"schema_version", "identity", "mode", "sha256"}
            or manifest["schema_version"] != 1 or manifest["mode"] not in ("live", "dry_run")
            or manifest["sha256"] != {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}):
        _fail("The setup bundle manifest or file checksums are invalid.")
    config, record, identity = _validate_files(files)
    if manifest["identity"] != identity:
        _fail("The setup bundle geometry identity is invalid.")
    config["devices"]["mac"]["model"] = str(model)
    files["devices.json"] = _encode(config)
    try:
        destination.mkdir(mode=0o700)
    except OSError:
        _fail("The new installation directory could not be created; choose a new name in an existing private directory.")
    try:
        for name, raw in files.items():
            target = destination / name
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as output:
                output.write(raw)
    except BaseException:
        shutil.rmtree(destination)
        raise
    return {"config": str(destination / "devices.json"), "active_device": record["device_id"], "mode": manifest["mode"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="Create a private portable setup while Pi or Lynx is selected and idle")
    export.add_argument("--config", required=True, type=Path)
    export.add_argument("--output", required=True, type=Path)
    install = commands.add_parser("install", help="Install into a new private folder without contacting devices")
    install.add_argument("--bundle", required=True, type=Path)
    install.add_argument("--directory", required=True, type=Path)
    install.add_argument("--model", type=Path, default=HERE / "best.pt")
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            result = export_bundle(args.config, args.output)
            print(f"Private setup saved: {result['bundle']}")
            print("Close the source dashboard before launching the installed copy. Export a fresh setup before returning to this computer.")
        else:
            result = install_bundle(args.bundle, args.directory, model_path=args.model)
            print(f"Installed configuration: {result['config']}")
            print(f"Existing {result['active_device']} ownership preserved. No device was contacted or activated.")
        return 0
    except SetupError as error:
        print(f"Setup blocked: {error}")
    except Exception:
        print("Setup blocked: could not complete the portable setup. No device command was sent; check the selected files.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
