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


class EndDetectionTest(unittest.TestCase):
    """Observed live 2026-11-14 on a Linn: the queue stopped after every track.

    Linn answers GetPositionInfo slowly, so readings arrived 6-47 seconds apart
    and the last one before a 343s track ended was 339s. Ending required the
    last reading to be within 2s of the end (or, on STOPPED, within 1ms), so no
    track ever counted as having finished.
    """

    def _advances(self, a, change, seconds_since_reading):
        a._elapsed_seen_at = 1000.0
        scheduled = []
        with mock.patch.object(pa, "monotonic", return_value=1000.0 + seconds_since_reading), \
                mock.patch.object(pa.asyncio, "run_coroutine_threadsafe",
                                  side_effect=lambda coro, loop: (coro.close(), scheduled.append(1))[1]):
            a.check_auto_next(change)
        return len(scheduled)

    def _linn(self, last_reading):
        a = adapter(duration=343000, elapsed=last_reading)
        a.current_track_info = Track("t-old", duration=343000)
        return a

    def _to_zero(self, last_reading):
        return DotMap({"state": None, "uri": None, "elapsed": 0,
                       "old": {"state": "PLAYING", "elapsed": last_reading}})

    def _to_stopped(self):
        return DotMap({"state": "STOPPED", "uri": None, "elapsed": None,
                       "old": {"state": "PLAYING", "elapsed": 0}})

    def test_elapsed_reset_four_seconds_short_of_the_end_advances(self):
        self.assertEqual(self._advances(self._linn(339000), self._to_zero(339000), 6), 1)

    def test_stopped_four_seconds_short_of_the_end_advances(self):
        self.assertEqual(self._advances(self._linn(339000), self._to_stopped(), 6), 1)

    def test_a_reading_long_before_the_end_still_advances_once_time_has_passed(self):
        self.assertEqual(self._advances(self._linn(295000), self._to_zero(295000), 47), 1)

    def test_a_stop_mid_track_does_not_advance(self):
        self.assertEqual(self._advances(self._linn(100000), self._to_zero(100000), 1), 0)
        self.assertEqual(self._advances(self._linn(100000), self._to_stopped(), 1), 0)


class AdvanceReportsPlayingTest(unittest.TestCase):
    """Observed live 2026-11-14: Linn reports STOPPED while switching to the
    next track, Plexamp saw "stopped" in the timeline and sent stop."""

    def _advanced(self):
        a = adapter(duration=314000, elapsed=313000)
        a.current_track_info = Track("t-old", duration=314080)
        a._elapsed_seen_at = None
        with mock.patch.object(pa.asyncio, "run_coroutine_threadsafe",
                               side_effect=lambda coro, loop: coro.close()):
            self.assertTrue(a.check_auto_next(DotMap({"state": None, "uri": None, "elapsed": 0,
                                                      "old": {"state": "PLAYING", "elapsed": 313000}})))
        return a

    def test_stopped_mid_advance_is_reported_as_playing(self):
        a = self._advanced()
        a.state.state = "STOPPED"
        self.assertEqual(a.plex_state, "playing")

    def _report(self, a, state, uri, elapsed):
        a.dlna = mock.Mock(name="dlna")
        a.loop = mock.Mock(is_closed=lambda: False)
        a.state.state, a.state.current_uri, a.state.elapsed = state, uri, elapsed
        with mock.patch.object(pa.asyncio, "run_coroutine_threadsafe", side_effect=lambda coro, loop: coro.close()):
            a.state_changed_callback(DotMap({"state": state, "old": {"state": "STOPPED"}}))

    def _loading(self, a):
        """The renderer then goes STOPPED to load the track."""
        a.state.state = "STOPPED"
        return a.plex_state

    def _starting(self):
        a = self._advanced()
        a._starting_uri = "http://pms/new.flac"
        a._starting_offset = 0
        return a

    def test_the_grace_ends_once_the_new_track_is_under_way(self):
        a = self._starting()
        self._report(a, "PLAYING", "http://pms/new.flac", 2000)
        self.assertEqual(self._loading(a), "stopped")

    def test_playing_for_the_outgoing_track_keeps_the_grace(self):
        """Observed live 2026-11-14 18:43: PLAYING for the old track as it
        ended, then STOPPED to load the next; the stop went out."""
        a = self._starting()
        self._report(a, "PLAYING", "http://pms/old.flac", 2000)
        self.assertEqual(self._loading(a), "playing")

    def test_playing_for_the_new_uri_before_it_has_loaded_keeps_the_grace(self):
        """Observed live 2026-11-14 19:53: PLAYING for the new URI at 0, then
        the old track's position against it, then STOPPED to load."""
        a = self._starting()
        self._report(a, "PLAYING", "http://pms/new.flac", 0)
        self._report(a, "PLAYING", "http://pms/new.flac", 222000)
        self.assertEqual(self._loading(a), "playing")

    def test_a_resumed_track_ends_the_grace_once_past_its_offset(self):
        a = self._starting()
        a._starting_offset = 120000
        self._report(a, "PLAYING", "http://pms/new.flac", 120000)
        self.assertEqual(self._loading(a), "playing")
        self._report(a, "PLAYING", "http://pms/new.flac", 122000)
        self.assertEqual(self._loading(a), "stopped")

    def test_the_grace_expires(self):
        a = self._advanced()
        a.state.state = "STOPPED"
        with mock.patch.object(pa, "monotonic", return_value=pa.monotonic() + pa.ADVANCE_GRACE_SECONDS + 1):
            self.assertEqual(a.plex_state, "stopped")

    def test_a_real_stop_is_reported_at_once(self):
        a = self._advanced()
        a.dlna = mock.Mock(Stop=mock.AsyncMock())
        pa.asyncio.run(a.stop())
        self.assertEqual(a.plex_state, "stopped")

    def test_stopped_while_a_new_track_loads_is_reported_as_playing(self):
        """Observed live 2026-11-14: Linn sat in STOPPED for 12-20s after
        playMedia, and Plexamp sent stop before it ever started."""
        a = adapter(duration=0, elapsed=0)
        a.dlna = mock.Mock(SetAVTransportURI=mock.AsyncMock(), name="dlna")
        a.queue = mock.Mock(selected_track=mock.AsyncMock(return_value=Track("t-new")),
                            url_for_track=lambda t: "http://pms/new.flac")
        a._start_when_ready = mock.AsyncMock()
        pa.asyncio.run(a.play_selected_queue_item())
        a.state.state = "STOPPED"
        self.assertEqual(a.plex_state, "playing")


if __name__ == "__main__":
    unittest.main()
