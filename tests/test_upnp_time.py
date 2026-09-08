import asyncio
import unittest
from datetime import timedelta

import plex.adapters as pa
from utils import parse_timedelta


class ParseTimedeltaTest(unittest.TestCase):
    """AVTransport times are H+:MM:SS[.F+].

    strptime("%H:%M:%S") rejects the fractional form the spec allows, any track
    past the 24 hour mark, and "NOT_IMPLEMENTED", which is the ordinary answer
    from a renderer that does not track position. Each of those raised out of
    the state loop and ended it for that device.
    """

    def test_the_ordinary_form(self):
        self.assertEqual(parse_timedelta("0:03:21"), timedelta(minutes=3, seconds=21))

    def test_the_fractional_form_the_spec_allows(self):
        self.assertEqual(parse_timedelta("00:00:00.000"), timedelta(0))
        self.assertEqual(parse_timedelta("0:03:21.500"),
                         timedelta(minutes=3, seconds=21, milliseconds=500))

    def test_hours_are_not_capped_at_a_day(self):
        self.assertEqual(parse_timedelta("25:13:00"), timedelta(hours=25, minutes=13))

    def test_values_that_are_not_times_are_none_rather_than_an_exception(self):
        for s in ("NOT_IMPLEMENTED", "", "   ", "nonsense", "1:60:00", "1:00:99", None, 0):
            self.assertIsNone(parse_timedelta(s), repr(s))


class StateLoopSurvivesTest(unittest.TestCase):
    """A renderer that answers with something unusable must not end the loop.

    _check_loop exiting is permanent: update() then discards every change with
    "no running loop", so that device reports nothing until the bridge restarts.
    """

    def test_one_failing_check_does_not_end_the_loop(self):
        state = pa.DlnaState.__new__(pa.DlnaState)
        state.dlna = type("D", (), {"name": "Fake"})()
        state._thread_should_stop = False
        state.change_session_lock = None
        state.looping_wait_event = None
        state.running_loop = None
        calls = []

        async def check(client, check_count=0):
            calls.append(check_count)
            if len(calls) == 1:
                raise ValueError("time data 'NOT_IMPLEMENTED' does not match format")
            if len(calls) >= 3:
                state._thread_should_stop = True

        async def wait_for_next_loop():
            return

        state.check = check
        state.wait_for_next_loop = wait_for_next_loop
        asyncio.run(state._check_loop())
        self.assertGreaterEqual(len(calls), 3, "the loop stopped at the first bad answer")


class MissingPositionTest(unittest.TestCase):
    def test_an_absent_position_keeps_the_last_known_one(self):
        """Reporting 0 for a renderer that cannot say would look like a stuck track."""
        self.assertIsNone(parse_timedelta("NOT_IMPLEMENTED"))


if __name__ == "__main__":
    unittest.main()
