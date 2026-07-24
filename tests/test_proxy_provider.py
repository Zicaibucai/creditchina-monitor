import json
import os
import base64
import hashlib
import hmac
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from creditchina_merged.proxy_provider import (
    KuaidailiPrivateProxyProvider,
    ProxyProviderError,
)


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


class KuaidailiProviderTests(unittest.TestCase):
    def test_extracts_exactly_one_authenticated_proxy(self):
        provider = KuaidailiPrivateProxyProvider(
            "https://dps.kdlapi.com/api/getdps?num=1&format=json&f_auth=1"
        )
        payload = {
            "code": 0,
            "msg": "",
            "data": {"count": 1, "proxy_list": ["1.2.3.4:23456@proxy-user:proxy-pass"]},
        }
        with patch("creditchina_merged.proxy_provider.urlopen", return_value=_Response(payload)):
            lease = provider.extract_one()

        self.assertEqual("1.2.3.4", lease.host)
        self.assertEqual(23456, lease.port)
        self.assertEqual("1.2.*.*:23456", lease.masked_label)
        self.assertEqual(
            "http://proxy-user:proxy-pass@1.2.3.4:23456",
            lease.spec.http,
        )

    def test_generated_url_must_request_one_ip(self):
        with patch.dict(
            os.environ,
            {"KDL_DPS_API_URL": "https://dps.kdlapi.com/api/getdps?num=10"},
            clear=True,
        ):
            with self.assertRaisesRegex(ProxyProviderError, "num=1"):
                KuaidailiPrivateProxyProvider.from_env()

    def test_explicit_credentials_authenticate_plain_proxy_address(self):
        provider = KuaidailiPrivateProxyProvider(
            "https://dps.kdlapi.com/api/getdps?num=1&format=json&f_auth=1",
            proxy_username="configured-user",
            proxy_password="configured-pass",
        )
        payload = {
            "code": 0,
            "msg": "",
            "data": {"count": 1, "proxy_list": ["1.2.3.4:23456"]},
        }
        with patch("creditchina_merged.proxy_provider.urlopen", return_value=_Response(payload)):
            lease = provider.extract_one()

        self.assertEqual(
            "http://configured-user:configured-pass@1.2.3.4:23456",
            lease.spec.http,
        )

    def test_explicit_credentials_must_be_configured_as_a_pair(self):
        with patch.dict(
            os.environ,
            {
                "KDL_DPS_SECRET_ID": "order-id",
                "KDL_DPS_SECRET_TOKEN": "token",
                "KDL_DPS_USERNAME": "configured-user",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ProxyProviderError, "必须同时配置"):
                KuaidailiPrivateProxyProvider.from_env()

    def test_secret_token_builds_single_ip_request(self):
        with patch.dict(
            os.environ,
            {"KDL_DPS_SECRET_ID": "order-id", "KDL_DPS_SECRET_TOKEN": "token"},
            clear=True,
        ):
            provider = KuaidailiPrivateProxyProvider.from_env()

        self.assertIsNotNone(provider)
        query = parse_qs(urlparse(provider.api_url).query)
        self.assertEqual(["1"], query["num"])
        self.assertEqual(["1"], query["f_auth"])
        self.assertEqual(["token"], query["sign_type"])
        self.assertEqual(["token"], query["signature"])
        self.assertNotIn("order-id", repr(provider))

    def test_legacy_signature_variable_is_rejected_as_ambiguous(self):
        with patch.dict(
            os.environ,
            {"KDL_DPS_SECRET_ID": "order-id", "KDL_DPS_SIGNATURE": "token"},
            clear=True,
        ):
            with self.assertRaisesRegex(ProxyProviderError, "KDL_DPS_SECRET_TOKEN"):
                KuaidailiPrivateProxyProvider.from_env()

    def test_secret_key_uses_official_hmac_sha1_signature(self):
        with patch.dict(
            os.environ,
            {"KDL_DPS_SECRET_ID": "order-id", "KDL_DPS_SECRET_KEY": "secret-key"},
            clear=True,
        ), patch("creditchina_merged.proxy_provider.time.time", return_value=1700000000), patch(
            "creditchina_merged.proxy_provider.secrets.randbelow",
            return_value=41,
        ):
            provider = KuaidailiPrivateProxyProvider.from_env()

        query = parse_qs(urlparse(provider.api_url).query)
        signature = query.pop("signature")[0]
        plain = {key: values[0] for key, values in query.items()}
        raw_query = "&".join("%s=%s" % (key, plain[key]) for key in sorted(plain))
        expected = base64.b64encode(
            hmac.new(b"secret-key", ("GET/api/getdps?" + raw_query).encode(), hashlib.sha1).digest()
        ).decode()

        self.assertEqual("hmacsha1", plain["sign_type"])
        self.assertEqual("1700000000", plain["timestamp"])
        self.assertEqual("42", plain["nonce"])
        self.assertEqual(expected, signature)


if __name__ == "__main__":
    unittest.main()
