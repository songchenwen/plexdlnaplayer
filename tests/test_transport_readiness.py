import asyncio
import unittest
from unittest import mock

import plex.adapters as pa


class Track:
    def __init__(self, key, duration=200000):
        self.key = key
        self.duration = duration


class FakeQueue:
    def __init__(self, track):
        self.track = track
        self.repeat = 0

    async def selected_track(self):
        return self.track

    def url_for_track(self, track):
        return "http://plex/%s.flac" % track.key


class FakeState:
    """__del__ on the real adapter touches state, so every fake needs one."""

    def __init__(self, state=None):
        self.state = state
        self.current_uri = None
        self.check_all_next_loop = False
        self._thread_should_stop = False
        self.updates = []

    def update(self, **kwargs):
        self.updates.append(kwargs)
        if "state" in kwargs:
            self.state = kwargs["state"]


class FakeDlna:
    """A renderer that reports a scripted sequence of transport actions.

    Each entry is what GetCurrentTransportActions answers on the next call, so
    a renderer that is not ready yet is written as ["Stop", "Stop", "Play"].
    """

    def __init__(self, action_sequence=("Play",), supports=True):
        self.name = "Fake"
        self._actions = list(action_sequence)
        self._supports = supports
        self.action_reads = 0
        self.played = 0

    async def supports(self, action):
        return self._supports

    async def GetCurrentTransportActions(self, data=None, client=None):
        actions = self._actions[min(self.action_reads, len(self._actions) - 1)]
        self.action_reads += 1
        return type("R", (), {"Actions": actions})()

    async def SetAVTransportURI(self, data=None, client=None):
        return None

    async def Play(self, data=None, client=None):
        self.played += 1
        return None


def make_adapter(dlna, queue=None, state=None):
    a = pa.PlexDlnaAdapter.__new__(pa.PlexDlnaAdapter)
    a.dlna = dlna
    a.queue = queue
    a.state = state if state is not None else FakeState()
    return a


class AllowedActionsTest(unittest.TestCase):
    def test_a_renderer_without_the_action_is_not_asked(self):
        """None means unknown, which is different from an empty set."""
        dlna = FakeDlna(supports=False)
        a = make_adapter(dlna)
        self.assertIsNone(asyncio.run(a.allowed_actions()))
        self.assertEqual(dlna.action_reads, 0)

    def test_the_comma_list_is_split_and_trimmed(self):
        a = make_adapter(FakeDlna(action_sequence=["Play, Stop ,Seek"]))
        self.assertEqual(asyncio.run(a.allowed_actions()), {"Play", "Stop", "Seek"})

    def test_an_empty_answer_is_an_empty_set_not_a_blank_action(self):
        a = make_adapter(FakeDlna(action_sequence=[""]))
        self.assertEqual(asyncio.run(a.allowed_actions()), set())

    def test_a_failed_read_is_unknown_rather_than_an_exception(self):
        dlna = FakeDlna()

        async def boom(data=None, client=None):
            raise Exception("no answer")

        dlna.GetCurrentTransportActions = boom
        a = make_adapter(dlna)
        self.assertIsNone(asyncio.run(a.allowed_actions()))


