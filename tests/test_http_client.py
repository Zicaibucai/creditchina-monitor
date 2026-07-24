import unittest

from creditchina_merged.config import HttpConfig
from creditchina_merged.http_client import HttpClient, HttpStatusError, RequestFailed


class MissingEndpointTransport:
    def __init__(self):
        self.calls = 0

    def get_json(self, url, headers, timeout, proxy):
        self.calls += 1
        raise HttpStatusError(404, url, "Not Found")


class HttpClientTests(unittest.TestCase):
    def test_404_is_not_retried(self):
        client = HttpClient(HttpConfig(transport="urllib", retries=5, backoff=0))
        transport = MissingEndpointTransport()
        client.transport = transport
        with self.assertRaises(RequestFailed):
            client.get_json("https://example.test/missing")
        self.assertEqual(transport.calls, 1)


if __name__ == "__main__":
    unittest.main()

