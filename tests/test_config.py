import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from creditchina_merged.config import load_local_environment, parse_proxy, proxies_from_values


class ProxyConfigTests(unittest.TestCase):
    def test_single_proxy_applies_to_both_protocols(self):
        proxy = parse_proxy("127.0.0.1:7890")
        self.assertEqual(
            proxy.as_requests(),
            {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"},
        )

    def test_protocol_specific_proxy(self):
        proxy = parse_proxy("http=127.0.0.1:8080;https=https://127.0.0.1:8443")
        self.assertEqual(proxy.http, "http://127.0.0.1:8080")
        self.assertEqual(proxy.https, "https://127.0.0.1:8443")

    def test_static_proxy_environment(self):
        with patch.dict(os.environ, {"CREDITCHINA_PROXIES": '["127.0.0.1:7890"]'}):
            proxies = proxies_from_values(None)
        self.assertEqual(len(proxies), 1)

    def test_local_environment_does_not_override_exported_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env.local"
            path.write_text("EXISTING=from-file\nNEW_VALUE='loaded'\n", encoding="utf-8")
            with patch.dict(os.environ, {"EXISTING": "from-shell"}, clear=False):
                os.environ.pop("NEW_VALUE", None)
                load_local_environment(path)
                self.assertEqual(os.environ["EXISTING"], "from-shell")
                self.assertEqual(os.environ["NEW_VALUE"], "loaded")
                os.environ.pop("NEW_VALUE", None)


if __name__ == "__main__":
    unittest.main()
