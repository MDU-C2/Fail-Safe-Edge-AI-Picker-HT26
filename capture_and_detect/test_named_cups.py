"""Named reference persistence and cropping tests use only temporary images/files."""

import base64
import copy
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from cup_inventory import SelectionError
from named_cups import (MAX_COMPARISON_BYTES, MAX_IMAGE_BYTES, MAX_SOURCE_BYTES,
                        NamedCupStore, PUBLIC_FIELDS, candidate_images, comparison_sheet,
                        visually_duplicate_ids)


class NamedCupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.images = self.root / "images"
        self.images.mkdir()
        self.path = self.root / "references" / "cups.json"
        self.store = NamedCupStore(self.path, self.images)
        self.rgb = self.images / "rgb.png"
        image = np.full((240, 320, 3), (10, 80, 160), dtype=np.uint8)
        self.assertTrue(cv2.imwrite(str(self.rgb), image))
        self.annotated = self.images / "annotated.jpg"
        self.assertTrue(cv2.imwrite(str(self.annotated), np.full_like(image, (0, 255, 0))))
        self.inventory = {"capture": "scan_001", "images": {"rgb": str(self.rgb), "annotated": str(self.annotated)},
                          "cups": [self.cup()]}

    @staticmethod
    def cup(cup_id=1, detection_id=0, **changes):
        return {"id": cup_id, "cup_detection_id": detection_id, "bbox_xyxy": [20, 20, 80, 100],
                "x": 510, "y": 100, "z": 50, "robot_mm": {"x": 510, "y": 100, "z": 50},
                "selectable": True, "blocked_reason": None, **changes}

    def write_document(self, value):
        self.path.parent.mkdir(exist_ok=True)
        self.path.write_text(json.dumps(value))

    def test_save_restart_case_insensitive_lookup_and_public_records_have_no_image_or_coordinates(self):
        self.assertEqual(self.store.list(), [])
        self.assertFalse(self.path.exists())
        record = self.store.save("  Office   Cup_1-Blue  ", self.inventory, 1)
        self.assertEqual(set(record), set(PUBLIC_FIELDS))
        self.assertEqual(record["name"], "Office Cup_1-Blue")
        self.assertRegex(record["id"], r"^[0-9a-f]{32}$")
        restarted = NamedCupStore(self.path, self.images)
        found = restarted.get("office cup_1-blue")
        self.assertEqual({key: found[key] for key in PUBLIC_FIELDS}, record)
        self.assertIsInstance(found["image_bytes"], bytes)
        self.assertEqual(found["reference_sha256"], hashlib.sha256(found["image_bytes"]).hexdigest())
        self.assertEqual(restarted.get_id(record["id"]), found)
        self.assertEqual(restarted.list(), [record])
        persisted = json.loads(self.path.read_text())
        self.assertEqual(set(persisted), {"schema_version", "cups"})
        self.assertEqual(set(persisted["cups"][0]), set(PUBLIC_FIELDS) | {"image_jpeg_base64"})

    def test_crop_uses_raw_image_padding_and_clamps_at_edges(self):
        saved = self.store.save("Cup", self.inventory, 1)
        data = self.store.get_id(saved["id"])["image_bytes"]
        decoded = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(decoded.shape, (88, 66, 3))
        np.testing.assert_allclose(decoded.mean(axis=(0, 1)), (10, 80, 160), atol=3)
        self.inventory["cups"][0]["bbox_xyxy"] = [0, 0, 320, 240]
        self.store.save("Edge Cup", self.inventory, 1)
        data = self.store.get("Edge Cup")["image_bytes"]
        decoded = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(decoded.shape, (240, 320, 3))

    def test_large_crop_resizes_preserving_aspect_ratio_with_bounded_jpeg(self):
        self.assertTrue(cv2.imwrite(str(self.rgb), np.full((1200, 1600, 3), 128, dtype=np.uint8)))
        self.inventory["cups"][0]["bbox_xyxy"] = [0, 0, 1600, 1200]
        record = self.store.save("Large", self.inventory, 1)
        data = self.store.get_id(record["id"])["image_bytes"]
        self.assertLessEqual(len(data), MAX_IMAGE_BYTES)
        self.assertTrue(data.startswith(b"\xff\xd8"))
        decoded = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(decoded.shape, (384, 512, 3))

    def test_duplicate_name_never_overwrites_original_reference(self):
        record = self.store.save("My Cup", self.inventory, 1)
        before = self.path.read_bytes()
        with self.assertRaises(SelectionError):
            self.store.save("my  CUP", self.inventory, 1)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.store.get_id(record["id"])["name"], "My Cup")

    def test_invalid_names_never_create_a_store(self):
        for name in (None, "", "   ", "2cups", "_cup", "-cup", "Cup!", "Café", "Cup\nTwo",
                     "Cup\tTwo", "Cup/One", "Cup\\One", "C" * 49, True):
            with self.subTest(name=name), self.assertRaises(SelectionError):
                self.store.save(name, self.inventory, 1)
        self.assertFalse(self.path.exists())

    def test_full_cup_identity_is_required_unique_and_blocked_full_cups_can_be_saved(self):
        for cups in ([self.cup(detection_id=None)],
                     [self.cup(), self.cup()],
                     [self.cup(), self.cup(cup_id=2)],
                     [self.cup(detection_id=True)]):
            with self.subTest(cups=cups), self.assertRaises(SelectionError):
                self.store.save("Cup", dict(self.inventory, cups=cups), 1)
        blocked = self.cup(selectable=False, blocked_reason="no CUP BASE detected")
        record = self.store.save("Blocked Cup", dict(self.inventory, cups=[blocked]), 1)
        self.assertEqual(record["cup_id"], 1)
        with self.assertRaises(SelectionError):
            self.store.save("Boolean", self.inventory, True)

    def test_missing_invalid_or_out_of_image_boxes_are_rejected(self):
        for box in (None, [], [1, 2, 3], [1, 2, 1, 5], [1, 2, 4, 2],
                    [False, 2, 4, 5], [float("nan"), 2, 4, 5],
                    [1, 2, float("inf"), 5], [-1, 0, 10, 10], [0, 0, 321, 240]):
            with self.subTest(box=box), self.assertRaises(SelectionError):
                self.store.save("Cup", dict(self.inventory, cups=[self.cup(bbox_xyxy=box)]), 1)
        self.assertFalse(self.path.exists())

    def test_path_escape_symlink_escape_missing_and_wrong_extension_are_rejected(self):
        outside = self.root / "outside.png"
        outside.write_bytes(self.rgb.read_bytes())
        link = self.images / "link.png"
        link.symlink_to(outside)
        invalid = self.images / "rgb.txt"
        invalid.write_bytes(self.rgb.read_bytes())
        for source in (str(outside), "../outside.png", str(link), "missing.jpg", str(invalid)):
            with self.subTest(source=source), self.assertRaises(SelectionError):
                self.store.save("Cup", dict(self.inventory, images={"rgb": source}), 1)
        with self.assertRaises(SelectionError):
            self.store.save("Cup", dict(self.inventory, images={"annotated": str(self.annotated)}), 1)
        self.assertFalse(self.path.exists())
        self.store.save("Relative", dict(self.inventory, images={"rgb": "rgb.png"}), 1)

    def test_oversized_pixel_header_and_file_are_rejected_before_decode(self):
        header = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", 6000, 4000) + bytes(9)
        self.rgb.write_bytes(header)
        with patch("cv2.imdecode", side_effect=AssertionError("oversized image must not be decoded")):
            with self.assertRaises(SelectionError):
                self.store.save("Cup", self.inventory, 1)
        with self.rgb.open("wb") as stream:
            stream.truncate(MAX_SOURCE_BYTES + 1)
        with patch("cv2.imdecode", side_effect=AssertionError("oversized file must not be decoded")):
            with self.assertRaises(SelectionError):
                self.store.save("Cup", self.inventory, 1)
        self.assertFalse(self.path.exists())

    def test_unreadable_image_or_encoder_failure_cannot_save_partial_record(self):
        for content in (b"", b"not an image", b"\xff\xd8\xff\xd9"):
            self.rgb.write_bytes(content)
            with self.subTest(content=content), self.assertRaises(SelectionError):
                self.store.save("Cup", self.inventory, 1)
        self.assertTrue(cv2.imwrite(str(self.rgb), np.zeros((240, 320, 3), np.uint8)))
        with patch("cv2.imdecode", return_value=None), self.assertRaises(SelectionError):
            self.store.save("Cup", self.inventory, 1)
        with patch("cv2.imencode", return_value=(False, None)), self.assertRaises(SelectionError):
            self.store.save("Cup", self.inventory, 1)
        self.assertFalse(self.path.exists())

    def test_candidates_include_all_blocked_full_cups_and_exclude_orphan_bases(self):
        self.inventory["cups"] = [self.cup(), self.cup(2, "demo-CUP", selectable=False, blocked_reason="out of reach"),
                                  self.cup(3, None)]
        candidates = candidate_images(self.inventory, self.images)
        self.assertEqual([candidate["cup_id"] for candidate in candidates], [1, 2])
        self.assertTrue(all(set(candidate) == {"cup_id", "image_bytes"} for candidate in candidates))
        saved = self.store.save("First", self.inventory, 1)
        self.assertEqual(candidates[0]["image_bytes"], self.store.get_id(saved["id"])["image_bytes"])

    def test_candidate_limit_or_one_invalid_candidate_fails_without_subsetting(self):
        for cups in ([], [self.cup(1, None)], [self.cup(index, index) for index in range(1, 14)],
                     [self.cup(), self.cup(2, 2, bbox_xyxy=None)]):
            with self.subTest(count=len(cups)), self.assertRaises(SelectionError):
                candidate_images(dict(self.inventory, cups=cups), self.images)
        candidates = candidate_images(dict(self.inventory, cups=[self.cup(index, index) for index in range(1, 13)]), self.images)
        self.assertEqual(len(candidates), 12)

    def test_delete_is_persistent_returns_public_record_and_old_id_stays_missing(self):
        record = self.store.save("Cup", self.inventory, 1)
        self.assertEqual(self.store.delete(record["id"]), record)
        restarted = NamedCupStore(self.path, self.images)
        self.assertEqual(restarted.list(), [])
        for lookup in (lambda: restarted.get("Cup"), lambda: restarted.get_id(record["id"]),
                       lambda: restarted.delete(record["id"])):
            with self.assertRaises(SelectionError):
                lookup()
        replacement = restarted.save("Cup", self.inventory, 1)
        self.assertNotEqual(replacement["id"], record["id"])

    def test_store_limit_preserves_all_existing_references(self):
        for index in range(32):
            self.store.save(f"Cup {index}", self.inventory, 1)
        before = self.path.read_bytes()
        with self.assertRaises(SelectionError):
            self.store.save("Extra", self.inventory, 1)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(self.store.list()), 32)

    def test_corrupt_store_is_never_reset_or_overwritten(self):
        self.store.save("Cup", self.inventory, 1)
        original = json.loads(self.path.read_text())
        mutations = []
        for field, value in (("id", "bad"), ("name", "  Cup"), ("cup_id", True), ("saved_at", "yesterday"),
                             ("capture_id", "../scan"), ("reference_sha256", "0" * 64),
                             ("image_jpeg_base64", "%%%")):
            changed = copy.deepcopy(original)
            changed["cups"][0][field] = value
            mutations.append(changed)
        changed = copy.deepcopy(original)
        changed["cups"].append(copy.deepcopy(changed["cups"][0]))
        mutations.append(changed)
        changed = copy.deepcopy(original)
        changed["cups"].append(dict(changed["cups"][0], id="1" * 32, name="CUP"))
        mutations.append(changed)
        changed = copy.deepcopy(original)
        changed["cups"][0]["robot_mm"] = {"x": 500}
        mutations.append(changed)
        mutations.extend([{}, {"schema_version": True, "cups": []}, {"schema_version": 2, "cups": []}])
        for malformed in mutations:
            self.write_document(malformed)
            before = self.path.read_bytes()
            for action in (self.store.list, lambda: self.store.save("Other", self.inventory, 1),
                           lambda: self.store.delete(original["cups"][0]["id"])):
                with self.subTest(malformed=malformed), self.assertRaises(SelectionError):
                    action()
                self.assertEqual(self.path.read_bytes(), before)
        for malformed in ('{"schema_version":1,"cups":[],"cups":[]}', '{"schema_version":NaN,"cups":[]}', "{"):
            self.path.write_text(malformed)
            with self.assertRaises(SelectionError):
                self.store.list()
            self.assertEqual(self.path.read_text(), malformed)

    def test_matching_hash_cannot_make_non_image_or_oversize_reference_valid(self):
        self.store.save("Cup", self.inventory, 1)
        original = json.loads(self.path.read_text())
        png = self.rgb.read_bytes()
        ok, large_jpeg = cv2.imencode(".jpg", np.zeros((600, 600, 3), np.uint8))
        self.assertTrue(ok)
        for data in (b"not jpeg", png, large_jpeg.tobytes(), b"x" * (MAX_IMAGE_BYTES + 1)):
            malformed = copy.deepcopy(original)
            malformed["cups"][0]["image_jpeg_base64"] = base64.b64encode(data).decode()
            malformed["cups"][0]["reference_sha256"] = hashlib.sha256(data).hexdigest()
            self.write_document(malformed)
            with self.assertRaises(SelectionError):
                self.store.list()

    def test_broken_store_symlink_is_not_mistaken_for_an_empty_store(self):
        self.path.parent.mkdir()
        self.path.symlink_to(self.root / "missing.json")
        with self.assertRaises(SelectionError):
            self.store.list()
        with self.assertRaises(SelectionError):
            self.store.save("Cup", self.inventory, 1)
        self.assertTrue(self.path.is_symlink())

    def test_atomic_replace_failure_preserves_original_and_removes_temporary(self):
        self.store.save("Cup", self.inventory, 1)
        before = self.path.read_bytes()
        with patch("cup_inventory.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(SelectionError):
                self.store.save("Other", self.inventory, 1)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(list(self.path.parent.glob("*.tmp")))

    def test_concurrent_store_instances_do_not_lose_records_or_duplicate_names(self):
        outcomes, errors = [], []
        barrier = threading.Barrier(3)

        def save(name):
            try:
                barrier.wait(timeout=3)
                outcomes.append(NamedCupStore(self.path, self.images).save(name, self.inventory, 1))
            except Exception as error:
                errors.append(error)

        workers = [threading.Thread(target=save, args=(name,)) for name in ("Cup", "CUP", "Other")]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], SelectionError)
        self.assertEqual({record["name"].lower() for record in self.store.list()}, {"cup", "other"})


