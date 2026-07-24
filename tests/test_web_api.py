import tempfile
import time
import unittest
import os
import json
import threading
from pathlib import Path
from unittest.mock import patch

from creditchina_merged.crawler import EnterpriseRecord
from creditchina_merged.http_client import AccessIntercepted, ProxyUnavailable
from fastapi.middleware.cors import CORSMiddleware

from creditchina_merged.web_api import CreditScoreManager, CrawlManager, TaskStore, create_app


class FakeClient:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeCrawler:
    def crawl_company(self, company_name):
        return EnterpriseRecord(
            name=company_name,
            encry_str="real-session-key",
            basic={
                "法人": "测试法人",
                "企业状态": "存续",
                "统一社会信用代码": "CODE-001",
            },
            permissions=[{"行政许可决定书文号": "许可-1"}],
            penalties=[{"决定书文号": "处罚-1"}],
        )


class InterceptedCrawler(FakeCrawler):
    def __init__(self):
        self.calls = []

    def crawl_company(self, company_name):
        self.calls.append(company_name)
        if len(self.calls) == 2:
            raise AccessIntercepted("访问频繁")
        return super().crawl_company(company_name)


class UnavailableProxyCrawler:
    def crawl_company(self, company_name):
        raise ProxyUnavailable("代理隧道连接失败")


class AlwaysInterceptedCrawler:
    def __init__(self):
        self.calls = []

    def crawl_company(self, company_name):
        self.calls.append(company_name)
        raise AccessIntercepted("新版接口 HTTP 412，风控刷新仍未恢复")


class EvidenceClient(FakeClient):
    def __init__(self, failure=None):
        super().__init__()
        self.failure = failure
        self.capture_calls = []

    def capture_penalty_evidence(self, company_name, credit_code, encry_str, penalties, output_dir):
        self.capture_calls.append(company_name)
        if self.failure is not None:
            raise self.failure
        return {"overview_path": "", "metadata_path": "", "items": []}


class CountingCrawler(FakeCrawler):
    def __init__(self):
        self.calls = []

    def crawl_company(self, company_name):
        self.calls.append(company_name)
        return super().crawl_company(company_name)


class BlockingCrawler(FakeCrawler):
    def __init__(self, started, release):
        self.started = started
        self.release = release

    def crawl_company(self, company_name):
        self.started.set()
        self.release.wait(timeout=5)
        return super().crawl_company(company_name)


class AbortableClient(FakeClient):
    def __init__(self, release):
        super().__init__()
        self.release = release
        self.aborted = False

    def abort(self):
        self.aborted = True
        self.closed = True
        self.release.set()


class BlockedCompanyCrawler(CountingCrawler):
    def __init__(self, blocked_company):
        super().__init__()
        self.blocked_company = blocked_company

    def crawl_company(self, company_name):
        self.calls.append(company_name)
        if company_name == self.blocked_company:
            raise AccessIntercepted("当前 IP 已被限流")
        return FakeCrawler.crawl_company(self, company_name)


class FakeCreditScoreSpider:
    def __init__(self):
        self.calls = []
        self.closed = False

    def crawl(self, company_name, company_code=""):
        self.calls.append((company_name, company_code))
        return {
            "enterpriseName": company_name,
            "scoreTotal": 96.5,
            "scoreBasic": 65,
            "creditreportEnddate": "2026年07月17日",
            "detail_fetched": True,
        }

    def close(self):
        self.closed = True


