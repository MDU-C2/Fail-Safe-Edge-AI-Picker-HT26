"""Fault-injection checks without models, networking, cameras, or robot access."""

from collections import deque
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from inference_supervisor import InferenceAuditError, InferenceSupervisor, InferenceUnavailable


def request(number=1):
    return {"protocol_version": 1, "request_id": f"request-{number}",
            "observation_id": f"observation-{number}", "width": 2, "height": 2,
            "image_sha256": "image-digest", "model_sha256": "model-digest",
            "settings": {"imgsz": 640}, "image": "exact-pixels"}


def response(item):
    result = deepcopy(item)
    result.pop("image", None)
    result["detections"] = []
    return result


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds=1.0):
        self.now += seconds


class Backend:
    def __init__(self, *outcomes):
        self.outcomes = deque(outcomes)
        self.calls = []
        self.closed = False

    def predict(self, item, timeout_s):
        self.calls.append((deepcopy(item), timeout_s))
        outcome = self.outcomes.popleft() if self.outcomes else response
        if isinstance(outcome, Exception):
            raise outcome
        return outcome(item) if callable(outcome) else deepcopy(outcome)

    def close(self):
        self.closed = True


class ManualSupervisor(InferenceSupervisor):
    """Tests advance the fake clock and explicitly run each recovery iteration."""

    def start(self):
        self._check_open()
        return self


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.supervisors = []

    def tearDown(self):
        for supervisor in self.supervisors:
            supervisor.close()

    def make(self, local=None, remote=None, **kwargs):
        supervisor = ManualSupervisor(local or Backend(), remote,
                                      clock=self.clock, probe_interval_s=1.0, **kwargs)
        self.supervisors.append(supervisor)
        return supervisor

    def recover(self, supervisor, checks=3):
        for _ in range(checks):
            self.clock.advance()
            self.assertTrue(supervisor._probe_once())

    def test_first_request_uses_mac_and_empty_detection_is_success(self):
        local, remote = Backend(), Backend()
        supervisor = self.make(local, remote)
        self.assertIsNone(supervisor.snapshot()["active_provider"])
        result = supervisor.predict(request())
        self.assertEqual(result["detections"], [])
        self.assertEqual(result["_inference"]["provider"], "mac")
        self.assertEqual(len(local.calls), 0)
        self.assertEqual(supervisor.snapshot()["counters"], {"mac": 1, "pi": 0})
        self.assertEqual(supervisor.snapshot()["state"], "mac_active")

    def test_failure_falls_back_with_exact_request_and_no_repeated_remote_wait(self):
        local, remote = Backend(), Backend(ConnectionError("disconnected"))
        supervisor = self.make(local, remote)
        original = request()
        result = supervisor.predict(original)
        self.assertEqual(result["_inference"]["provider"], "pi")
        self.assertEqual(remote.calls[0][0], local.calls[0][0])
        self.assertEqual(original, request())
        supervisor.predict(request(2))
        self.assertEqual(len(remote.calls), 1)
        self.assertEqual(supervisor.generation, 1)
        self.assertEqual(supervisor.snapshot()["counters"], {"mac": 0, "pi": 2})

    def test_invalid_response_falls_back(self):
        bad = response(request())
        bad["observation_id"] = "another-image"
        supervisor = self.make(remote=Backend(bad))
        result = supervisor.predict(request())
        self.assertEqual(result["_inference"]["provider"], "pi")
        self.assertTrue(any("observation_id" in event["message"]
                            for event in supervisor.snapshot()["events"]))

    def test_custom_validation_runs_for_both_providers(self):
        calls = []

        def validate(result, item):
            calls.append((result, item))
            if result.get("bad"):
                raise ValueError("invalid segmentation")
            result["checked"] = True
            return result

        bad = response(request())
        bad["bad"] = True
        supervisor = self.make(remote=Backend(bad), validate_response=validate)
        result = supervisor.predict(request())
        self.assertEqual(len(calls), 2)
        self.assertTrue(result["checked"])
        self.assertEqual(result["_inference"]["provider"], "pi")

    def test_validation_time_is_part_of_the_deadline(self):
        validations = []

        def validate(result, item):
            validations.append(item["request_id"])
            if len(validations) == 1:
                self.clock.advance(3)
            return result

        supervisor = self.make(remote=Backend(), validate_response=validate, remote_timeout_s=2)
        self.assertEqual(supervisor.predict(request())["_inference"]["provider"], "pi")
        self.assertEqual(len(validations), 2)

    def test_worker_cannot_spoof_provider_metadata(self):
        spoofed = response(request())
        spoofed["_inference"] = {"provider": "mac", "generation": 999}
        supervisor = self.make(local=Backend(spoofed))
        result = supervisor.predict(request())
        self.assertEqual(result["_inference"]["provider"], "pi")
        self.assertEqual(result["_inference"]["generation"], 0)

    def test_both_fail_without_accepting_any_result(self):
        supervisor = self.make(Backend(RuntimeError("local stopped")),
                               Backend(TimeoutError("Mac timed out")))
        with self.assertRaisesRegex(InferenceUnavailable, "local stopped"):
            supervisor.predict(request())
        snapshot = supervisor.snapshot()
        self.assertEqual(snapshot["state"], "unavailable")
        self.assertEqual(snapshot["counters"], {"mac": 0, "pi": 0})
        self.assertIsNone(snapshot["active_provider"])
        self.assertIsNone(snapshot["in_flight_provider"])

    def test_recovery_requires_spaced_valid_inferences_and_does_not_count_probes(self):
        remote = Backend(ConnectionError("offline"))
        supervisor = self.make(remote=remote)
        supervisor.predict(request())
        self.assertFalse(supervisor._probe_once())
        self.clock.advance()
        self.assertTrue(supervisor._probe_once())
        self.assertFalse(supervisor._probe_once())
        self.assertEqual(supervisor.snapshot()["recovery"]["successes"], 1)
        self.recover(supervisor, checks=2)
        snapshot = supervisor.snapshot()
        self.assertTrue(snapshot["recovery"]["ready"])
        self.assertEqual(snapshot["selected_provider"], "pi")
        self.assertEqual(snapshot["active_provider"], "pi")
        self.assertEqual(snapshot["counters"], {"mac": 0, "pi": 1})
        self.assertEqual(len({call[0]["request_id"] for call in remote.calls}), 4)
        for item, _ in remote.calls:
            self.assertEqual({key: value for key, value in item.items() if key != "request_id"},
                             {key: value for key, value in request().items() if key != "request_id"})
        result = supervisor.predict(request(2))
        self.assertEqual(result["_inference"]["provider"], "mac")
        self.assertEqual(supervisor.snapshot()["counters"], {"mac": 1, "pi": 1})

    def test_failed_probe_resets_consecutive_successes(self):
        remote = Backend(ConnectionError("offline"), response,
                         ConnectionError("intermittent"), response, response, response)
        supervisor = self.make(remote=remote)
        supervisor.predict(request())
        self.recover(supervisor, checks=1)
        self.clock.advance()
        self.assertFalse(supervisor._probe_once())
        self.assertEqual(supervisor.snapshot()["recovery"]["successes"], 0)
        self.recover(supervisor, checks=2)
        self.assertFalse(supervisor.snapshot()["recovery"]["ready"])
        self.recover(supervisor, checks=1)
        self.assertTrue(supervisor.snapshot()["recovery"]["ready"])

    def test_malformed_probe_cannot_promote(self):
        remote = Backend(ConnectionError("offline"), {"status": "healthy"})
        supervisor = self.make(remote=remote)
        supervisor.predict(request())
        self.clock.advance()
        self.assertFalse(supervisor._probe_once())
        self.assertEqual(supervisor.snapshot()["recovery"]["successes"], 0)
        self.assertEqual(supervisor.snapshot()["active_provider"], "pi")

    def test_group_defers_promotion_and_marks_fallback_generation(self):
        supervisor = self.make(remote=Backend(ConnectionError("offline")))
        with supervisor.observation_group() as start_generation:
            supervisor.predict(request())
            self.assertNotEqual(start_generation, supervisor.generation)
        with supervisor.observation_group() as local_generation:
            self.recover(supervisor)
            result = supervisor.predict(request(2))
            self.assertEqual(result["_inference"]["provider"], "pi")
            self.assertEqual(local_generation, supervisor.generation)
            self.assertEqual(supervisor.snapshot()["state"], "failback_pending")
        with supervisor.observation_group() as next_generation:
            self.assertNotEqual(next_generation, local_generation)
            self.assertEqual(supervisor.snapshot()["selected_provider"], "mac")
            self.assertEqual(supervisor.snapshot()["active_provider"], "pi")
            self.assertEqual(supervisor.snapshot()["state"], "switching_to_mac")
            supervisor.predict(request(3))
            self.assertEqual(supervisor.snapshot()["active_provider"], "mac")

    def test_failed_first_normal_mac_request_after_recovery_returns_to_pi(self):
        remote = Backend(ConnectionError("offline"), response, response, response,
                         ConnectionError("offline again"))
        supervisor = self.make(remote=remote)
        supervisor.predict(request())
        self.recover(supervisor)
        self.assertEqual(supervisor.predict(request(2))["_inference"]["provider"], "pi")
        self.assertEqual(supervisor.snapshot()["counters"], {"mac": 0, "pi": 2})
        self.assertEqual(supervisor.snapshot()["recovery"]["successes"], 0)

    def test_local_only_label_does_not_claim_running_on_pi(self):
        supervisor = self.make(local_provider="local")
        result = supervisor.predict(request())
        snapshot = supervisor.snapshot()
        self.assertFalse(snapshot["enabled"])
        self.assertEqual(result["_inference"]["provider"], "local")
        self.assertEqual(snapshot["state"], "local_active")
        self.assertEqual(snapshot["counters"], {"mac": 0, "local": 1})

    def test_log_restores_counts_and_history_but_no_active_result(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            supervisor = self.make(remote=Backend(), event_path=path)
            supervisor.predict(request())
            previous_session = supervisor.snapshot()["session_id"]
            supervisor.close()
            restored = self.make(remote=Backend(), event_path=path)
            snapshot = restored.snapshot()
            self.assertEqual(snapshot["counters"]["mac"], 1)
            self.assertIsNone(snapshot["active_provider"])
            self.assertIsNone(snapshot["last_result"])
            self.assertEqual(snapshot["selected_provider"], "mac")
            self.assertNotEqual(snapshot["session_id"], previous_session)
            self.assertTrue(any(event["session_id"] == previous_session
                                for event in snapshot["events"]))
            restored.close()

    def test_corrupt_and_truncated_audit_lines_do_not_erase_good_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            original = self.make(event_path=path)
            original.predict(request())
            original.close()
            with path.open("a") as stream:
                stream.write('not-json\n{"truncated":')
            restored = self.make(event_path=path)
            self.assertEqual(restored.snapshot()["counters"]["pi"], 1)
            self.assertIn("Ignored 2 damaged", restored.snapshot()["events"][-1]["message"])
            restored.predict(request(2))
            restored.close()
            again = self.make(event_path=path)
            self.assertEqual(again.snapshot()["counters"]["pi"], 2)
            again.close()

    def test_disk_and_memory_timeline_are_bounded_without_losing_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            supervisor = self.make(event_path=path, event_limit=3)
            for number in range(20):
                supervisor.predict(request(number))
            self.assertEqual(len(supervisor.snapshot()["events"]), 3)
            self.assertLessEqual(len(path.read_text().splitlines()), 6)
            self.assertEqual(json.loads(path.read_text().splitlines()[-1])["counters"]["pi"], 20)
            supervisor.close()
            restored = self.make(event_path=path, event_limit=3)
            self.assertEqual(restored.snapshot()["counters"]["pi"], 20)
            restored.close()

    def test_audit_failure_blocks_acceptance_and_further_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            local, remote = Backend(), Backend()
            supervisor = self.make(local, remote, event_path=Path(directory) / "events.jsonl")
            supervisor._path = Path(directory)  # Simulate storage changing to an unwritable target.
            with self.assertRaises(InferenceAuditError):
                supervisor.predict(request())
            snapshot = supervisor.snapshot()
            self.assertEqual(snapshot["state"], "audit_failed")
            self.assertEqual(snapshot["counters"], {"mac": 0, "pi": 0})
            self.assertEqual(len(local.calls), 0)
            with self.assertRaises(InferenceAuditError):
                supervisor.predict(request(2))
            self.assertEqual(len(remote.calls), 1)

    def test_callback_runs_without_state_lock_and_snapshot_has_no_disk_io(self):
        holder, failures = {}, []

        def update(snapshot):
            if "supervisor" not in holder:
                return
            worker = threading.Thread(target=holder["supervisor"].snapshot)
            worker.start()
            worker.join(timeout=0.2)
            if worker.is_alive():
                failures.append("snapshot blocked by callback lock")

        supervisor = self.make(on_update=update)
        holder["supervisor"] = supervisor
        supervisor.predict(request())
        self.assertEqual(failures, [])
        supervisor._path = Path("/a/nonexistent/audit/path")
        self.assertEqual(supervisor.snapshot()["active_provider"], "pi")
        supervisor._path = None

    def test_config_rejects_unbounded_or_invalid_deadlines(self):
        for value in (0, -1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.make(remote_timeout_s=value)


class ConcurrencyTests(unittest.TestCase):
    def test_failback_is_not_active_until_a_normal_mac_result_is_accepted(self):
        entered, release = threading.Event(), threading.Event()

        def delayed(item):
            entered.set()
            release.wait(1)
            return response(item)

        remote = Backend(ConnectionError("offline"), response, response, response, delayed)
        clock = FakeClock()
        supervisor = ManualSupervisor(Backend(), remote, clock=clock, probe_interval_s=1)
        results = []
        supervisor.predict(request())
        for _ in range(3):
            clock.advance()
            supervisor._probe_once()
        worker = threading.Thread(target=lambda: results.append(supervisor.predict(request(2))))
        worker.start()
        try:
            self.assertTrue(entered.wait(0.5))
            snapshot = supervisor.snapshot()
            self.assertEqual(snapshot["selected_provider"], "mac")
            self.assertEqual(snapshot["in_flight_provider"], "mac")
            self.assertEqual(snapshot["active_provider"], "pi")
            self.assertEqual(snapshot["state"], "switching_to_mac")
            self.assertEqual(snapshot["counters"], {"mac": 0, "pi": 1})
        finally:
            release.set()
            worker.join(timeout=0.5)
        self.assertEqual(results[0]["_inference"]["provider"], "mac")
        self.assertIsNone(supervisor.snapshot()["failure"])
        supervisor.close()

    def test_late_remote_reply_is_ignored_and_cannot_queue_more_remote_calls(self):
        release = threading.Event()
        entered = threading.Event()

        def delayed(item):
            entered.set()
            release.wait(1)
            return response(item)

        clock = FakeClock()
        remote, local = Backend(delayed), Backend()
        supervisor = ManualSupervisor(local, remote, remote_timeout_s=0.04,
                                      clock=clock, probe_interval_s=0.01)
        try:
            result = supervisor.predict(request())
            self.assertTrue(entered.is_set())
            self.assertEqual(result["_inference"]["provider"], "pi")
            clock.advance()
            self.assertFalse(supervisor._probe_once())
            self.assertEqual(len(remote.calls), 1)
            release.set()
            time.sleep(0.02)
            snapshot = supervisor.snapshot()
            self.assertEqual(snapshot["active_provider"], "pi")
            self.assertEqual(snapshot["counters"], {"mac": 0, "pi": 1})
        finally:
            release.set()
            supervisor.close()

    def test_close_cancels_waiting_operation_without_accepting_late_result(self):
        entered, release = threading.Event(), threading.Event()

        def delayed(item):
            entered.set()
            release.wait(1)
            return response(item)

        supervisor = ManualSupervisor(Backend(delayed), local_timeout_s=1)
        errors = []

        def predict():
            try:
                supervisor.predict(request())
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=predict)
        worker.start()
        try:
            self.assertTrue(entered.wait(0.5))
            supervisor.close()
            worker.join(timeout=0.2)
            self.assertFalse(worker.is_alive())
            self.assertTrue(errors)
            self.assertIsInstance(errors[0], InferenceUnavailable)
            self.assertEqual(supervisor.snapshot()["counters"]["pi"], 0)
            self.assertEqual(supervisor.snapshot()["state"], "stopped")
        finally:
            release.set()
            worker.join(timeout=0.5)
            supervisor.close()

    def test_concurrent_operation_is_rejected_without_queue(self):
        entered, release = threading.Event(), threading.Event()

        def delayed(item):
            entered.set()
            release.wait(1)
            return response(item)

        supervisor = ManualSupervisor(Backend(delayed), local_timeout_s=1)
        outcomes = []
        worker = threading.Thread(target=lambda: outcomes.append(supervisor.predict(request())))
        worker.start()
        try:
            self.assertTrue(entered.wait(0.5))
            with self.assertRaisesRegex(InferenceUnavailable, "already in progress"):
                supervisor.predict(request(2))
            with self.assertRaisesRegex(InferenceUnavailable, "another thread"):
                with supervisor.observation_group():
                    pass
        finally:
            release.set()
            worker.join(timeout=0.5)
            supervisor.close()
        self.assertEqual(len(outcomes), 1)

    def test_background_recovers_while_no_normal_requests_are_running(self):
        remote = Backend(ConnectionError("offline"))
        supervisor = InferenceSupervisor(Backend(), remote, probe_interval_s=0.01,
                                         recovery_successes=2)
        try:
            supervisor.predict(request())
            deadline = time.monotonic() + 1.0
            while not supervisor.snapshot()["recovery"]["ready"] and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(supervisor.snapshot()["recovery"]["ready"])
            self.assertEqual(supervisor.snapshot()["active_provider"], "pi")
            self.assertEqual(supervisor.snapshot()["counters"]["mac"], 0)
            self.assertEqual(len(remote.calls), 3)
        finally:
            supervisor.close()


if __name__ == "__main__":
    unittest.main()