class ComparisonSheetTests(unittest.TestCase):
    @staticmethod
    def jpeg(color=(0, 0, 180), shape=(100, 100)):
        ok, encoded = cv2.imencode(".jpg", np.full((*shape, 3), color, dtype=np.uint8))
        if not ok:
            raise AssertionError("Cannot create test JPEG")
        return encoded.tobytes()

    @staticmethod
    def decode(data):
        return cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)

    def test_reference_and_actual_candidate_ids_are_labelled_in_row_major_order(self):
        colors = [(0, 0, 180), (0, 180, 0), (180, 0, 0), (40, 160, 180)]
        images = [self.jpeg(color) for color in colors]
        candidates = [{"cup_id": cup_id, "image_bytes": data}
                      for cup_id, data in zip((9, 2, 17), images[1:])]
        original = copy.deepcopy(candidates)
        with patch("cv2.putText", wraps=cv2.putText) as draw:
            data = comparison_sheet(images[0], candidates)
        self.assertEqual([call.args[1] for call in draw.call_args_list], ["REFERENCE", "CUP 9", "CUP 2", "CUP 17"])
        sheet = self.decode(data)
        self.assertEqual(sheet.shape, (512, 512, 3))
        for (row, column), color in zip(((0, 0), (0, 1), (1, 0), (1, 1)), colors):
            np.testing.assert_allclose(sheet[row * 256 + 148, column * 256 + 128], color, atol=4)
        self.assertGreater(int(sheet[10, 220, 0]), int(sheet[10, 476, 0]) + 50)
        self.assertEqual(candidates, original)
        self.assertIsInstance(data, bytes)
        self.assertLessEqual(len(data), MAX_COMPARISON_BYTES)

    def test_all_twelve_candidates_remain_visible_with_bounded_dimensions_and_decode(self):
        data = self.jpeg()
        candidates = [{"cup_id": index, "image_bytes": data} for index in range(1, 13)]
        from named_cups import _decode
        with patch("named_cups._decode", wraps=_decode) as decode, patch("cv2.putText", wraps=cv2.putText) as draw:
            sheet = self.decode(comparison_sheet(data, candidates))
        self.assertEqual(sheet.shape, (896, 896, 3))
        self.assertEqual(decode.call_count, 13)
        self.assertTrue(all(call.kwargs == {"reference": True} for call in decode.call_args_list))
        self.assertEqual([call.args[1] for call in draw.call_args_list], ["REFERENCE"] + [f"CUP {i}" for i in range(1, 13)])
        # Last candidate is first tile on the last row; unused tiles stay white.
        np.testing.assert_allclose(sheet[3 * 224 + 130, 112], (0, 0, 180), atol=4)
        np.testing.assert_allclose(sheet[-50, -50], (255, 255, 255), atol=1)

    def test_portrait_and_landscape_images_are_letterboxed_without_stretching_or_crop(self):
        portrait = self.jpeg((0, 0, 180), shape=(200, 100))
        landscape = self.jpeg((180, 0, 0), shape=(100, 200))
        sheet = self.decode(comparison_sheet(portrait, [{"cup_id": 5, "image_bytes": landscape}]))
        self.assertEqual(sheet.shape, (256, 512, 3))
        for column, channel, ratio in ((0, 2, .5), (1, 0, 2)):
            area = sheet[36:, column * 256:(column + 1) * 256]
            other = [index for index in range(3) if index != channel]
            mask = (area[:, :, channel] > 150) & (area[:, :, other[0]] < 30) & (area[:, :, other[1]] < 30)
            y, x = np.where(mask)
            self.assertGreater(len(x), 0)
            self.assertAlmostEqual((x.max() - x.min() + 1) / (y.max() - y.min() + 1), ratio, delta=.05)
            self.assertGreater(y.min(), 0)
            self.assertLess(y.max(), len(area) - 1)
            np.testing.assert_allclose(area[10, 2], (255, 255, 255), atol=10)

    def test_invalid_mapping_and_candidate_limits_are_rejected_without_partial_sheet(self):
        data = self.jpeg()
        valid = {"cup_id": 1, "image_bytes": data}
        for candidates in (None, (), [], [valid] * 13, [valid, valid],
                           [{"cup_id": True, "image_bytes": data}],
                           [{"cup_id": 0, "image_bytes": data}],
                           [{"cup_id": 1, "image_bytes": "not bytes"}],
                           [{**valid, "label": "CUP 99"}]):
            with self.subTest(candidates=candidates), self.assertRaises(SelectionError):
                comparison_sheet(data, candidates)
        with self.assertRaises(SelectionError):
            comparison_sheet("not bytes", [valid])

    def test_malformed_or_oversized_input_cannot_reach_sheet_encoding(self):
        valid = self.jpeg()
        oversized = self.jpeg(shape=(513, 100))
        for bad in (b"not JPEG", valid[:-2], oversized):
            for bad_reference in (False, True):
                reference = bad if bad_reference else valid
                candidates = [{"cup_id": 1, "image_bytes": valid if bad_reference else bad}]
                with self.subTest(bad_reference=bad_reference, size=len(bad)):
                    with patch("cv2.imencode", side_effect=AssertionError("No partial sheet")):
                        with self.assertRaises(SelectionError):
                            comparison_sheet(reference, candidates)


