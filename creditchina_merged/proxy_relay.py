"""把带用户名密码的 HTTP 代理转成 Chrome 可直接使用的本机代理。

Chrome 由命令行启动时不适合把代理密码放在启动参数中。本转发器只监听
127.0.0.1，在转发到上游代理时注入 Proxy-Authorization；HTTPS 的 TLS
仍然由 Chrome 与目标站点端到端建立。
"""

from __future__ import annotations

import base64
import select
import socket
import socketserver
import threading
from typing import Optional, Tuple
from urllib.parse import unquote, urlparse

from .config import ProxySpec


class ProxyRelayError(RuntimeError):
    pass


def _selected_proxy_url(proxy: ProxySpec) -> str:
    raw = proxy.https or proxy.http
    if not raw:
        raise ProxyRelayError("代理配置没有可用的 HTTP/HTTPS 地址")
    return raw


def _host_port(host: str, port: int) -> str:
    return "[%s]:%d" % (host, port) if ":" in host else "%s:%d" % (host, port)


def _receive_headers(stream: socket.socket, limit: int = 65536) -> Tuple[bytes, bytes]:
    data = bytearray()
    marker = b"\r\n\r\n"
    while marker not in data:
        chunk = stream.recv(8192)
        if not chunk:
            raise ProxyRelayError("连接在完整 HTTP 头到达前已关闭")
        data.extend(chunk)
        if len(data) > limit:
            raise ProxyRelayError("HTTP 头超过本机代理转发上限")
    index = data.index(marker) + len(marker)
    return bytes(data[:index]), bytes(data[index:])


class _RelayServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _RelayHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        relay: "AuthenticatedProxyRelay" = self.server.relay  # type: ignore[attr-defined]
        client = self.request
        client.settimeout(relay.connect_timeout)
        upstream: Optional[socket.socket] = None
        response_started = False
        try:
            request_headers, remainder = _receive_headers(client)
            request_line = request_headers.split(b"\r\n", 1)[0]
            method = request_line.split(b" ", 1)[0].upper()
            request_headers = relay.authorized_headers(
                request_headers,
                close_connection=method != b"CONNECT",
            )

            upstream = socket.create_connection(
                (relay.upstream_host, relay.upstream_port),
                timeout=relay.connect_timeout,
            )
            upstream.settimeout(None)
            upstream.sendall(request_headers)
            if remainder:
                upstream.sendall(remainder)

            if method == b"CONNECT":
                response_headers, response_remainder = _receive_headers(upstream)
                client.sendall(response_headers)
                response_started = True
                if response_remainder:
                    client.sendall(response_remainder)
                status_line = response_headers.split(b"\r\n", 1)[0]
                parts = status_line.split(b" ", 2)
                if len(parts) < 2 or not parts[1].startswith(b"2"):
                    return

            client.settimeout(None)
            self._tunnel(client, upstream, relay.idle_timeout)
        except (OSError, ProxyRelayError):
            if not response_started:
                try:
                    client.sendall(
                        b"HTTP/1.1 502 Bad Gateway\r\n"
                        b"Connection: close\r\nContent-Length: 0\r\n\r\n"
                    )
                except OSError:
                    pass
        finally:
            if upstream is not None:
                try:
                    upstream.close()
                except OSError:
                    pass

    @staticmethod
    def _tunnel(left: socket.socket, right: socket.socket, idle_timeout: float) -> None:
        peers = {left: right, right: left}
        while True:
            readable, _, _ = select.select(tuple(peers), (), (), idle_timeout)
            if not readable:
                return
            for source in readable:
                data = source.recv(65536)
                if not data:
                    return
                peers[source].sendall(data)


class AuthenticatedProxyRelay:
    """仅在本机暴露的上游 HTTP 代理鉴权转发器。"""

    def __init__(
        self,
        upstream_url: str,
        connect_timeout: float = 20.0,
        idle_timeout: float = 120.0,
    ) -> None:
        parsed = urlparse(upstream_url)
        if parsed.scheme.lower() != "http":
            raise ProxyRelayError("带密码的普通 Chrome 代理目前仅支持 HTTP 上游")
        if not parsed.hostname or not parsed.port:
            raise ProxyRelayError("无效的上游代理地址")
        username = unquote(parsed.username or "")
        password = unquote(parsed.password or "")
        if not username:
            raise ProxyRelayError("本机鉴权转发需要上游代理用户名")

        self.upstream_host = parsed.hostname
        self.upstream_port = parsed.port
        self.connect_timeout = connect_timeout
        self.idle_timeout = idle_timeout
        token = base64.b64encode((username + ":" + password).encode("utf-8")).decode("ascii")
        self._authorization = ("Proxy-Authorization: Basic " + token).encode("ascii")
        self._server: Optional[_RelayServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def chrome_proxy_server(self) -> str:
        if self._server is None:
            raise ProxyRelayError("本机代理转发器尚未启动")
        host, port = self._server.server_address[:2]
        return "http://%s" % _host_port(str(host), int(port))

    def authorized_headers(self, headers: bytes, close_connection: bool = False) -> bytes:
        lines = headers.decode("iso-8859-1").split("\r\n")
        if not lines or not lines[0]:
            raise ProxyRelayError("无效的 HTTP 请求头")
        filtered = [lines[0]]
        for line in lines[1:]:
            if not line:
                continue
            name = line.partition(":")[0].strip().lower()
            if name == "proxy-authorization":
                continue
            if close_connection and name in {"connection", "proxy-connection"}:
                continue
            filtered.append(line)
        filtered.append(self._authorization.decode("ascii"))
        if close_connection:
            filtered.extend(("Connection: close", "Proxy-Connection: close"))
        return ("\r\n".join(filtered) + "\r\n\r\n").encode("iso-8859-1")

    def start(self) -> "AuthenticatedProxyRelay":
        if self._server is not None:
            return self
        server = _RelayServer(("127.0.0.1", 0), _RelayHandler)
        server.relay = self  # type: ignore[attr-defined]
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="代理鉴权转发",
            daemon=True,
        )
        self._server = server
        self._thread = thread
        thread.start()
        return self

    def stop(self) -> None:
        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=5)


def chrome_proxy_for(
    proxy: Optional[ProxySpec],
) -> Tuple[Optional[str], Optional[AuthenticatedProxyRelay]]:
    """返回 Chrome --proxy-server 值及需要随 Chrome 关闭的本机转发器。"""

    if proxy is None:
        return None, None
    raw = _selected_proxy_url(proxy)
    parsed = urlparse(raw)
    if not parsed.hostname or not parsed.port:
        raise ProxyRelayError("无效的代理地址")
    if parsed.username:
        relay = AuthenticatedProxyRelay(raw).start()
        return relay.chrome_proxy_server, relay
    return "%s://%s" % (
        parsed.scheme,
        _host_port(parsed.hostname, parsed.port),
    ), None
