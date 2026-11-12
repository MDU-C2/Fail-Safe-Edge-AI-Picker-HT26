"""Packaging checks never connect to the camera or robot."""
import importlib.util
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
        }
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


if __name__ == "__main__":
    unittest.main()
