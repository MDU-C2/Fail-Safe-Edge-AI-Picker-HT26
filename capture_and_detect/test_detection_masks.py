"""Lossless placement mask storage; all detector/worker inputs are offline fakes."""

import base64
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from detection_masks import decode_mask, encode_mask


class DetectionMaskTests(unittest.TestCase):
    def setUp(self):
        self.mask = np.zeros((17, 23), dtype=bool)
        self.mask[3:12, 5:15] = True
        self.mask[5:10, 7:13] = False  # A real hole must not become occupied pixels.
        self.mask[14, 19] = True      # A disconnected region must survive too.

    def restore(self, value, shape):
        x0, y0, crop = decode_mask(value, shape)
        restored = np.zeros(shape, dtype=bool)
        restored[y0:y0 + crop.shape[0], x0:x0 + crop.shape[1]] = crop
        return restored

    def test_round_trip_keeps_holes_disconnected_regions_and_exact_origin(self):
        value = json.loads(json.dumps(encode_mask(self.mask), allow_nan=False))
        self.assertEqual(value["origin"], [5, 3])
        self.assertEqual(value["size"], [12, 15])
        self.assertEqual(len(base64.b64decode(value["data"])), (12 * 15 + 7) // 8)
        np.testing.assert_array_equal(self.restore(value, self.mask.shape), self.mask)

    def test_noncontiguous_and_boundary_masks_round_trip(self):
        for mask in (self.mask[:, ::-1], self.mask.T, np.eye(9, dtype=bool), np.ones((3, 7), dtype=bool)):
            with self.subTest(shape=mask.shape):
                np.testing.assert_array_equal(self.restore(encode_mask(mask), mask.shape), mask)

    def test_single_pixel_uses_big_endian_bit_and_zero_padding(self):
        mask = np.zeros((4, 5), dtype=bool)
        mask[3, 4] = True
        value = encode_mask(mask)
        self.assertEqual(value, {"encoding": "base64-packbits-big", "origin": [4, 3],
                                 "size": [1, 1], "data": "gA=="})
        np.testing.assert_array_equal(self.restore(value, mask.shape), mask)

    def test_empty_mask_is_absent_and_invalid_encoder_inputs_are_rejected(self):
        self.assertIsNone(encode_mask(np.zeros((3, 4), dtype=bool)))
        for value in ([[True]], np.ones((2, 2)), np.ones((2, 2), dtype=np.uint8),
                      np.ones((1, 1, 1), dtype=bool), np.zeros((0, 3), dtype=bool),
                      np.zeros((1, 4097), dtype=bool)):
            with self.subTest(shape=getattr(value, "shape", None)), self.assertRaises(ValueError):
                encode_mask(value)

    def test_missing_or_malformed_schema_never_creates_a_bbox_mask(self):
        value = encode_mask(self.mask)
        candidates = [None, [], {}, {"bbox_xyxy": [0, 0, 20, 16]},
                      dict(value, encoding="polygon"), dict(value, unexpected=True),
                      {key: item for key, item in value.items() if key != "origin"},
                      dict(value, origin=[1]), dict(value, size=[1, 2, 3]),
                      dict(value, data=b"gA==")]
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                self.assertIsNone(decode_mask(candidate, self.mask.shape))

    def test_noninteger_nonfinite_negative_and_outside_bounds_are_rejected(self):
        value = encode_mask(self.mask)
        for changes in ({"origin": [True, 3]}, {"origin": [5.0, 3]}, {"origin": [-1, 3]},
                        {"origin": [float("inf"), 3]}, {"origin": [5, 100000000000000000000]},
                        {"origin": [20, 3]}, {"size": [0, 15]}, {"size": [12, False]},
                        {"size": [12.0, 15]}, {"size": [float("nan"), 15]},
                        {"size": [4096, 4096]}, {"size": [18, 15]}):
            with self.subTest(changes=changes):
                self.assertIsNone(decode_mask(dict(value, **changes), self.mask.shape))
        for shape in (None, (0, 23), (17.0, 23), (True, 23), (17, 23, 3), (4097, 1), (4096, 4096)):
            with self.subTest(shape=shape):
                self.assertIsNone(decode_mask(value, shape))

    def test_data_length_alphabet_canonical_encoding_and_padding_are_checked(self):
        value = encode_mask(np.ones((1, 1), dtype=bool))
        for data in ("", "gA=", "gA===", "gA==\n", "!!!!", "éééé", "gB==", "gQ==", "AA==", "gIAA"):
            with self.subTest(data=data):
                self.assertIsNone(decode_mask(dict(value, data=data), (1, 1)))
        value = encode_mask(np.ones((3, 3), dtype=bool))
        self.assertIsNone(decode_mask(dict(value, data="/w=="), (3, 3)))  # Same encoded length, short bytes.

    def test_bad_dimensions_are_rejected_before_decoding_or_allocating_pixels(self):
        value = dict(encode_mask(self.mask), size=[4096, 4096])
        with patch("detection_masks.base64.b64decode") as decode:
            self.assertIsNone(decode_mask(value, self.mask.shape))
            decode.assert_not_called()

    def test_detector_and_mac_pi_adapter_preserve_identical_segmentation(self):
        from capture_and_detect import Detector
        from inference_integration import ProviderDetector
        from inference_protocol import make_response, make_settings

        calibration = SimpleNamespace(to_robot=lambda x, y, z: (x, y, z))
        intrinsics = {"width": 23, "height": 17, "fx": 100, "fy": 100, "cx": 11, "cy": 8}
        depth = np.full(self.mask.shape, 800, dtype=np.uint16)
        local = Detector(SimpleNamespace(names={0: "CUP"}), calibration, intrinsics, .5)
        local_object = local.describe("CUP", .9, self.mask, depth)
        detections = [{"class_name": "CUP", "confidence": .9,
                       "bbox_xyxy": [5, 3, 20, 15], "mask": self.mask}]
        supervisor = SimpleNamespace(predict=lambda request: make_response(request, detections, inference_ms=1))
        provider = ProviderDetector(supervisor, "a" * 64, make_settings(), calibration, intrinsics)
        remote_objects, _ = provider.analyse(np.zeros((17, 23, 3), dtype=np.uint8), depth)
        self.assertEqual(remote_objects[0]["segmentation"], local_object["segmentation"])
        np.testing.assert_array_equal(self.restore(local_object["segmentation"], self.mask.shape), self.mask)

    def test_capture_archive_retains_exact_mask_in_json_and_legacy_csv_still_writes(self):
        from capture_and_detect import CaptureStore

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = {"capture": "offline_scan", "frame": {}, "cups": [],
                      "calibration_sha256": "offline", "tag_offset_mm": [0, 0, 0],
                      "images": {kind: str(root / f"{kind}.png") for kind in ("rgb", "depth", "annotated")}}
            objects = [{"class": "CUP", "segmentation": encode_mask(self.mask)}]
            rgb = np.zeros((*self.mask.shape, 3), dtype=np.uint8)
            CaptureStore(root).save_capture(record, rgb, np.ones(self.mask.shape, dtype=np.uint16),
                                            rgb.copy(), objects, {"width": 23, "height": 17}, "scan")
            archived = json.loads((root / "detections_offline_scan.json").read_text())
            np.testing.assert_array_equal(self.restore(archived["objects"][0]["segmentation"], self.mask.shape), self.mask)
            self.assertEqual(json.loads((root / "latest.json").read_text())["objects"], objects)
            self.assertTrue((root / "detections.csv").is_file())


if __name__ == "__main__":
    unittest.main()
