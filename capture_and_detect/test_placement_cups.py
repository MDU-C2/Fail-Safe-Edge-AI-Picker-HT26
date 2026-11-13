"""Cup-cell ownership and obstacle preservation using synthetic RGB-D geometry."""

import copy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from cup_inventory import SelectionError
from detection_masks import encode_mask
from placement_cups import assign_cups
from placement_vision import PlacementVision


class PlacementCupFixture(unittest.TestCase):
    def setUp(self):
        # Non-unit scale and all three offsets are deliberate. Physical table
        # Z is zero at optical depth 500; a depth 450 surface is 100 mm above it.
        self.calibration = SimpleNamespace(
            scale=2.0, R=np.diag([1., -1., -1.]),
            t=np.array([315., 83., 960.]), offset=(-115., 17., 40.))
        self.K = {"width": 640, "height": 480, "fx": 600., "fy": 600.,
                  "cx": 320., "cy": 240.}
        self.vision = PlacementVision(self.calibration, self.K, cell_size=80.)
        self.locations = [
            {"id": 11, "x": 200., "y": 100., "z": 230., "occupied": False},
            {"id": 3, "x": 280., "y": 100., "z": 230., "occupied": False},
        ]
        self.depth = np.full((480, 640), 500.)

    def mask(self, x0=330, x1=358, y0=232, y1=248):
        mask = np.zeros(self.depth.shape, dtype=bool)
        mask[y0:y1, x0:x1] = True
        return mask

    def obj(self, mask, *, kind="CUP", confidence=.9, detection_id=20, box=None, **extra):
        ys, xs = np.nonzero(mask)
        if box is None:
            box = [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1]
        return {"class": kind, "confidence": confidence, "detection_id": detection_id,
                "bbox_xyxy": box, "segmentation": encode_mask(mask), **extra}

    def assign(self, objects, *, depth=None, locations=None, vision=None):
        vision = self.vision if vision is None else vision
        return assign_cups(self.depth if depth is None else depth, objects,
                           self.locations if locations is None else locations,
                           transform=vision.transform, translation=vision.translation,
                           intrinsics=self.K, cell_size=vision.cell_size, table_z=vision.table_z)

    def states(self, objects=(), *, depth=None, locations=None):
        return [slot["state"] for slot in self.vision.assess(
            self.depth if depth is None else depth, objects,
            self.locations if locations is None else locations)]


