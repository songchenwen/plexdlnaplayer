import asyncio
import unittest
from unittest import mock

import plex.adapters as pa  # noqa: F401  (import first; dlna imports it back)
import plex.subscribe as ps


class FakeAdapter:
    def __init__(self, state="playing"):
        self.plex_lib = object()
        self.queue = object()
        self.no_notice = False
        self.plex_state = state

    async def get_pms_state(self):
        return {"state": self.plex_state, "time": 1000}


class Device:
    uuid = "u1"
    name = "Fake"


class ServerNotifyTest(unittest.TestCase):
    """The server was only told what was playing while a controller was subscribed.

    Closing the controller part way through an album stopped the timeline
    updates, so the server expired the session and the rest of the album was
    never counted as played.
    """

    def setUp(self):
        self.man = ps.SubscribeManager()
        self.man.subscribers = {}
        self.man.last_server_notify_state = {}
        self.man.last_server_notify_at = {}
        self.device = Device()
        self.adapter = FakeAdapter()
        self.sent = []

    def _notify(self, force=False):
        class Res:
            def raise_for_status(self):
                return None

        class Ctx:
            def __init__(self, outer):
                self.outer = outer

            async def __aenter__(self):
                self.outer.sent.append(1)
                return Res()

            async def __aexit__(self, *a):
                return False

        with mock.patch.object(ps, "adapter_by_device", return_value=self.adapter), \
             mock.patch.object(ps, "pms_header", return_value={}), \
             mock.patch.object(ps.g, "http", create=True) as http:
            http.get = lambda *a, **k: Ctx(self)
            self.adapter.plex_lib = mock.Mock()
            asyncio.run(self.man.notify_server_device(self.device, force=force))

    def test_the_server_is_told_with_nothing_subscribed(self):
        self._notify()
        self.assertEqual(len(self.sent), 1, "server never heard about playback")

    def test_an_unchanged_state_is_not_resent_every_pass(self):
        self._notify()
        self._notify()
        self.assertEqual(len(self.sent), 1, "the loop would hammer the server")

    def test_the_server_hears_again_once_the_interval_is_up(self):
        self._notify()
        self.man.last_server_notify_at["u1"] = ps.monotonic() - ps.SERVER_NOTIFY_SECONDS - 1
        self._notify()
        self.assertEqual(len(self.sent), 2, "the session would expire")

    def test_a_state_change_is_sent_at_once(self):
        self._notify()
        self.adapter.plex_state = "paused"
        self._notify()
        self.assertEqual(len(self.sent), 2)


if __name__ == "__main__":
    unittest.main()
