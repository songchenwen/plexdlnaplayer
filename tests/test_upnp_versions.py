import unittest

from utils import (UPNP_AVT_SERVICE_TYPE, UPNP_RC_SERVICE_TYPE,
                   UPNP_VERSIONED_NAMESPACES, same_service, service_version)

AVT2 = "urn:schemas-upnp-org:service:AVTransport:2"
RC2 = "urn:schemas-upnp-org:service:RenderingControl:2"


class ServiceVersionTest(unittest.TestCase):

    def test_same_service_ignores_version(self):
        self.assertTrue(same_service(AVT2, UPNP_AVT_SERVICE_TYPE))
        self.assertTrue(same_service(UPNP_AVT_SERVICE_TYPE, UPNP_AVT_SERVICE_TYPE))

    def test_same_service_distinguishes_services(self):
        self.assertFalse(same_service(AVT2, UPNP_RC_SERVICE_TYPE))

    def test_service_version(self):
        self.assertEqual(service_version(AVT2), 2)
        self.assertEqual(service_version(UPNP_AVT_SERVICE_TYPE), 1)
        self.assertEqual(service_version("urn:something:weird"), 0)

    def test_versioned_namespaces_cover_v1_and_v2(self):
        # v1 must keep working exactly as before
        for service_type in (UPNP_AVT_SERVICE_TYPE, UPNP_RC_SERVICE_TYPE, AVT2, RC2):
            self.assertIn(service_type, UPNP_VERSIONED_NAMESPACES)
            self.assertIsNone(UPNP_VERSIONED_NAMESPACES[service_type])


if __name__ == "__main__":
    unittest.main()


class FallbackCharsetTest(unittest.TestCase):

    def test_utf8_bodies_stay_utf8(self):
        from utils import fallback_charset
        self.assertEqual(fallback_charset(None, "Hegel H150".encode("utf-8")), "utf-8")
        self.assertEqual(fallback_charset(None, "Café".encode("utf-8")), "utf-8")

    def test_undeclared_latin1_does_not_raise(self):
        from utils import fallback_charset
        body = "Café".encode("windows-1252")
        encoding = fallback_charset(None, body)
        # the point is that decoding succeeds rather than raising
        self.assertEqual(body.decode(encoding), "Café")
