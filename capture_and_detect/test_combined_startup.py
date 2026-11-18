"""Offline retirement checks. No real PID, server or signal is used."""
from pathlib import Path
import ctypes
import errno
import signal
import sys
from unittest.mock import Mock, patch

import tempfile
import unittest

import combined_startup as startup


def make_installation(tmp_path, monkeypatch):
    tmp_path = tmp_path.resolve()
    root, repo = tmp_path / "old installation", tmp_path / "repository with spaces"
    private = root / "private"
    private.mkdir(parents=True)
    (private / "dashboard.pid").write_text("12001\n")
    (private / "mac-service.pid").write_text("12002\n")
    (private / "mac-service.token").write_text("fake-test-token-" + "a" * 32)
    dashboard = (sys.executable, str(repo / "capture_and_detect/web_app.py"),
                 "--device-config", str(private / "devices.json"), "--live", "--output",
                 str(private / "dashboard-cache"), "--port", "8086")
    service = (sys.executable, "-m", "device_service", "--device-id", "mac", "--backend", "python",
               "--state-dir", str(private / "mac-state"), "--model", str(repo / "capture_and_detect/best.pt"),
               "--token-file", str(private / "mac-service.token"), "--robot-id", "mdu-yumi-lynx-9113c0",
               "--gateway", "http://192.168.125.20:8000", "--controller-profile", str(private / "mac-state/controller-status.json"),
               "--listen", "127.0.0.1", "--port", "8091", "--live")
    processes = {12001: startup.ProcessIdentity(12001, dashboard, "start-dashboard"),
                 12002: startup.ProcessIdentity(12002, service, "start-service")}
    dashboard_state = {"mode": "live", "busy": False, "stop_in_flight": False, "devices": {"switching": False}}
    service_state = {"health": {"device_id": "mac", "mode": "live", "busy": False,
                     "transfer_incomplete": False, "handoff_blocked": False, "controller_present": True},
                     "state": {"busy": False, "stop_in_flight": False}}
    requests, signals = [], []

    def get_json(port, path, token=None):
        requests.append((port, path, bool(token)))
        assert (port, path) in [(8086, "/api/state"), (8091, "/v1/state")]
        assert bool(token) == (port == 8091)
        return dashboard_state if port == 8086 else service_state

    def kill(pid, signum):
        signals.append((pid, signum))
        processes.pop(pid)

    monkeypatch.setattr(startup, "_identity", lambda pid: processes.get(pid))
    monkeypatch.setattr(startup, "_listener", lambda port: {12001 if port == 8086 else 12002})
    monkeypatch.setattr(startup, "_get_json", get_json)
    monkeypatch.setattr(startup.os, "kill", kill)
    return root, repo, private, processes, dashboard_state, service_state, requests, signals


class Patcher:
    def __init__(self, cleanup):
        self.cleanup = cleanup

    def setattr(self, target, name, value):
        change = patch.object(target, name, value)
        change.start()
        self.cleanup(change.stop)


class CombinedStartupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.patcher = Patcher(self.addCleanup)
        self.installation = make_installation(Path(self.temporary.name), self.patcher)

    def test_graceful_shutdown_verifies_both_then_rechecks_in_order_and_preserves_files(self):
        installation, monkeypatch = self.installation, self.patcher
        root, repo, private, processes, _, _, requests, signals = installation
        before = {path.name: path.read_bytes() for path in private.iterdir()}
        result = startup.retire_idle_legacy(root, repo)
        assert signals == [(12001, signal.SIGTERM), (12002, signal.SIGTERM)]
        assert requests == [(8086, "/api/state", False), (8091, "/v1/state", True),
                            (8086, "/api/state", False), (8091, "/v1/state", True)]
        assert result["stopped"] == ["dashboard", "Mac service"]
        assert processes == {}
        assert {path.name: path.read_bytes() for path in private.iterdir()} == before


    def test_busy_or_unverified_state_aborts_before_any_signal(self):
        for change in ["dashboard_busy", "switching", "dashboard_stop", "controller_busy", "service_busy", "service_stop", "handoff", "unverified"]:
            with self.subTest(change=change):
                installation = make_installation(Path(self.temporary.name) / change, self.patcher)
                root, repo, _, _, dashboard, service, _, signals = installation
                if change == "dashboard_busy": dashboard["busy"] = True
                elif change == "switching": dashboard["devices"]["switching"] = True
                elif change == "dashboard_stop": dashboard["stop_in_flight"] = True
                elif change == "controller_busy": service["state"]["busy"] = True
                elif change == "service_busy": service["health"]["busy"] = True
                elif change == "service_stop": service["state"]["stop_in_flight"] = True
                elif change == "handoff": service["health"]["transfer_incomplete"] = True
                elif change == "unverified": service["health"].pop("busy")
                with self.assertRaises(startup.CombinedStartupError):
                    startup.retire_idle_legacy(root, repo)
                assert signals == []


    def test_saved_foreign_pid_is_never_signalled_or_contacted(self):
        installation, monkeypatch = self.installation, self.patcher
        root, repo, _, processes, _, _, requests, signals = installation
        processes[12002] = startup.ProcessIdentity(12002, (sys.executable, "-m", "other_service"), "another-start")
        with self.assertRaisesRegex(startup.CombinedStartupError, "unexpected process"):
            startup.retire_idle_legacy(root, repo)
        assert signals == requests == []


    def test_matching_command_on_another_port_owner_is_not_stopped(self):
        installation, monkeypatch = self.installation, self.patcher
        root, repo, _, _, _, _, _, signals = installation
        monkeypatch.setattr(startup, "_listener", lambda port: {99123})
        with self.assertRaisesRegex(startup.CombinedStartupError, "not owned solely"):
            startup.retire_idle_legacy(root, repo)
        assert signals == []


    def test_unreachable_service_aborts_without_stopping_dashboard(self):
        installation, monkeypatch = self.installation, self.patcher
        root, repo, _, _, _, _, _, signals = installation
        monkeypatch.setattr(startup, "_get_json", Mock(side_effect=startup.CombinedStartupError("unreachable")))
        with self.assertRaisesRegex(startup.CombinedStartupError, "unreachable"):
            startup.retire_idle_legacy(root, repo)
        assert signals == []


    def test_timeout_sends_no_force_kill_and_preserves_pid_files(self):
        installation, monkeypatch = self.installation, self.patcher
        root, repo, private, _, _, _, _, signals = installation
        monkeypatch.setattr(startup.os, "kill", lambda pid, signum: signals.append((pid, signum)))
        times = iter([0, 11])
        monkeypatch.setattr(startup.time, "monotonic", lambda: next(times))
        with self.assertRaisesRegex(startup.CombinedStartupError, "No force-kill"):
            startup.retire_idle_legacy(root, repo)
        assert signals == [(12001, signal.SIGTERM)]
        assert (private / "dashboard.pid").read_text() == "12001\n"
        assert (private / "mac-service.pid").read_text() == "12002\n"


    def test_missing_pid_files_are_a_no_op(self):
        installation, monkeypatch = self.installation, self.patcher
        root, repo, private, _, _, _, requests, signals = installation
        (private / "dashboard.pid").unlink()
        (private / "mac-service.pid").unlink()
        result = startup.retire_idle_legacy(root, repo)
        assert result == {"checked": [], "stopped": [], "absent": ["dashboard", "Mac service"]}
        assert requests == signals == []


    def test_dead_pids_are_not_signalled(self):
        installation, monkeypatch = self.installation, self.patcher
        root, repo, _, processes, _, _, requests, signals = installation
        processes.clear()
        assert startup.retire_idle_legacy(root, repo)["checked"] == []
        assert requests == signals == []


    def test_check_only_validates_without_signalling(self):
        installation, monkeypatch = self.installation, self.patcher
        root, repo, _, _, _, _, requests, signals = installation
        result = startup.retire_idle_legacy(root, repo, check_only=True)
        assert result["checked"] == ["dashboard", "Mac service"]
        assert result["stopped"] == signals == []
        assert len(requests) == 2


    def test_service_admitted_job_before_dashboard_closed_is_preserved(self):
        installation, monkeypatch = self.installation, self.patcher
        root, repo, _, processes, _, service, _, signals = installation

        def kill(pid, signum):
            signals.append((pid, signum))
            processes.pop(pid)
            service["state"]["busy"] = True

        monkeypatch.setattr(startup.os, "kill", kill)
        with self.assertRaisesRegex(startup.CombinedStartupError, "Already closed: dashboard.*busy"):
            startup.retire_idle_legacy(root, repo)
        assert signals == [(12001, signal.SIGTERM)]
        assert 12002 in processes


    def test_identity_change_before_signal_aborts(self):
        installation, monkeypatch = self.installation, self.patcher
        root, repo, _, processes, _, _, _, signals = installation
        calls = 0

        def identity(pid):
            nonlocal calls
            calls += 1
            previous = processes[pid]
            return previous if calls < 5 else startup.ProcessIdentity(pid, previous.argv, "reused-pid")

        monkeypatch.setattr(startup, "_identity", identity)
        with self.assertRaisesRegex(startup.CombinedStartupError, "process changed"):
            startup.retire_idle_legacy(root, repo)
        assert signals == []


    def test_inactive_mac_service_can_close_without_a_controller(self):
        installation, monkeypatch = self.installation, self.patcher
        root, repo, _, _, _, service, _, signals = installation
        service["state"] = None
        service["health"]["controller_present"] = False
        startup.retire_idle_legacy(root, repo)
        assert signals == [(12001, signal.SIGTERM), (12002, signal.SIGTERM)]

    def test_darwin_arguments_keep_spaces_and_exclude_environment(self):
        expected = ("/python", "/repository with spaces/web_app.py", "--port", "8086")
        raw = len(expected).to_bytes(4, sys.byteorder) + b"/python\0\0" + b"\0".join(value.encode() for value in expected) + b"\0PRIVATE_TEST_KEY=do-not-return\0"

        class Kernel:
            def sysctl(self, mib, count, destination, size, new, new_size):
                ctypes.cast(size, ctypes.POINTER(ctypes.c_size_t)).contents.value = len(raw)
                if destination is not None:
                    ctypes.memmove(destination, raw, len(raw))
                return 0

        self.patcher.setattr(startup.sys, "platform", "darwin")
        self.patcher.setattr(startup.ctypes, "CDLL", lambda *args, **kwargs: Kernel())
        assert startup._argv(12001) == expected

    def test_malformed_pid_file_fails_without_any_lookup_or_signal(self):
        root, repo, private, _, _, _, requests, signals = self.installation
        (private / "dashboard.pid").write_text("1\n")
        identity = Mock(side_effect=AssertionError("No process lookup for an invalid PID"))
        self.patcher.setattr(startup, "_identity", identity)
        with self.assertRaisesRegex(startup.CombinedStartupError, "PID is invalid"):
            startup.retire_idle_legacy(root, repo)
        assert signals == requests == []


