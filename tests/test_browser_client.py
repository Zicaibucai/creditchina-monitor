import json
import os
import unittest
from unittest.mock import patch

from creditchina_merged.browser_client import BrowserClient
from creditchina_merged.config import ApiConfig, HttpConfig, parse_proxy
from creditchina_merged.http_client import AccessIntercepted, RequestFailed


class _ApiResponse:
    status = 200

    @staticmethod
    def text():
        return json.dumps({"status": 0, "data": {"total": 1}})


class _RequestContext:
    def __init__(self):
        self.call = None

    def get(self, url, **kwargs):
        self.call = (url, kwargs)
        return _ApiResponse()


class _Context:
    def __init__(self):
        self.request = _RequestContext()


class _ChallengeResponse:
    status = 412

    @staticmethod
    def text():
        return "<html><body></body></html>"


class _JsonBody:
    @staticmethod
    def inner_text():
        return json.dumps({"status": 0, "data": {"recovered": True}})


class _ApiPage:
    def __init__(self):
        self.closed = False

    @staticmethod
    def goto(*args, **kwargs):
        return None

    @staticmethod
    def wait_for_function(*args, **kwargs):
        return None

    @staticmethod
    def locator(selector):
        return _JsonBody()

    def close(self):
        self.closed = True


class _ChallengeRequestContext(_RequestContext):
    def get(self, url, **kwargs):
        self.call = (url, kwargs)
        return _ChallengeResponse()


class _ChallengeContext:
    def __init__(self):
        self.request = _ChallengeRequestContext()
        self.api_page = _ApiPage()

    def new_page(self):
        return self.api_page


class _Page:
    url = "https://www.creditchina.gov.cn/xinyongxinxi/index.html"

    def __init__(self, fail=False):
        self.fail = fail
        self.context = _Context()
        self.script = ""
        self.argument = None

    def evaluate(self, script, argument):
        self.script = script
        self.argument = argument
        if self.fail:
            raise TypeError("Failed to fetch")
        return {"status": 200, "text": json.dumps({"status": 0, "data": []})}


class _ForbiddenPage(_Page):
    def evaluate(self, script, argument):
        self.script = script
        self.argument = argument
        return {"status": 403, "text": "<html>forbidden</html>"}


class _RateLimitedPage(_Page):
    def evaluate(self, script, argument):
        self.script = script
        self.argument = argument
        return {"status": 429, "text": "<html>too many requests</html>"}


class _MissingLocator:
    @staticmethod
    def wait_for(**kwargs):
        raise TimeoutError("blank page")


class _BlankPage:
    def __init__(self):
        self.goto_count = 0

    def goto(self, *args, **kwargs):
        self.goto_count += 1

    @staticmethod
    def locator(selector):
        return _MissingLocator()

    @staticmethod
    def wait_for_timeout(timeout):
        return None


class _CookiePage:
    @staticmethod
    def evaluate(script):
        return "vcode=1" in script


class _PacingPage:
    def __init__(self):
        self.waits = []

    def wait_for_timeout(self, timeout):
        self.waits.append(timeout)


class _CaptchaLocator:
    def __init__(self, page, action):
        self.page = page
        self.action = action
        self.screenshot_path = ""

    def screenshot(self, path):
        self.screenshot_path = path

    def click(self):
        if self.action == "confirm":
            self.page.verified = True

    def fill(self, value):
        self.page.answer = value

    @staticmethod
    def inner_text():
        return "验证码错误"


class _CaptchaPage:
    def __init__(self):
        self.verified = False
        self.answer = ""
        self.locators = {
            "#vcodeimg": _CaptchaLocator(self, "image"),
            "#vcode": _CaptchaLocator(self, "input"),
            ".vcodepop .confirm": _CaptchaLocator(self, "confirm"),
            ".vcodepop .vcodeinputbox p": _CaptchaLocator(self, "error"),
        }

    def locator(self, selector):
        return self.locators[selector]

    @staticmethod
    def wait_for_function(*args, **kwargs):
        return None

    @staticmethod
    def wait_for_timeout(timeout):
        return None

    def evaluate(self, script):
        return self.verified


class _SlowCaptchaPage(_CaptchaPage):
    def __init__(self):
        super().__init__()
        self.image_waits = 0
        self.refreshes = 0

    def wait_for_function(self, script, **kwargs):
        if "naturalWidth" in script:
            self.image_waits += 1
            if self.image_waits == 1:
                raise TimeoutError("image stalled")
        return None

    def evaluate(self, script):
        if "updateVocdeFun" in script:
            self.refreshes += 1
            return None
        return self.verified

    @staticmethod
    def wait_for_timeout(timeout):
        return None


class _MissingCatalogLocator:
    """模拟元素不存在的 locator。"""

    @property
    def first(self):
        return self

    @staticmethod
    def wait_for(**kwargs):
        raise TimeoutError("Timeout 10000ms exceeded.\nCall log:\nwaiting for locator('#xzglCatalog')")

    @staticmethod
    def inner_text(**kwargs):
        return "页面正常内容"

    @staticmethod
    def count():
        return 0