class CupOwnershipTests(PlacementCupFixture):
    def test_malformed_detection_containers_fail_closed_in_helper_and_assessment(self):
        for objects in ([None], [[]], [{"class": []}], {"class": "CUP"}, [{}]):
            with self.subTest(objects=objects):
                with self.assertRaises(SelectionError):
                    self.assign(objects)
                with self.assertRaises(SelectionError):
                    self.states(objects)

    def test_unique_physical_majority_uses_location_index_and_preserves_exact_mask(self):
        mask = self.mask()
        self.depth[mask] = 450.
        objects = [self.obj(mask)]
        original = copy.deepcopy(objects)
        owner, consumed, assignments = self.assign(objects)
        self.assertEqual(owner.dtype, np.int32)
        self.assertEqual(owner.shape, self.depth.shape)
        self.assertTrue(np.all(owner[mask] == 1))  # Index+1, not location ID 11.
        self.assertFalse(owner[~mask].any())
        self.assertEqual(consumed, {0})
        self.assertEqual(len(assignments), 1)
        record = assignments[0]
        self.assertEqual(record["location_id"], 11)
        self.assertEqual(record["object_indices"], [0])
        self.assertEqual(record["detection_ids"], [20])
        self.assertEqual(record["mask_pixels"], int(mask.sum()))
        self.assertAlmostEqual(record["overlap_fraction"], 17 / 28, places=3)
        np.testing.assert_allclose(record["center_mm"], [235.25, 100.75])
        self.assertEqual(objects, original)

    def test_mask_holes_are_never_owned(self):
        mask = self.mask(y0=230, y1=250)
        mask[236:240, 349:353] = False
        self.depth[mask] = 450.
        owner, consumed, _ = self.assign([self.obj(mask)])
        self.assertEqual(consumed, {0})
        self.assertFalse(owner[236:240, 349:353].any())
        self.assertEqual(np.count_nonzero(owner), int(mask.sum()))

    def test_depth_halo_cannot_flip_exact_mask_majority_or_change_reported_center(self):
        mask, halo = self.mask(), self.mask(358, 365)
        self.depth[mask] = 450.
        objects = [self.obj(mask)]
        baseline = self.assign(objects)[2][0]
        self.depth[halo] = 450.
        # Exact segmentation has 17 columns in cell11 and 11 in cell3. The
        # accepted seven-column halo would reverse those totals to 17 versus18
        # if depth recovery were allowed to vote on cup location.
        self.assertEqual(halo.sum(), mask.sum() * .25)
        owner, consumed, assignments = self.assign(objects)
        self.assertEqual(consumed, {0})
        self.assertEqual(len(assignments), 1)
        record = assignments[0]
        self.assertEqual(record["location_id"], 11)
        self.assertEqual(record["depth_fringe_pixels"], int(halo.sum()))
        self.assertEqual(record["overlap_fraction"], baseline["overlap_fraction"])
        self.assertAlmostEqual(record["overlap_fraction"], 17 / 28, places=3)
        self.assertEqual(record["center_mm"], baseline["center_mm"])
        np.testing.assert_allclose(record["center_mm"], [235.25, 100.75])
        self.assertTrue(np.all(owner[mask | halo] == 1))
        self.assertEqual(self.states(objects), ["occupied", "clear"])

    def test_two_distinct_cups_in_one_cell_count_twice_without_blocking_another_cell(self):
        first, second = self.mask(304, 314), self.mask(326, 336)
        self.depth[first | second] = 450.
        objects = [self.obj(first, detection_id=20), self.obj(second, detection_id=21)]
        owner, consumed, assignments = self.assign(objects)
        self.assertEqual(consumed, {0, 1})
        self.assertEqual(len(assignments), 2)
        self.assertEqual({record["location_id"] for record in assignments}, {11})
        self.assertEqual([record["detection_ids"] for record in assignments], [[20], [21]])
        self.assertTrue(np.all(owner[first | second] == 1))
        slots = self.vision.assess(self.depth, objects, self.locations)
        self.assertEqual([slot["state"] for slot in slots], ["occupied", "clear"])
        self.assertEqual([len(slot["assigned_cups"]) for slot in slots], [2, 0])

    def test_duplicate_bases_and_associated_handle_are_one_parent_cup(self):
        # A thin CUP outline can coexist with much larger overlapping base
        # predictions; they must form one union rather than three cup owners.
        body = self.mask(330, 332)
        first = self.mask(332, 350)
        duplicate = self.mask(338, 358)
        handle = self.mask(354, 358, 235, 244)
        union = body | first | duplicate | handle
        self.depth[union] = 450.
        objects = [self.obj(body, box=[330, 232, 358, 248], detection_id=1),
                   self.obj(first, kind="CUP BASE", detection_id=2, is_cup=True),
                   self.obj(duplicate, kind="CUP BASE", detection_id=3, is_cup=True),
                   self.obj(handle, kind="CUP HANDLE", detection_id=4)]
        owner, consumed, assignments = self.assign(objects)
        self.assertEqual(consumed, {0, 1, 2, 3})
        self.assertEqual(len(assignments), 1)
        self.assertEqual(assignments[0]["object_indices"], [0, 1, 2, 3])
        self.assertEqual(assignments[0]["detection_ids"], [1, 2, 3, 4])
        self.assertEqual(assignments[0]["mask_pixels"], int(union.sum()))
        self.assertTrue(np.all(owner[union] == 1))

    def test_supported_standalone_base_can_assign_but_unsupported_base_cannot(self):
        mask = self.mask()
        self.depth[mask] = 450.
        for supported in (True, False):
            with self.subTest(supported=supported):
                _, consumed, assignments = self.assign([
                    self.obj(mask, kind="CUP BASE", is_cup=supported, cup_height=supported)])
                self.assertEqual(bool(assignments), supported)
                self.assertEqual(consumed, {0} if supported else set())

    def test_part_shared_by_two_body_boxes_is_not_consumed(self):
        body = self.mask(330, 334)
        other = self.mask(354, 358)
        base = self.mask(338, 350)
        self.depth[body | other | base] = 450.
        objects = [self.obj(body, box=[330, 232, 358, 248]),
                   self.obj(other, box=[330, 232, 358, 248], detection_id=21),
                   self.obj(base, kind="CUP BASE", is_cup=True, detection_id=22)]
        _, consumed, _ = self.assign(objects)
        self.assertNotIn(2, consumed)

    def test_equal_cell_winners_are_unassigned(self):
        mask = self.mask(340, 354)
        self.depth[mask] = 450.
        owner, consumed, assignments = self.assign([self.obj(mask)])
        self.assertFalse(owner.any())
        self.assertEqual(consumed, set())
        self.assertEqual(assignments, [])

    def test_depth_halo_cannot_break_an_exact_segmentation_boundary_tie(self):
        mask, halo = self.mask(340, 354), self.mask(354, 356, 236, 244)
        self.depth[mask | halo] = 450.
        # Seven segmented columns lie on each side. An attached halo entirely
        # in cell3 must not make an otherwise ambiguous cup count as assigned.
        self.assertLess(halo.sum(), mask.sum() * .25)
        obj = self.obj(mask)
        owner, consumed, assignments = self.assign([obj])
        self.assertFalse(owner.any())
        self.assertEqual(consumed, set())
        self.assertEqual(assignments, [])
        self.assertEqual(self.states([obj]), ["occupied", "occupied"])

    def test_no_cell_contains_half_the_cup_surface(self):
        mask = self.mask(330, 340) | self.mask(430, 460)
        self.depth[mask] = 450.
        owner, consumed, assignments = self.assign([self.obj(mask)])
        self.assertFalse(owner.any())
        self.assertEqual((consumed, assignments), (set(), []))

    def test_conflicting_cup_owners_do_not_exempt_their_overlap(self):
        left, right = self.mask(), self.mask(342, 370)
        self.depth[left | right] = 450.
        owner, consumed, assignments = self.assign([
            self.obj(left), self.obj(right, detection_id=21)])
        self.assertEqual(consumed, {0, 1})
        self.assertEqual({record["location_id"] for record in assignments}, {11, 3})
        self.assertTrue(np.all(owner[left & right] == -1))
        self.assertTrue(np.all(owner[left & ~right] == 1))
        self.assertTrue(np.all(owner[right & ~left] == 2))

    def test_low_confidence_invalid_depth_and_implausible_height_do_not_grant_ownership(self):
        mask = self.mask()
        for confidence, optical_depth in ((.499, 450.), (.9, 0.), (.9, np.nan),
                                          (.9, np.inf), (.9, 400.), (.9, 520.)):
            with self.subTest(confidence=confidence, optical_depth=optical_depth):
                depth = self.depth.copy()
                depth[mask] = optical_depth
                owner, consumed, assignments = self.assign([self.obj(mask, confidence=confidence)], depth=depth)
                self.assertFalse(owner.any())
                self.assertEqual((consumed, assignments), (set(), []))

    def test_assignment_requires_95_percent_valid_depth(self):
        mask = self.mask()
        pixels = np.flatnonzero(mask)
        for invalid_count, assigned in ((22, True), (23, False)):  #448 mask pixels.
            with self.subTest(invalid_count=invalid_count):
                depth = self.depth.copy()
                depth[mask] = 450.
                depth.flat[pixels[:invalid_count]] = 0.
                _, consumed, assignments = self.assign([self.obj(mask)], depth=depth)
                self.assertEqual(bool(assignments), assigned)
                self.assertEqual(consumed, {0} if assigned else set())

    def test_95_percent_requirement_combines_invalid_and_implausible_depth(self):
        mask = self.mask()
        pixels = np.flatnonzero(mask)
        self.depth[mask] = 450.
        self.depth.flat[pixels[:22]] = 0.
        self.depth.flat[pixels[22:43]] = 400.
        # Each defect alone affects less than 5%; together they invalidate
        # more than 5% of the entire cup, so no segmentation can be exempted.
        owner, consumed, assignments = self.assign([self.obj(mask)])
        self.assertFalse(owner.any())
        self.assertEqual((consumed, assignments), (set(), []))

    def test_parallax_uses_surface_depth_instead_of_projecting_pixels_onto_table(self):
        # At X>camera centre, a raised cup's image lies farther outward than
        # its real XY. The table projection assigns these pixels to the other
        # cell; measured depth puts every point in the first cell.
        locations = [dict(self.locations[0], x=500.), dict(self.locations[1], x=580.)]
        mask = self.mask(525, 539)
        self.depth[mask] = 450.
        _, _, assigned = self.assign([self.obj(mask)], locations=locations)
        self.assertEqual(assigned[0]["location_id"], 11)
        floor_depth = self.depth.copy()
        floor_depth[mask] = 500.
        _, _, projected_to_floor = self.assign([self.obj(mask)], depth=floor_depth, locations=locations)
        self.assertEqual(projected_to_floor[0]["location_id"], 3)

    def test_plausible_height_is_relative_to_configured_table_plane(self):
        mask = self.mask()
        self.depth[mask] = 400.  #Physical Z200: too high for table0, valid for table75.
        obj = self.obj(mask)
        self.assertEqual(self.assign([obj])[1], set())
        raised_table = PlacementVision(self.calibration, self.K, cell_size=80., table_z=75.)
        _, consumed, assignments = self.assign([obj], vision=raised_table)
        self.assertEqual(consumed, {0})
        self.assertEqual(assignments[0]["location_id"], 11)


