"""Packaging checks never connect to the camera or robot."""
import importlib.util
import hashlib
import json
from pathlib import Path
import tempfile
import unittest


spec = importlib.util.spec_from_file_location("prepare_context", Path(__file__).with_name("prepare_context.py"))
prepare_context = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare_context)


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.model = self.root / "model"
        files = {
            "gateway/main.py": "pass\n", "gateway/.env": "SECRET=do-not-copy",
            "gateway/auth_clients.json": "private", "gateway/calibration.json": "private",
            "gateway/camera/service.py": "pass\n", "gateway/tests/test_camera.py": "pass\n",
            "capture_and_detect/web_app.py": "pass\n", "capture_and_detect/test_web.py": "pass\n",
            "capture_and_detect/placing_grid.json": "live-state", "capture_and_detect/best.pt": "weights",
            "capture_and_detect/web_static/index.html": "<!doctype html>",
            "capture_and_detect/web_static/secret.json": "private",
            "lynx/app.py": "pass\n", "lynx/native/CMakeLists.txt": "project(test)",
            "lynx/native/worker.cpp": "int main() {}", "lynx/deploy/Dockerfile": "FROM scratch",
            "lynx/deploy/start.sh": "#!/bin/sh\n", "lynx/deploy/requirements-arm64.lock": "",
            "lynx/deploy/measure_resources.py": "pass\n",
            "lynx/deploy/Dockerfile.cli": "FROM scratch\n# cli profile\n",
            "lynx/deploy/start-cli.sh": "#!/bin/sh\nexec python -m lynx.picker\n",
            "lynx/deploy/requirements-cli-arm64.lock": "requests==2.32.5\n",
            "lynx/native/placement.cpp": "// native grid\n",
            "lynx/native/vendor/nlohmann/json.hpp": "// vendored header\n",
            "lynx/native/vendor/nlohmann/LICENSE.MIT": "MIT License\n",
            "lynx/native/vendor/nlohmann/README.md": "source hashes\n",
            "capture_and_detect/capture_and_detect.py": "import cv2\nimport numpy\n",
            "capture_and_detect/depth_average.py": "import cv2\nimport numpy\n",
            "capture_and_detect/picker_workflow.py": "import cv2\nimport numpy\n",
            "capture_and_detect/picker_depth.py": "import cv2\nimport numpy\n",
            "capture_and_detect/placement_vision.py": "import cv2\nimport numpy\n",
            "capture_and_detect/placing_grid_outside_tray.json": "live-state",
            "capture_and_detect/placing_grid_outside_tray.table-reference.json": "private-reference",
            "capture_and_detect/Data/Detections/robot_recovery.json": "live-recovery",
            "lynx/gateway-token": "private-token",
        }
        self.cli_control = {
            "camera_client", "cup_commands", "cup_inventory", "handover_plan",
            "placing_grid", "recovery_journal", "robot_client", "robot_controls",
            "controller_status", "gateway_capabilities", "terminal_controller",
            "terminal_commands", "operator_commands", "language_commands", "local_llm",
            "table_reference_contract", "web_controller", "device_agent", "device_service", "placement_records",
            "workspace_check", "queued_robot_client", "failover_coordinator", "failover_workflow", "failover_worker",
        }
        self.cli_runtime = {
            "__init__", "picker", "capture", "raw_images", "model", "resources",
            "placement_records", "native_transport", "native_perception", "runtime", "runtime_lock", "standby", "armed_service",
        }
        files.update({"capture_and_detect/" + name + ".py": "# shared control\n"
                      for name in self.cli_control})
        files.update({"lynx/" + name + ".py": "# lean runtime\n"
                      for name in self.cli_runtime})
        for name, content in files.items():
            target = self.repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        self.model.mkdir()
        for name in prepare_context.MODEL_REQUIRED:
            (self.model / name).write_text("prepared")
        (self.model / "unrelated.pt").write_text("private")

    def test_context_excludes_machine_secrets_and_live_state(self):
        output = self.root / "context"
        files = prepare_context.stage(self.repo, self.model, output)
        self.assertIn("app/gateway/camera/service.py", files)
        self.assertIn("app/capture_and_detect/picker_workflow.py", files)
        self.assertIn("app/capture_and_detect/picker_depth.py", files)
        self.assertIn("model/manifest.json", files)
        for forbidden in (".env", "auth_clients.json", "calibration.json", "placing_grid.json", "best.pt", "unrelated.pt", "secret.json"):
            self.assertFalse(any(path.name == forbidden for path in output.rglob("*")), forbidden)
        self.assertFalse(any(path.name.startswith("test_") for path in output.rglob("*")))
        manifest = json.loads((output / "source-manifest.json").read_text())
        self.assertEqual(files, manifest["sha256"])

    def test_refuses_symlinked_source(self):
        source = self.repo / "gateway/main.py"
        source.unlink()
        source.symlink_to(self.repo / "gateway/.env")
        with self.assertRaisesRegex(ValueError, "regular source file"):
            prepare_context.stage(self.repo, self.model, self.root / "context")

    def test_refuses_existing_output_and_unprepared_model(self):
        with self.assertRaisesRegex(ValueError, "already exists"):
            prepare_context.stage(self.repo, self.model, self.root)
        (self.model / "manifest.json").unlink()
        with self.assertRaisesRegex(ValueError, "prepare a validated model"):
            prepare_context.stage(self.repo, self.model, self.root / "context")

    def test_cli_stages_only_control_runtime_and_selects_cli_build_inputs(self):
        output = self.root / "cli-context"
        files = prepare_context.stage(self.repo, self.model, output, profile="cli")
        app_files = {name for name in files if name.startswith("app/")}
        expected = {"app/capture_and_detect/" + name + ".py" for name in self.cli_control}
        expected.update("app/lynx/" + name + ".py" for name in self.cli_runtime)
        self.assertEqual(app_files, expected)
        for staged, source in (("Dockerfile", "Dockerfile.cli"),
                               ("start.sh", "start-cli.sh"),
                               ("requirements-arm64.lock", "requirements-cli-arm64.lock")):
            self.assertEqual((output / staged).read_bytes(),
                             (self.repo / "lynx/deploy" / source).read_bytes())
        self.assertIn("native/placement.cpp", files)
        for name in ("json.hpp", "LICENSE.MIT", "README.md"):
            self.assertIn("native/vendor/nlohmann/" + name, files)
        self.assertFalse((output / "app/gateway").exists())
        self.assertFalse((output / "app/capture_and_detect/web_static").exists())
        for forbidden in ("app.py", "web_app.py", "capture_and_detect.py", "depth_average.py",
                          "picker_workflow.py", "picker_depth.py", "placement_vision.py",
                          ".env", "gateway-token", "robot_recovery.json", "placing_grid_outside_tray.json",
                          "placing_grid_outside_tray.table-reference.json", "unrelated.pt"):
            self.assertFalse(any(path.name == forbidden for path in output.rglob("*")), forbidden)
        manifest = json.loads((output / "source-manifest.json").read_text())
        self.assertEqual(manifest["sha256"], files)
        for name, digest in files.items():
            self.assertEqual(hashlib.sha256((output / name).read_bytes()).hexdigest(), digest, name)

    def test_cli_refuses_symlinked_runtime_dependency(self):
        source = self.repo / "capture_and_detect/camera_client.py"
        source.unlink()
        source.symlink_to(self.repo / "gateway/.env")
        with self.assertRaisesRegex(ValueError, "regular source file"):
            prepare_context.stage(self.repo, self.model, self.root / "context", profile="cli")

    def test_unknown_profile_is_rejected_before_creating_context(self):
        output = self.root / "context"
        with self.assertRaisesRegex(ValueError, "Profile must"):
            prepare_context.stage(self.repo, self.model, output, profile="unknown")
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
