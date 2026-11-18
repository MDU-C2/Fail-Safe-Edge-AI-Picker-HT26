"""Image streaming releases native page cache, including a disconnected reader."""
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from device_service import PickerHandler


class DeviceImageCacheTests(unittest.TestCase):
    def test_native_stream_and_disconnect_advise_only_served_image(self):
        for disconnect in (False, True):
            with self.subTest(disconnect=disconnect), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                image = root / "scan.png"
                image.write_bytes(b"image-data")
                advise = Mock()
                handler = self.handler(root, image, native=True)
                if disconnect:
                    handler.wfile = SimpleNamespace(write=Mock(side_effect=BrokenPipeError))
                with patch.dict("sys.modules", {"lynx.resources": SimpleNamespace(advise_capture_cache=advise)}):
                    if disconnect:
                        with self.assertRaises(BrokenPipeError):
                            handler.do_GET()
                    else:
                        handler.do_GET()
                        self.assertEqual(handler.wfile.getvalue(), b"image-data")
                advise.assert_called_once_with(root, paths=(image,))
                self.assertEqual(image.read_bytes(), b"image-data")

    def test_python_stream_does_not_import_lynx(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            image = root / "scan.png"
            image.write_bytes(b"image-data")
            handler = self.handler(root, image, native=False)
            with patch.dict("sys.modules", {"lynx": None, "lynx.resources": None}):
                handler.do_GET()
            self.assertEqual(handler.wfile.getvalue(), b"image-data")

    @staticmethod
    def handler(root, image, native):
        handler = object.__new__(PickerHandler)
        handler.path = "/v1/image/scan"
        handler.server = SimpleNamespace(agent=SimpleNamespace(native=native,
            health=Mock(), state=Mock(), last_handoff=Mock(),
            store=SimpleNamespace(root=root, capture_root=root), image=lambda _: image))
        handler.authorized = lambda: True
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = io.BytesIO()
        return handler
