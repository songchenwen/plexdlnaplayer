import unittest
from unittest import mock

from dotmap import DotMap

import plex.adapters as pa


class Track:
    def __init__(self, key, duration=316000):
        self.key = key
        self.duration = duration


class FakeState:
    def __init__(self, duration, elapsed, current_uri="u"):
        self.state = "STOPPED"
        self.current_uri = current_uri
        self.current_track_duration = duration
        self.elapsed = elapsed
        self.check_all_next_loop = False
        self._thread_should_stop = False

    def update(self, **kwargs):
        if "state" in kwargs:
            self.state = kwargs["state"]
        if "uri" in kwargs:
            self.current_uri = kwargs["uri"]


def adapter(duration=316000, elapsed=315000, track_key="t-old"):
    a = pa.PlexDlnaAdapter.__new__(pa.PlexDlnaAdapter)
    a.queue = object()
    a.state = FakeState(duration, elapsed)
    a.current_track_info = Track(track_key)
    a.shuffle = 0
    a.no_notice = False
    a.loop = None
    a._advanced_from_key = None
    a._advance_claim_until = 0
    return a


def ended_by_elapsed():
    return DotMap({"state": None, "uri": None, "elapsed": 0,
                   "old": {"state": "PLAYING", "elapsed": 315000}})


def ended_by_stop():
    return DotMap({"state": "STOPPED", "uri": None, "elapsed": None,
                   "old": {"state": "PLAYING", "elapsed": 0}})


class DoubleAdvanceTest(unittest.TestCase):
    """Observed live 2026-09-01: one boundary advanced three times.

    Both branches can fire for the same track ending, from separate state
    notifications a millisecond apart, and auto_next is handed to another loop
    rather than run inline. Roughly a quarter of boundaries in a 17 hour soak
    skipped a track this way.
    """

    def _count_advances(self, a, changes):
        scheduled = []
        with mock.patch.object(pa.asyncio, "run_coroutine_threadsafe",
                               side_effect=lambda coro, loop: (coro.close(), scheduled.append(1))[1]):
            for c in changes:
                a.check_auto_next(c)
        return len(scheduled)

    def test_one_boundary_schedules_one_advance(self):
        a = adapter()
        self.assertEqual(self._count_advances(a, [ended_by_elapsed(), ended_by_stop()]), 1)

    def test_three_triggers_for_one_boundary_still_advance_once(self):
        a = adapter()
        changes = [ended_by_elapsed(), ended_by_stop(), ended_by_elapsed()]
        self.assertEqual(self._count_advances(a, changes), 1)

    def test_the_next_boundary_advances_again(self):
        """The claim must release, or playback stops advancing after one track."""
        a = adapter()
        self.assertEqual(self._count_advances(a, [ended_by_elapsed()]), 1)
        # What the renderer does next: reports the new track and its position.
        a.current_track_info = Track("t-new")
        a.state.current_uri = "u2"
        a.state.elapsed = 315000
        self.assertEqual(self._count_advances(a, [ended_by_elapsed()]), 1)

    def test_a_stale_claim_expires_rather_than_wedging_playback(self):
        a = adapter()
        self.assertEqual(self._count_advances(a, [ended_by_elapsed()]), 1)
        a._advance_claim_until = pa.monotonic() - 1
        a.state.current_uri = "u"
        self.assertEqual(self._count_advances(a, [ended_by_elapsed()]), 1)


class UnknownDurationTest(unittest.TestCase):
    def test_a_track_with_no_duration_yet_has_not_ended(self):
        """duration 0 and elapsed 0 satisfied `duration - elapsed <= 1`."""
        a = adapter(duration=0, elapsed=0)
        scheduled = []
        with mock.patch.object(pa.asyncio, "run_coroutine_threadsafe",
                               side_effect=lambda coro, loop: (coro.close(), scheduled.append(1))[1]):
            a.check_auto_next(ended_by_stop())
        self.assertEqual(len(scheduled), 0)


if __name__ == "__main__":
    unittest.main()
