"""A renderer that drops keep-alive connections must not break playMedia.

The WiiM Pro closes idle connections, so a control request sent on a pooled
one fails with ServerDisconnectedError. control() reported that as None, and
allowed_actions() then read None.Actions, so playMedia answered 500 and
Plexamp stopped the queue.
"""
import asyncio
import unittest
from unittest import mock

from aiohttp import ServerDisconnectedError

import plex.adapters as pa
from dlna.dlna_device import DlnaDeviceService

from tests.test_transport_readiness import FakeDlna, FakeQueue, Track, make_adapter

AVT = "urn:schemas-upnp-org:service:AVTransport:1"

RESPONSE = (
    '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
    '<s:Body><u:GetCurrentTransportActionsResponse xmlns:u="' + AVT + '">'
    "<Actions>Play,Stop</Actions>"
    "</u:GetCurrentTransportActionsResponse></s:Body></s:Envelope>"
)


class NoAnswerDlna(FakeDlna):
    async def GetCurrentTransportActions(self, data=None, client=None):
        self.action_reads += 1
        return None


class NoAnswerTest(unittest.TestCase):
    def test_no_answer_reads_as_unknown(self):
        a = make_adapter(NoAnswerDlna(), FakeQueue(Track("t1")))
        self.assertIsNone(asyncio.run(a.allowed_actions()))

    def test_no_answer_still_starts_playback(self):
        dlna = NoAnswerDlna()
        a = make_adapter(dlna, FakeQueue(Track("t1")))
        with mock.patch.object(pa.asyncio, "sleep", new=mock.AsyncMock()):
            asyncio.run(a._start_when_ready())
        self.assertEqual(dlna.played, 1)


class FakeResponse:
    ok = True
    status = 200

    async def text(self):
        return RESPONSE

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class DroppingClient:
    """Fails the first request the way a closed keep-alive connection does."""

    def __init__(self, drops=1):
        self.drops = drops
        self.posts = 0

    def post(self, *args, **kwargs):
        self.posts += 1
        if self.posts <= self.drops:
            raise ServerDisconnectedError()
        return FakeResponse()


def service():
    device = type("D", (), {"name": "Fake", "repeat_error_count": 0})()
    s = DlnaDeviceService.__new__(DlnaDeviceService)
    s.service_type = AVT
    s.urn = AVT
    s.control_url = "http://renderer/control"
    s.device = device

    async def spec(action, client=None):
        return type("Spec", (), {"argumentList": type("L", (), {"argument": None})()})()

    s.get_action_spec = spec
    return s


class DroppedConnectionRetryTest(unittest.TestCase):
    def test_a_dropped_connection_is_retried(self):
        client = DroppingClient(drops=1)
        with mock.patch("dlna.dlna_device.asyncio.sleep", new=mock.AsyncMock()):
            r = asyncio.run(service().control("GetCurrentTransportActions", {}, client=client))
        self.assertEqual(client.posts, 2)
        self.assertEqual(r.Actions, "Play,Stop")
