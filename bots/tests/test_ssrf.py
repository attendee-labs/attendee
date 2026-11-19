from unittest.mock import patch

from django.test import SimpleTestCase

from bots.ssrf import (
    assert_safe_https_media_url,
    assert_safe_rtms_websocket_url,
    hostname_resolves_public,
    rtms_ssl_context,
    url_is_public,
)


class SsrfHelpersTests(SimpleTestCase):
    def test_literal_loopback_rejected(self):
        self.assertFalse(hostname_resolves_public("127.0.0.1"))
        self.assertFalse(url_is_public("https://127.0.0.1/webhook"))

    def test_literal_private_rejected(self):
        self.assertFalse(hostname_resolves_public("10.0.0.5"))
        self.assertFalse(url_is_public("https://10.0.0.5/x"))

    def test_literal_public_accepted(self):
        self.assertTrue(hostname_resolves_public("8.8.8.8"))
        self.assertTrue(url_is_public("https://8.8.8.8/webhook"))

    def test_rtms_rejects_non_wss(self):
        with self.assertRaises(ValueError):
            assert_safe_rtms_websocket_url("https://rtms.zoom.us/path")

    def test_rtms_rejects_private_host(self):
        with self.assertRaises(ValueError):
            assert_safe_rtms_websocket_url("wss://127.0.0.1/rtms")

    def test_rtms_rejects_non_zoom_host(self):
        with patch("bots.ssrf.hostname_resolves_public", return_value=True):
            with self.assertRaises(ValueError) as ctx:
                assert_safe_rtms_websocket_url("wss://evil.example.com/rtms")
        self.assertIn("allowlist", str(ctx.exception).lower())

    def test_rtms_accepts_zoom_host_when_public(self):
        with patch("bots.ssrf.hostname_resolves_public", return_value=True):
            url = assert_safe_rtms_websocket_url("wss://rtms.zoom.us/ws")
        self.assertEqual(url, "wss://rtms.zoom.us/ws")

    def test_media_url_requires_https_and_public(self):
        with self.assertRaises(ValueError):
            assert_safe_https_media_url("http://example.com/a.mp4")
        with self.assertRaises(ValueError):
            assert_safe_https_media_url("https://127.0.0.1/a.mp4")
        self.assertEqual(
            assert_safe_https_media_url("https://8.8.8.8/a.mp4"),
            "https://8.8.8.8/a.mp4",
        )

    def test_rtms_ssl_context_verifies_by_default(self):
        import ssl

        ctx = rtms_ssl_context()
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)

    def test_rtms_ssl_insecure_opt_in(self):
        import ssl

        with patch.dict("os.environ", {"ZOOM_RTMS_SSL_INSECURE": "true"}):
            ctx = rtms_ssl_context()
        self.assertEqual(ctx.verify_mode, ssl.CERT_NONE)
