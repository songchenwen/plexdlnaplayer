import json
import tempfile
import unittest
from pathlib import Path

from utils import (
    CONTROL_RETRY_BUDGET,
    CONTROL_RETRY_DELAYS,
    is_transient_failure,
    upnp_error_code,
)

FAULT = (
    '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
    "<s:Body><s:Fault><faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring>"
    '<detail><UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
    "<errorCode>{code}</errorCode><errorDescription>{desc}</errorDescription>"
    "</UPnPError></detail></s:Fault></s:Body></s:Envelope>"
)


class UpnpErrorCodeTest(unittest.TestCase):

    def test_reads_code_from_fault(self):
        body = FAULT.format(code=701, desc="Transition not available")
        self.assertEqual(upnp_error_code(body), "701")

    def test_tolerates_whitespace(self):
        self.assertEqual(upnp_error_code("<errorCode> 705 </errorCode>"), "705")

    def test_no_code_is_none(self):
        self.assertIsNone(upnp_error_code("<html>gateway timeout</html>"))
        self.assertIsNone(upnp_error_code(""))
        self.assertIsNone(upnp_error_code(None))


class TransientFailureTest(unittest.TestCase):
    """Retry only when the renderer has not actually answered.

    A UPnP fault code means it is awake and refused. A refused connection or a
    404 from a stack that is still starting means it is not ready yet. Retrying
    the former blocks a call the controller is waiting on: a six second Pause
    retry made playMedia take 6.1s and Plexamp reported "could not switch to
    player". No 701 retry has ever succeeded; every 404 retry has.
    """

    def test_an_answer_is_never_retried(self):
        from utils import is_transient_failure
        # 701 "Transition not available" from a stopped transport
        self.assertFalse(is_transient_failure(500, "701"))
        # a renderer that cannot seek
        self.assertFalse(is_transient_failure(500, "710"))
        # anything else it chose to say
        self.assertFalse(is_transient_failure(500, "718"))
        self.assertFalse(is_transient_failure(404, "701"))

    def test_a_renderer_that_is_not_answering_is_retried(self):
        from utils import is_transient_failure
        # UPnP stack still coming up
        self.assertTrue(is_transient_failure(404, None))
        # server error with no fault body
        self.assertTrue(is_transient_failure(503, None))
        self.assertTrue(is_transient_failure(500, None))

    def test_client_errors_are_not_retried(self):
        from utils import is_transient_failure
        self.assertFalse(is_transient_failure(400, None))
        self.assertFalse(is_transient_failure(401, None))

    def test_first_attempt_is_immediate(self):
        from utils import CONTROL_RETRY_DELAYS
        self.assertEqual(CONTROL_RETRY_DELAYS[0], 0)
        self.assertGreater(len(CONTROL_RETRY_DELAYS), 1)

    def test_retries_stay_within_a_sensible_budget(self):
        from utils import CONTROL_RETRY_BUDGET, CONTROL_RETRY_DELAYS
        # the delays alone do not bound the call: each attempt can also spend its
        # own request timeout, so the wall clock budget is what actually bounds it
        self.assertLess(sum(CONTROL_RETRY_DELAYS), CONTROL_RETRY_BUDGET)
        self.assertLessEqual(CONTROL_RETRY_BUDGET, 15)


class KnownDeviceCacheTest(unittest.TestCase):

    def setUp(self):
        from settings import settings

        self.settings = settings
        self.tmp = tempfile.TemporaryDirectory()
        self._old_path = settings.config_path
        settings.config_path = self.tmp.name

    def tearDown(self):
        self.settings.config_path = self._old_path
        self.tmp.cleanup()

    def test_remembers_and_returns_url(self):
        self.settings.remember_device(
            "uuid-1", "Hegel H150", "http://10.0.0.9:16500/desc.xml"
        )
        self.assertEqual(
            self.settings.known_device_urls(), ["http://10.0.0.9:16500/desc.xml"]
        )

    def test_does_not_duplicate_or_lose_other_data(self):
        self.settings.set_token_for_uuid("uuid-1", "tok")
        self.settings.remember_device("uuid-1", "Hegel H150", "http://a/desc.xml")
        self.settings.remember_device("uuid-1", "Hegel H150", "http://a/desc.xml")
        self.assertEqual(self.settings.known_device_urls(), ["http://a/desc.xml"])
        # remembering a device must not clobber its token
        self.assertEqual(self.settings.get_token_for_uuid("uuid-1"), "tok")

    def test_moved_device_updates_url(self):
        self.settings.remember_device("uuid-1", "Hegel H150", "http://old/desc.xml")
        self.settings.remember_device("uuid-1", "Hegel H150", "http://new/desc.xml")
        self.assertEqual(self.settings.known_device_urls(), ["http://new/desc.xml"])

    def test_empty_when_nothing_seen(self):
        self.assertEqual(self.settings.known_device_urls(), [])

    def test_survives_unrelated_entries(self):
        p = Path(self.tmp.name).joinpath(self.settings.data_file_name)
        p.write_text(json.dumps({"uuid-1": {"alias": "Amp"}, "junk": "not-a-dict"}))
        self.settings.remember_device("uuid-2", "Other", "http://b/desc.xml")
        self.assertEqual(self.settings.known_device_urls(), ["http://b/desc.xml"])


