import asyncio
import unittest

import plex.plexserver as ps
from dlna.discover import DlnaDiscover, get_protocol


class DiscoveryDedupeTest(unittest.TestCase):
    """Only rejected URLs may be remembered.

    device_locations used to record every URL it had seen, and nothing cleared
    it, so once a renderer was removed after ERROR_COUNT_TO_REMOVE failures the
    M-SEARCH answers still arriving every SEND_INTERVAL_SECS were all deduped
    away and it could never come back.
    """

    def _discover(self, accept):
        seen = []

        async def cb(url):
            seen.append(url)
            return accept(url)

        return DlnaDiscover(cb), seen

    def test_an_accepted_url_is_offered_again(self):
        d, seen = self._discover(lambda url: True)
        asyncio.run(d.on_new_device("http://amp/desc.xml"))
        asyncio.run(d.on_new_device("http://amp/desc.xml"))
        self.assertEqual(seen, ["http://amp/desc.xml"] * 2)
        self.assertEqual(d.device_locations, [])

    def test_a_rejected_url_is_only_tried_once(self):
        d, seen = self._discover(lambda url: False)
        asyncio.run(d.on_new_device("http://hue/desc.xml"))
        asyncio.run(d.on_new_device("http://hue/desc.xml"))
        self.assertEqual(seen, ["http://hue/desc.xml"])

    def test_a_burst_of_answers_probes_a_rejected_url_once(self):
        """SSDP answers arrive several at a time and each starts its own task.

        Marking the URL only after the callback returned let every task in the
        burst past the check, and the same description was fetched once per
        packet.
        """
        seen = []
        started = asyncio.Event()

        async def slow_reject(url):
            seen.append(url)
            started.set()
            await asyncio.sleep(0)
            return False

        d = DlnaDiscover(slow_reject)

        async def burst():
            await asyncio.gather(*[d.on_new_device("http://hue/desc.xml")
                                   for _ in range(5)])

        asyncio.run(burst())
        self.assertEqual(seen, ["http://hue/desc.xml"])
        self.assertEqual(d.device_locations, ["http://hue/desc.xml"])

    def test_rejections_do_not_block_other_urls(self):
        d, seen = self._discover(lambda url: "amp" in url)
        asyncio.run(d.on_new_device("http://hue/desc.xml"))
        asyncio.run(d.on_new_device("http://amp/desc.xml"))
        asyncio.run(d.on_new_device("http://hue/desc.xml"))
        asyncio.run(d.on_new_device("http://amp/desc.xml"))
        self.assertEqual(
            seen,
            ["http://hue/desc.xml", "http://amp/desc.xml", "http://amp/desc.xml"],
        )


class RegisterKnownDevicesTest(unittest.TestCase):
    """Startup probes remembered renderers directly.

    The first M-SEARCH sweep can be SEND_INTERVAL_SECS away, so without this the
    player is missing from Plex after a restart even with the amp awake.
    """

    def setUp(self):
        self._devices = list(ps.devices)
        self._settings = ps.settings
        self._on_new = ps.on_new_dlna_device
        self.probed = []

        # Settings is a pydantic v1 BaseSettings, which refuses attribute
        # assignment for anything that is not a declared field, so the whole
        # object is swapped rather than the one method.
        class FakeSettings:
            urls = []

            def known_device_urls(self):
                return list(self.urls)

        ps.settings = FakeSettings()

        async def fake_on_new(url):
            self.probed.append(url)

        ps.on_new_dlna_device = fake_on_new

    def tearDown(self):
        ps.devices[:] = self._devices
        ps.settings = self._settings
        ps.on_new_dlna_device = self._on_new

    def _run(self):
        asyncio.run(ps.register_known_devices(quiet=True))

    def test_probes_a_remembered_device(self):
        ps.settings.urls = ["http://amp/desc.xml"]
        ps.devices[:] = []
        self._run()
        self.assertEqual(self.probed, ["http://amp/desc.xml"])

    def test_registered_device_is_never_reprobed(self):
        """Steady state must cost nothing: no request for a live renderer."""

        class Device:
            location_url = "http://amp/desc.xml"

        ps.settings.urls = ["http://amp/desc.xml"]
        ps.devices[:] = [Device()]
        self._run()
        self.assertEqual(self.probed, [])

    def test_only_the_missing_one_is_reprobed(self):
        class Device:
            location_url = "http://amp/desc.xml"

        ps.settings.urls = ["http://amp/desc.xml", "http://other/desc.xml"]
        ps.devices[:] = [Device()]
        self._run()
        self.assertEqual(self.probed, ["http://other/desc.xml"])

    def test_an_unreachable_renderer_does_not_stop_the_others(self):
        async def flaky(url):
            self.probed.append(url)
            if "dead" in url:
                raise OSError("connection refused")

        ps.on_new_dlna_device = flaky
        ps.settings.urls = ["http://dead/desc.xml", "http://amp/desc.xml"]
        ps.devices[:] = []
        self._run()
        self.assertEqual(
            sorted(self.probed), ["http://amp/desc.xml", "http://dead/desc.xml"]
        )


class SsdpPacketTest(unittest.TestCase):
    """The multicast socket receives more than answers to our own M-SEARCH.

    ssdp:byebye carries no LOCATION and arbitrary hosts emit non-UTF-8 payloads,
    so neither can be assumed. Both used to raise inside the protocol callback.
    """

    def _protocol(self):
        seen = []

        async def cb(url):
            seen.append(url)

        d = DlnaDiscover(cb)
        proto = get_protocol(d)()
        return proto, seen

    def test_a_packet_without_location_is_ignored(self):
        proto, seen = self._protocol()
        byebye = (
            "NOTIFY * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
            "NTS: ssdp:byebye\r\nUSN: uuid:abc\r\n\r\n"
        ).encode()
        proto.datagram_received(byebye, ("10.0.0.9", 1900))
        self.assertEqual(seen, [])

    def test_a_non_utf8_packet_is_ignored(self):
        proto, seen = self._protocol()
        proto.datagram_received(b"\xff\xfe\x00garbage", ("10.0.0.9", 1900))
        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
