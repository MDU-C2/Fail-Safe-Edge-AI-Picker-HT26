"""Bounded verification and return-phase prefetch with offline doubles only."""

import copy
import threading
import unittest
from unittest.mock import Mock

from cup_commands import CupSession
from cup_inventory import SelectionError
from test_cup_selection import RobotDouble, detection, inventory, TRANSFER_JOB_COUNT
import test_placement_verification as fixtures


class MultiFrameVerificationTests(unittest.TestCase):
    setUp = fixtures.PlacementVerificationTests.setUp
    observed = staticmethod(fixtures.PlacementVerificationTests.observed)
    recheck_observed = classmethod(fixtures.PlacementVerificationTests.recheck_observed.__func__)
    execute = staticmethod(fixtures.PlacementVerificationTests.execute)

    def session(self, **kwargs):
        session = fixtures.PlacementVerificationTests.session(self, **kwargs)
        session.verification_frames = 3
        return session

    def frames(self, states):
        frames = []
        for sequence, state in enumerate(states, 11):
            kind = "clear" if state == "clear" else "occupied" if state in ("raised_depth", "cup", "detected_object") else "unknown"
            frame = self.recheck_observed(kind, sequence=sequence, reason_code=state)
            slot = frame["placement_observation"]["slots"][0]
            if state in ("raised_depth", "cup", "detected_object"):
                slot["evidence"] = [state]
            if state == "cup":
                slot["cup_count"] = 1
                slot["assigned_cups"] = [{"object_indices": [0]}]
            if state == "occluded":
                slot["visible_fraction"] = 0.2
                slot["floor_visible_fraction"] = 0.3
            if state == "invalid_depth":
                slot["valid_fraction"] = 0.7
            if state == "table_depth":
                slot["table_fraction"] = 0.8
            frames.append(frame)
        return frames

    def test_constructor_accepts_only_one_or_three(self):
        for value in (0, 2, 4, True, 3.0, "3", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                CupSession(Mock(), Mock(), self.robot, "cal-1", verification_frames=value)
        for value in (1, 3):
            session = CupSession(Mock(), Mock(), self.robot, "cal-1", verification_frames=value)
            self.assertEqual(session.verification_frames, value)

    def test_three_clear_frames_are_required_without_unknown_trigger(self):
        frames = self.frames(["clear"] * 3)
        session = self.session(first=frames[0], retries=frames[1:])
        jobs = self.execute(session)
        self.assertEqual(len(jobs), TRANSFER_JOB_COUNT)
        self.assertEqual(self.capture.call_count, 4)
        audit = session.inventory["cups"][0]["last_verification"]
        self.assertEqual(audit["multi_frame"], {"required_clear": 3, "clear_count": 3,
                                               "max_post_home_observations": 6, "post_home_observations": 3})
        self.assertEqual(audit["prefetch"]["capture_count"], 0)
        self.assertEqual([check["prefetched"] for check in audit["placement_checks"]], [False] * 3)

    def test_each_recoverable_depth_result_resets_the_clear_streak(self):
        for kind in ("raised_depth", "table_depth", "invalid_depth", "occluded"):
            with self.subTest(kind=kind):
                self.robot = RobotDouble()
                frames = self.frames(["clear", "clear", kind, "clear", "clear", "clear"])
                session = self.session(first=frames[0], retries=frames[1:])
                before = self.grid.path.read_bytes()
                self.execute(session)
                self.assertEqual(self.capture.call_count, 7)
                audit = session.inventory["cups"][0]["last_verification"]
                self.assertEqual(len(audit["placement_checks"]), 6)
                self.assertEqual(audit["multi_frame"]["clear_count"], 3)
                self.grid.path.write_bytes(before)

    def test_continuous_or_intermittent_depth_failure_cannot_pass_by_majority(self):
        for states in (["table_depth"] * 6, ["clear", "clear", "raised_depth", "clear", "clear", "table_depth"]):
            with self.subTest(states=states):
                frames = self.frames(states)
                session = self.session(first=frames[0], retries=frames[1:])
                before = self.grid.path.read_bytes()
                with self.assertRaisesRegex(SelectionError, "three consecutive clear camera checks in six post-home"):
                    self.execute(session)
                self.assertEqual(self.capture.call_count, 7)
                self.assertEqual(self.robot.jobs, [])
                self.assertEqual(self.grid.path.read_bytes(), before)
                self.assertFalse(self.recovery.required())

    def test_cup_or_labelled_object_veto_cannot_be_overridden_by_later_clear_frames(self):
        for kind in ("cup", "detected_object"):
            with self.subTest(kind=kind):
                frames = self.frames(["clear", kind, "clear", "clear", "clear"])
                session = self.session(first=frames[0], retries=frames[1:])
                before = self.grid.path.read_bytes()
                with self.assertRaisesRegex(SelectionError, "location 1 is occupied"):
                    self.execute(session)
                self.assertEqual(self.capture.call_count, 3)
                self.assertEqual(self.robot.jobs, [])
                self.assertEqual(self.grid.path.read_bytes(), before)

    def test_bad_sequence_calibration_target_and_metrics_abort_without_extra_frames(self):
        for kind in ("sequence", "calibration", "target", "grip", "metrics", "reference",
                     "source", "model", "capture_id", "offset_bool"):
            with self.subTest(kind=kind):
                frames = self.frames(["clear"] * 3)
                bad = frames[1]
                if kind == "sequence": bad["frame"]["sequence"] = 11
                elif kind == "calibration": bad["calibration_sha256"] = "changed"
                elif kind == "target": bad["cups"][0]["x"] += 40
                elif kind == "grip": bad["cups"][0]["rz"] = 190
                elif kind == "metrics": bad["placement_observation"]["slots"][0]["valid_fraction"] = float("nan")
                elif kind == "source": bad["gateway"] = "http://different.invalid"
                elif kind == "model": bad["model"]["weights"] = "different"
                elif kind == "capture_id": bad["capture"] = frames[0]["capture"]
                elif kind == "offset_bool": bad["tag_offset_mm"] = [False, False, False]
                else: bad["placement_observation"]["table_reference"] = {"file_sha256": "changed"}
                session = self.session(first=frames[0], retries=frames[1:])
                before = self.grid.path.read_bytes()
                with self.assertRaises(SelectionError): self.execute(session)
                self.assertEqual(self.capture.call_count, 3)
                self.assertEqual(self.robot.jobs, [])
                self.assertEqual(self.grid.path.read_bytes(), before)

    def test_first_clear_expiring_during_third_capture_blocks_even_with_fresh_last_frame(self):
        frames = self.frames(["clear"] * 3)
        session = self.session(first=frames[0])
        now, remaining = [100], iter(zip(frames, (100, 110, 116)))
        session.clock = lambda: now[0]
        def capture(_):
            frame, started = next(remaining)
            now[0] = started
            return frame, started
        self.capture.side_effect = capture
        before = self.grid.path.read_bytes()
        with self.assertRaisesRegex(SelectionError, "16.0s old"):
            self.execute(session)
        self.assertEqual(self.robot.jobs, [])
        self.assertEqual(self.grid.path.read_bytes(), before)

    def test_oldest_accepted_clear_is_rechecked_at_dispatch(self):
        frames = self.frames(["clear"] * 3)
        session = self.session(first=frames[0])
        now, remaining = [100], iter(zip(frames, (100, 105, 110)))
        session.clock = lambda: now[0]
        def capture(_):
            frame, started = next(remaining)
            now[0] = started
            return frame, started
        self.capture.side_effect = capture
        self.robot.after_move = lambda _: now.__setitem__(0, 116) if self.robot.jobs[-1]["name"] == "left_ready" else None
        with self.assertRaisesRegex(SelectionError, "16.0s old"):
            self.execute(session)
        self.assertNotIn("approach_cup", [job["name"] for job in self.robot.jobs])
        self.assertTrue(self.grid.snapshot()["locations"][0]["reserved"])

    def batch_session(self, *, home_failure=False, stop_during_prefetch=False, bad_post_home_time=False,
                      mutate_prefetch=None, home_time=110.0, prefetch_time=None, post_home_states=()):
        # Two stable source cups, two clear destinations, and deterministic time.
        cups = inventory(objects=[detection("CUP", 100), detection("CUP BASE", 100),
                                  detection("CUP", 250, x=560), detection("CUP BASE", 250, x=560)])["cups"]
        scan = self.observed()
        scan["cups"] = copy.deepcopy(cups)
        session = self.session(scan=scan)
        now = [100.0]
        session.clock = lambda: now[0]
        sequence = [10]
        active = [0]
        max_active = [0]
        calls, saves = [], []
        worker_entered, worker_finished = threading.Event(), threading.Event()
        release_worker = threading.Event()
        coordinator = threading.get_ident()
        lock = threading.Lock()

        def capture(_):
            with lock:
                active[0] += 1
                max_active[0] = max(active[0], max_active[0])
                sequence[0] += 1
                seq = sequence[0]
            try:
                background = threading.get_ident() != coordinator
                started = now[0]
                if background:
                    if prefetch_time is not None:
                        started = prefetch_time
                    worker_entered.set()
                    self.assertIn("right_clear", [job["name"] for job in session.inventory["cups"][0]["execution"]["completed_steps"]])
                    self.assertTrue(release_worker.wait(2), "test did not release camera worker")
                if not background and seq >= 16:
                    self.assertTrue(session.inventory["cups"][0]["execution"]["state"] == "completed")
                    self.assertEqual(len(self.robot.jobs), TRANSFER_JOB_COUNT)
                    if bad_post_home_time:
                        started = 109.0
                fresh = self.recheck_observed(sequence=seq)
                fresh["cups"] = copy.deepcopy(cups)
                slot = fresh["placement_observation"]["slots"][0]
                fresh["placement_observation"]["slots"].append(dict(copy.deepcopy(slot), id=2))
                if background and mutate_prefetch is not None:
                    mutate_prefetch(fresh, seq)
                if not background and seq >= 16 and post_home_states:
                    state = post_home_states[min(seq - 16, len(post_home_states) - 1)]
                    changed_slot = self.frames([state])[0]["placement_observation"]["slots"][0]
                    fresh["placement_observation"]["slots"][1] = dict(copy.deepcopy(changed_slot), id=2)
                calls.append((seq, background, started))
                if background and seq == 15:
                    worker_finished.set()
                return fresh, started
            finally:
                with lock:
                    active[0] -= 1

        self.capture.side_effect = capture
        self.save.side_effect = lambda _: saves.append(threading.get_ident())
        homes = [0]
        def after_move(_):
            if self.robot.jobs[-1]["name"] != "right_home":
                return
            homes[0] += 1
            if homes[0] != 1:
                return
            self.assertTrue(worker_entered.wait(2), "prefetch did not start during return")
            if stop_during_prefetch:
                session.latch_stop()
            release_worker.set()
            if not stop_during_prefetch:
                self.assertTrue(worker_finished.wait(2), "prefetch did not collect its two frames")
            now[0] = home_time
            if home_failure:
                raise RuntimeError("home failed")
        self.robot.after_move = after_move
        return session, calls, saves, max_active, active, coordinator

    def test_batch_prefetch_overlaps_return_but_final_capture_waits_for_home(self):
        session, calls, saves, max_active, active, coordinator = self.batch_session()
        jobs = session.execute_batch(capture_id=session.inventory["capture"])
        self.assertEqual(len(jobs), 2 * TRANSFER_JOB_COUNT)
        self.assertEqual(calls, [(11, False, 100.0), (12, False, 100.0), (13, False, 100.0),
                                 (14, True, 100.0), (15, True, 100.0), (16, False, 110.0)])
        self.assertEqual(max_active, [1])
        self.assertEqual(active, [0])
        self.assertEqual(set(saves), {coordinator})
        audit = session.inventory["cups"][1]["last_verification"]
        self.assertEqual(audit["prefetch"]["capture_count"], 2)
        self.assertEqual([row["prefetched"] for row in audit["placement_checks"]], [True, True, False])
        self.assertFalse(any(thread.name == "cup-camera-prefetch" for thread in threading.enumerate()))

    def test_failed_home_or_stop_discards_and_drains_prefetch_without_next_pick(self):
        for stop in (False, True):
            with self.subTest(stop=stop):
                self.robot = RobotDouble()
                session, calls, saves, _, active, coordinator = self.batch_session(
                    home_failure=not stop, stop_during_prefetch=stop)
                with self.assertRaisesRegex((RuntimeError, SelectionError), "home failed|Software stop"):
                    session.execute_batch(capture_id=session.inventory["capture"])
                self.assertEqual(active, [0])
                self.assertEqual(set(saves), {coordinator})
                self.assertTrue(all(seq < 16 for seq, _, _ in calls))
                self.assertLessEqual(sum(background for _, background, _ in calls), 2)
                self.assertTrue(session.motion_uncertain)
                self.assertTrue(self.grid.snapshot()["locations"][1]["reserved"])
                self.assertFalse(any(thread.name == "cup-camera-prefetch" for thread in threading.enumerate()))
                # A fresh independent grid/recovery double for the next case.
                if not stop:
                    fixtures.PlacementVerificationTests.setUp(self)

    def test_direct_final_frame_must_have_started_after_confirmed_home(self):
        session, _, _, _, active, _ = self.batch_session(bad_post_home_time=True)
        with self.assertRaisesRegex(SelectionError, "after both arms have confirmed home"):
            session.execute_batch(capture_id=session.inventory["capture"])
        self.assertEqual(len(self.robot.jobs), TRANSFER_JOB_COUNT)
        self.assertEqual(active, [0])
        self.assertTrue(self.grid.snapshot()["locations"][1]["reserved"])

    def test_unusable_return_views_are_discarded_and_require_three_post_home_clears(self):
        for kind in ("missing_target", "ambiguous_target", "moved_target", "grip", "blocked_depth",
                     "occluded", "labelled_object", "stale"):
            with self.subTest(kind=kind):
                fixtures.PlacementVerificationTests.setUp(self)
                def mutate(frame, sequence):
                    if sequence == 15:  # A later clear prefetch cannot revive discarded credit.
                        return
                    if kind == "missing_target":
                        frame["cups"] = []
                    elif kind == "ambiguous_target":
                        frame["cups"].append(dict(copy.deepcopy(frame["cups"][1]), id=3))
                    elif kind == "moved_target":
                        frame["cups"][1]["x"] += 40
                    elif kind == "grip":
                        frame["cups"][1]["rz"] = 190
                    elif kind == "blocked_depth":
                        frame["cups"][1].update(x=None, y=None, z=None, selectable=False,
                                               status="blocked", blocked_reason="No usable depth")
                    elif kind in ("occluded", "labelled_object"):
                        replacement = self.frames(["occluded" if kind == "occluded" else "detected_object"])[0]
                        frame["placement_observation"]["slots"][1] = dict(
                            replacement["placement_observation"]["slots"][0], id=2)
                session, calls, saves, max_active, active, coordinator = self.batch_session(
                    mutate_prefetch=mutate, home_time=130.0 if kind == "stale" else 110.0)
                jobs = session.execute_batch(capture_id=session.inventory["capture"])
                self.assertEqual(len(jobs), 2 * TRANSFER_JOB_COUNT)
                self.assertEqual([seq for seq, background, _ in calls if not background], [11, 12, 13, 16, 17, 18])
                self.assertEqual(active, [0])
                self.assertEqual(max_active, [1])
                self.assertEqual(set(saves), {coordinator})
                audit = session.inventory["cups"][1]["last_verification"]
                self.assertEqual(audit["multi_frame"]["post_home_observations"], 3)
                self.assertEqual(audit["prefetch"]["discarded_count"], 2)
                self.assertTrue(audit["prefetch"]["requires_three_post_home"])
                self.assertEqual([row["disposition"] for row in audit["placement_checks"]],
                                 ["discarded", "discarded", "clear", "clear", "clear"])
                self.assertTrue(all(row.get("view_status") == "pending_view" for row in audit["placement_checks"][:2]))

    def test_bad_background_schema_or_configuration_still_fails_closed(self):
        for kind in ("cup_container", "cup_id", "cup_nan", "cup_missing", "cup_bool", "cup_pixel",
                     "slot_fraction", "slot_count", "slot_evidence", "slot_code", "source", "calibration",
                     "sequence", "capture_id", "polygon", "future_time"):
            with self.subTest(kind=kind):
                fixtures.PlacementVerificationTests.setUp(self)
                def mutate(frame, sequence):
                    if sequence == 15: return
                    cup = frame["cups"][1]
                    slot = frame["placement_observation"]["slots"][1]
                    if kind == "cup_container": frame["cups"] = [None]
                    elif kind == "cup_id": cup["id"] = frame["cups"][0]["id"]
                    elif kind == "cup_nan": cup["x"] = float("nan")
                    elif kind == "cup_missing": cup.pop("x")
                    elif kind == "cup_bool": cup["confidence"] = True
                    elif kind == "cup_pixel": cup["pixel"] = []
                    elif kind == "slot_fraction": slot["valid_fraction"] = float("nan")
                    elif kind == "slot_count": slot["cup_count"] = False
                    elif kind == "slot_evidence": slot["evidence"] = ["unexpected"]
                    elif kind == "slot_code": slot["reason_code"] = "unexpected"
                    elif kind == "source": frame["gateway"] = "http://changed.invalid"
                    elif kind == "calibration": frame["calibration_sha256"] = "changed"
                    elif kind == "sequence": frame["frame"]["sequence"] = 13
                    elif kind == "capture_id": frame["capture"] = "capture-10"
                    elif kind == "polygon": slot["polygon_px"][0][0] += 1
                session, calls, _, _, active, _ = self.batch_session(mutate_prefetch=mutate,
                    prefetch_time=200.0 if kind == "future_time" else None)
                with self.assertRaises(SelectionError):
                    session.execute_batch(capture_id=session.inventory["capture"])
                self.assertEqual(len(self.robot.jobs), TRANSFER_JOB_COUNT)
                self.assertTrue(all(sequence < 16 for sequence, _, _ in calls))
                self.assertEqual(active, [0])
                self.assertTrue(self.grid.snapshot()["locations"][1]["reserved"])

    def test_discarded_prefetch_does_not_consume_six_post_home_attempts(self):
        def missing(frame, _): frame["cups"] = []
        session, calls, _, _, active, _ = self.batch_session(mutate_prefetch=missing,
            post_home_states=["table_depth", "clear", "occluded", "clear", "clear", "clear"])
        session.execute_batch(capture_id=session.inventory["capture"])
        audit = session.inventory["cups"][1]["last_verification"]
        self.assertEqual(audit["multi_frame"]["post_home_observations"], 6)
        self.assertEqual(len(audit["placement_checks"]), 8)
        self.assertEqual(calls[-1][0], 21)
        self.assertEqual(active, [0])

    def test_prefetch_expiring_during_post_home_capture_is_refreshed(self):
        session, calls, _, _, active, _ = self.batch_session()
        original_capture = self.capture.side_effect
        def capture(kind):
            result = original_capture(kind)
            if result[0]["frame"]["sequence"] == 16:
                session.clock = lambda: 116.0  # Early frames age16; new frame age6.
            return result
        self.capture.side_effect = capture
        session.execute_batch(capture_id=session.inventory["capture"])
        audit = session.inventory["cups"][1]["last_verification"]
        self.assertEqual(audit["multi_frame"]["post_home_observations"], 3)
        self.assertEqual(audit["prefetch"]["discarded_count"], 2)
        self.assertEqual(calls[-1][0], 18)
        self.assertEqual(active, [0])

    def test_prefetch_infrastructure_failure_is_not_discarded_as_a_bad_view(self):
        session, _, _, _, active, _ = self.batch_session()
        original_capture = self.capture.side_effect
        def capture(kind):
            result = original_capture(kind)
            if result[0]["frame"]["sequence"] == 15:
                raise RuntimeError("camera transport failed")
            return result
        self.capture.side_effect = capture
        with self.assertRaisesRegex(RuntimeError, "camera transport failed"):
            session.execute_batch(capture_id=session.inventory["capture"])
        self.assertEqual(len(self.robot.jobs), TRANSFER_JOB_COUNT)
        self.assertEqual(active, [0])
        self.assertTrue(self.grid.snapshot()["locations"][1]["reserved"])

    def test_obstruction_after_home_still_blocks_despite_opportunistic_prefetch(self):
        for states, expected_attempts in ((["detected_object"], 1), (["table_depth"] * 6, 6)):
            with self.subTest(states=states):
                fixtures.PlacementVerificationTests.setUp(self)
                def missing(frame, _): frame["cups"] = []
                session, calls, _, _, active, _ = self.batch_session(mutate_prefetch=missing, post_home_states=states)
                with self.assertRaises(SelectionError):
                    session.execute_batch(capture_id=session.inventory["capture"])
                self.assertEqual(len(self.robot.jobs), TRANSFER_JOB_COUNT)
                self.assertEqual(len([seq for seq, background, _ in calls if not background and seq >= 16]), expected_attempts)
                self.assertEqual(active, [0])
                self.assertTrue(self.grid.snapshot()["locations"][1]["reserved"])


if __name__ == "__main__":
    unittest.main()
