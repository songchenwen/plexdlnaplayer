"""A track loaded but never started can be neither paused nor seeked.

The H150 settles back to STOPPED after SetAVTransportURI, where Rygel refuses
Pause with 701 and Seek with 710. Both were sent anyway.
"""
import asyncio
import unittest
from unittest import mock

import plex.adapters as pa

from tests.test_transport_readiness import FakeDlna, FakeQueue, FakeState, Track, make_adapter


class PausingDlna(FakeDlna):
    def __init__(self, action_sequence=("Play",), supports=True, pause_fails=False):
        super().__init__(action_sequence, supports)
        self.paused = 0
        self.seeks = []
        self.pause_fails = pause_fails

    async def Pause(self, data=None, client=None):
        self.paused += 1
        if self.pause_fails:
            raise Exception("UPnPError 701 Transition not available")

    async def Seek(self, data=None, client=None):
        self.seeks.append(data)


def adapter(dlna, state=None):
    return make_adapter(dlna, FakeQueue(Track("t1")), state)


class PauseWhenReadyTest(unittest.TestCase):
    def test_a_stopped_renderer_is_not_asked_to_pause(self):
        """The whole bug: Pause on a transport that only offers Play is a 701."""
        dlna = PausingDlna(action_sequence=["Play"])
        a = adapter(dlna)
        asyncio.run(a._pause_when_ready())
        self.assertEqual(dlna.paused, 0)

    def test_a_playing_renderer_is_paused(self):
        dlna = PausingDlna(action_sequence=["Pause,Stop,Seek"])
        a = adapter(dlna)
        asyncio.run(a._pause_when_ready())
        self.assertEqual(dlna.paused, 1)

    def test_a_renderer_that_cannot_report_is_paused_as_before(self):
        """No regression for renderers without GetCurrentTransportActions."""
        dlna = PausingDlna(supports=False)
        a = adapter(dlna)
        with mock.patch.object(pa.asyncio, "sleep", new=mock.AsyncMock()):
            asyncio.run(a._pause_when_ready())
        self.assertEqual(dlna.paused, 1)

    def test_a_refused_pause_does_not_leave_a_paused_state_behind(self):
        """pause() reports PAUSED_PLAYBACK before the command, so a refusal
        used to leave the bridge claiming a state the amp was not in."""
        dlna = PausingDlna(action_sequence=["Play"])
        state = FakeState(state="STOPPED")
        a = adapter(dlna, state)
        asyncio.run(a._pause_when_ready())
        self.assertNotIn("PAUSED_PLAYBACK", [u.get("state") for u in state.updates])
        self.assertEqual(state.state, "STOPPED")


class SeekWhenAllowedTest(unittest.TestCase):
    def test_a_transport_that_never_offers_seek_is_not_asked(self):
        # A real sleep here on purpose: the poll loop has to yield for the
        # caller's timeout to be able to fire at all.
        dlna = PausingDlna(action_sequence=["Play"])
        a = adapter(dlna)
        asyncio.run(a._seek_when_allowed(30000, timeout=0.05))
        self.assertEqual(dlna.seeks, [])

    def test_a_seekable_transport_is_seeked(self):
        dlna = PausingDlna(action_sequence=["Pause,Stop,Seek"])
        a = adapter(dlna)
        asyncio.run(a._seek_when_allowed(30000))
        self.assertEqual(len(dlna.seeks), 1)

    def test_a_renderer_that_cannot_report_is_seeked_straight_away(self):
        dlna = PausingDlna(supports=False)
        a = adapter(dlna)
        asyncio.run(a._seek_when_allowed(30000))
        self.assertEqual(len(dlna.seeks), 1)

    def test_seek_waits_for_a_transport_that_is_still_settling(self):
        dlna = PausingDlna(action_sequence=["Play", "Play", "Pause,Stop,Seek"])
        a = adapter(dlna)
        with mock.patch.object(pa.asyncio, "sleep", new=mock.AsyncMock()):
            asyncio.run(a._seek_when_allowed(30000))
        self.assertEqual(len(dlna.seeks), 1)


if __name__ == "__main__":
    unittest.main()
