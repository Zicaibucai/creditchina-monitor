"""支持 requests/urllib 的 JSON 请求器及静态代理轮换。"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from .config import HttpConfig, ProxySpec


class RequestFailed(RuntimeError):
    pass


class AccessIntercepted(RequestFailed):
    """官网已在验证码通过后因访问频率触发风控。"""


class ProxyUnavailable(RequestFailed):
    """当前代理自身无法建立连接，不代表目标网站风控。"""


class HttpStatusError(RuntimeError):
    def __init__(self, status: int, url: str, message: str = "") -> None:
        self.status = status
        self.url = url
        super().__init__("HTTP %d %s%s" % (status, url, (": " + message) if message else ""))


class RequestsTransport:
    def __init__(self) -> None:
        try:
            import requests
        except ImportError as exc:
            raise RuntimeError("requests 传输需要先安装 requests") from exc
        self._requests = requests
        self._session = requests.Session()

    def get_json(
        self,
        url: str,
        headers: Dict[str, str],
        timeout: float,
        proxy: Optional[ProxySpec],
    ) -> Any:
        response = self._session.get(
            url,
            headers=headers,
            proxies=proxy.as_requests() if proxy else None,
            timeout=timeout,
        )
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.status_code >= 400:
            # 新版接口用 HTTP 500 返回结构化的验证码/业务状态，交给上层判断。
            if isinstance(payload, dict) and any(
                key in payload for key in ("status", "code", "message")
            ):
                return payload
            raise HttpStatusError(response.status_code, url, response.reason)
        if payload is None:
            raise ValueError("响应不是 JSON")
        return payload

    def close(self) -> None:
        self._session.close()


class UrllibTransport:
    def get_json(
        self,
        url: str,
        headers: Dict[str, str],
        timeout: float,
        proxy: Optional[ProxySpec],
    ) -> Any:
        request = urllib.request.Request(url, headers=headers, method="GET")
        handlers = []
        if proxy:
            handlers.append(urllib.request.ProxyHandler(proxy.as_urllib()))
        opener = urllib.request.build_opener(*handlers)
        with opener.open(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            body = response.read().decode(charset, errors="replace")
        return json.loads(body)

    def close(self) -> None:
        return None


class HttpClient:
    """每次尝试随机 UA，并只从进程内的静态列表选取代理。"""

    def __init__(self, config: HttpConfig) -> None:
        self.config = config
        if config.transport == "requests":
            self.transport = RequestsTransport()
        else:
            self.transport = UrllibTransport()

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Connection": "keep-alive",
            "Referer": self.config.referer,
            "User-Agent": random.choice(self.config.user_agents),
            "X-Requested-With": "XMLHttpRequest",
        }
        if self.config.cookie:
            headers["Cookie"] = self.config.cookie
        return headers

    def get_json(self, url: str) -> Any:
        last_error: Optional[BaseException] = None
        for attempt in range(1, self.config.retries + 1):
            proxy = random.choice(self.config.proxies) if self.config.proxies else None
            try:
                payload = self.transport.get_json(
                    url=url,
                    headers=self._headers(),
                    timeout=self.config.timeout,
                    proxy=proxy,
                )
                if not isinstance(payload, (dict, list)):
                    raise ValueError("响应不是 JSON 对象或数组")
                return payload
            except HttpStatusError as exc:
                # 4xx（尤其 404）是确定性错误，重试不会改变结果。
                if exc.status in (403, 412, 429):
                    raise AccessIntercepted("官网触发访问风控（HTTP %d）" % exc.status) from exc
                raise RequestFailed(str(exc)) from exc
            except Exception as exc:  # 网络、HTTP、解码错误均参与重试
                last_error = exc
                if attempt < self.config.retries:
                    time.sleep(self.config.backoff * attempt)
        proxy_mode = "静态代理" if self.config.proxies else "直连"
        raise RequestFailed(
            "%s 请求失败（%s，已尝试 %d 次）：%s"
            % (url, proxy_mode, self.config.retries, last_error)
        )

    def close(self) -> None:
        close = getattr(self.transport, "close", None)
        if close:
            close()
