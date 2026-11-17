"""Import and launcher checks that never initialize a camera, model or robot."""

from contextlib import ExitStack, redirect_stdout
import builtins
import importlib
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch


class PickerWorkflowEntrypointTests(unittest.TestCase):
    def test_fresh_import_needs_no_device_model_network_or_web_dependencies(self):
        path = Path(__file__).with_name("picker_workflow.py")
        spec = importlib.util.spec_from_file_location("_workflow_import_check", path)
        module = importlib.util.module_from_spec(spec)
        real_import = builtins.__import__

        def no_optional_import(name, *args, **kwargs):
            if name.split(".", 1)[0] in {"capture_and_detect", "depth_average", "ultralytics", "depthai", "fastapi", "uvicorn"}:
                raise AssertionError(f"Unexpected optional dependency at import: {name}")
            return real_import(name, *args, **kwargs)

        with ExitStack() as stack:
            stack.enter_context(patch("builtins.__import__", side_effect=no_optional_import))
            guarded = [stack.enter_context(patch(target, side_effect=AssertionError(target)))
                       for target in ("camera_client.CameraClient", "robot_client.RobotClient",
                                      "requests.sessions.Session.request", "socket.socket.connect",
                                      "subprocess.Popen", "builtins.input")]
            spec.loader.exec_module(module)
            self.assertTrue(callable(module.main))
            self.assertTrue(callable(module.Detector))
            self.assertTrue(callable(module.CaptureService))
            for guard in guarded:
                guard.assert_not_called()

    def test_dashboard_and_python_adapters_never_import_the_frozen_terminal(self):
        code = """
import importlib.abc
import sys

blocked = {'capture_and_detect', 'depth_average', 'ultralytics', 'torch'}
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in blocked:
            raise AssertionError('Unexpected terminal dependency: ' + fullname)
sys.meta_path.insert(0, Guard())

import picker_workflow
import web_app
import inference_integration
import lynx.inference
import lynx.app
import lynx.prepare_state
assert not blocked.intersection(sys.modules)
"""
        root = Path(__file__).resolve().parent.parent
        # Use both ordinary source roots without relying on pytest's import cache.
        code = f"import sys\nsys.path[:0] = {[str(root), str(root / 'capture_and_detect')]!r}\n" + code
        result = subprocess.run([sys.executable, "-c", code], cwd=root,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_terminal_help_exits_before_initialization(self):
        app = importlib.import_module("picker_workflow")
        output = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(output))
            guarded = [stack.enter_context(patch(target, side_effect=AssertionError(target)))
                       for target in ("picker_workflow.load_token", "picker_workflow.CameraClient",
                                      "picker_workflow.Calibration", "picker_workflow.CaptureStore",
                                      "inference_integration.create_detector",
                                      "inference_integration.create_robot")]
            with self.assertRaises(SystemExit) as exit_result:
                app.main(["--help"])
            self.assertEqual(exit_result.exception.code, 0)
            for guard in guarded:
                guard.assert_not_called()
        for option in ("--web", "--left-only", "--dry-run"):
            self.assertIn(option, output.getvalue())

    def test_web_mode_passes_only_remaining_arguments_to_web_launcher(self):
        app = importlib.import_module("picker_workflow")
        arguments = ["--web", "--demo", "--port", "8081"]
        with patch("web_app.main", return_value=17) as web_main, \
                patch.object(app, "load_token", side_effect=AssertionError("terminal initialization")):
            self.assertEqual(app.main(arguments), 17)
            web_main.assert_called_once_with(["--demo", "--port", "8081"])
        self.assertEqual(arguments, ["--web", "--demo", "--port", "8081"])

    def test_script_arguments_route_web_help_without_starting_terminal(self):
        app = importlib.import_module("picker_workflow")
        with patch.object(sys, "argv", ["picker_workflow.py", "--web", "--help"]), \
                patch("web_app.main", return_value=0) as web_main:
            self.assertEqual(app.main(), 0)
            web_main.assert_called_once_with(["--help"])


if __name__ == "__main__":
    unittest.main()
