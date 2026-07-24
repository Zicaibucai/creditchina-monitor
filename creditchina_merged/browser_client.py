"""通过可见 Chrome 和官网验证码建立新版 API 会话。"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import queue
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.error import URLError
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import urlopen

from .config import ApiConfig, HttpConfig, ProxySpec
from .http_client import AccessIntercepted, ProxyUnavailable, RequestFailed
from .proxy_relay import AuthenticatedProxyRelay, ProxyRelayError, chrome_proxy_for


logger = logging.getLogger(__name__)


def _is_challenge(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    values = (payload.get("status"), payload.get("code"))
    if any(str(item) == "40001" for item in values if item is not None):
        return True
    message = str(payload.get("message") or payload.get("msg") or "")
    return "验证码" in message or "刷新后重试" in message


def _is_access_interception(payload: Any) -> bool:
    """识别验证码之外的访问频率限制。"""

    if not isinstance(payload, dict):
        return False
    values = (payload.get("status"), payload.get("code"))
    if any(str(item) in {"403", "412", "429"} for item in values if item is not None):
        return True
    message = str(payload.get("message") or payload.get("msg") or "").lower()
    markers = (
        "too many requests",
        "rate limit",
        "access denied",
        "访问频繁",
        "请求频繁",
        "操作频繁",
        "访问受限",
        "请稍后再试",
        "请求过多",
        "风控拦截",
    )
    return any(marker in message for marker in markers)


class BrowserClient:
    """把全部 Playwright 调用固定在专用线程，供多个采集线程安全复用。"""

    def __init__(
        self,
        http: HttpConfig,
        api: ApiConfig,
        captcha_timeout: float = 300.0,
        request_interval: float = 1.0,
        allow_manual_captcha: bool = True,
        captcha_auto_attempts: int = 3,
        captcha_solver_timeout: float = 30.0,
    ) -> None:
        self.http = http
        self.api = api
        self.captcha_timeout = captcha_timeout
        self.request_interval = max(0.0, request_interval)
        self.allow_manual_captcha = allow_manual_captcha
        self.captcha_auto_attempts = max(1, captcha_auto_attempts)
        self.captcha_solver_timeout = max(1.0, captcha_solver_timeout)
        self._last_official_request_at = 0.0
        self._commands: "queue.Queue[Optional[Tuple[str, Any, queue.Queue[Any]]]]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="浏览器会话", daemon=True)
        self._closed = False
        self._stop_queued = False
        self._process_lock = threading.Lock()
        self._chrome_process: Optional[subprocess.Popen[Any]] = None
        self._thread.start()

    def _proxy_failure(self, error: BaseException) -> bool:
        if not self.http.proxies:
            return False
        message = str(error).lower()
        return any(
            marker in message
            for marker in (
                "err_proxy_connection_failed",
                "err_tunnel_connection_failed",
                "err_socks_connection_failed",
                "proxy authentication required",
                "proxy authentication expired",
                "proxyconnect tcp",
                "http 407",
                "status 407",
                "tunnel connection failed: 454",
                "tunnel connection failed: 516",
                "tunnel connection failed: 517",
                "516 proxy failed",
                "517 proxy setup failed",
                "timeout",
                "timed out",
                "signal is aborted",
            )
        )

    def _pace_official_request(self, page: Any) -> None:
        """对同一 IP 的官网请求做最小间隔控制。"""

        interval = max(0.0, float(getattr(self, "request_interval", 0.0)))
        last_request = float(getattr(self, "_last_official_request_at", 0.0))
        now = time.monotonic()
        remaining = interval - (now - last_request)
        if remaining > 0:
            page.wait_for_timeout(int(remaining * 1000))
        self._last_official_request_at = time.monotonic()

    def get_json(self, url: str) -> Any:
        if self._closed:
            raise RequestFailed("浏览器会话已经关闭")
        result: "queue.Queue[Any]" = queue.Queue(maxsize=1)
        self._commands.put(("json", url, result))
        success, value = result.get()
        if success:
            return value
        raise value

    def capture_penalty_evidence(
        self,
        company_name: str,
        company_code: str,
        uuid_value: str,
        penalties: Sequence[Mapping[str, Any]],
        output_dir: Path,
    ) -> Dict[str, Any]:
        """保存官网行政处罚整页、逐条截图、DOM 源码和校验信息。"""

        if self._closed:
            raise RequestFailed("浏览器会话已经关闭")
        result: "queue.Queue[Any]" = queue.Queue(maxsize=1)
        detail_url = (
            self.api.site_url
            + "/xinyongxinxixiangqing/xyDetail.html?"
            + urlencode(
                {
                    "searchState": 1,
                    "entityType": 1,
                    "keyword": company_name,
                    "uuid": uuid_value,
                    "tyshxydm": company_code,
                }
            )
        )
        self._commands.put(
            (
                "penalty_evidence",
                {
                    "company_name": company_name,
                    "company_code": company_code,
                    "detail_url": detail_url,
                    "penalties": list(penalties),
                    "output_dir": str(output_dir),
                },
                result,
            )
        )
        success, value = result.get()
        if success:
            return value
        raise value

    @staticmethod
    def _evidence_identity(item: Mapping[str, Any]) -> str:
        for key in ("决定书文号", "行政处罚决定书文号"):
            value = str(item.get(key, "")).strip()
            if value:
                return value
        raw = item.get("_原始字段")
        if isinstance(raw, Mapping):
            for key in ("recid", "uuid", "flowno"):
                value = str(raw.get(key, "")).strip()
                if value:
                    return value
        encoded = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _capture_penalty_page(self, page: Any, payload: Mapping[str, Any]) -> Dict[str, Any]:
        company_name = str(payload["company_name"])
        company_code = str(payload["company_code"])
        detail_url = str(payload["detail_url"])
        penalties = list(payload.get("penalties") or [])
        captured_at = datetime.now().astimezone()
        safe_company = "".join(character if character not in '<>:"/\\|?*' else "_" for character in company_name).strip() or "company"
        evidence_dir = (
            Path(str(payload["output_dir"]))
            / "evidence"
            / captured_at.strftime("%Y-%m-%d")
            / safe_company
            / captured_at.strftime("%H%M%S")
        )
        evidence_dir.mkdir(parents=True, exist_ok=True)

        self._pace_official_request(page)
        try:
            response = page.goto(detail_url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            if self._proxy_failure(exc):
                raise ProxyUnavailable("当前代理无法打开官网证据页") from exc
            raise
        response_status = getattr(response, "status", None)
        if response_status in (403, 412, 429):
            raise AccessIntercepted("官网证据页触发访问风控（HTTP %d）" % response_status)
        body_text = page.locator("body").inner_text(timeout=5000).lower()
        if any(
            marker in body_text
            for marker in (
                "too many requests",
                "rate limit",
                "访问频繁",
                "请求频繁",
                "操作频繁",
                "访问受限",
                "请求过多",
            )
        ):
            raise AccessIntercepted("官网证据页已显示访问频率拦截")
        page.locator("#xzglCatalog").wait_for(state="visible", timeout=30000)
        penalty_tab = page.locator("#cataNum24")
        expected_count = 0
        if penalty_tab.count():
            count_text = penalty_tab.locator("span").inner_text().strip() if penalty_tab.locator("span").count() else "0"
            expected_count = int("".join(character for character in count_text if character.isdigit()) or "0")
            penalty_tab.evaluate("element => element.click()")
            if expected_count:
                page.wait_for_function(
                    """
                    () => {
                      const first = document.querySelector('#resultTab2 .result-table tr td.graybg');
                      return first && first.textContent.trim() === '行政处罚决定书文号';
                    }
                    """,
                    timeout=30000,
                )
            else:
                page.wait_for_timeout(1500)
        page.mouse.move(5, 5)
        page.wait_for_timeout(500)

        overview_path = evidence_dir / "行政处罚-整页.png"
        panel_path = evidence_dir / "行政处罚-全部条目.png"
        html_path = evidence_dir / "行政处罚-页面源码.html"
        page.screenshot(path=str(overview_path), full_page=True)
        page.locator(".result-tab1").screenshot(path=str(panel_path))
        html_path.write_text(page.content(), encoding="utf-8")

        by_document: Dict[str, Mapping[str, Any]] = {}
        for item in penalties:
            document = str(item.get("决定书文号") or item.get("行政处罚决定书文号") or "").strip()
            if document:
                by_document[document] = item

        items: List[Dict[str, Any]] = []
        tables = page.locator("#resultTab2 .result-table")
        penalty_tables = []
        for table_index in range(tables.count()):
            candidate = tables.nth(table_index)
            first_label = candidate.locator("td.graybg").first.inner_text().strip()
            if first_label == "行政处罚决定书文号":
                penalty_tables.append(candidate)
        consistency_error = ""
        if len(penalty_tables) != len(penalties):
            consistency_error = (
                "官网处罚截图条数与 JSON 不一致：页面 %d 条，JSON %d 条"
                % (len(penalty_tables), len(penalties))
            )
        for index, table in enumerate(penalty_tables):
            document = (
                table.locator("td.graybg")
                .first.locator("xpath=following-sibling::td[1]")
                .locator("p")
                .first.inner_text()
                .strip()
            )
            item = by_document.get(document, penalties[index] if index < len(penalties) else {})
            identity = self._evidence_identity(item) if item else (
                document or "page-item-%d" % (index + 1)
            )
            token = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
            screenshot_path = evidence_dir / ("行政处罚-%02d-%s.png" % (index + 1, token))
            table_box = table.bounding_box()
            heading = table.locator("xpath=preceding-sibling::h3[1]")
            heading_box = heading.bounding_box() if heading.count() else None
            if table_box:
                top = heading_box["y"] if heading_box else table_box["y"]
                dimensions = page.evaluate(
                    "() => ({width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight})"
                )
                left = max(0, table_box["x"])
                top = max(0, top)
                width = min(table_box["width"], max(0, dimensions["width"] - left))
                height = min(
                    table_box["y"] + table_box["height"] - top,
                    max(0, dimensions["height"] - top),
                )
                try:
                    if width <= 1 or height <= 1:
                        raise ValueError("处罚截图裁剪区域为空")
                    page.screenshot(
                        path=str(screenshot_path),
                        clip={"x": left, "y": top, "width": width, "height": height},
                    )
                except Exception:
                    table.screenshot(path=str(screenshot_path))
            else:
                table.screenshot(path=str(screenshot_path))
            items.append(
                {
                    "identity": identity,
                    "document_number": document,
                    "screenshot_path": str(screenshot_path.resolve()),
                    "sha256": self._file_sha256(screenshot_path),
                }
            )

        metadata_path = evidence_dir / "行政处罚-证据清单.json"
        metadata = {
            "company_name": company_name,
            "company_code": company_code,
            "source_url": detail_url,
            "final_url": page.url,
            "page_title": page.title(),
            "captured_at": captured_at.isoformat(),
            "capture_method": "Playwright Chrome official DOM screenshot",
            "penalty_count_json": len(penalties),
            "penalty_count_page": len(items),
            "consistency_error": consistency_error,
            "overview_path": str(overview_path.resolve()),
            "overview_sha256": self._file_sha256(overview_path),
            "penalty_panel_path": str(panel_path.resolve()),
            "penalty_panel_sha256": self._file_sha256(panel_path),
            "html_path": str(html_path.resolve()),
            "html_sha256": self._file_sha256(html_path),
            "items": items,
        }
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        metadata["metadata_path"] = str(metadata_path.resolve())
        metadata["metadata_sha256"] = self._file_sha256(metadata_path)
        return metadata

    def _search_page_url(self, api_url: str) -> str:
        keyword = parse_qs(urlparse(api_url).query).get("keyword", [""])[0]
        return (
            self.api.site_url
            + "/xinyongxinxi/index.html?index=0&scenes=defaultScenario"
            + "&tableName=credit_xyzx_tyshxydm&searchState=2"
            + "&entityType=1,2,4,5,6,7,8&keyword="
            + quote(keyword)
        )

    @staticmethod
    def _has_vcode(page: Any) -> bool:
        return bool(
            page.evaluate(
                "document.cookie.split(';').some(v => v.trim() === 'vcode=1')"
            )
        )

    def _open_search_page(self, page: Any, api_url: str) -> None:
        """等待企业结果或验证码弹窗，拒绝把 WAF 空白页当作成功。"""

        last_error: Optional[BaseException] = None
        for attempt in range(2):
            try:
                page.goto(
                    self._search_page_url(api_url),
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                page.wait_for_function(
                    """
                    () => {
                      const result = document.querySelector('#companylists .company-item');
                      const popup = document.querySelector('.vcodepop');
                      const popupVisible = !!popup && getComputedStyle(popup).display !== 'none';
                      return !!result || popupVisible;
                    }
                    """,
                    timeout=20000,
                )
                return
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    page.wait_for_timeout(1500)
        if last_error is not None and self._proxy_failure(last_error):
            raise ProxyUnavailable("当前代理无法建立官网连接") from last_error
        raise AccessIntercepted(
            "信用中国返回空白风控页，企业结果和验证码均未加载；"
            "程序已自动重试，仍未恢复"
        ) from last_error

    @staticmethod
    def _has_search_results(page: Any) -> bool:
        return page.locator("#companylists .company-item").count() > 0

    def _open_first_search_result(self, page: Any) -> Any:
        """点击官网搜索结果第一家，并把同一会话切换到企业详情页。"""

        first = page.locator("#companylists .company-item").first
        with page.expect_popup(timeout=10000) as popup_info:
            first.click()
        detail_page = popup_info.value
        detail_page.wait_for_load_state("domcontentloaded", timeout=60000)
        try:
            page.close()
        except Exception:
            logger.debug("关闭搜索页失败（可忽略）", exc_info=True)
        return detail_page

    def _wait_for_search_results(self, page: Any) -> None:
        page.locator("#companylists .company-item").first.wait_for(
            state="visible",
            timeout=max(20000, int(self.http.timeout * 1000)),
        )

    def _wait_for_human_verification(self, page: Any, api_url: str, force: bool = False) -> Any:
        if force:
            try:
                page.context.clear_cookies(name="vcode")
            except Exception:
                page.evaluate(
                    "document.cookie='vcode=;expires=Thu, 01 Jan 1970 00:00:00 GMT;path=/;domain=.creditchina.gov.cn'"
                )
        self._open_search_page(page, api_url)

        # 官网只有在验证码已经通过、搜索接口成功返回后才渲染企业结果。
        # 结果已经出现时直接点击第一家，不再重复等待验证码弹窗或图片。
        if self._has_search_results(page):
            return self._open_first_search_result(page)

        if not force and self._has_vcode(page):
            self._wait_for_search_results(page)
            return self._open_first_search_result(page)
        popup = page.locator(".vcodepop")
        try:
            popup.wait_for(state="visible", timeout=20000)
        except Exception as exc:
            raise RequestFailed("官网已加载，但验证码窗口没有显示") from exc
        if not sys.stdin.isatty() and not self.http.jfbym_token:
            raise RequestFailed("新版官网需要人工验证码，请在交互式终端运行")
        self._verify_from_downloaded_image(page)
        self._wait_for_search_results(page)
        return self._open_first_search_result(page)

    @staticmethod
    def _open_local_image(path: str) -> bool:
        if sys.platform == "darwin":
            command = ["open", "-a", "Preview", path]
        elif sys.platform.startswith("linux") and shutil.which("xdg-open"):
            command = ["xdg-open", path]
        else:
            return False
        try:
            subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except OSError:
            return False

    def _solve_captcha_jfbym(self, image_b64: str) -> Optional[str]:
        import requests
        url = "http://api.jfbym.com/api/YmServer/customApi"
        data = {
            "token": self.http.jfbym_token,
            "type": self.http.jfbym_type,
            "image": image_b64,
        }
        headers = {
            "Content-Type": "application/json"
        }
        # requests 会自动读取 macOS 系统代理。打码接口与官网代理会话无关，
        # 因此这里明确直连，避免本机代理软件关闭后卡在 127.0.0.1 端口。
        session = requests.Session()
        session.trust_env = False
        try:
            response = session.post(
                url,
                headers=headers,
                json=data,
                timeout=float(getattr(self, "captcha_solver_timeout", 30.0)),
            ).json()
            if response.get("code") == 10000:
                val = response.get("data", {}).get("data")
                return str(val).strip() if val is not None else None
            else:
                msg = response.get("msg", "未知错误")
                logger.info(f"打码平台返回失败：code={response.get('code')}, msg={msg}")
        except Exception as e:
            logger.info(f"打码平台请求异常: {e}")
        finally:
            session.close()
        return None

    def _verify_from_downloaded_image(self, page: Any) -> None:
        """保存同会话验证码，由打码平台自动识别或终端人工输入提交。"""

        deadline = time.monotonic() + self.captcha_timeout
        allow_manual = bool(getattr(self, "allow_manual_captcha", True))
        max_auto_attempts = max(1, int(getattr(self, "captcha_auto_attempts", 3)))
        auto_failures = 0
        captcha = page.locator("#vcodeimg")
        captcha_input = page.locator("#vcode")
        confirm = page.locator(".vcodepop .confirm")
        error_tip = page.locator(".vcodepop .vcodeinputbox p")

        while time.monotonic() < deadline:
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
            try:
                page.wait_for_function(
                    """
                    () => {
                      const img = document.querySelector('#vcodeimg');
                      return !!img && img.complete && img.naturalWidth > 0;
                    }
                    """,
                    timeout=min(3000, remaining_ms),
                )
            except Exception:
                if time.monotonic() >= deadline:
                    break
                logger.info("验证码图片 5 秒内未加载，正在自动刷新……")
                try:
                    page.evaluate(
                        """
                        () => {
                          if (typeof updateVocdeFun === 'function') {
                            updateVocdeFun();
                          } else {
                            const img = document.querySelector('#vcodeimg');
                            if (img) img.click();
                          }
                        }
                        """
                    )
                except Exception:
                    captcha.click()
                page.wait_for_timeout(300)
                continue

            handle, image_path = tempfile.mkstemp(
                prefix="creditchina-captcha-",
                suffix=".png",
            )
            os.close(handle)
            try:
                captcha.screenshot(path=image_path)
                answer = ""
                if self.http.jfbym_token:
                    logger.info("检测到打码平台 Token，正在请求 jfbym.com 自动识别验证码...")
                    try:
                        with open(image_path, "rb") as f:
                            image_b64 = base64.b64encode(f.read()).decode()
                        answer = self._solve_captcha_jfbym(image_b64)
                        if answer:
                            logger.info(f"打码平台自动识别成功：{answer}")
                        else:
                            logger.info("打码平台识别失败")
                    except Exception as e:
                        logger.info(f"打码平台调用出错：{e}")

                if not answer:
                    if not allow_manual:
                        auto_failures += 1
                        if not self.http.jfbym_token:
                            raise RequestFailed(
                                "网页后台未配置验证码自动识别 Token，任务未进入人工输入"
                            )
                        if auto_failures >= max_auto_attempts:
                            raise RequestFailed(
                                "验证码自动识别连续失败 %d 次，任务未进入人工输入；"
                                "请检查打码服务后从断点继续" % auto_failures
                            )
                        logger.info(
                            "验证码自动识别失败（%d/%d），正在自动换图重试……"
                            % (auto_failures, max_auto_attempts),
                        )
                        captcha.click()
                        page.wait_for_timeout(300)
                        continue
                    opened = self._open_local_image(image_path)
                    logger.info(
                        "\n验证码图片已下载%s：%s"
                        % ("并打开预览" if opened else "", image_path),
                    )
                    answer = input("请查看图片并在此输入验证码（直接回车可换一张）：").strip()
                    if not answer:
                        captcha.click()
                        continue

                captcha_input.fill(answer)
                confirm.click()
                remaining_ms = max(1000, int((deadline - time.monotonic()) * 1000))
                try:
                    page.wait_for_function(
                        """
                        () => {
                          const passed = document.cookie.split(';')
                            .some(v => v.trim() === 'vcode=1');
                          const tip = document.querySelector('.vcodepop .vcodeinputbox p');
                          const failed = !!tip && getComputedStyle(tip).display !== 'none';
                          return passed || failed;
                        }
                        """,
                        timeout=min(15000, remaining_ms),
                    )
                except Exception as exc:
                    raise RequestFailed("官网没有返回验证码校验结果") from exc

                if self._has_vcode(page):
                    logger.info("验证码验证成功，继续采集。")
                    return
                message = (error_tip.inner_text() or "验证码错误").strip()
                if not allow_manual:
                    auto_failures += 1
                    if auto_failures >= max_auto_attempts:
                        raise RequestFailed(
                            "验证码自动识别或官网校验连续失败 %d 次（最后结果：%s）；"
                            "请检查打码服务后从断点继续"
                            % (auto_failures, message)
                        )
                    logger.info(
                        "%s（%d/%d），正在自动换图重试……"
                        % (message, auto_failures, max_auto_attempts),
                    )
                    captcha.click()
                    page.wait_for_timeout(300)
                    continue
                logger.info("%s，已自动下载新图片。" % message)
            finally:
                try:
                    os.unlink(image_path)
                except FileNotFoundError:
                    pass

        if allow_manual:
            raise RequestFailed("等待人工输入或加载官网验证码超时")
        raise RequestFailed("后台自动识别或加载官网验证码超时；请从断点继续")

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    @staticmethod
    def _chrome_executable() -> str:
        candidates = (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            shutil.which("google-chrome"),
            shutil.which("google-chrome-stable"),
            shutil.which("chromium"),
        )
        for candidate in candidates:
            if candidate and os.path.isfile(candidate):
                return candidate
        raise RequestFailed("未找到 Google Chrome，请先安装 Chrome")

    @classmethod
    def _launch_regular_chrome(
        cls,
        playwright: Any,
        proxy: Optional[ProxySpec] = None,
    ) -> Tuple[Any, Any, Any, str, Optional[AuthenticatedProxyRelay]]:
        """启动普通 Chrome 后通过 CDP 连接，代理与直连使用同一条路径。"""

        port = cls._free_port()
        profile_dir = tempfile.mkdtemp(prefix="creditchina-chrome-")
        proxy_relay: Optional[AuthenticatedProxyRelay] = None
        try:
            chrome_proxy, proxy_relay = chrome_proxy_for(proxy)
        except ProxyRelayError as exc:
            shutil.rmtree(profile_dir, ignore_errors=True)
            raise RequestFailed("无法为普通 Chrome 配置代理：%s" % exc) from exc
        arguments = [
            cls._chrome_executable(),
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=%d" % port,
            "--user-data-dir=%s" % profile_dir,
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if chrome_proxy:
            arguments.append("--proxy-server=%s" % chrome_proxy)
        arguments.append("about:blank")
        process = subprocess.Popen(
            arguments,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        endpoint = "http://127.0.0.1:%d" % port
        try:
            for _ in range(100):
                if process.poll() is not None:
                    raise RequestFailed("Chrome 启动失败")
                try:
                    with urlopen(endpoint + "/json/version", timeout=0.5):
                        break
                except (OSError, URLError):
                    time.sleep(0.1)
            else:
                raise RequestFailed("等待 Chrome 调试端口超时")
            browser = playwright.chromium.connect_over_cdp(endpoint)
            if not browser.contexts:
                raise RequestFailed("Chrome 没有可用浏览器上下文")
            return browser, browser.contexts[0], process, profile_dir, proxy_relay
        except Exception:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            shutil.rmtree(profile_dir, ignore_errors=True)
            if proxy_relay is not None:
                proxy_relay.stop()
            raise

    def _fetch_via_navigation(self, context: Any, url: str) -> Any:
        """让 Chrome 执行 412 页面中的风控脚本，再读取重载后的 JSON。"""

        api_page = context.new_page()
        timeout_ms = max(1000, int(self.http.timeout * 1000))
        try:
            api_page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            api_page.wait_for_function(
                """
                () => {
                  const text = (document.body && document.body.innerText || '').trim();
                  return text.startsWith('{') || text.startsWith('[');
                }
                """,
                timeout=timeout_ms,
            )
            return json.loads(api_page.locator("body").inner_text())
        except Exception as exc:
            raise AccessIntercepted("新版接口 HTTP 412，风控刷新仍未恢复") from exc
        finally:
            api_page.close()

    def _fetch(self, page: Any, url: str) -> Any:
        """按官网 AJAX 方式请求；CORS/WAF 拒绝时复用同一上下文直取响应。"""

        self._pace_official_request(page)
        timeout_ms = max(1000, int(self.http.timeout * 1000))
        try:
            response = page.evaluate(
                """
                async ({url, timeout}) => {
                  const controller = new AbortController();
                  const timer = setTimeout(() => controller.abort(), timeout);
                  const response = await fetch(url, {
                    method: 'GET',
                    credentials: 'include',
                    headers: {'Accept': 'application/json, text/plain, */*'},
                    signal: controller.signal
                  });
                  try {
                    return {status: response.status, text: await response.text()};
                  } finally {
                    clearTimeout(timer);
                  }
                }
                """,
                {"url": url, "timeout": timeout_ms},
            )
        except Exception as fetch_error:
            # public.creditchina.gov.cn 的部分风控响应不带 CORS 头，浏览器只会
            # 暴露模糊的 ``TypeError: Failed to fetch``。context.request 与页面
            # 共享验证码 Cookie，但不受页面跨域策略限制，因此可读取真实响应。
            try:
                api_response = page.context.request.get(
                    url,
                    headers={
                        "Accept": "application/json, text/plain, */*",
                        "Origin": "https://www.creditchina.gov.cn",
                        "Referer": page.url,
                    },
                    timeout=timeout_ms,
                    fail_on_status_code=False,
                )
                response = {"status": api_response.status, "text": api_response.text()}
            except Exception as fallback_error:
                if self._proxy_failure(fetch_error) or self._proxy_failure(fallback_error):
                    raise ProxyUnavailable("当前代理无法请求官网接口") from fallback_error
                raise RequestFailed(
                    "新版接口的浏览器请求及同会话回退请求均失败：%s；%s"
                    % (fetch_error, fallback_error)
                ) from fallback_error
        if response.get("status") == 412:
            return self._fetch_via_navigation(page.context, url)
        if response.get("status") == 429:
            raise AccessIntercepted("官网触发访问频率限制（HTTP 429）")
        if response.get("status") == 403:
            # 官网会在验证码 Cookie 过期或风控状态变化时返回 HTML 403。
            # 转成统一的验证码挑战，让浏览器线程重新远程识别并重试一次。
            return {
                "code": 40001,
                "message": "官网会话返回 HTTP 403，需要重新验证验证码",
            }
        try:
            return json.loads(response["text"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise RequestFailed(
                "新版接口返回非 JSON（HTTP %s）：%s"
                % (response.get("status", "?"), str(response.get("text", ""))[:200])
            ) from exc

    def _run(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self._fail_all(
                RuntimeError("浏览器传输需要 Playwright，请重新执行：pip install -r requirements.txt")
            )
            return

        try:
            with sync_playwright() as playwright:
                selected_proxy = (
                    random.choice(self.http.proxies) if self.http.proxies else None
                )
                browser, context, chrome_process, profile_dir, proxy_relay = (
                    self._launch_regular_chrome(playwright, selected_proxy)
                )
                with self._process_lock:
                    self._chrome_process = chrome_process
                page = context.pages[0] if context.pages else context.new_page()
                verified = False
                try:
                    while True:
                        command = self._commands.get()
                        if command is None:
                            return
                        kind, command_payload, result = command
                        try:
                            if not verified:
                                verification_url = command_payload if kind == "json" else command_payload["detail_url"]
                                page = self._wait_for_human_verification(page, verification_url)
                                verified = True
                            if kind == "penalty_evidence":
                                result.put((True, self._capture_penalty_page(page, command_payload)))
                            else:
                                url = str(command_payload)
                                response_payload = self._fetch(page, url)
                                if _is_challenge(response_payload):
                                    raise AccessIntercepted(
                                        "当前 IP 完成初始验证后又被要求验证，需要更换 IP"
                                    )
                                if _is_access_interception(response_payload):
                                    raise AccessIntercepted("官网已返回访问频率限制")
                                result.put((True, response_payload))
                        except Exception as exc:
                            result.put((False, exc))
                finally:
                    try:
                        context.close()
                    except Exception:
                        logger.debug("关闭浏览器上下文失败（可忽略）", exc_info=True)
                    try:
                        browser.close()
                    except Exception:
                        logger.debug("关闭浏览器失败（可忽略）", exc_info=True)
                    if chrome_process is not None and chrome_process.poll() is None:
                        chrome_process.terminate()
                        try:
                            chrome_process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            chrome_process.kill()
                    with self._process_lock:
                        self._chrome_process = None
                    if profile_dir:
                        shutil.rmtree(profile_dir, ignore_errors=True)
                    if proxy_relay is not None:
                        proxy_relay.stop()
        except Exception as exc:
            if self._proxy_failure(exc):
                self._fail_all(ProxyUnavailable("当前代理无法建立 Chrome 会话"))
            else:
                self._fail_all(exc)

    def _fail_all(self, error: BaseException) -> None:
        while True:
            command = self._commands.get()
            if command is None:
                return
            _, _, result = command
            result.put((False, error))

    def close(self) -> None:
        self._closed = True
        if not self._stop_queued:
            self._stop_queued = True
            self._commands.put(None)
        self._thread.join(timeout=10)

    def abort(self) -> None:
        """立即中断当前 Chrome 请求，并停止接受后续采集命令。"""

        self._closed = True
        if not self._stop_queued:
            self._stop_queued = True
            self._commands.put(None)
        with self._process_lock:
            process = self._chrome_process
        if process is not None and process.poll() is None:
            process.terminate()