class WebApiTests(unittest.TestCase):
    _proxy_env_keys = (
        "KDL_DPS_API_URL",
        "KDL_DPS_SECRET_ID",
        "KDL_DPS_SECRET_KEY",
        "KDL_DPS_SECRET_TOKEN",
        "KDL_DPS_SIGNATURE",
        "KDL_DPS_USERNAME",
        "KDL_DPS_PASSWORD",
    )

    def setUp(self):
        self._saved_proxy_env = {
            key: os.environ[key] for key in self._proxy_env_keys if key in os.environ
        }
        for key in self._proxy_env_keys:
            os.environ.pop(key, None)

    def tearDown(self):
        for key in self._proxy_env_keys:
            os.environ.pop(key, None)
        os.environ.update(self._saved_proxy_env)

    def test_cors_preflight_allows_settings_put(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = create_app(
                state_path=root / "state.sqlite3",
                output_dir=root / "output",
            )
            cors = next(
                middleware
                for middleware in app.user_middleware
                if middleware.cls is CORSMiddleware
            )

            self.assertIn("PUT", cors.kwargs["allow_methods"])

    def test_store_uses_shared_connection_and_survives_concurrent_access(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "state.sqlite3")
            store.create_task("t1", ["企业一"])

            errors = []

            def worker(index):
                try:
                    for step in range(30):
                        store.create_task("t%d-%d" % (index, step), ["企业%d" % index])
                        store.list_tasks()
                except Exception as exc:  # pragma: no cover - 失败时统一断言
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual([], errors)
            self.assertEqual(121, len(store.list_tasks()))

            # close() 包装不应真正断开共享连接
            connection = store.connect()
            connection.close()
            self.assertEqual(121, len(store.list_tasks()))
            store.close()

    def test_cors_defaults_to_localhost_and_is_configurable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = create_app(
                state_path=root / "state.sqlite3",
                output_dir=root / "output",
            )
            cors = next(
                middleware
                for middleware in app.user_middleware
                if middleware.cls is CORSMiddleware
            )

            self.assertNotIn("*", cors.kwargs["allow_origins"])
            self.assertIn("http://localhost:3000", cors.kwargs["allow_origins"])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            os.environ["CREDITCHINA_CORS_ORIGINS"] = "https://board.example.com"
            try:
                app = create_app(
                    state_path=root / "state.sqlite3",
                    output_dir=root / "output",
                )
            finally:
                os.environ.pop("CREDITCHINA_CORS_ORIGINS", None)
            cors = next(
                middleware
                for middleware in app.user_middleware
                if middleware.cls is CORSMiddleware
            )

            self.assertEqual(["https://board.example.com"], cors.kwargs["allow_origins"])

    def test_api_token_is_enforced_when_configured(self):
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            os.environ["CREDITCHINA_API_TOKEN"] = "test-token-123"
            try:
                app = create_app(
                    state_path=root / "state.sqlite3",
                    output_dir=root / "output",
                )
            finally:
                os.environ.pop("CREDITCHINA_API_TOKEN", None)
            client = TestClient(app)

            self.assertEqual(200, client.get("/api/v1/health").status_code)
            self.assertEqual(401, client.get("/api/v1/tasks").status_code)
            self.assertEqual(
                200,
                client.get("/api/v1/tasks", headers={"X-API-Token": "test-token-123"}).status_code,
            )
            self.assertEqual(
                200,
                client.get("/api/v1/tasks?token=test-token-123").status_code,
            )
            self.assertEqual(
                401,
                client.get("/api/v1/tasks", headers={"X-API-Token": "wrong"}).status_code,
            )

    def test_api_token_is_optional_when_not_configured(self):
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            os.environ.pop("CREDITCHINA_API_TOKEN", None)
            app = create_app(
                state_path=root / "state.sqlite3",
                output_dir=root / "output",
            )
            client = TestClient(app)

            self.assertEqual(200, client.get("/api/v1/tasks").status_code)

    def test_credit_score_is_stored_separately_from_existing_company_data(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "state.sqlite3")
            original = FakeCrawler().crawl_company("企业一")
            store.save_record(original)

            store.save_credit_score(
                "企业一",
                {
                    "scoreTotal": 98.85,
                    "scoreBasic": 65,
                    "creditreportEnddate": "2026年07月17日",
                },
            )

            persisted = store.latest_record("企业一")
            score = store.credit_score("企业一")
            self.assertIsNotNone(persisted)
            self.assertEqual([{"决定书文号": "处罚-1"}], persisted.penalties)
            self.assertEqual(98.85, score["scoreTotal"])
            self.assertEqual("2026年07月17日", score["reportDate"])

    def test_credit_score_manager_collects_all_companies_and_updates_job(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "state.sqlite3")
            spider = FakeCreditScoreSpider()
            manager = CreditScoreManager(store, spider_factory=lambda: spider)
            manager.start()
            job = store.create_credit_score_job(["企业一", "企业二"])
            manager.enqueue(job["id"])
            deadline = time.time() + 5
            status = ""
            while time.time() < deadline:
                current = store.raw_credit_score_job(job["id"])
                status = str(current["status"]) if current else ""
                if status == "completed":
                    break
                time.sleep(0.05)
            manager.stop()

            self.assertEqual("completed", status)
            self.assertEqual(2, len(spider.calls))
            self.assertEqual(96.5, store.credit_score("企业一")["scoreTotal"])
            self.assertEqual(96.5, store.credit_score("企业二")["scoreTotal"])
            self.assertTrue(spider.closed)

    def test_zero_penalty_company_completes_without_proxy_replacement(self):
        """无处罚企业：整轮采集应一次完成，不触发换 IP/重建会话。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = TaskStore(root / "state.sqlite3")

            class ZeroPenaltyCrawler:
                def crawl_company(self, company_name):
                    return EnterpriseRecord(
                        name=company_name,
                        encry_str="uuid-zero",
                        basic={"统一社会信用代码": "CODE-ZERO"},
                        permissions=[],
                        penalties=[],
                    )

            class ZeroPenaltyClient(FakeClient):
                def __init__(self):
                    super().__init__()
                    self.captured = []

                def capture_penalty_evidence(self, company, code, uuid_value, penalties, output_dir):
                    self.captured.append((company, list(penalties)))
                    return {
                        "overview_path": "",
                        "panel_path": "",
                        "html_path": "",
                        "metadata_path": "",
                        "items": [],
                        "consistency_error": "",
                    }

            factory_calls = []

            def factory():
                factory_calls.append(1)
                return ZeroPenaltyClient(), ZeroPenaltyCrawler()

            manager = CrawlManager(store, root / "output", crawler_factory=factory)
            manager.start()
            task = store.create_task("", ["零处罚企业"])
            manager.enqueue(task["id"])
            deadline = time.time() + 5
            status = ""
            while time.time() < deadline:
                current = store.raw_task(task["id"])
                status = str(current["status"]) if current else ""
                if status in ("completed", "failed", "intercepted"):
                    break
                time.sleep(0.05)
            manager.stop()

            self.assertEqual("completed", status)
            self.assertEqual(1, len(factory_calls), "零处罚企业不应触发会话重建/换 IP")

    def test_zero_penalty_company_evidence_is_registered_with_all_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            evidence_dir = output / "evidence" / "2026-07-17" / "零处罚企业" / "120000"
            evidence_dir.mkdir(parents=True)
            overview = evidence_dir / "行政处罚-整页.png"
            panel = evidence_dir / "行政处罚-全部条目.png"
            html = evidence_dir / "行政处罚-页面源码.html"
            metadata = evidence_dir / "行政处罚-证据清单.json"
            overview.write_bytes(b"overview-png")
            panel.write_bytes(b"panel-png")
            html.write_text("<html>零处罚</html>", encoding="utf-8")
            metadata.write_text("{}", encoding="utf-8")

            store = TaskStore(root / "state.sqlite3")
            store.save_record(
                EnterpriseRecord(name="零处罚企业", encry_str="zero", penalties=[]),
                evidence={
                    "source_url": "https://www.creditchina.gov.cn/example",
                    "penalty_count_page": 0,
                    "overview_path": str(overview),
                    "penalty_panel_path": str(panel),
                    "html_path": str(html),
                    "metadata_path": str(metadata),
                    "items": [],
                },
            )

            captures = store.list_company_evidence("零处罚企业")
            self.assertEqual(1, len(captures))
            capture = captures[0]
            self.assertEqual(0, capture["penaltyCount"])
            self.assertTrue(capture["hasOverview"])
            self.assertTrue(capture["hasPanel"])
            self.assertTrue(capture["hasHtml"])
            self.assertEqual(overview.resolve(), store.evidence_asset_path(capture["id"], "overview"))

    def test_existing_evidence_directory_is_indexed_for_web_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence_dir = root / "output" / "evidence" / "2026-07-17" / "存量企业" / "130000"
            evidence_dir.mkdir(parents=True)
            overview = evidence_dir / "行政处罚-整页.png"
            overview.write_bytes(b"legacy-overview")
            metadata_path = evidence_dir / "行政处罚-证据清单.json"
            metadata_path.write_text(
                json.dumps(
                    {
                        "company_name": "存量企业",
                        "captured_at": "2026-07-17T13:00:00+08:00",
                        "penalty_count_json": 0,
                        "penalty_count_page": 0,
                        "overview_path": str(overview.resolve()),
                        "items": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            store = TaskStore(root / "state.sqlite3")
            self.assertEqual(1, store.index_evidence_directory(root / "output"))
            self.assertEqual(0, store.index_evidence_directory(root / "output"))
            captures = store.list_company_evidence("存量企业")
            self.assertEqual(1, len(captures))
            self.assertTrue(captures[0]["hasOverview"])
            self.assertEqual(0, captures[0]["penaltyCount"])

    def test_intercepted_task_can_resume_from_same_task_breakpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "state.sqlite3")
            task = store.create_task("", ["企业一", "企业二"])
            store.update_task(
                task["id"],
                status="intercepted",
                completed=1,
                progress=50,
                checkpoint_company="企业二",
                checkpoint_payload=json.dumps({"name": "企业二"}),
            )
            resumed = store.set_action(task["id"], "resume")
            raw = store.raw_task(task["id"])
            self.assertEqual("queued", resumed["status"])
            self.assertEqual(1, raw["completed"])
            self.assertEqual("企业二", raw["checkpoint_company"])

    def test_cancel_all_work_stops_administration_and_credit_score_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "state.sqlite3")
            task = store.create_task("", ["企业一"])
            job = store.create_credit_score_job(["企业一"])
            store.update_task(task["id"], status="running", current_company="企业一")
            store.update_credit_score_job(job["id"], status="running", current_company="企业一")

            cancelled = store.cancel_all_work()

            self.assertEqual([task["id"]], cancelled["tasks"])
            self.assertEqual([job["id"]], cancelled["creditScoreJobs"])
            self.assertEqual("cancelled", store.raw_task(task["id"])["status"])
            self.assertEqual("", store.raw_task(task["id"])["current_company"])
            self.assertEqual("cancelled", store.raw_credit_score_job(job["id"])["status"])
            self.assertEqual("", store.raw_credit_score_job(job["id"])["current_company"])

    def test_cancel_interrupts_active_crawler_without_saving_partial_company(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = TaskStore(root / "state.sqlite3")
            started = threading.Event()
            release = threading.Event()
            client = AbortableClient(release)
            crawler = BlockingCrawler(started, release)
            manager = CrawlManager(
                store,
                root / "output",
                crawler_factory=lambda: (client, crawler),
            )
            manager.start()
            task = store.create_task("", ["企业一"])
            manager.enqueue(task["id"])
            self.assertTrue(started.wait(timeout=2))

            cancelled = store.cancel_all_work()
            manager.cancel(cancelled["tasks"])
            deadline = time.time() + 2
            while time.time() < deadline and manager._active_client is not None:
                time.sleep(0.02)
            manager.stop()

            self.assertTrue(client.aborted)
            self.assertEqual("cancelled", store.raw_task(task["id"])["status"])
            self.assertIsNone(store.latest_record("企业一"))

    def test_same_ip_collects_multiple_companies_until_blocked_then_resumes_current_company(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "KDL_DPS_API_URL": "https://dps.kdlapi.com/api/getdps?num=1",
                "KDL_MAX_PROXY_REPLACEMENTS_PER_TASK": "3",
                "KDL_PROXY_REPLACEMENT_COOLDOWN_SECONDS": "0",
            },
            clear=False,
        ):
            root = Path(directory)
            store = TaskStore(root / "state.sqlite3")
            clients = [FakeClient(), FakeClient()]
            crawlers = [BlockedCompanyCrawler("企业三"), CountingCrawler()]
            factory_calls = []

            def factory():
                index = len(factory_calls)
                factory_calls.append(index)
                clients[index].proxy_label = "快代理测试"
                return clients[index], crawlers[index]

            manager = CrawlManager(store, root / "output", crawler_factory=factory)
            manager.start()
            task = store.create_task("", ["企业一", "企业二", "企业三", "企业四"])
            manager.enqueue(task["id"])
            deadline = time.time() + 5
            status = ""
            while time.time() < deadline:
                current = store.raw_task(task["id"])
                status = current["status"] if current else ""
                if status == "completed":
                    break
                time.sleep(0.05)
            manager.stop()

            self.assertEqual("completed", status)
            self.assertEqual(2, len(factory_calls))
            self.assertEqual(["企业一", "企业二", "企业三"], crawlers[0].calls)
            self.assertEqual(["企业三", "企业四"], crawlers[1].calls)
            self.assertTrue(all(client.closed for client in clients))

    def test_restart_resumes_after_completed_company_breakpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = TaskStore(root / "state.sqlite3")
            task = store.create_task("", ["企业一", "企业二", "企业三"])
            store.update_task(task["id"], completed=2, progress=66)
            client = FakeClient()
            crawler = CountingCrawler()
            manager = CrawlManager(
                store,
                root / "output",
                crawler_factory=lambda: (client, crawler),
            )
            manager.start()
            deadline = time.time() + 5
            status = ""
            while time.time() < deadline:
                current = store.raw_task(task["id"])
                status = current["status"] if current else ""
                if status == "completed":
                    break
                time.sleep(0.05)
            manager.stop()

            self.assertEqual("completed", status)
            self.assertEqual(["企业三"], crawler.calls)

    def test_restart_resumes_evidence_phase_without_repeating_company_api(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "KDL_DPS_API_URL": "https://dps.kdlapi.com/api/getdps?num=1",
                "KDL_MAX_PROXY_REPLACEMENTS_PER_TASK": "0",
                "KDL_PROXY_REPLACEMENT_COOLDOWN_SECONDS": "0",
            },
            clear=False,
        ):
            root = Path(directory)
            store = TaskStore(root / "state.sqlite3")
            first_client = EvidenceClient(ProxyUnavailable("证据页代理失效"))
            first_crawler = CountingCrawler()
            first_manager = CrawlManager(
                store,
                root / "output",
                crawler_factory=lambda: (first_client, first_crawler),
            )
            first_manager.start()
            task = store.create_task("", ["企业一"])
            first_manager.enqueue(task["id"])
            deadline = time.time() + 5
            while time.time() < deadline:
                current = store.raw_task(task["id"])
                if current and current["status"] == "failed":
                    break
                time.sleep(0.05)
            first_manager.stop()

            failed = store.raw_task(task["id"])
            self.assertEqual("failed", failed["status"])
            self.assertEqual("企业一", failed["checkpoint_company"])
            self.assertTrue(failed["checkpoint_payload"])

            store.set_action(task["id"], "resume")
            second_client = EvidenceClient()
            second_crawler = CountingCrawler()
            second_manager = CrawlManager(
                store,
                root / "output",
                crawler_factory=lambda: (second_client, second_crawler),
            )
            second_manager.start()
            deadline = time.time() + 5
            status = ""
            while time.time() < deadline:
                current = store.raw_task(task["id"])
                status = current["status"] if current else ""
                if status == "completed":
                    break
                time.sleep(0.05)
            second_manager.stop()

            self.assertEqual("completed", status)
            self.assertEqual(["企业一"], first_crawler.calls)
            self.assertEqual([], second_crawler.calls)
            self.assertEqual(["企业一"], second_client.capture_calls)
            completed = store.raw_task(task["id"])
            self.assertEqual("", completed["checkpoint_company"])
            self.assertEqual("", completed["checkpoint_payload"])

    def test_evidence_failure_replaces_ip_without_repeating_api_collection(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "KDL_DPS_API_URL": "https://dps.kdlapi.com/api/getdps?num=1",
                "KDL_MAX_PROXY_REPLACEMENTS_PER_TASK": "3",
                "KDL_PROXY_REPLACEMENT_COOLDOWN_SECONDS": "0",
            },
            clear=False,
        ):
            root = Path(directory)
            store = TaskStore(root / "state.sqlite3")
            clients = [EvidenceClient(ProxyUnavailable("证据页代理失效")), EvidenceClient()]
            crawlers = [CountingCrawler(), CountingCrawler()]
            factory_calls = []

            def factory():
                index = len(factory_calls)
                factory_calls.append(index)
                clients[index].proxy_label = "快代理测试"
                return clients[index], crawlers[index]

            manager = CrawlManager(store, root / "output", crawler_factory=factory)
            manager.start()
            task = store.create_task("", ["企业一"])
            manager.enqueue(task["id"])
            deadline = time.time() + 5
            status = ""
            while time.time() < deadline:
                current = store.raw_task(task["id"])
                status = current["status"] if current else ""
                if status == "completed":
                    break
                time.sleep(0.05)
            manager.stop()

            self.assertEqual("completed", status)
            self.assertEqual(2, len(factory_calls))
            self.assertEqual(["企业一"], crawlers[0].calls)
            self.assertEqual([], crawlers[1].calls)
            self.assertEqual(["企业一"], clients[0].capture_calls)
            self.assertEqual(["企业一"], clients[1].capture_calls)
            self.assertTrue(all(client.closed for client in clients))

    def test_unavailable_proxy_is_replaced_and_current_company_is_retried(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "KDL_DPS_API_URL": "https://dps.kdlapi.com/api/getdps?num=1",
                "KDL_MAX_PROXY_REPLACEMENTS_PER_TASK": "3",
                "KDL_PROXY_REPLACEMENT_COOLDOWN_SECONDS": "0",
            },
            clear=False,
        ):
            root = Path(directory)
            store = TaskStore(root / "state.sqlite3")
            clients = [FakeClient(), FakeClient()]
            crawlers = [UnavailableProxyCrawler(), FakeCrawler()]
            factory_calls = []

            def factory():
                index = len(factory_calls)
                factory_calls.append(index)
                clients[index].proxy_label = "快代理测试"
                return clients[index], crawlers[index]

            manager = CrawlManager(store, root / "output", crawler_factory=factory)
            manager.start()
            task = store.create_task("", ["企业一"])
            manager.enqueue(task["id"])
            deadline = time.time() + 5
            status = ""
            while time.time() < deadline:
                current = store.raw_task(task["id"])
                status = current["status"] if current else ""
                if status == "completed":
                    break
                time.sleep(0.05)
            manager.stop()

            self.assertEqual("completed", status)
            self.assertEqual(2, len(factory_calls))
            self.assertTrue(all(client.closed for client in clients))

    def test_access_interception_replaces_ip_and_retries_current_company(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "KDL_DPS_API_URL": "https://dps.kdlapi.com/api/getdps?num=1",
                "KDL_MAX_PROXY_REPLACEMENTS_PER_TASK": "3",
                "KDL_PROXY_REPLACEMENT_COOLDOWN_SECONDS": "0",
            },
            clear=False,
        ):
            root = Path(directory)
            store = TaskStore(root / "state.sqlite3")
            clients = [FakeClient(), FakeClient()]
            crawlers = [AlwaysInterceptedCrawler(), FakeCrawler()]
            factory_calls = []

            def factory():
                index = len(factory_calls)
                factory_calls.append(index)
                clients[index].proxy_label = "快代理测试"
                return clients[index], crawlers[index]

            manager = CrawlManager(store, root / "output", crawler_factory=factory)
            manager.start()
            task = store.create_task("", ["企业一"])
            manager.enqueue(task["id"])
            deadline = time.time() + 5
            status = ""
            while time.time() < deadline:
                current = store.raw_task(task["id"])
                status = current["status"] if current else ""
                if status == "completed":
                    break
                time.sleep(0.05)
            manager.stop()

            self.assertEqual("completed", status)
            self.assertEqual(2, len(factory_calls))
            self.assertEqual(["企业一"], crawlers[0].calls)
            self.assertTrue(all(client.closed for client in clients))

    def test_access_interception_stops_only_after_replacement_limit(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "KDL_DPS_API_URL": "https://dps.kdlapi.com/api/getdps?num=1",
                "KDL_MAX_PROXY_REPLACEMENTS_PER_TASK": "1",
                "KDL_PROXY_REPLACEMENT_COOLDOWN_SECONDS": "0",
            },
            clear=False,
        ):
            root = Path(directory)
            store = TaskStore(root / "state.sqlite3")
            clients = [FakeClient(), FakeClient()]
            crawlers = [AlwaysInterceptedCrawler(), AlwaysInterceptedCrawler()]
            factory_calls = []

            def factory():
                index = len(factory_calls)
                factory_calls.append(index)
                return clients[index], crawlers[index]

            manager = CrawlManager(store, root / "output", crawler_factory=factory)
            manager.start()
            task = store.create_task("", ["企业一"])
            manager.enqueue(task["id"])
            deadline = time.time() + 5
            status = ""
            while time.time() < deadline:
                current = store.raw_task(task["id"])
                status = current["status"] if current else ""
                if status == "intercepted":
                    break
                time.sleep(0.05)
            manager.stop()

            current = store.raw_task(task["id"])
            self.assertEqual("intercepted", status)
            self.assertEqual(2, len(factory_calls))
            self.assertIn("已更换 1 个 IP", current["error"])
            self.assertTrue(all(client.closed for client in clients))

    def test_batch_runs_without_interval_and_stops_on_rate_interception(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = TaskStore(root / "state.sqlite3")
            client = FakeClient()
            crawler = InterceptedCrawler()
            manager = CrawlManager(
                store,
                root / "output",
                crawler_factory=lambda: (client, crawler),
            )

            self.assertEqual(0, manager.company_interval)
            manager.start()
            task = store.create_task("", ["企业一", "企业二", "企业三"])
            manager.enqueue(task["id"])
            deadline = time.time() + 5
            status = ""
            while time.time() < deadline:
                current = store.raw_task(task["id"])
                status = current["status"] if current else ""
                if status == "intercepted":
                    break
                time.sleep(0.05)
            manager.stop()

            current = store.raw_task(task["id"])
            self.assertIsNotNone(current)
            self.assertEqual("intercepted", status)
            self.assertEqual(1, current["completed"])
            self.assertIn("保留 1/3 家", current["error"])
            self.assertEqual(["企业一", "企业二"], crawler.calls)
            self.assertTrue(client.closed)

    def test_real_worker_progress_and_tasks_survive_store_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = TaskStore(root / "state.sqlite3")
            task = store.create_task("", ["华为机器有限公司"])
            client = FakeClient()
            manager = CrawlManager(
                store,
                root / "output",
                crawler_factory=lambda: (client, FakeCrawler()),
            )
            manager.start()
            manager.enqueue(task["id"])

            deadline = time.time() + 5
            status = ""
            while time.time() < deadline:
                current = store.raw_task(task["id"])
                status = current["status"] if current else ""
                if status == "completed":
                    break
                time.sleep(0.05)
            manager.stop()

            self.assertEqual(status, "completed")
            self.assertTrue(client.closed)
            restarted = TaskStore(root / "state.sqlite3")
            persisted = restarted.list_tasks()[0]
            self.assertEqual(persisted["name"], "华为机器有限公司")
            self.assertEqual(persisted["progress"], 100)
            self.assertEqual(persisted["status"], "completed")
            self.assertEqual(restarted.list_companies()[0]["code"], "CODE-001")
            self.assertTrue((root / "output" / "华为机器有限公司-本次采集.xlsx").exists())

    def test_history_is_append_only_when_old_penalty_disappears(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "state.sqlite3")
            old = EnterpriseRecord(
                name="示例有限公司",
                encry_str="key",
                penalties=[{"决定书文号": "旧处罚"}],
            )
            new = EnterpriseRecord(
                name="示例有限公司",
                encry_str="key",
                penalties=[{"决定书文号": "新处罚"}],
            )
            store.save_record(old)
            store.save_record(new)

            rows = store.history_rows("行政处罚", "示例有限公司")
            self.assertEqual(len(rows), 2)
            self.assertIn("旧处罚", {row["record_key"] for row in rows})
            self.assertIn("新处罚", {row["record_key"] for row in rows})
            self.assertEqual(
                {"added", "deleted"},
                {item["type"] for item in store.list_announcements()},
            )

    def test_penalty_content_change_and_failed_fetch_are_distinguished(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "state.sqlite3")
            baseline = EnterpriseRecord(
                name="示例有限公司",
                encry_str="key",
                penalties=[{"决定书文号": "处罚-1", "处罚结果": "罚款10万元"}],
            )
            changed = EnterpriseRecord(
                name="示例有限公司",
                encry_str="key",
                penalties=[{"决定书文号": "处罚-1", "处罚结果": "罚款5万元"}],
            )
            failed = EnterpriseRecord(
                name="示例有限公司",
                encry_str="key",
                errors={"行政处罚": "官网暂时不可用"},
            )
            store.save_record(baseline)
            store.save_record(changed)
            store.save_record(failed)

            announcements = store.list_announcements()
            self.assertEqual(1, len(announcements))
            self.assertEqual("modified", announcements[0]["type"])
            self.assertIn("处罚结果", announcements[0]["summary"])
            self.assertEqual("罚款5万元", store.latest_record("示例有限公司").penalties[0]["处罚结果"])

    def test_second_collection_creates_increment_announcement(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "state.sqlite3")
            first = EnterpriseRecord(
                name="示例有限公司",
                encry_str="x",
                basic={"统一社会信用代码": "123"},
                permissions=[{"行政许可决定书文号": "许可-1"}],
            )
            second = EnterpriseRecord(
                name="示例有限公司",
                encry_str="x",
                basic={"统一社会信用代码": "123"},
                permissions=[
                    {"行政许可决定书文号": "许可-1"},
                    {"行政许可决定书文号": "许可-2"},
                ],
            )
            self.assertEqual({}, store.save_record(first))
            changes = store.save_record(second)

            self.assertEqual(1, changes["行政许可"])
            announcements = store.list_announcements()
            self.assertEqual("示例有限公司", announcements[0]["company"])
            self.assertEqual("行政许可", announcements[0]["section"])

    def test_penalty_event_links_before_and_after_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before_image = root / "before.png"
            after_image = root / "after.png"
            before_image.write_bytes(b"before-image")
            after_image.write_bytes(b"after-image")
            store = TaskStore(root / "state.sqlite3")
            baseline = EnterpriseRecord(
                name="示例有限公司",
                encry_str="key",
                penalties=[{"决定书文号": "处罚-1", "处罚结果": "罚款10万元"}],
            )
            changed = EnterpriseRecord(
                name="示例有限公司",
                encry_str="key",
                penalties=[{"决定书文号": "处罚-1", "处罚结果": "罚款5万元"}],
            )
            store.save_record(
                baseline,
                evidence={
                    "overview_path": str(before_image),
                    "items": [{"identity": "处罚-1", "screenshot_path": str(before_image), "sha256": "before"}],
                },
            )
            store.save_record(
                changed,
                evidence={
                    "overview_path": str(after_image),
                    "items": [{"identity": "处罚-1", "screenshot_path": str(after_image), "sha256": "after"}],
                },
            )

            announcement = store.list_announcements()[0]
            self.assertTrue(announcement["hasBeforeEvidence"])
            self.assertTrue(announcement["hasAfterEvidence"])
            self.assertEqual(before_image.resolve(), store.announcement_evidence_path(announcement["id"], "before"))
            self.assertEqual(after_image.resolve(), store.announcement_evidence_path(announcement["id"], "after"))


if __name__ == "__main__":
    unittest.main()
