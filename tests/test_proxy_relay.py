import base64
import socket
import socketserver
import threading
import unittest

from creditchina_merged.config import parse_proxy
from creditchina_merged.proxy_relay import AuthenticatedProxyRelay, chrome_proxy_for


def _receive_headers(stream):
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = stream.recv(4096)
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


class _FakeUpstreamHandler(socketserver.BaseRequestHandler):
    def handle(self):
        headers = _receive_headers(self.request)
        self.server.received_headers = headers
        self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        while True:
            data = self.request.recv(4096)
            if not data:
                return
            self.request.sendall(data)


class _FakeUpstream(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class ProxyRelayTests(unittest.TestCase):
    def setUp(self):
        self.upstream = _FakeUpstream(("127.0.0.1", 0), _FakeUpstreamHandler)
        self.upstream.received_headers = b""
        self.thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.upstream.shutdown()
        self.upstream.server_close()
        self.thread.join(timeout=5)

    def test_authenticated_connect_is_forwarded_without_exposing_credentials_to_chrome(self):
        host, port = self.upstream.server_address
        relay = AuthenticatedProxyRelay(
            "http://proxy-user:proxy-pass@%s:%d" % (host, port),
            idle_timeout=5,
        ).start()
        try:
            parsed = relay.chrome_proxy_server.rsplit(":", 1)
            relay_host = parsed[0].replace("http://", "")
            relay_port = int(parsed[1])
            with socket.create_connection((relay_host, relay_port), timeout=5) as client:
                client.sendall(
                    b"CONNECT www.creditchina.gov.cn:443 HTTP/1.1\r\n"
                    b"Host: www.creditchina.gov.cn:443\r\n\r\n"
                )
                response = _receive_headers(client)
                self.assertIn(b"200 Connection Established", response)
                client.sendall(b"test-tunnel")
                self.assertEqual(b"test-tunnel", client.recv(64))
        finally:
            relay.stop()

        expected = base64.b64encode(b"proxy-user:proxy-pass")
        self.assertIn(b"Proxy-Authorization: Basic " + expected, self.upstream.received_headers)

    def test_proxy_without_credentials_is_passed_directly_to_chrome(self):
        chrome_proxy, relay = chrome_proxy_for(parse_proxy("127.0.0.1:18080"))

        self.assertEqual("http://127.0.0.1:18080", chrome_proxy)
        self.assertIsNone(relay)

    def test_authenticated_proxy_uses_loopback_relay(self):
        host, port = self.upstream.server_address
        chrome_proxy, relay = chrome_proxy_for(
            parse_proxy("http://name:secret@%s:%d" % (host, port))
        )
        try:
            self.assertTrue(chrome_proxy.startswith("http://127.0.0.1:"))
            self.assertNotIn("name", chrome_proxy)
            self.assertNotIn("secret", chrome_proxy)
        finally:
            relay.stop()


if __name__ == "__main__":
    unittest.main()