class PlacementCupIntegrationTests(PlacementCupFixture):
    def test_boundary_crossing_cup_blocks_only_its_dominant_cell(self):
        mask = self.mask()
        self.depth[mask] = 450.
        self.assertEqual(self.states([self.obj(mask)]), ["occupied", "clear"])

    def test_unclassified_depth_object_still_blocks_all_affected_cells(self):
        mask = self.mask()
        self.depth[mask] = 450.
        self.assertEqual(self.states(), ["occupied", "occupied"])

    def test_small_unlabelled_fragment_inside_cup_box_uses_local_uncertainty(self):
        mask = self.mask()
        self.depth[mask] = 450.
        obj = self.obj(mask, box=[330, 232, 380, 248])
        self.assertEqual(self.states([obj]), ["occupied", "clear"])
        fragment = self.mask(370, 374, 236, 240)
        self.depth[fragment] = 450.
        # This roughly 6 x 6 mm fragment is within the accepted 40 mm cup
        # uncertainty band. Its small-object ambiguity is intentional.
        self.assertEqual(self.states([obj]), ["occupied", "clear"])
        other = self.obj(fragment, kind="BLOCK", detection_id=21)
        self.assertEqual(self.states([obj, other]), ["occupied", "occupied"])

    def test_hole_inside_cup_mask_keeps_missing_depth_unknown_and_other_obstacle_blocked(self):
        mask = self.mask(y0=230, y1=250)
        mask[236:240, 349:353] = False
        self.depth[mask] = 450.
        obj = self.obj(mask)
        self.assertEqual(self.states([obj]), ["occupied", "clear"])
        for hole_depth, expected in ((0., "unknown"), (400., "occupied")):
            with self.subTest(hole_depth=hole_depth):
                depth = self.depth.copy()
                depth[236:240, 349:353] = hole_depth
                self.assertEqual(self.states([obj], depth=depth), ["occupied", expected])

    def test_unusable_depth_inside_otherwise_valid_cup_mask_keeps_neighbor_blocked(self):
        mask = self.mask()
        self.depth[mask] = 450.
        obj = self.obj(mask)
        #16 of 448 pixels are under 5%; the rest still supports a cup assignment.
        #Those 16 pixels must not inherit the valid pixels' depth exemption.
        for bad_depth, expected in ((0., "unknown"), (np.nan, "unknown"), (400., "occupied")):
            with self.subTest(bad_depth=bad_depth):
                depth = self.depth.copy()
                depth[236:240, 350:354] = bad_depth
                owner, consumed, assignments = self.assign([obj], depth=depth)
                self.assertEqual(consumed, {0})
                self.assertEqual(assignments[0]["location_id"], 11)
                self.assertFalse(owner[236:240, 350:354].any())
                self.assertEqual(self.states([obj], depth=depth), ["occupied", expected])

    def test_cup_covering_most_of_neighbor_floor_cannot_confirm_that_cell_clear(self):
        mask = self.mask(300, 389, 218, 262)
        self.depth[mask] = 450.
        obj = self.obj(mask)
        _, consumed, assignments = self.assign([obj])
        self.assertEqual(consumed, {0})
        self.assertEqual(assignments[0]["location_id"], 11)
        slots = self.vision.assess(self.depth, [obj], self.locations)
        self.assertEqual(slots[0]["state"], "occupied")
        self.assertLess(slots[1]["floor_visible_fraction"], .5)
        self.assertEqual(slots[1]["state"], "unknown")

    def test_missing_malformed_or_misaligned_mask_preserves_conservative_box_veto(self):
        mask = self.mask()
        self.depth[mask] = 450.
        valid = self.obj(mask)
        elsewhere = encode_mask(self.mask(10, 20, 10, 20))
        for segmentation in (None, {}, {"encoding": "bad"}, elsewhere):
            with self.subTest(segmentation=segmentation):
                obj = dict(valid, segmentation=segmentation)
                self.assertEqual(self.states([obj]), ["occupied", "occupied"])
                self.assertEqual(self.assign([obj])[1], set())
        without = {key: value for key, value in valid.items() if key != "segmentation"}
        self.assertEqual(self.states([without]), ["occupied", "occupied"])

    def test_low_confidence_and_unpaired_handle_never_clear_an_overlapped_cell(self):
        mask = self.mask()
        self.depth[mask] = 450.
        for obj in (self.obj(mask, confidence=.49), self.obj(mask, kind="CUP HANDLE")):
            with self.subTest(kind=obj["class"], confidence=obj["confidence"]):
                self.assertEqual(self.assign([obj])[1], set())
                self.assertEqual(self.states([obj]), ["occupied", "occupied"])

    def test_duplicate_base_group_does_not_reintroduce_neighbor_box_veto(self):
        body, base, duplicate = self.mask(330, 332), self.mask(332, 358), self.mask(336, 354)
        self.depth[body | base | duplicate] = 450.
        objects = [self.obj(body, box=[330, 232, 358, 248]),
                   self.obj(base, kind="CUP BASE", is_cup=True, detection_id=21),
                   self.obj(duplicate, kind="CUP BASE", is_cup=True, detection_id=22)]
        self.assertEqual(self.states(objects), ["occupied", "clear"])

    def test_assessment_and_combination_do_not_change_reservations(self):
        mask = self.mask()
        self.depth[mask] = 450.
        locations = copy.deepcopy(self.locations)
        locations[1].update(occupied=True, reserved=True, reservation_id="inspection-needed")
        snapshot = {"fingerprint": "test-only", "locations": locations}
        before = copy.deepcopy(snapshot)
        frames = [self.vision.assess(self.depth, [self.obj(mask)], locations) for _ in range(3)]
        self.assertEqual([slot["state"] for slot in frames[0]], ["occupied", "clear"])
        self.assertTrue(frames[0][1]["reserved"])
        combined = self.vision.combine(snapshot, frames, [10, 11, 12])
        self.assertTrue(combined["slots"][1]["reserved"])
        self.assertEqual(combined["released_ids"], [])
        self.assertFalse(combined["applied"])
        self.assertEqual(snapshot, before)

    def test_ambiguous_boundary_frame_cannot_be_combined_into_clear_neighbor(self):
        stable = self.mask()
        ambiguous = self.mask(340, 354)
        self.depth[stable] = 450.
        normal = self.vision.assess(self.depth, [self.obj(stable)], self.locations)
        self.assertEqual([slot["state"] for slot in normal], ["occupied", "clear"])
        depth = np.full(self.depth.shape, 500.)
        depth[ambiguous] = 450.
        uncertain = self.vision.assess(depth, [self.obj(ambiguous)], self.locations)
        self.assertEqual([slot["state"] for slot in uncertain], ["occupied", "occupied"])
        snapshot = {"fingerprint": "test-only", "locations": self.locations}
        combined = self.vision.combine(snapshot, [normal, uncertain, normal], [10, 11, 12])
        self.assertNotEqual(combined["slots"][1]["state"], "clear")


