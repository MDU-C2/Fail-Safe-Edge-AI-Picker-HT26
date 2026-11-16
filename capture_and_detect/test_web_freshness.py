"""Mac dashboard freshness limits, with synthetic captures and no robot connection."""

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

    def test_default_accepts_reported_age_but_still_blocks_over_thirty_seconds(self):
        options = self.web_options()
        for age, accepted in ((15.1, True), (30.0, True), (30.1, False)):
            with self.subTest(age=age), redirect_stdout(io.StringIO()):
                capture = Mock(side_effect=[(inventory(), 90),
                    (inventory(capture="verification-1", sequence=11), 100 - age)])
                robot = RobotDouble()
                session = CupSession(capture, Mock(), robot, "cal-1", clock=lambda: 100,
                    max_result_age=options.max_result_age, placing_grid=temporary_grid(self))
                session.scan()
                request = {"action": "hover", "capture_id": "scan-1", "cup_id": 1}
                if accepted:
                    session.execute_selection(**request)
                    self.assertEqual([job["name"] for job in robot.jobs], ["left_ready", "approach_cup"])
                else:
                    with self.assertRaisesRegex(SelectionError, r"30\.1s old; motion blocked \(limit 30s\)"):
                        session.execute_selection(**request)
                    self.assertEqual(robot.jobs, [])
                    self.assertFalse(session.scan_usable)


if __name__ == "__main__":
    unittest.main()
