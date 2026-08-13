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

    def test_renderer_not_ready_is_retried(self):
        # the case that makes a first play from cold fail
        self.assertTrue(is_transient_failure(500, "701"))
        self.assertTrue(is_transient_failure(500, "705"))
        self.assertTrue(is_transient_failure(500, "716"))

    def test_stack_still_starting_is_retried(self):
        # the amp answers 404 on its control url until Rygel is up
        self.assertTrue(is_transient_failure(404, None))
        self.assertTrue(is_transient_failure(503, None))

    def test_permanent_limitations_are_not_retried(self):
        # this renderer cannot seek; asking four more times will not change that
        self.assertFalse(is_transient_failure(500, "710"))
        self.assertFalse(is_transient_failure(500, "401"))
        self.assertFalse(is_transient_failure(500, "402"))

    def test_considered_refusal_is_not_retried(self):
        # a 5xx that carries a fault code we do not know is still a real answer
        self.assertFalse(is_transient_failure(500, "718"))

    def test_first_attempt_is_immediate(self):
        self.assertEqual(CONTROL_RETRY_DELAYS[0], 0)
        self.assertGreater(len(CONTROL_RETRY_DELAYS), 1)

    def test_retries_stay_within_a_sensible_budget(self):
        # Long enough for an amp leaving standby, short enough that the Plex
        # controller does not give up on the player first. Checking the delays
        # alone is not enough: each attempt can also spend its own request
        # timeout, so the wall-clock budget is what actually bounds the call.
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