class PlacementCupDepthFringeTests(PlacementCupFixture):
    def test_large_fringe_region_falls_back_to_exact_mask_before_allocating_pixel_geometry(self):
        mask, spill = self.mask(), self.mask(358, 360, 236, 244)
        self.depth[mask | spill] = 450.
        obj = self.obj(mask)
        self.assertEqual(self.assign([obj])[2][0]["depth_fringe_pixels"], int(spill.sum()))
        # At 2000 pixels the 20 mm ROI exceeds the resource cap while a 10 mm
        # ROI would fit. Resource rejection must not trigger the narrower retry.
        for limit in (1, 2000):
            with self.subTest(limit=limit), patch("placement_cups.MAX_FRINGE_PIXELS", limit), \
                    patch("placement_cups.np.indices", side_effect=AssertionError("Oversized fringe geometry allocated")):
                owner, consumed, assignments = self.assign([obj])
            self.assertEqual(consumed, {0})
            self.assertEqual(assignments[0]["location_id"], 11)
            self.assertEqual(assignments[0]["depth_fringe_pixels"], 0)
            self.assertTrue(np.all(owner[mask] == 1))
            self.assertFalse(owner[spill].any())

    def test_small_connected_registered_depth_spill_belongs_to_the_same_cup(self):
        mask, spill = self.mask(), self.mask(358, 360, 236, 244)
        self.depth[mask | spill] = 450.
        obj = self.obj(mask)
        owner, consumed, assignments = self.assign([obj])
        self.assertEqual(consumed, {0})
        self.assertEqual(assignments[0]["location_id"], 11)
        self.assertEqual(assignments[0]["depth_fringe_pixels"], int(spill.sum()))
        self.assertTrue(np.all(owner[spill] == 1))
        self.assertEqual(self.states([obj]), ["occupied", "clear"])

    def test_small_disconnected_fragment_remains_unowned_but_uses_uncertainty(self):
        mask, obstacle = self.mask(), self.mask(360, 362, 236, 244)
        self.depth[mask | obstacle] = 450.
        obj = self.obj(mask)
        owner, _, assignments = self.assign([obj])
        self.assertFalse(owner[obstacle].any())
        self.assertEqual(assignments[0]["depth_fringe_pixels"], 0)
        # The local uncertainty policy accepts detached tiny fragments without
        # asserting that the cup owns them. Exact ownership stays unchanged.
        slots = self.vision.assess(self.depth, [obj], self.locations)
        self.assertEqual([slot["state"] for slot in slots], ["occupied", "clear"])
        self.assertGreater(slots[1]["cup_uncertain_excluded_pixels"], 0)

    def test_legacy_fringe_rejects_abrupt_step_but_whole_depth_region_can_own_it(self):
        mask, obstacle = self.mask(), self.mask(358, 360, 236, 244)
        self.depth[mask] = 450.
        # Distant seed pixels establish a supported height of 120 mm, so the
        # candidate cannot be rejected merely by the overall cup-height band.
        self.depth[232:248, 330:332] = 440.
        self.depth[obstacle] = 440.
        obj = self.obj(mask)
        owner, _, assignments = self.assign([obj])
        self.assertFalse(owner[obstacle].any())
        self.assertEqual(assignments[0]["depth_fringe_pixels"], 0)
        # The accepted connected-region policy does not use the old fringe's
        # local surface-step cutoff. This is part of its touching-object limit.
        slots = self.vision.assess(self.depth, [obj], self.locations)
        self.assertEqual([slot["state"] for slot in slots], ["occupied", "clear"])
        self.assertEqual(slots[0]["assigned_cups"][0]["depth_assignment_method"], "connected_depth_area")

    def test_continuous_curved_surface_can_follow_the_cup_without_a_large_local_step(self):
        mask, spill = self.mask(), self.mask(358, 367, 236, 240)
        self.depth[mask] = 450.
        self.depth[232:248, 330:332] = 440.
        self.depth[236:240, 358:367] = np.linspace(449., 440., 9)
        obj = self.obj(mask)
        owner, _, assignments = self.assign([obj])
        self.assertTrue(np.all(owner[spill] == 1))
        self.assertEqual(assignments[0]["depth_fringe_pixels"], int(spill.sum()))
        self.assertEqual(self.states([obj]), ["occupied", "clear"])

    def test_detected_noncup_touching_at_the_same_height_is_protected(self):
        mask, obstacle = self.mask(), self.mask(358, 360, 236, 244)
        self.depth[mask | obstacle] = 450.
        cup = self.obj(mask)
        for has_mask in (True, False):
            with self.subTest(has_mask=has_mask):
                other = self.obj(obstacle, kind="BLOCK", detection_id=21)
                if not has_mask:
                    other.pop("segmentation")
                owner, consumed, assignments = self.assign([cup, other])
                self.assertEqual(consumed, {0})
                self.assertFalse(owner[obstacle].any())
                self.assertEqual(assignments[0]["depth_fringe_pixels"], 0)
                self.assertEqual(self.states([cup, other]), ["occupied", "occupied"])

    def test_depth_fringe_follows_exterior_but_never_fills_an_interior_mask_hole(self):
        mask = self.mask(y0=230, y1=250)
        hole = self.mask(349, 353, 236, 240)
        mask[hole] = False
        spill = self.mask(358, 360, 236, 244)
        self.depth[mask | hole | spill] = 450.
        obj = self.obj(mask)
        owner, _, assignments = self.assign([obj])
        self.assertTrue(np.all(owner[spill] == 1))
        self.assertFalse(owner[hole].any())
        self.assertEqual(assignments[0]["depth_fringe_pixels"], int(spill.sum()))
        self.assertEqual(self.states([obj]), ["occupied", "occupied"])

    def test_narrower_complete_fringe_recovers_neighbor_edge_without_owning_outer_spill(self):
        mask = self.mask()
        # The broad outline is over budget mostly on the cup's own-cell side.
        # Its short opposite edge crosses into the neighbouring cell, like the
        # observed P3/P4 depth misregistration. A 10 mm outline fits exactly 25%.
        outer = self.mask(317, 330)
        edge = self.mask(358, 360, 236, 244)
        self.depth[mask | outer | edge] = 450.
        obj = self.obj(mask)
        owner, consumed, assignments = self.assign([obj])
        self.assertEqual(consumed, {0})
        self.assertGreater(int((outer | edge).sum()), mask.sum() * .25)
        self.assertEqual(assignments[0]["depth_fringe_pixels"], 112)
        self.assertEqual(assignments[0]["depth_fringe_pixels"], mask.sum() * .25)
        self.assertTrue(np.all(owner[mask | edge] == 1))
        self.assertTrue(np.all(owner[232:248, 324:330] == 1))
        self.assertFalse(owner[232:248, 317:324].any())
        self.assertEqual(self.states([obj]), ["occupied", "clear"])

    def test_narrower_fringe_preserves_labels_while_tiny_unlabelled_fragment_is_uncertain(self):
        mask, outer = self.mask(), self.mask(317, 330)
        for kind in ("disconnected", "segmented", "box"):
            with self.subTest(kind=kind):
                self.depth.fill(500.)
                obstacle = (self.mask(360, 362, 236, 244) if kind == "disconnected"
                            else self.mask(358, 360, 236, 244))
                self.depth[mask | outer | obstacle] = 450.
                objects = [self.obj(mask)]
                if kind != "disconnected":
                    other = self.obj(obstacle, kind="BLOCK", detection_id=21)
                    if kind == "box":
                        other.pop("segmentation")
                    objects.append(other)
                owner, consumed, assignments = self.assign(objects)
                self.assertEqual(consumed, {0})
                self.assertEqual(assignments[0]["depth_fringe_pixels"], 96)
                self.assertFalse(owner[obstacle].any())
                self.assertFalse(owner[232:248, 317:324].any())
                expected = "clear" if kind == "disconnected" else "occupied"
                self.assertEqual(self.states(objects), ["occupied", expected])

    def test_strip_over_fringe_budget_still_fits_the_local_uncertainty_band(self):
        mask, spill = self.mask(328, 358), self.mask(358, 366)
        self.assertGreater(spill.sum(), mask.sum() * .25)
        self.depth[mask | spill] = 450.
        obj = self.obj(mask)
        owner, _, assignments = self.assign([obj])
        self.assertEqual(assignments[0]["depth_fringe_pixels"], 96)
        self.assertEqual(assignments[0]["location_id"], 11)
        self.assertTrue(np.all(owner[232:248, 358:364] == 1))
        self.assertFalse(owner[232:248, 364:366].any())
        self.assertTrue(np.all(owner[mask] == 1))
        # Over 25% of this small mask is only a 12 x 24 mm strip. The separate
        # 40 mm uncertainty policy accepts it without expanding exact ownership.
        self.assertEqual(self.states([obj]), ["occupied", "clear"])

    def test_legacy_fringe_rejects_over_quarter_but_depth_region_uses_majority_area(self):
        mask = self.mask()
        spill = self.mask(324, 330) | self.mask(358, 366)
        # Even the narrower complete outline has 192 pixels against a 112-pixel
        # budget. It must not select just enough pixels to satisfy the limit.
        self.depth[mask | spill] = 450.
        obj = self.obj(mask)
        owner, _, assignments = self.assign([obj])
        self.assertEqual(assignments[0]["depth_fringe_pixels"], 0)
        self.assertEqual(assignments[0]["location_id"], 11)
        self.assertFalse(owner[spill].any())
        self.assertTrue(np.all(owner[mask] == 1))
        slots = self.vision.assess(self.depth, [obj], self.locations)
        self.assertEqual([slot["state"] for slot in slots], ["occupied", "clear"])
        self.assertEqual(slots[0]["assigned_cups"][0]["depth_assignment_method"], "connected_depth_area")

    def test_legacy_fringe_hull_limit_does_not_cap_accepted_whole_depth_region(self):
        # Unequal focal lengths make the image-distance estimate deliberately
        # permissive in X. Physical XY must still enforce the 20 mm hull bound.
        self.K["fx"] = 300.
        mask, spill = self.mask(320, 332), self.mask(332, 342, 236, 240)
        self.depth[mask | spill] = 450.
        obj = self.obj(mask)
        owner, _, assignments = self.assign([obj])
        self.assertEqual(assignments[0]["location_id"], 11)
        self.assertTrue(np.all(owner[236:240, 332:338] == 1))
        # Last seed X233; pixel338 is X254, already 21 mm outside its hull.
        self.assertFalse(owner[236:240, 338:342].any())
        self.assertEqual(assignments[0]["depth_fringe_pixels"], 24)
        slots = self.vision.assess(self.depth, [obj], self.locations)
        self.assertEqual([slot["state"] for slot in slots], ["occupied", "clear"])
        self.assertEqual(slots[0]["assigned_cups"][0]["depth_assignment_method"], "connected_depth_area")


if __name__ == "__main__":
    unittest.main()