class VisualDuplicateTests(unittest.TestCase):
    @staticmethod
    def jpeg(image, quality=90):
        ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            raise AssertionError("Cannot encode duplicate test JPEG")
        return encoded.tobytes()

    @staticmethod
    def candidates(*images):
        return [{"cup_id": cup_id, "image_bytes": data} for cup_id, data in images]

    def test_exact_duplicate_bytes_vetoes_every_competitor_in_input_order(self):
        data = self.jpeg(np.full((100, 80, 3), 128, np.uint8))
        candidates = self.candidates((8, data), (4, data), (12, data))
        original = copy.deepcopy(candidates)
        self.assertEqual(visually_duplicate_ids(candidates, 8), [4, 12])
        self.assertEqual(visually_duplicate_ids(candidates, 4), [8, 12])
        self.assertEqual(candidates, original)

    def test_same_decoded_pixels_reencoded_to_different_jpeg_bytes_are_duplicates(self):
        image = np.full((100, 80, 3), 128, np.uint8)
        first, second = self.jpeg(image, 70), self.jpeg(image, 95)
        self.assertNotEqual(first, second)
        np.testing.assert_array_equal(ComparisonSheetTests.decode(first), ComparisonSheetTests.decode(second))
        self.assertEqual(visually_duplicate_ids(self.candidates((8, first), (4, second)), 8), [4])

    def test_minor_brightness_changes_are_vetoed(self):
        image = np.full((128, 96, 3), 100, np.uint8)
        image[32:96, 24:72] = 160
        first, second = self.jpeg(image), self.jpeg(image + 2)
        self.assertEqual(visually_duplicate_ids(self.candidates((8, first), (4, second)), 8), [4])

    def test_distinct_colors_and_patterns_do_not_become_duplicate_by_mean_color(self):
        vertical = np.full((128, 128, 3), 40, np.uint8)
        vertical[:, 64:] = 210
        horizontal = np.full_like(vertical, 40)
        horizontal[64:] = 210
        colored = np.full_like(vertical, (170, 50, 50))
        candidates = self.candidates((8, self.jpeg(vertical)), (4, self.jpeg(horizontal)), (12, self.jpeg(colored)))
        self.assertEqual(visually_duplicate_ids(candidates, 8), [])

    def test_unrelated_duplicate_candidates_do_not_veto_unique_selected_cup(self):
        selected = self.jpeg(np.full((80, 80, 3), 80, np.uint8))
        other = self.jpeg(np.full((80, 80, 3), 200, np.uint8))
        self.assertEqual(visually_duplicate_ids(self.candidates((8, selected), (4, other), (12, other)), 8), [])

    def test_padding_cannot_hide_differences_in_narrow_candidates(self):
        first = self.jpeg(np.full((256, 8, 3), 100, np.uint8))
        second = self.jpeg(np.full((256, 8, 3), 130, np.uint8))
        self.assertEqual(visually_duplicate_ids(self.candidates((8, first), (4, second)), 8), [])

    def test_aspect_ratio_difference_above_five_percent_is_not_a_near_duplicate(self):
        first = self.jpeg(np.full((100, 100, 3), 128, np.uint8))
        second = self.jpeg(np.full((100, 120, 3), 128, np.uint8))
        self.assertEqual(visually_duplicate_ids(self.candidates((8, first), (4, second)), 8), [])

    def test_every_candidate_is_checked_and_single_candidate_has_no_competitor(self):
        data = self.jpeg(np.full((100, 100, 3), 128, np.uint8))
        candidates = self.candidates(*[(index, data) for index in range(1, 13)])
        self.assertEqual(visually_duplicate_ids(candidates, 7), [index for index in range(1, 13) if index != 7])
        self.assertEqual(visually_duplicate_ids(self.candidates((7, data)), 7), [])

    def test_invalid_id_mapping_or_one_bad_image_rejects_the_guard(self):
        data = self.jpeg(np.full((100, 100, 3), 128, np.uint8))
        for candidates, matched_id in ((self.candidates((8, data)), 99),
                                       (self.candidates((8, data)), True),
                                       (self.candidates((8, data), (8, data)), 8),
                                       (self.candidates((8, data), (4, b"bad JPEG")), 8),
                                       ([], 8)):
            with self.subTest(matched_id=matched_id), self.assertRaises(SelectionError):
                visually_duplicate_ids(candidates, matched_id)


if __name__ == "__main__":
    unittest.main()
