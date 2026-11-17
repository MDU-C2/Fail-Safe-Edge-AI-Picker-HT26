"""Mac startup from its documented folder uses fake camera/model services only."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


COLD_START = r'''
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import picker_workflow as picker
from device_service import arguments, python_factory
from robot_client import RobotClient

# Reproduce the ordinary `cd capture_and_detect` launch, not the test runner's
# repository-root PYTHONPATH. The Python picker must not need the Lynx package.
assert importlib.util.find_spec('lynx') is None
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    grid = root / 'placing-grid.json'
    grid.write_text(json.dumps({'cell_size_mm': 90, 'locations': [
        {'id': 2, 'x': 400, 'y': 0, 'z': 230, 'occupied': True, 'reserved': True,
         'reservation_id': 'existing-offline-reservation'}]}))
    before = grid.read_bytes()
    model = root / 'fake-model.pt'
    model.write_bytes(b'not-a-real-model')
    K = {'width': 64, 'height': 48, 'fx': 60., 'fy': 60., 'cx': 32., 'cy': 24.}
    calibration = SimpleNamespace(scale=1., R=np.diag([1., -1., -1.]),
        t=np.array([515., 0., 960.]), offset=(-115., 0., 40.),
        digest='offline-calibration', ensure_unchanged=Mock())
    frame_count = 0
    def capture(**kwargs):
        global frame_count
        frame_count += 1
        return (np.zeros((48, 64, 3), dtype=np.uint8),
                np.full((48, 64), 1000, dtype=np.uint16),
                {'sequence': frame_count, 'received_monotonic': time.monotonic(),
                 'observation_started_monotonic': time.monotonic()})
    camera = SimpleNamespace(api='http://offline.invalid:8000/api/v1',
        intrinsics=Mock(return_value=K), capture=Mock(side_effect=capture))
    detector = SimpleNamespace(calibration=calibration, K=K, conf=.5,
        analyse=Mock(side_effect=lambda rgb, depth: ([], rgb.copy())))
    args = arguments(['--device-id', 'mac', '--backend', 'python',
        '--state-dir', str(root), '--model', str(model), '--token-file', str(root / 'unused'),
        '--robot-id', 'offline-mac', '--gateway', 'http://offline.invalid:8000'])
    controller = Mock()
    with patch.dict(os.environ, {'GATEWAY_TOKEN': 'offline-generated-test-token'}), \
            patch('requests.Session.request', side_effect=AssertionError('Network forbidden')), \
            patch.object(picker, 'Calibration', return_value=calibration), \
            patch('camera_client.CameraClient', return_value=camera), \
            patch('inference_integration.create_detector', return_value=(detector, model)), \
            patch('inference_integration.create_robot', return_value=RobotClient(camera.api, {}, dry_run=True)):
        session, language = python_factory(args, prefer_environment_token=True)(controller)
        inventory, started = session.capture('scan')
    assert session.robot.dry_run
    assert camera.capture.call_count == 5 and detector.analyse.call_count == 1
    assert inventory['placement_observation']['frame_count'] == 1
    assert inventory['placement_observation']['frame_sequences'] == [5]
    assert grid.read_bytes() == before
    assert 'lynx' not in __import__('sys').modules
'''


class MacColdStartTests(unittest.TestCase):
    def test_startup_and_single_scan_need_no_sibling_lynx_package(self):
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        result = subprocess.run([sys.executable, "-c", textwrap.dedent(COLD_START)],
                                cwd=Path(__file__).resolve().parent, env=env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
