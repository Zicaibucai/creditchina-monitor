"""快代理私密代理提取器。

每次调用只允许提取一个 IP。API 密钥只从环境变量读取，
不会写入日志、任务数据库或导出文件。
"""

from __future__ import annotations

import json
import os
import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import Request, urlopen

from .config import ProxySpec, parse_proxy
from .http_client import RequestFailed


class ProxyProviderError(RequestFailed):
    pass


@dataclass(frozen=True)
class PrivateProxyLease:
    spec: ProxySpec
    host: str
    port: int
    expires_in: Optional[int] = None

    @property
    def masked_label(self) -> str:
        parts = self.host.split(".")
        masked = ".".join([*parts[:2], "*", "*"]) if len(parts) == 4 else "***"
        return "%s:%d" % (masked, self.port)


def _api_url_from_env() -> str:
    explicit = os.getenv("KDL_DPS_API_URL", "").strip()
    if explicit:
        parsed = urlparse(explicit)
        query = parse_qs(parsed.query)
        if query.get("num", [""])[0] != "1":
            raise ProxyProviderError("KDL_DPS_API_URL 必须配置 num=1，防止一次多提取 IP")
        return explicit

    secret_id = os.getenv("KDL_DPS_SECRET_ID", "").strip()
    secret_key = os.getenv("KDL_DPS_SECRET_KEY", "").strip()
    # Token mode requires a SecretToken obtained from get_secret_token.  A
    # SecretKey must never be sent directly as the signature query parameter.
    secret_token = os.getenv("KDL_DPS_SECRET_TOKEN", "").strip()
    if not secret_id and not secret_key and not secret_token:
        return ""
    if not secret_id:
        raise ProxyProviderError("KDL_DPS_SECRET_ID 未配置")

    params: dict[str, Any] = {
        "secret_id": secret_id,
        "num": 1,
        "format": "json",
        "f_auth": 1,
        "generateType": 2,
        "dedup": 1,
    }
    if secret_key:
        params.update(
            {
                "sign_type": "hmacsha1",
                "timestamp": int(time.time()),
                "nonce": secrets.randbelow(100000000) + 1,
            }
        )
        query_to_sign = "&".join("%s=%s" % (key, params[key]) for key in sorted(params))
        raw = "GET/api/getdps?" + query_to_sign
        params["signature"] = base64.b64encode(
            hmac.new(secret_key.encode("utf-8"), raw.encode("utf-8"), hashlib.sha1).digest()
        ).decode("ascii")
    elif secret_token:
        params.update({"sign_type": "token", "signature": secret_token})
    else:
        raise ProxyProviderError("请配置 KDL_DPS_SECRET_KEY 或 KDL_DPS_SECRET_TOKEN")
    query = urlencode(params)
    return "https://dps.kdlapi.com/api/getdps?" + query


def kuaidaili_enabled() -> bool:
    return bool(
        os.getenv("KDL_DPS_API_URL", "").strip()
        or os.getenv("KDL_DPS_SECRET_ID", "").strip()
        or os.getenv("KDL_DPS_SECRET_KEY", "").strip()
        or os.getenv("KDL_DPS_SECRET_TOKEN", "").strip()
    )


def _proxy_url(
    raw: str,
    default_username: str = "",
    default_password: str = "",
) -> tuple[str, str, int]:
    value = raw.strip()
    if not value:
        raise ProxyProviderError("快代理返回了空代理地址")

    username = default_username
    password = default_password
    server = value
    if "@" in value:
        server, auth = value.split("@", 1)
        username, separator, password = auth.partition(":")
        if not separator:
            raise ProxyProviderError("快代理鉴权格式无效")
    else:
        parts = value.split(":")
        if len(parts) >= 4:
            server = ":".join(parts[:2])
            username = parts[2]
            password = ":".join(parts[3:])

    host, separator, port_text = server.rpartition(":")
    if not separator or not host:
        raise ProxyProviderError("快代理返回的 IP:PORT 格式无效")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ProxyProviderError("快代理返回的端口无效") from exc
    if not 1 <= port <= 65535:
        raise ProxyProviderError("快代理返回的端口超出范围")

    if username:
        url = "http://%s:%s@%s:%d" % (
            quote(username, safe=""),
            quote(password, safe=""),
            host,
            port,
        )
    else:
        url = "http://%s:%d" % (host, port)
    return url, host, port


class KuaidailiPrivateProxyProvider:
    def __init__(
        self,
        api_url: str,
        timeout: float = 10.0,
        proxy_username: str = "",
        proxy_password: str = "",
    ) -> None:
        self.api_url = api_url
        self.timeout = timeout
        self.proxy_username = proxy_username
        self.proxy_password = proxy_password

    @classmethod
    def from_env(cls) -> Optional["KuaidailiPrivateProxyProvider"]:
        url = _api_url_from_env()
        if not url:
            return None
        proxy_username = os.getenv("KDL_DPS_USERNAME", "").strip()
        proxy_password = os.getenv("KDL_DPS_PASSWORD", "").strip()
        if bool(proxy_username) != bool(proxy_password):
            raise ProxyProviderError(
                "KDL_DPS_USERNAME 与 KDL_DPS_PASSWORD 必须同时配置"
            )
        return cls(
            url,
            timeout=float(os.getenv("KDL_DPS_TIMEOUT", "10")),
            proxy_username=proxy_username,
            proxy_password=proxy_password,
        )

    def extract_one(self) -> PrivateProxyLease:
        request = Request(
            self.api_url,
            headers={"Accept": "application/json", "User-Agent": "merged-creditchina/1.0"},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
        except Exception as exc:
            raise ProxyProviderError("无法连接快代理提取接口") from exc

        try:
            payload: Any = json.loads(body)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, Mapping):
            if str(payload.get("code", "")) not in ("0", "0.0"):
                raise ProxyProviderError("快代理提取失败：%s" % str(payload.get("msg") or "未知错误"))
            data = payload.get("data")
            proxy_list = data.get("proxy_list") if isinstance(data, Mapping) else None
            if not isinstance(proxy_list, list) or not proxy_list:
                raise ProxyProviderError("快代理未返回可用 IP")
            raw_proxy = str(proxy_list[0])
            expires_raw = data.get("f_et") or data.get("valid_time") or data.get("expire_time")
        else:
            if body.lstrip().startswith("ERROR("):
                raise ProxyProviderError("快代理提取失败：%s" % body.strip()[:160])
            raw_proxy = next((line.strip() for line in body.splitlines() if line.strip()), "")
            expires_raw = None

        proxy_url, host, port = _proxy_url(
            raw_proxy,
            default_username=self.proxy_username,
            default_password=self.proxy_password,
        )
        try:
            expires_in = int(expires_raw) if expires_raw not in (None, "") else None
        except (TypeError, ValueError):
            expires_in = None
        return PrivateProxyLease(
            spec=parse_proxy(proxy_url),
            host=host,
            port=port,
            expires_in=expires_in,
        )
