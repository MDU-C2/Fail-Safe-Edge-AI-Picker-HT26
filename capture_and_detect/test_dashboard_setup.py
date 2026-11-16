"""Portable setup tests: fake remote state, private temporary files, no hardware."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch
import warnings
import zipfile
from contextlib import redirect_stdout

import dashboard_setup as setup
from device_agent import HandoffStore


class DashboardSetupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.source = self.base / "source computer"
        self.source.mkdir()
        self.state = self.source / "old-private-state"
        self.state.mkdir()
        self.runtime = self.source / "current runtime"
        self.runtime.mkdir()
        (self.runtime / "captures").mkdir()
        self.owner = "a" * 32
        self.record = {"owner_id": self.owner, "generation": 9, "phase": "active", "device_id": "lynx",
                       "target_id": None, "handoff": None,
                       "context": {"boot_id": "b" * 32, "owner_id": self.owner, "generation": 9}}
        self.calibration = b'{\r\n  "fixture": "calibration"\r\n}\r\n'
        self.reference = setup._encode({"schema_version": 1, "kind": "placement_table_reference",
            "identity": {"calibration_sha256": hashlib.sha256(self.calibration).hexdigest()}}).replace(b"\n", b"\r\n")
        (self.runtime / "calibration.json").write_bytes(self.calibration)
        (self.runtime / "grid.table-reference.json").write_bytes(self.reference)
        self.grid = {"cell_size_mm": 90, "locations": [{"id": 1, "x": 420, "y": -200, "z": 230, "occupied": True},
                                                       {"id": 2, "x": 500, "y": -200, "z": 230, "occupied": False}]}
        self.write(self.runtime / "grid.json", self.grid)
        self.write(self.runtime / "captures/robot_recovery.json", {"schema_version": 1, "recovery_required": False})
        self.write(self.state / "device-owner.json", {"owner_id": self.owner, "highest_generation": 8, "active_generation": None})
        self.write(self.state / "device-motion.json", {"speed": 250, "home_speed": 200,
            "camera_z": False, "handover_offset": [0, 0, 0],
            "phase_speeds": {"pick_lift": 100, "handover": None, "flip": 120, "placement": None}})
        (self.state / "gateway-token").write_bytes(b"dummy-gateway-credential-for-offline-tests\r\n")
        self.write(self.source / "robot_controller.json", {"schema_version": 1,
            "gateway_api": "http://gateway.invalid:8000/api/v1", "controller_url": "http://controller.invalid"})
        self.write(self.source / "owner.json", self.record)
        for name in ("pi", "lynx"):
            (self.source / f"{name}.token").write_text(f"dummy-{name}-service-token-" + "x" * 32)
        self.config = {"schema_version": 1, "initial_device": "mac", "state_file": "owner.json", "devices": {
            "mac": {"backend": "local", "state_dir": str(self.state), "model": "/old/computer/best.pt",
                    "gateway": "http://gateway.invalid:8000", "robot_id": "robot-test-001",
                    "controller_profile": "robot_controller.json", "max_result_age": 30,
                    "runtime_paths": {"placing_grid": str(self.runtime / "grid.json"),
                        "calibration": str(self.runtime / "calibration.json"), "output": str(self.runtime / "captures")}},
            "pi": {"url": "http://pi.invalid:8091", "token_file": "pi.token"},
            "lynx": {"url": "http://lynx.invalid:8091", "token_file": "lynx.token"}}}
        self.config_path = self.source / "devices.json"
        self.write(self.config_path, self.config)
        identity = HandoffStore(self.state, "robot-test-001", runtime_paths={
            "placing_grid": self.runtime / "grid.json", "calibration": self.runtime / "calibration.json",
            "output": self.runtime / "captures", "table_reference": self.runtime / "grid.table-reference.json"}).identity()
        self.remote = {}
        for name in ("pi", "lynx"):
            active = name == "lynx"
            self.remote[name] = {"health": {"device_id": name, "boot_id": "b" * 32 if active else "c" * 32,
                "identity": identity, "owner": {"owner_id": self.owner, "highest_generation": 9 if active else 7,
                    "active_generation": 9 if active else None}, "mode": "live", "controller_present": active,
                "ready": active, "busy": False, "transfer_incomplete": False, "handoff_blocked": False},
                "state": {"ready": True, "busy": False, "stop_requested": False, "stop_in_flight": False,
                    "recovery_required": False, "proposal": None, "robot_controls": {"proposal": None},
                    "placement_grid": copy.deepcopy(self.grid)} if active else None}
        self.requests = []
        self.after_request = None
        self.bundle = self.base / "private-transfer.zip"
        self.model = self.base / "new clone" / "best.pt"
        self.model.parent.mkdir()
        self.model.write_bytes(b"offline-model-placeholder")
        # Excluded material must never enter the portable setup.
        for path in (self.runtime / "captures/rgb_private.jpg", self.runtime / "captures/saved_cups.json",
                     self.state / "assistant-settings.json"):
            path.write_text("EXCLUDED-PRIVATE-MATERIAL")
        self.write(self.state / "device-runtime-binding.json", {"schema_version": 1,
            "paths": setup.digest({"grid": str(self.runtime / "grid.json"), "output": str(self.runtime / "captures"),
                "calibration": str(self.runtime / "calibration.json"), "table_reference": str(self.runtime / "grid.table-reference.json")}),
            "occupancy": setup.digest([[p["id"], p["occupied"]] for p in self.grid["locations"]])})

    def write(self, path, value):
        path.write_bytes(setup._encode(value))

    def clients(self, url, token):
        name = "pi" if "pi.invalid" in url else "lynx"
        self.assertEqual(token, (self.source / f"{name}.token").read_text().strip())
        def request(path, body=None, **kwargs):
            self.assertEqual(path, "/v1/state")
            self.assertIsNone(body)
            self.requests.append(name)
            result = copy.deepcopy(self.remote[name])
            if self.after_request:
                self.after_request(name, len(self.requests))
            return result
        return Mock(request=request)

    def export(self):
        return setup.export_bundle(self.config_path, self.bundle, client_factory=self.clients)

    def entries(self):
        with zipfile.ZipFile(self.bundle) as archive:
            return {name: archive.read(name) for name in archive.namelist()}

    def rewrite(self, files, *, manifest=True):
        if manifest and "manifest.json" in files:
            metadata = json.loads(files["manifest.json"])
            metadata["sha256"] = {name: hashlib.sha256(raw).hexdigest()
                for name, raw in files.items() if name != "manifest.json"}
            files["manifest.json"] = setup._encode(metadata)
        with zipfile.ZipFile(self.bundle, "w") as archive:
            for name, raw in files.items():
                archive.writestr(name, raw)

    def test_export_is_read_only_exact_and_excludes_models_captures_and_api_settings(self):
        before = {path.relative_to(self.source): path.read_bytes() for path in self.source.rglob("*") if path.is_file()}
        result = self.export()
        self.assertEqual(result["active_device"], "lynx")
        self.assertEqual(self.requests, ["pi", "lynx", "pi", "lynx"])
        self.assertEqual(before, {path.relative_to(self.source): path.read_bytes()
                                for path in self.source.rglob("*") if path.is_file()})
        files = self.entries()
        self.assertEqual(files["local-state/calibration.json"], self.calibration)
        self.assertEqual(files["local-state/placing-grid.table-reference.json"], self.reference)
        self.assertEqual(files["dashboard-owner.json"], (self.source / "owner.json").read_bytes())
        self.assertFalse(any(b"EXCLUDED-PRIVATE-MATERIAL" in raw for raw in files.values()))
        portable = json.loads(files["devices.json"])
        self.assertEqual(portable["initial_device"], "lynx")
        self.assertNotIn("runtime_paths", portable["devices"]["mac"])
        self.assertNotIn(str(self.source).encode(), files["devices.json"])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.bundle.stat().st_mode), 0o600)

    def test_install_rebases_both_computers_preserving_bytes_and_ownership_without_network(self):
        self.export()
        with patch("requests.request", side_effect=AssertionError("No install network")), \
             patch("requests.get", side_effect=AssertionError("No install network")):
            for name in ("Windows PC", "Returning MacBook"):
                destination = self.base / name
                result = setup.install_bundle(self.bundle, destination, model_path=self.model)
                self.assertEqual(result["active_device"], "lynx")
                config = json.loads((destination / "devices.json").read_bytes())
                self.assertEqual(config["devices"]["mac"]["model"], str(self.model))
                self.assertEqual(config["devices"]["mac"]["state_dir"], "local-state")
                self.assertEqual(config["devices"]["pi"]["token_file"], "tokens/pi.token")
                self.assertEqual((destination / "dashboard-owner.json").read_bytes(), (self.source / "owner.json").read_bytes())
                self.assertEqual((destination / "local-state/calibration.json").read_bytes(), self.calibration)
                self.assertEqual((destination / "local-state/placing-grid.table-reference.json").read_bytes(), self.reference)
                self.assertEqual(HandoffStore(destination / "local-state", "robot-test-001").identity(),
                                 self.remote["lynx"]["health"]["identity"])
                self.assertFalse((destination / "local-state/device-runtime-binding.json").exists())
                if os.name != "nt":
                    self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)
                    self.assertEqual(stat.S_IMODE((destination / "tokens/pi.token").stat().st_mode), 0o600)

    def test_standard_installed_runtime_can_be_exported_again_without_absolute_binding(self):
        self.export()
        destination = self.base / "portable installed state"
        setup.install_bundle(self.bundle, destination, model_path=self.model)
        result = setup.export_bundle(destination / "devices.json", self.base / "return.zip", client_factory=self.clients)
        self.assertEqual(result["active_device"], "lynx")
        self.assertFalse((destination / "local-state/device-runtime-binding.json").exists())

    def test_local_active_or_unfinished_dashboard_refuses_before_remote_reads(self):
        for field, value in (("device_id", "mac"), ("phase", "deactivate_pending"), ("handoff", {})):
            record = dict(self.record, **{field: value})
            self.write(self.source / "owner.json", record)
            with self.subTest(field=field), self.assertRaises(setup.SetupError):
                self.export()
            self.assertEqual(self.requests, [])
            self.assertFalse(self.bundle.exists())

    def test_local_recovery_reservation_pending_or_active_owner_refuses_without_mutation(self):
        cases = [(self.runtime / "captures/robot_recovery.json", {"schema_version": 1, "recovery_required": True}),
                 (self.state / "device-handoff-pending.json", {}),
                 (self.state / "device-owner.json", {"owner_id": self.owner, "highest_generation": 8, "active_generation": 8}),
                 (self.runtime / "grid.json", {"locations": [dict(self.grid["locations"][0], reserved=True)]})]
        for path, value in cases:
            original = path.read_bytes() if path.exists() else None
            self.write(path, value)
            with self.subTest(path=path.name), self.assertRaises(setup.SetupError):
                self.export()
            self.assertEqual(path.read_bytes(), setup._encode(value))
            if original is None:
                path.unlink()
            else:
                path.write_bytes(original)
        self.assertEqual(self.requests, [])

    def test_missing_journal_checks_legacy_without_writing_a_recovery_file(self):
        journal = self.runtime / "captures/robot_recovery.json"
        journal.unlink()
        self.write(self.runtime / "captures/cups.json", {"cups": [{"status": "command_pending"}]})
        with self.assertRaisesRegex(setup.SetupError, "legacy recovery"):
            self.export()
        self.assertFalse(journal.exists())
        self.write(self.runtime / "captures/cups.json", {"cups": [{"status": "placed"}]})
        self.export()
        self.assertFalse(journal.exists())
        self.assertFalse(json.loads(self.entries()["local-state/captures/robot_recovery.json"])["recovery_required"])

    def test_remote_busy_proposal_recovery_foreign_owner_or_identity_refuses(self):
        baseline = copy.deepcopy(self.remote)
        changes = [("health", "busy", True), ("health", "handoff_blocked", True),
                   ("health", "ready", False), ("health", "boot_id", "d" * 32),
                   ("health", "identity", {}), ("state", "proposal", {"token": "pending"}),
                   ("state", "recovery_required", True), ("state", "stop_in_flight", True),
                   ("health", "owner", {"owner_id": "f" * 32, "highest_generation": 9, "active_generation": 9})]
        for category, field, value in changes:
            self.remote = copy.deepcopy(baseline)
            self.remote["lynx"][category][field] = value
            with self.subTest(field=field), self.assertRaises(setup.SetupError):
                self.export()
            self.assertFalse(self.bundle.exists())

    def test_changes_between_probes_abort_export(self):
        def change(name, count):
            if count == 2:
                self.remote["pi"]["health"]["boot_id"] = "d" * 32
        self.after_request = change
        with self.assertRaisesRegex(setup.SetupError, "changed"):
            self.export()
        self.assertFalse(self.bundle.exists())

    def test_custom_runtime_occupancy_requires_existing_matching_binding(self):
        binding = self.state / "device-runtime-binding.json"
        original = binding.read_bytes()
        binding.unlink()
        with self.assertRaisesRegex(setup.SetupError, "reconciliation"):
            self.export()
        binding.write_bytes(original)
        changed = copy.deepcopy(self.grid)
        changed["locations"][1]["occupied"] = True
        self.write(self.runtime / "grid.json", changed)
        with self.assertRaisesRegex(setup.SetupError, "reconciliation"):
            self.export()
        self.assertEqual(self.requests, [])
        self.assertTrue(json.loads((self.runtime / "grid.json").read_bytes())["locations"][1]["occupied"])
        self.assertFalse(self.bundle.exists())

    def test_changes_in_source_after_probes_abort_export(self):
        def mutate_source(name, count):
            if count == 4:
                self.write(self.source / "owner.json", dict(self.record, generation=10))
        self.after_request = mutate_source
        with self.assertRaisesRegex(setup.SetupError, "Source state changed"):
            self.export()
        self.assertFalse(self.bundle.exists())

    def test_probe_error_redacts_server_response_and_credentials(self):
        secret = "dummy-secret-that-must-never-be-displayed"
        with patch("device_dashboard.DeviceClient", return_value=Mock(request=Mock(side_effect=RuntimeError(secret)))):
            output = io.StringIO()
            with redirect_stdout(output):
                result = setup.main(["export", "--config", str(self.config_path), "--output", str(self.bundle)])
        self.assertEqual(result, 1)
        self.assertNotIn(secret, output.getvalue())
        self.assertIn("Could not verify pi", output.getvalue())

    def test_destinations_inside_git_existing_outputs_and_missing_model_are_rejected(self):
        repository = self.base / "repository"
        repository.mkdir()
        (repository / ".git").write_text("gitdir: elsewhere")
        with self.assertRaisesRegex(setup.SetupError, "outside every Git"):
            setup.export_bundle(self.config_path, repository / "private.zip", client_factory=self.clients)
        self.export()
        with self.assertRaisesRegex(setup.SetupError, "already exists"):
            self.export()
        with self.assertRaisesRegex(setup.SetupError, "outside every Git"):
            setup.install_bundle(self.bundle, repository / "private", model_path=self.model)
        with self.assertRaisesRegex(setup.SetupError, "already exists"):
            setup.install_bundle(self.bundle, self.source, model_path=self.model)
        with self.assertRaisesRegex(setup.SetupError, "model is missing"):
            setup.install_bundle(self.bundle, self.base / "missing", model_path=self.base / "missing.pt")

    def test_bundle_traversal_checksums_unknown_entries_and_invalid_options_are_rejected(self):
        self.export()
        original = self.entries()
        for case in ("traversal", "unknown", "checksum", "settings", "owner"):
            files = dict(original)
            if case == "traversal": files["../escape"] = b"bad"
            elif case == "unknown": files["assistant-settings.json"] = b"bad"
            elif case == "checksum": files["local-state/calibration.json"] += b" "
            elif case == "settings":
                config = json.loads(files["devices.json"])
                config["devices"]["mac"]["threads"] = True
                files["devices.json"] = setup._encode(config)
            elif case == "owner":
                record = json.loads(files["dashboard-owner.json"])
                record["phase"] = "new"
                files["dashboard-owner.json"] = setup._encode(record)
            self.rewrite(files, manifest=case != "checksum")
            destination = self.base / case
            with self.subTest(case=case), self.assertRaises(setup.SetupError):
                setup.install_bundle(self.bundle, destination, model_path=self.model)
            self.assertFalse(destination.exists())
        self.assertFalse((self.base / "escape").exists())

    def test_duplicate_symlink_and_oversized_zip_entries_are_rejected(self):
        self.export()
        original = self.entries()
        for case in ("duplicate", "symlink", "oversized"):
            files = dict(original)
            if case != "duplicate":
                del files["local-state/gateway-token"]
            self.rewrite(files)
            with warnings.catch_warnings(), zipfile.ZipFile(self.bundle, "a") as archive:
                warnings.simplefilter("ignore", UserWarning)
                info = zipfile.ZipInfo("local-state/gateway-token")
                if case == "symlink": info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, b"x" * (setup.MAX_FILE + 1 if case == "oversized" else 1))
            with self.subTest(case=case), self.assertRaises(setup.SetupError):
                setup.install_bundle(self.bundle, self.base / case, model_path=self.model)


if __name__ == "__main__":
    unittest.main()
