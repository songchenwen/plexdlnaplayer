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


class StoppedTimelineVolumeTest(unittest.TestCase):
    """The stopped timeline must carry the renderer's volume.

    Without it the controller has no starting point, assumes zero, and the first
    volume command after a stop is computed from zero. Observed live: an amp
    playing at 23 was sent volume=5 on the first press of volume up.
    """

    class FakeState:
        def __init__(self, volume, muted=0):
            self.volume = volume
            self.muted = muted

    class FakeAdapter:
        def __init__(self, state):
            self.state = state

    def test_volume_is_included_when_known(self):
        from plex.subscribe import stopped_timeline
        xml = stopped_timeline(self.FakeAdapter(self.FakeState(23)))
        self.assertIn('volume="23"', xml)
        self.assertIn('mute="0"', xml)
        self.assertIn('state="stopped"', xml)

    def test_mute_is_normalised(self):
        from plex.subscribe import stopped_timeline
        self.assertIn('mute="1"', stopped_timeline(self.FakeAdapter(self.FakeState(10, "1"))))
        self.assertIn('mute="0"', stopped_timeline(self.FakeAdapter(self.FakeState(10, "0"))))

    def test_zero_volume_is_still_reported(self):
        from plex.subscribe import stopped_timeline
        # zero is a real level and must be sent, not treated as unknown
        self.assertIn('volume="0"', stopped_timeline(self.FakeAdapter(self.FakeState(0))))

    def test_unknown_volume_adds_nothing(self):
        from plex.subscribe import stopped_timeline
        xml = stopped_timeline(self.FakeAdapter(self.FakeState(None)))
        self.assertNotIn("volume=", xml)
        self.assertIn('state="stopped"', xml)