class StartWhenReadyTest(unittest.TestCase):
    def _adapter(self, dlna):
        return make_adapter(dlna, FakeQueue(Track("t1")))

    def test_a_ready_renderer_is_not_waited_on(self):
        """The flat sleep cost every track start a second it did not need."""
        dlna = FakeDlna(action_sequence=["Play"])
        a = self._adapter(dlna)
        with mock.patch.object(pa.asyncio, "sleep", new=mock.AsyncMock()) as slept:
            asyncio.run(a._start_when_ready())
        slept.assert_not_awaited()
        self.assertEqual(dlna.action_reads, 1)
        self.assertEqual(dlna.played, 1)

    def test_it_waits_out_a_transport_that_is_still_settling(self):
        dlna = FakeDlna(action_sequence=["", "", "Play"])
        a = self._adapter(dlna)
        with mock.patch.object(pa.asyncio, "sleep", new=mock.AsyncMock()):
            asyncio.run(a._start_when_ready())
        self.assertEqual(dlna.action_reads, 3)
        self.assertEqual(dlna.played, 1)

    def test_a_renderer_that_already_started_is_not_told_to_play(self):
        """Measured on a Hegel H150: while PLAYING it offers Pause,Stop,Seek.

        Play never appears, so waiting for it alone burned the whole timeout on
        every track the renderer auto-started, then sent a Play that 701s. Seen
        as six of ten track starts stalling for the full two seconds.
        """
        dlna = FakeDlna(action_sequence=["Pause,Stop,Seek"])
        a = self._adapter(dlna)
        with mock.patch.object(pa.asyncio, "sleep", new=mock.AsyncMock()) as slept:
            asyncio.run(a._start_when_ready())
        slept.assert_not_awaited()
        self.assertEqual(dlna.action_reads, 1)
        self.assertEqual(dlna.played, 0, "played into a transport already playing")

    def test_a_renderer_that_never_settles_falls_back_to_the_old_guard(self):
        dlna = FakeDlna(action_sequence=[""])
        a = make_adapter(dlna, FakeQueue(Track("t1")), FakeState("STOPPED"))
        asyncio.run(a._start_when_ready(timeout=0.3))
        self.assertEqual(dlna.played, 1)

    def test_a_renderer_that_never_settles_is_left_alone_if_already_playing(self):
        dlna = FakeDlna(action_sequence=[""])
        a = make_adapter(dlna, FakeQueue(Track("t1")), FakeState("PLAYING"))
        asyncio.run(a._start_when_ready(timeout=0.3))
        self.assertEqual(dlna.played, 0)

    def test_a_renderer_that_never_answers_cannot_outlast_the_bound(self):
        """One control request can spend the whole retry budget by itself.

        Bounding each attempt instead of the wait as a whole would make this
        slower than the flat sleep it replaces.
        """
        dlna = FakeDlna()

        async def never_answers(data=None, client=None):
            await asyncio.sleep(30)

        dlna.GetCurrentTransportActions = never_answers
        a = make_adapter(dlna, FakeQueue(Track("t1")), FakeState("PLAYING"))

        async def scenario():
            import time

            t0 = time.monotonic()
            await a._start_when_ready(timeout=0.3)
            return time.monotonic() - t0

        self.assertLess(asyncio.run(scenario()), 1.0)

    def test_a_renderer_that_cannot_be_asked_keeps_the_old_sleep(self):
        dlna = FakeDlna(supports=False)
        a = make_adapter(dlna, FakeQueue(Track("t1")), FakeState("STOPPED"))
        with mock.patch.object(pa.asyncio, "sleep", new=mock.AsyncMock()) as slept:
            asyncio.run(a._start_when_ready())
        slept.assert_awaited_once_with(1)
        self.assertEqual(dlna.played, 1)


class PlayGuardTest(unittest.TestCase):
    """`self.state != "PLAYING"` compared a DlnaState to a str.

    DlnaState defines no __eq__, so that was always true and Play was issued
    unconditionally, including at a renderer that had already started itself.
    Only the fallback path still reads it, so that is where it is checked.
    """

    def _run(self, reported_state):
        dlna = FakeDlna(action_sequence=[""])
        a = make_adapter(dlna, FakeQueue(Track("t1")), FakeState(reported_state))
        asyncio.run(a._start_when_ready(timeout=0.3))
        return dlna.played

    def test_play_is_skipped_when_the_bridge_already_saw_it_playing(self):
        self.assertEqual(self._run("PLAYING"), 0)

    def test_play_is_issued_when_the_renderer_is_still_stopped(self):
        self.assertEqual(self._run("STOPPED"), 1)


if __name__ == "__main__":
    unittest.main()