if __name__ == "__main__":
    unittest.main()


class ClampElapsedTest(unittest.TestCase):
    """The auto-next case that made Plex reject timeline updates with 400."""

    def test_elapsed_past_the_end_is_clamped(self):
        from utils import clamp_elapsed
        # observed live: previous track's 213.0s reported against a 205.9s track
        self.assertEqual(clamp_elapsed(213000, 205917), 205917)

    def test_normal_position_is_untouched(self):
        from utils import clamp_elapsed
        self.assertEqual(clamp_elapsed(120000, 205917), 120000)
        self.assertEqual(clamp_elapsed(0, 205917), 0)
        self.assertEqual(clamp_elapsed(205917, 205917), 205917)

    def test_negative_position_floors_at_zero(self):
        from utils import clamp_elapsed
        self.assertEqual(clamp_elapsed(-5, 205917), 0)

    def test_unknown_duration_passes_through(self):
        from utils import clamp_elapsed
        # a renderer that has not reported a duration yet must not be forced to 0
        self.assertEqual(clamp_elapsed(213000, None), 213000)
        self.assertEqual(clamp_elapsed(213000, 0), 213000)
        self.assertEqual(clamp_elapsed(213000, ""), 213000)

    def test_missing_elapsed_passes_through(self):
        from utils import clamp_elapsed
        self.assertEqual(clamp_elapsed(None, 205917), None)
        self.assertEqual(clamp_elapsed("", 205917), "")


class PlayedDeviceMemoryTest(unittest.TestCase):
    """A renderer someone plays to should survive going unreachable."""

    def setUp(self):
        from settings import settings
        self.settings = settings
        self.tmp = tempfile.TemporaryDirectory()
        self._old = settings.config_path
        settings.config_path = self.tmp.name

    def tearDown(self):
        self.settings.config_path = self._old
        self.tmp.cleanup()

    def test_unplayed_device_is_not_protected(self):
        # merely seen on the network is not enough to keep it listed
        self.settings.remember_device("u1", "Amp", "http://a/desc.xml")
        self.assertFalse(self.settings.device_was_played("u1"))

    def test_played_device_is_protected(self):
        self.settings.remember_device("u1", "Amp", "http://a/desc.xml")
        self.settings.mark_device_played("u1")
        self.assertTrue(self.settings.device_was_played("u1"))

    def test_unknown_device_is_not_protected(self):
        self.assertFalse(self.settings.device_was_played("nope"))

    def test_played_flag_survives_a_url_change(self):
        self.settings.mark_device_played("u1")
        self.settings.remember_device("u1", "Amp", "http://moved/desc.xml")
        self.assertTrue(self.settings.device_was_played("u1"))
        self.assertEqual(self.settings.known_device_urls(), ["http://moved/desc.xml"])


class Fake:
    def __init__(self, uuid, location_url):
        self.uuid = uuid
        self.location_url = location_url


class DeviceMovedTest(unittest.TestCase):
    """A renderer that changes IP must replace its old entry, not duplicate it."""

    def test_unseen_device_registers(self):
        from utils import device_registration_action
        action, existing = device_registration_action([], "u1", "http://10.0.0.12:16500/d.xml")
        self.assertEqual(action, "register")
        self.assertIsNone(existing)

    def test_same_device_same_address_is_ignored(self):
        from utils import device_registration_action
        d = Fake("u1", "http://10.0.0.12:16500/d.xml")
        action, existing = device_registration_action([d], "u1", "http://10.0.0.12:16500/d.xml")
        self.assertEqual(action, "ignore")
        self.assertIs(existing, d)

    def test_same_device_new_address_replaces(self):
        from utils import device_registration_action
        d = Fake("u1", "http://10.0.0.12:16500/d.xml")
        # DHCP moved the amp
        action, existing = device_registration_action([d], "u1", "http://10.0.0.99:16500/d.xml")
        self.assertEqual(action, "replace")
        self.assertIs(existing, d)

    def test_different_device_at_that_address_still_registers(self):
        from utils import device_registration_action
        d = Fake("u1", "http://10.0.0.12:16500/d.xml")
        action, existing = device_registration_action([d], "u2", "http://10.0.0.13:16500/d.xml")
        self.assertEqual(action, "register")

    def test_device_without_uuid_is_not_matched(self):
        from utils import device_registration_action
        d = Fake(None, "http://10.0.0.12:16500/d.xml")
        action, _ = device_registration_action([d], "u1", "http://10.0.0.99:16500/d.xml")
        self.assertEqual(action, "register")

    def test_moved_device_is_found_among_several(self):
        from utils import device_registration_action
        devs = [Fake("a", "http://1/d.xml"), Fake("u1", "http://2/d.xml"), Fake("c", "http://3/d.xml")]
        action, existing = device_registration_action(devs, "u1", "http://9/d.xml")
        self.assertEqual(action, "replace")
        self.assertEqual(existing.location_url, "http://2/d.xml")
