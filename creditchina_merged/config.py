"""配置对象与环境变量解析。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_local_environment(path: Optional[Path] = None) -> None:
    """Load a local key-value file without overriding exported variables."""

    configured_path = os.getenv("CREDITCHINA_ENV_PATH", "").strip()
    env_path = path or (Path(configured_path).expanduser() if configured_path else PROJECT_ROOT / ".env.local")
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))


load_local_environment()


DEFAULT_USER_AGENTS: Tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
    "Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
)

def _proxy_url(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("代理地址不能为空")
    if "://" not in value:
        value = "http://" + value
    parsed = urlparse(value)
    if not parsed.hostname or not parsed.port:
        raise ValueError("无效代理地址：%s" % value)
    return value


@dataclass(frozen=True)
class ProxySpec:
    """一个静态代理条目，可分别指定 HTTP 与 HTTPS 代理。"""

    http: Optional[str] = None
    https: Optional[str] = None

    def as_requests(self) -> Dict[str, str]:
        result: Dict[str, str] = {}
        if self.http:
            result["http"] = self.http
        if self.https:
            result["https"] = self.https
        return result

    def as_urllib(self) -> Dict[str, str]:
        return self.as_requests()

    def __str__(self) -> str:
        values = []
        if self.http:
            values.append("http=%s" % self.http)
        if self.https:
            values.append("https=%s" % self.https)
        return ";".join(values) or "DIRECT"


def parse_proxy(value: str) -> ProxySpec:
    """解析 URL 或 ``http=...;https=...`` 格式的静态代理。"""

    value = value.strip()
    if not value:
        raise ValueError("代理配置不能为空")
    if "=" not in value:
        url = _proxy_url(value)
        return ProxySpec(http=url, https=url)

    parts: Dict[str, str] = {}
    for item in value.split(";"):
        if not item.strip():
            continue
        key, separator, raw_url = item.partition("=")
        if not separator or key.strip().lower() not in ("http", "https"):
            raise ValueError("无效代理配置：%s" % value)
        parts[key.strip().lower()] = _proxy_url(raw_url)
    if not parts:
        raise ValueError("无效代理配置：%s" % value)
    return ProxySpec(http=parts.get("http"), https=parts.get("https"))


def parse_proxy_list(raw: Optional[str]) -> Tuple[ProxySpec, ...]:
    """从 JSON 数组或逗号分隔字符串读取静态代理列表。"""

    if not raw or not raw.strip():
        return ()
    value = raw.strip()
    if value.startswith("["):
        decoded = json.loads(value)
        if not isinstance(decoded, list):
            raise ValueError("CREDITCHINA_PROXIES 必须是 JSON 数组")
        entries = [str(item) for item in decoded]
    else:
        entries = [item.strip() for item in value.split(",") if item.strip()]
    return tuple(parse_proxy(item) for item in entries)


@dataclass(frozen=True)
class HttpConfig:
    transport: str = "requests"
    timeout: float = 10.0
    retries: int = 5
    backoff: float = 0.8
    proxies: Tuple[ProxySpec, ...] = ()
    user_agents: Tuple[str, ...] = DEFAULT_USER_AGENTS
    cookie: str = ""
    referer: str = "https://www.creditchina.gov.cn/"
    jfbym_token: str = ""
    jfbym_type: str = "10103"

    def __post_init__(self) -> None:
        if self.transport not in ("requests", "urllib"):
            raise ValueError("transport 只能是 requests 或 urllib")
        if self.timeout <= 0:
            raise ValueError("timeout 必须大于 0")
        if self.retries < 1:
            raise ValueError("retries 必须至少为 1")
        if not self.user_agents:
            raise ValueError("至少需要一个 User-Agent")


@dataclass(frozen=True)
class ApiConfig:
    """信用中国当前/旧版 API；路径可通过环境变量替换。"""

    mode: str = "current"
    base_url: str = "https://public.creditchina.gov.cn/private-api"
    site_url: str = "https://www.creditchina.gov.cn"
    search_path: str = "/catalogSearchHome"
    detail_path: str = "/getTyshxydmDetailsContent"
    category_path: str = "/typeSourceSearch"
    permission_path: str = "/api/pub_permissions_name"  # 仅 legacy
    penalty_path: str = "/api/pub_penalty_name"  # 仅 legacy
    record_path: str = "/api/record_param"  # 仅 legacy
    page_size: int = 10
    max_pages: int = 20

    def __post_init__(self) -> None:
        if self.mode not in ("current", "legacy"):
            raise ValueError("API mode 只能是 current 或 legacy")

    @classmethod
    def from_env(
        cls,
        page_size: int = 10,
        max_pages: int = 20,
        mode: Optional[str] = None,
    ) -> "ApiConfig":
        selected_mode = mode or os.getenv("CREDITCHINA_API_MODE", "current")
        if selected_mode == "legacy":
            default_base = "https://www.creditchina.gov.cn"
            default_search = "/api/credit_info_search"
            default_detail = "/api/credit_info_detail"
        else:
            default_base = "https://public.creditchina.gov.cn/private-api"
            default_search = "/catalogSearchHome"
            default_detail = "/getTyshxydmDetailsContent"
        return cls(
            mode=selected_mode,
            base_url=os.getenv("CREDITCHINA_API_BASE", default_base).rstrip("/"),
            site_url=os.getenv("CREDITCHINA_SITE_URL", "https://www.creditchina.gov.cn").rstrip("/"),
            search_path=os.getenv("CREDITCHINA_SEARCH_PATH", default_search),
            detail_path=os.getenv("CREDITCHINA_DETAIL_PATH", default_detail),
            category_path=os.getenv("CREDITCHINA_CATEGORY_PATH", "/typeSourceSearch"),
            permission_path=os.getenv("CREDITCHINA_PERMISSION_PATH", "/api/pub_permissions_name"),
            penalty_path=os.getenv("CREDITCHINA_PENALTY_PATH", "/api/pub_penalty_name"),
            record_path=os.getenv("CREDITCHINA_RECORD_PATH", "/api/record_param"),
            page_size=page_size,
            max_pages=max_pages,
        )

    @classmethod
    def legacy(cls, page_size: int = 10, max_pages: int = 20) -> "ApiConfig":
        return cls(
            mode="legacy",
            base_url="https://www.creditchina.gov.cn",
            search_path="/api/credit_info_search",
            detail_path="/api/credit_info_detail",
            page_size=page_size,
            max_pages=max_pages,
        )


@dataclass(frozen=True)
class DatabaseConfig:
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "root"
    password: str = ""
    database: str = "creditchina"
    charset: str = "utf8mb4"

    @classmethod
    def from_env(cls) -> "DatabaseConfig":
        return cls(
            host=os.getenv("CREDITCHINA_DB_HOST", "127.0.0.1"),
            port=int(os.getenv("CREDITCHINA_DB_PORT", "3306")),
            user=os.getenv("CREDITCHINA_DB_USER", "root"),
            password=os.getenv("CREDITCHINA_DB_PASSWORD", ""),
            database=os.getenv("CREDITCHINA_DB_NAME", "creditchina"),
            charset=os.getenv("CREDITCHINA_DB_CHARSET", "utf8mb4"),
        )


def proxies_from_values(values: Optional[Sequence[str]]) -> Tuple[ProxySpec, ...]:
    if values:
        return tuple(parse_proxy(value) for value in values)
    return parse_proxy_list(os.getenv("CREDITCHINA_PROXIES"))