class _NoCatalogPage:
    """详情页缺少信用信息目录锚点（本次线上 bug 的场景）。"""

    url = "https://www.creditchina.gov.cn/xinyongxinxixiangqing/xyDetail.html"

    def goto(self, *args, **kwargs):
        return None

    @staticmethod
    def locator(selector):
        return _MissingCatalogLocator()


class _AltCatalogPage(_NoCatalogPage):
    """xzglCatalog 缺失、但 cataNum24 存在的页面：应继续采集。"""

    def locator(self, selector):
        if selector == "#cataNum24":
            return _AltCatalogTab()
        if selector == "body":
            return _MissingCatalogLocator()
        if selector == ".result-tab1":
            return _AltCatalogTab()
        return _MissingCatalogLocator()


class _AltCatalogTab:
    @property
    def first(self):
        return self

    @staticmethod
    def wait_for(**kwargs):
        return None

    @staticmethod
    def count():
        return 0

    @staticmethod
    def evaluate(script):
        return None


class BrowserClientTests(unittest.TestCase):
    @staticmethod
    def _client(timeout=10):
        client = BrowserClient.__new__(BrowserClient)
        client.http = HttpConfig(timeout=timeout)
        client.api = ApiConfig()
        client.captcha_timeout = 30
        return client

    def test_fetch_matches_official_cross_origin_request(self):
        page = _Page()
        client = self._client(timeout=7)

        payload = client._fetch(page, "https://public.example/api")

        self.assertEqual(0, payload["status"])
        self.assertNotIn("X-Requested-With", page.script)
        self.assertIn("credentials: 'include'", page.script)
        self.assertEqual(7000, page.argument["timeout"])

    def test_failed_browser_fetch_uses_shared_context_request(self):
        page = _Page(fail=True)
        client = self._client(timeout=6)

        payload = client._fetch(page, "https://public.example/api")

        self.assertEqual(1, payload["data"]["total"])
        url, kwargs = page.context.request.call
        self.assertEqual("https://public.example/api", url)
        self.assertEqual(page.url, kwargs["headers"]["Referer"])
        self.assertFalse(kwargs["fail_on_status_code"])
        self.assertEqual(6000, kwargs["timeout"])

    def test_412_uses_same_browser_context_navigation(self):
        page = _Page(fail=True)
        page.context = _ChallengeContext()
        client = self._client(timeout=4)

        payload = client._fetch(page, "https://public.example/api")

        self.assertTrue(payload["data"]["recovered"])
        self.assertTrue(page.context.api_page.closed)

    def test_403_is_converted_to_captcha_challenge(self):
        page = _ForbiddenPage()
        client = self._client(timeout=4)

        payload = client._fetch(page, "https://public.example/api")

        self.assertEqual(40001, payload["code"])
        self.assertIn("403", payload["message"])

    def test_429_stops_the_batch_instead_of_retrying_captcha(self):
        page = _RateLimitedPage()
        client = self._client(timeout=4)

        with self.assertRaisesRegex(AccessIntercepted, "429"):
            client._fetch(page, "https://public.example/api")

    def test_proxy_transport_failure_is_distinct_from_target_interception(self):
        client = self._client(timeout=4)
        client.http = HttpConfig(proxies=(parse_proxy("127.0.0.1:18080"),))

        self.assertTrue(client._proxy_failure(RuntimeError("net::ERR_PROXY_CONNECTION_FAILED")))
        self.assertTrue(client._proxy_failure(RuntimeError("Tunnel connection failed: 517 Proxy Setup Failed")))
        self.assertTrue(client._proxy_failure(RuntimeError("Timeout 10000ms exceeded")))
        self.assertFalse(client._proxy_failure(RuntimeError("HTTP 429")))

    def test_missing_catalog_is_not_mistaken_for_proxy_failure(self):
        """证据页结构缺失的超时不能误判为代理故障去换 IP。"""
        import tempfile
        from pathlib import Path

        client = self._client(timeout=4)
        client.http = HttpConfig(proxies=(parse_proxy("127.0.0.1:18080"),))
        client.request_interval = 0

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RequestFailed, "信用信息目录"):
                client._capture_penalty_page(
                    _NoCatalogPage(),
                    {
                        "company_name": "示例公司",
                        "company_code": "CODE",
                        "detail_url": "https://example/detail",
                        "penalties": [],
                        "output_dir": directory,
                    },
                )

    def test_alternate_catalog_anchor_allows_capture_to_continue(self):
        """xzglCatalog 缺失但处罚栏存在时，不应整页作废。"""
        import tempfile
        from pathlib import Path

        client = self._client(timeout=4)
        client.http = HttpConfig(proxies=(parse_proxy("127.0.0.1:18080"),))
        client.request_interval = 0

        with tempfile.TemporaryDirectory() as directory:
            # 页面没有 result-table，截图步骤会因模拟对象不完整而失败，
            # 但只要越过了目录锚点等待，就说明不会再卡在 xzglCatalog 上。
            with self.assertRaises(Exception) as caught:
                client._capture_penalty_page(
                    _AltCatalogPage(),
                    {
                        "company_name": "示例公司",
                        "company_code": "CODE",
                        "detail_url": "https://example/detail",
                        "penalties": [],
                        "output_dir": directory,
                    },
                )
            self.assertNotIsInstance(caught.exception, RequestFailed)
            self.assertNotIn("信用信息目录", str(caught.exception))

    def test_official_requests_are_paced_within_one_ip_session(self):
        client = self._client()
        client.request_interval = 3
        client._last_official_request_at = 100
        page = _PacingPage()

        with patch(
            "creditchina_merged.browser_client.time.monotonic",
            side_effect=[101, 104],
        ):
            client._pace_official_request(page)

        self.assertEqual([2000], page.waits)
        self.assertEqual(104, client._last_official_request_at)

    def test_blank_page_is_retried_and_never_treated_as_verified(self):
        client = self._client()
        client.captcha_timeout = 1
        page = _BlankPage()

        with self.assertRaisesRegex(RequestFailed, "空白风控页"):
            client._open_search_page(page, "https://public.example/api?keyword=test")

        self.assertEqual(2, page.goto_count)

    def test_visible_company_result_bypasses_captcha_and_opens_first_company(self):
        client = self._client()
        search_page = object()
        detail_page = object()

        with patch.object(client, "_open_search_page") as open_search, patch.object(
            client, "_has_search_results", return_value=True
        ), patch.object(
            client, "_open_first_search_result", return_value=detail_page
        ) as open_first, patch.object(
            client, "_verify_from_downloaded_image"
        ) as solve_captcha:
            selected_page = client._wait_for_human_verification(
                search_page,
                "https://public.example/api?keyword=test",
            )

        open_search.assert_called_once_with(search_page, "https://public.example/api?keyword=test")
        open_first.assert_called_once_with(search_page)
        solve_captcha.assert_not_called()
        self.assertIs(detail_page, selected_page)

    def test_verification_uses_official_vcode_cookie(self):
        self.assertTrue(BrowserClient._has_vcode(_CookiePage()))

    def test_captcha_is_downloaded_submitted_and_deleted(self):
        client = self._client()
        page = _CaptchaPage()

        with patch("builtins.input", return_value="A1B2"), patch.object(
            BrowserClient, "_open_local_image", return_value=True
        ):
            client._verify_from_downloaded_image(page)

        image_path = page.locators["#vcodeimg"].screenshot_path
        self.assertEqual("A1B2", page.answer)
        self.assertTrue(page.verified)
        self.assertTrue(image_path.endswith(".png"))
        self.assertFalse(os.path.exists(image_path))

    def test_stalled_captcha_image_refreshes_after_five_seconds(self):
        client = self._client()
        page = _SlowCaptchaPage()

        with patch("builtins.input", return_value="A1B2"), patch.object(
            BrowserClient, "_open_local_image", return_value=True
        ):
            client._verify_from_downloaded_image(page)

        self.assertEqual(2, page.image_waits)
        self.assertEqual(1, page.refreshes)
        self.assertTrue(page.verified)

    def test_jfbym_automatic_solving_success(self):
        client = self._client()
        client.http = HttpConfig(jfbym_token="dummy_token")
        page = _CaptchaPage()

        with patch("requests.Session") as session_factory:
            session = session_factory.return_value
            session.post.return_value.json.return_value = {
                "code": 10000,
                "msg": "识别成功",
                "data": {
                    "code": 0,
                    "data": "AutoSolved123"
                }
            }
            with patch("builtins.input") as mock_input:
                client._verify_from_downloaded_image(page)
                mock_input.assert_not_called()

        self.assertEqual("AutoSolved123", page.answer)
        self.assertTrue(page.verified)
        self.assertFalse(session.trust_env)
        self.assertEqual(30.0, session.post.call_args.kwargs["timeout"])
        session.close.assert_called_once()

    def test_jfbym_automatic_solving_failure_fallback(self):
        client = self._client()
        client.http = HttpConfig(jfbym_token="dummy_token")
        page = _CaptchaPage()

        with patch("requests.Session") as session_factory:
            session_factory.return_value.post.return_value.json.return_value = {
                "code": 10002,
                "msg": "余额不足"
            }
            with patch("builtins.input", return_value="ManualFallback") as mock_input, patch.object(
                BrowserClient, "_open_local_image", return_value=True
            ):
                client._verify_from_downloaded_image(page)
                mock_input.assert_called_once()

        self.assertEqual("ManualFallback", page.answer)
        self.assertTrue(page.verified)

    def test_web_background_captcha_failure_never_opens_preview_or_reads_input(self):
        client = self._client()
        client.http = HttpConfig(jfbym_token="dummy_token")
        client.allow_manual_captcha = False
        client.captcha_auto_attempts = 2
        page = _CaptchaPage()

        with patch.object(client, "_solve_captcha_jfbym", return_value=None), patch.object(
            BrowserClient, "_open_local_image"
        ) as open_image, patch("builtins.input") as read_input:
            with self.assertRaisesRegex(RequestFailed, "连续失败 2 次"):
                client._verify_from_downloaded_image(page)

        open_image.assert_not_called()
        read_input.assert_not_called()


if __name__ == "__main__":
    unittest.main()
