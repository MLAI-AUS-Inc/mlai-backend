from django.test import SimpleTestCase

from scripts.validate_community_bridge_adapter_url import validate_adapter_url


class CommunityBridgeAdapterUrlTests(SimpleTestCase):
    def test_accepts_exact_public_tls_route(self):
        for value in (
            "https://chat.mlai.au/_mlai/bridge",
            "https://chat.mlai.au:443/_mlai/bridge",
        ):
            with self.subTest(value=value):
                validate_adapter_url(value)

    def test_accepts_private_or_loopback_http_adapter(self):
        for value in (
            "http://10.49.0.9:8090",
            "http://127.0.0.1:8090/",
            "http://[fd00::9]:8090",
        ):
            with self.subTest(value=value):
                validate_adapter_url(value)

    def test_rejects_broader_public_or_credentialed_urls(self):
        invalid_values = (
            "http://chat.mlai.au:8090",
            "https://example.com/_mlai/bridge",
            "https://chat.mlai.au/",
            "https://chat.mlai.au/_mlai/bridge/",
            "https://chat.mlai.au:8443/_mlai/bridge",
            "https://user:password@chat.mlai.au/_mlai/bridge",
            "https://chat.mlai.au/_mlai/bridge?token=secret",
            "https://chat.mlai.au/_mlai/bridge#fragment",
            "http://10.49.0.9:8080",
            "http://8.8.8.8:8090",
            "http://buzz-bridge-adapter:8090",
        )
        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_adapter_url(value)
