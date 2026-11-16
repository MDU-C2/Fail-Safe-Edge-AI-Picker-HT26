"""Snapshot age never triggers capture or blocks a reviewed pick; offline only."""

from contextlib import redirect_stdout
import io
import unittest
from unittest.mock import Mock, patch

from cup_commands import CupSession
from cup_inventory import SelectionError
from test_cup_selection import RobotDouble, inventory
from test_support_grid import temporary_grid
from web_app import main


class MacWebFreshnessTests(unittest.TestCase):
    def web_options(self, *flags):
        # Stop before creating a controller or starting any server. This uses the
        # actual web parser and the options passed to its hardware factory.
        class OptionsCaptured(Exception):
            pass

        with patch("requests.Session.request", side_effect=AssertionError("Network forbidden")), \
                patch("web_app.hardware_factory", side_effect=OptionsCaptured) as factory, \
                patch("uvicorn.run") as server:
            with self.assertRaises(OptionsCaptured):
                main(["--gateway", "http://offline.invalid", *flags])
            server.assert_not_called()
        return factory.call_args.args[0]

    def test_web_default_is_thirty_seconds_and_explicit_override_is_retained(self):
        self.assertEqual(self.web_options().max_result_age, 30)
        self.assertEqual(self.web_options("--max-result-age", "12").max_result_age, 12)

    def test_old_snapshot_remains_usable_without_an_additional_capture(self):
        options = self.web_options()
        for age in (15.1, 30.0, 30.1, 86400):
            with self.subTest(age=age), redirect_stdout(io.StringIO()):
                capture = Mock(return_value=(inventory(), 100 - age))
                robot = RobotDouble()
                session = CupSession(capture, Mock(), robot, "cal-1", clock=lambda: 100,
                    max_result_age=options.max_result_age, placing_grid=temporary_grid(self))
                session.scan()
                capture.side_effect = AssertionError("Execution cannot capture a new observation")
                session.execute_selection(action="hover", capture_id="scan-1", cup_id=1)
                self.assertEqual([job["name"] for job in robot.jobs], ["left_ready", "approach_cup"])
                self.assertEqual(capture.call_count, 1)
                self.assertTrue(session.scan_usable)


if __name__ == "__main__":
    unittest.main()