class ProcessIdentityTests(unittest.TestCase):
    """Model Darwin exit/permission races without looking up any real process."""

    @staticmethod
    def status(start="Mon Nov 16 10:20:30 2026", state="S", *, returncode=0, stderr=""):
        return Mock(returncode=returncode, stdout=f"{start} {state}\n" if start else "",
                    stderr=stderr)

    def test_dead_pid_skips_argument_query(self):
        with patch.object(startup.subprocess, "run", return_value=self.status(start="", returncode=1)), \
                patch.object(startup, "_argv") as argv:
            self.assertIsNone(startup._identity(12001))
        argv.assert_not_called()

    def test_exit_during_darwin_argument_query_accepts_only_confirmed_absence(self):
        kernel = Mock()
        kernel.sysctl.return_value = -1
        observations = [self.status(), self.status(start="", returncode=1)]
        with patch.object(startup.subprocess, "run", side_effect=observations) as status, \
                patch.object(startup.sys, "platform", "darwin"), \
                patch.object(startup.ctypes, "CDLL", return_value=kernel), \
                patch.object(startup.ctypes, "get_errno", return_value=errno.EINVAL):
            self.assertIsNone(startup._identity(12001))
        self.assertEqual(status.call_count, 2)
        kernel.sysctl.assert_called_once()

    def test_live_process_argument_permission_error_is_not_absence(self):
        denied = startup.CombinedStartupError("Cannot inspect a saved process; nothing was signalled.")
        with patch.object(startup.subprocess, "run", return_value=self.status()) as status, \
                patch.object(startup, "_argv", side_effect=denied):
            with self.assertRaises(startup.CombinedStartupError) as caught:
                startup._identity(12001)
        self.assertIs(caught.exception, denied)
        self.assertEqual(status.call_count, 2)

    def test_failed_or_ambiguous_status_is_not_absence(self):
        results = [
            self.status(start="", returncode=1, stderr="Operation not permitted"),
            self.status(start="", returncode=2),
            self.status(start="", returncode=0),
            self.status(returncode=1),
            self.status(stderr="Cannot inspect all process information"),
            Mock(returncode=0, stdout="malformed\n", stderr=""),
        ]
        for result in results:
            with self.subTest(result=result), \
                    patch.object(startup.subprocess, "run", return_value=result), \
                    patch.object(startup, "_argv") as argv:
                with self.assertRaises(startup.CombinedStartupError):
                    startup._identity(12001)
                argv.assert_not_called()
        for failure in [PermissionError("denied"), startup.subprocess.TimeoutExpired("ps", 2)]:
            with self.subTest(failure=failure), \
                    patch.object(startup.subprocess, "run", side_effect=failure):
                with self.assertRaises(startup.CombinedStartupError):
                    startup._process_start(12001)

    def test_pid_reuse_between_status_reads_aborts(self):
        observations = [self.status(), self.status(start="Mon Nov 16 10:20:31 2026")]
        with patch.object(startup.subprocess, "run", side_effect=observations), \
                patch.object(startup, "_argv", return_value=("/python", "known script.py")):
            with self.assertRaisesRegex(startup.CombinedStartupError, "process changed"):
                startup._identity(12001)

    def test_zombie_status_skips_argument_query(self):
        with patch.object(startup.subprocess, "run", return_value=self.status(state="Z+")), \
                patch.object(startup, "_argv") as argv:
            self.assertIsNone(startup._identity(12001))
        argv.assert_not_called()

    def test_stable_process_retains_exact_arguments_and_start_time(self):
        arguments = ("/python", "/repository with spaces/web_app.py", "--port", "8086")
        with patch.object(startup.subprocess, "run", return_value=self.status()) as status, \
                patch.object(startup, "_argv", return_value=arguments):
            actual = startup._identity(12001)
        self.assertEqual(actual, startup.ProcessIdentity(12001, arguments, "Mon Nov 16 10:20:30 2026"))
        self.assertEqual(status.call_count, 2)


if __name__ == "__main__":
    unittest.main()
