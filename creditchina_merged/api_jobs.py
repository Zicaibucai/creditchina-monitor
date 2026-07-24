"""后台采集队列：信用中国采集与上海住建信用分采集。"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from .api_store import TaskStore, _record_from_payload, _record_payload
from .browser_client import BrowserClient
from .config import ApiConfig, HttpConfig, proxies_from_values
from .crawler import CreditChinaCrawler, EnterpriseRecord
from .http_client import AccessIntercepted, ProxyUnavailable, RequestFailed
from .proxy_provider import KuaidailiPrivateProxyProvider, kuaidaili_enabled
from .sh_zjw_spider import ShZjwSpider, default_company_code
from .storage import FileStorage

class CrawlManager:
    """单 Chrome 会话后台队列，进度只由真实采集阶段驱动。"""

    def __init__(
        self,
        store: TaskStore,
        output_dir: Path,
        crawler_factory: Optional[Callable[[], tuple[Any, CreditChinaCrawler]]] = None,
    ) -> None:
        self.store = store
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="API真实采集队列", daemon=True)
        self._crawler_factory = crawler_factory or self._new_crawler
        # 企业之间不另外停顿；同一 IP 内的官网请求由 BrowserClient
        # 按 request_interval 限速。
        self.company_interval = 0.0
        self.request_interval = max(
            0.0,
            float(os.getenv("CREDITCHINA_REQUEST_INTERVAL_SECONDS", "1")),
        )
        self.dynamic_proxy_enabled = kuaidaili_enabled()
        self.max_proxy_replacements = max(
            0,
            int(os.getenv("KDL_MAX_PROXY_REPLACEMENTS_PER_TASK", "20")),
        )
        self.proxy_replacement_cooldown = max(
            0.0,
            float(os.getenv("KDL_PROXY_REPLACEMENT_COOLDOWN_SECONDS", "1")),
        )
        self._started = False
        self._active_lock = threading.Lock()
        self._active_client: Any = None
        self._cancelled_task_ids: set[str] = set()

    @staticmethod
    def _new_crawler() -> tuple[BrowserClient, CreditChinaCrawler]:
        api_config = ApiConfig.from_env(
            page_size=int(os.getenv("CREDITCHINA_PAGE_SIZE", "10")),
            max_pages=int(os.getenv("CREDITCHINA_MAX_PAGES", "20")),
            mode=os.getenv("CREDITCHINA_API_MODE", "current"),
        )
        provider = KuaidailiPrivateProxyProvider.from_env()
        lease = provider.extract_one() if provider is not None else None
        proxies = (lease.spec,) if lease is not None else proxies_from_values(None)
        http_config = HttpConfig(
            transport="requests",
            timeout=float(os.getenv("CREDITCHINA_TIMEOUT", "10")),
            retries=int(os.getenv("CREDITCHINA_RETRIES", "5")),
            backoff=float(os.getenv("CREDITCHINA_BACKOFF", "0.8")),
            proxies=proxies,
            cookie=os.getenv("CREDITCHINA_COOKIE", ""),
            jfbym_token=os.getenv("JFBYM_TOKEN", ""),
            jfbym_type=os.getenv("JFBYM_TYPE", "10103"),
        )
        client = BrowserClient(
            http_config,
            api_config,
            captcha_timeout=float(os.getenv("CREDITCHINA_CAPTCHA_TIMEOUT", "300")),
            request_interval=float(os.getenv("CREDITCHINA_REQUEST_INTERVAL_SECONDS", "1")),
            allow_manual_captcha=False,
            captcha_auto_attempts=int(os.getenv("CREDITCHINA_CAPTCHA_AUTO_ATTEMPTS", "3")),
            captcha_solver_timeout=float(
                os.getenv("CREDITCHINA_CAPTCHA_SOLVER_TIMEOUT", "30")
            ),
        )
        client.proxy_label = "快代理 %s" % lease.masked_label if lease is not None else ("静态代理" if proxies else "直连")
        return client, CreditChinaCrawler(client, api_config)

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread.start()
        for task_id in self.store.recover_tasks():
            self._queue.put(task_id)

    def stop(self) -> None:
        if not self._started:
            return
        self._queue.put(None)
        self._thread.join(timeout=5)

    def enqueue(self, task_id: str) -> None:
        self._queue.put(task_id)

    def cancel(self, task_ids: Sequence[str]) -> None:
        with self._active_lock:
            self._cancelled_task_ids.update(str(task_id) for task_id in task_ids)
            client = self._active_client
        if client is not None:
            if hasattr(client, "abort"):
                client.abort()
            elif hasattr(client, "close"):
                client.close()

    def _is_cancelled(self, task_id: str) -> bool:
        with self._active_lock:
            if task_id in self._cancelled_task_ids:
                return True
        row = self.store.raw_task(task_id)
        return row is None or str(row["status"]) == "cancelled"

    def _wait_if_paused(self, task_id: str) -> bool:
        while True:
            row = self.store.raw_task(task_id)
            if row is None or str(row["status"]) == "cancelled":
                return False
            status = str(row["status"])
            if status == "pause_requested":
                self.store.update_task(task_id, status="paused", speed="--")
            if status not in ("paused", "pause_requested"):
                return True
            time.sleep(0.5)

    def _run(self) -> None:
        while True:
            task_id = self._queue.get()
            try:
                if task_id is None:
                    return
                self._execute(task_id)
            finally:
                self._queue.task_done()

    def _execute(self, task_id: str) -> None:
        row = self.store.raw_task(task_id)
        if row is None or str(row["status"]) == "cancelled":
            return
        companies = json.loads(str(row["company_names"]))
        client: Any = None
        crawler: Any = None
        partial_errors: List[str] = []
        proxy_replacements = 0
        try:
            if self._is_cancelled(task_id):
                return
            self.store.update_task(task_id, status="running", progress=max(1, int(row["progress"])), speed="真实采集中", error="")
            file_storage = FileStorage(self.output_dir, write_json=True, write_text=True, write_xlsx=True)
            total = len(companies)
            # completed 是已完整落盘的公司断点。任务恢复或服务重启时
            # 直接从下一家开始，不重复查询已完成公司。
            start_index = max(0, min(int(row["completed"]), total))
            checkpoint_company = str(row["checkpoint_company"] or "")
            checkpoint_payload = str(row["checkpoint_payload"] or "")
            for index in range(start_index, total):
                company = companies[index]
                # 官网数据和证据截图是两个独立阶段。如果数据已经成功获取，
                # 而进入详情页截图时当前 IP 被风控，保留内存中的 record；
                # 换 IP 后只重试截图，避免重复请求全部接口后又在截图前触发限流。
                record: Optional[EnterpriseRecord] = None
                if checkpoint_company == company and checkpoint_payload:
                    try:
                        record = _record_from_payload(json.loads(checkpoint_payload))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        checkpoint_company = ""
                        checkpoint_payload = ""
                        self.store.update_task(
                            task_id,
                            checkpoint_company="",
                            checkpoint_payload="",
                        )
                evidence: Optional[Dict[str, Any]] = None
                while True:
                    if not self._wait_if_paused(task_id):
                        return
                    try:
                        if client is None or crawler is None:
                            self.store.update_task(
                                task_id,
                                speed=("正在提取 1 个快代理 IP" if self.dynamic_proxy_enabled else "正在建立采集会话"),
                                current_company=company,
                            )
                            client, crawler = self._crawler_factory()
                            with self._active_lock:
                                self._active_client = client
                            if self._is_cancelled(task_id):
                                return
                        phase_start = int((index / total) * 100)
                        proxy_label = str(getattr(client, "proxy_label", "直连"))
                        self.store.update_task(
                            task_id,
                            status="running",
                            progress=max(1, phase_start + 2),
                            completed=index,
                            speed="正在请求官网 · %s" % proxy_label,
                            current_company=company,
                        )
                        if record is None:
                            if hasattr(crawler, "crawl_administration_company"):
                                record = crawler.crawl_administration_company(company)
                            else:
                                record = crawler.crawl_company(company)
                            # 企业接口阶段完成就写入持久断点。即使程序在证据
                            # 截图阶段退出，下次恢复也只会继续截图。
                            checkpoint_company = company
                            checkpoint_payload = json.dumps(
                                _record_payload(record),
                                ensure_ascii=False,
                                default=str,
                            )
                            self.store.update_task(
                                task_id,
                                checkpoint_company=checkpoint_company,
                                checkpoint_payload=checkpoint_payload,
                            )
                        if self._is_cancelled(task_id):
                            return
                        if evidence is None and hasattr(client, "capture_penalty_evidence"):
                            try:
                                self.store.update_task(task_id, speed="正在保存官网处罚截图 · %s" % proxy_label)
                                evidence = client.capture_penalty_evidence(
                                    record.name,
                                    str(record.basic.get("统一社会信用代码", "")),
                                    record.encry_str,
                                    record.penalties,
                                    self.output_dir,
                                )
                                consistency_error = str(evidence.get("consistency_error", "")).strip()
                                if consistency_error:
                                    record.errors["证据一致性"] = consistency_error
                            except (AccessIntercepted, ProxyUnavailable):
                                raise
                            except Exception as exc:
                                record.errors["证据截图"] = str(exc)
                        if record.errors:
                            partial_errors.append(
                                "%s：%s" % (company, "、".join(record.errors.keys()))
                            )
                        if self._is_cancelled(task_id):
                            return
                        self.store.update_task(
                            task_id,
                            progress=min(98, int(((index + 0.85) / total) * 100)),
                            speed="正在写入数据",
                        )
                        file_storage.save(record)
                        self.store.save_record(record, evidence=evidence)
                        completed = index + 1
                        self.store.update_task(
                            task_id,
                            progress=int((completed / total) * 100),
                            completed=completed,
                            speed="真实采集中" if completed < total else "--",
                            checkpoint_company="",
                            checkpoint_payload="",
                        )
                        checkpoint_company = ""
                        checkpoint_payload = ""
                        break
                    except (ProxyUnavailable, AccessIntercepted) as exc:
                        if self._is_cancelled(task_id):
                            return
                        if client is not None and hasattr(client, "close"):
                            client.close()
                        client = None
                        crawler = None
                        with self._active_lock:
                            self._active_client = None
                        if not self.dynamic_proxy_enabled:
                            raise
                        proxy_replacements += 1
                        if proxy_replacements > self.max_proxy_replacements:
                            if isinstance(exc, AccessIntercepted):
                                raise AccessIntercepted(
                                    "官网连续风控，已更换 %d 个 IP 仍未恢复：%s"
                                    % (self.max_proxy_replacements, str(exc))
                                ) from exc
                            raise RequestFailed(
                                "代理连续不可用，已达到本轮最大更换次数 %d：%s"
                                % (self.max_proxy_replacements, str(exc))
                            ) from exc
                        reason = (
                            "官网风控/限流，正在销毁当前会话并更换 IP"
                            if isinstance(exc, AccessIntercepted)
                            else "当前代理不可用，正在申请新 IP"
                        )
                        self.store.update_task(
                            task_id,
                            speed="%s（%d/%d）"
                            % (reason, proxy_replacements, self.max_proxy_replacements),
                            current_company=company,
                        )
                        deadline = time.monotonic() + self.proxy_replacement_cooldown
                        while time.monotonic() < deadline:
                            if not self._wait_if_paused(task_id):
                                return
                            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
            self.store.update_task(
                task_id,
                status="partial" if partial_errors else "completed",
                progress=100,
                completed=total,
                speed="--",
                current_company="",
                error=("部分栏目采集失败：" + "；".join(partial_errors)) if partial_errors else "",
                checkpoint_company="",
                checkpoint_payload="",
            )
        except AccessIntercepted as exc:
            if self._is_cancelled(task_id):
                return
            current = self.store.raw_task(task_id)
            completed = int(current["completed"]) if current is not None else 0
            total = int(current["total"]) if current is not None else len(companies)
            stop_reason = (
                "已用尽本轮换 IP 重试次数，官网风控仍未恢复，本轮停止；"
                if self.dynamic_proxy_enabled
                else "已检测到官网访问风控，本轮停止；"
            )
            self.store.update_task(
                task_id,
                status="intercepted",
                speed="--",
                current_company="",
                error=(
                    stop_reason
                    +
                    "已成功保留 %d/%d 家的采集结果。%s"
                    % (completed, total, str(exc))
                ),
            )
        except Exception as exc:
            if self._is_cancelled(task_id):
                return
            self.store.update_task(
                task_id,
                status="failed",
                speed="--",
                error=str(exc),
            )
        finally:
            if client is not None and hasattr(client, "close"):
                client.close()
            with self._active_lock:
                if self._active_client is client:
                    self._active_client = None


class CreditScoreManager:
    """独立执行上海住建信用分采集，不修改信用中国现有记录。"""

    def __init__(
        self,
        store: TaskStore,
        spider_factory: Optional[Callable[[], ShZjwSpider]] = None,
    ) -> None:
        self.store = store
        self._spider_factory = spider_factory or self._new_spider
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._thread = threading.Thread(
            target=self._run,
            name="上海住建信用分队列",
            daemon=True,
        )
        self._started = False
        self._active_lock = threading.Lock()
        self._active_spider: Any = None
        self._cancelled_job_ids: set[str] = set()

    @staticmethod
    def _new_spider() -> ShZjwSpider:
        return ShZjwSpider(
            timeout=float(os.getenv("SH_ZJW_TIMEOUT", "15")),
            proxy=os.getenv("SH_ZJW_PROXY", "").strip() or None,
        )

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread.start()
        for job_id in self.store.recover_credit_score_jobs():
            self._queue.put(job_id)

    def stop(self) -> None:
        if not self._started:
            return
        self._queue.put(None)
        self._thread.join(timeout=5)

    def enqueue(self, job_id: str) -> None:
        self._queue.put(job_id)

    def cancel(self, job_ids: Sequence[str]) -> None:
        with self._active_lock:
            self._cancelled_job_ids.update(str(job_id) for job_id in job_ids)
            spider = self._active_spider
        if spider is not None and hasattr(spider, "close"):
            spider.close()

    def _is_cancelled(self, job_id: str) -> bool:
        with self._active_lock:
            if job_id in self._cancelled_job_ids:
                return True
        row = self.store.raw_credit_score_job(job_id)
        return row is None or str(row["status"]) == "cancelled"

    def _run(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                if job_id is None:
                    return
                self._execute(job_id)
            finally:
                self._queue.task_done()

    def _execute(self, job_id: str) -> None:
        row = self.store.raw_credit_score_job(job_id)
        if row is None or str(row["status"]) == "cancelled":
            return
        companies = list(json.loads(str(row["company_names"])))
        total = len(companies)
        failures: List[str] = []
        spider = self._spider_factory()
        with self._active_lock:
            self._active_spider = spider
        try:
            if self._is_cancelled(job_id):
                return
            start_index = max(0, min(int(row["completed"]), total))
            self.store.update_credit_score_job(
                job_id,
                status="running",
                progress=int((start_index / total) * 100) if total else 100,
                error="",
            )
            for index in range(start_index, total):
                if self._is_cancelled(job_id):
                    return
                company_name = str(companies[index])
                self.store.update_credit_score_job(
                    job_id,
                    current_company=company_name,
                    progress=int((index / total) * 100) if total else 100,
                )
                existing = self.store.latest_record(company_name)
                company_code = ""
                if existing is not None:
                    company_code = str(
                        existing.basic.get("统一社会信用代码", "")
                    ).strip()
                if not company_code:
                    company_code = default_company_code(company_name)
                try:
                    payload = spider.crawl(company_name, company_code)
                    if self._is_cancelled(job_id):
                        return
                    if payload is None:
                        failures.append("%s：未查到" % company_name)
                    else:
                        payload["input_name"] = company_name
                        payload["input_code"] = company_code
                        self.store.save_credit_score(company_name, payload)
                except Exception as exc:
                    failures.append("%s：%s" % (company_name, str(exc)))
                completed = index + 1
                self.store.update_credit_score_job(
                    job_id,
                    completed=completed,
                    progress=int((completed / total) * 100) if total else 100,
                )
            self.store.update_credit_score_job(
                job_id,
                status="partial" if failures else "completed",
                progress=100,
                completed=total,
                current_company="",
                error="；".join(failures),
            )
        except Exception as exc:
            if self._is_cancelled(job_id):
                return
            self.store.update_credit_score_job(
                job_id,
                status="failed",
                current_company="",
                error=str(exc),
            )
        finally:
            if hasattr(spider, "close"):
                spider.close()
            with self._active_lock:
                if self._active_spider is spider:
                    self._active_spider = None
