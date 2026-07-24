"""基于 queue + threading 的并发任务执行器。"""

from __future__ import annotations

import logging
import queue
import random
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

from .crawler import CreditChinaCrawler, EnterpriseRecord, SearchHit
from .storage import FileStorage, MySQLRepository


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class CrawlTask:
    company_name: str
    encry_str: str = ""
    from_database: bool = False
    company_code: str = ""
    entity_type: str = "1"
    uuid: str = ""


@dataclass
class RunSummary:
    succeeded: List[str]
    failed: List[Tuple[str, str]]
    elapsed_seconds: float


class CrawlRunner:
    def __init__(
        self,
        crawler_factory: Callable[[], CreditChinaCrawler],
        file_storage: Optional[FileStorage] = None,
        mysql_repository: Optional[MySQLRepository] = None,
        workers: int = 1,
        delay_min: float = 0.0,
        delay_max: float = 0.0,
    ) -> None:
        if workers < 1:
            raise ValueError("workers 必须至少为 1")
        if delay_min < 0 or delay_max < delay_min:
            raise ValueError("请求间隔范围无效")
        self.crawler_factory = crawler_factory
        self.file_storage = file_storage
        self.mysql_repository = mysql_repository
        self.workers = workers
        self.delay_min = delay_min
        self.delay_max = delay_max
        self._queue: "queue.Queue[Optional[CrawlTask]]" = queue.Queue()
        self._succeeded: List[str] = []
        self._failed: List[Tuple[str, str]] = []
        self._result_lock = threading.Lock()

    def _save(self, record: EnterpriseRecord, task: CrawlTask) -> None:
        if self.file_storage:
            self.file_storage.save(record)
        if self.mysql_repository:
            # 栏目部分失败时保留 crawl_flag=0，下一次数据库任务仍可补采。
            self.mysql_repository.save(
                record,
                mark_task_completed=task.from_database and not record.errors,
            )

    def _worker(self) -> None:
        crawler = self.crawler_factory()
        while True:
            task = self._queue.get()
            try:
                if task is None:
                    return
                LOGGER.info("开始采集：%s", task.company_name)
                if task.encry_str or task.company_code or task.uuid:
                    record = crawler.crawl_hit(
                        SearchHit(
                            name=task.company_name,
                            encry_str=task.encry_str,
                            company_code=task.company_code,
                            entity_type=task.entity_type,
                            uuid=task.uuid,
                        )
                    )
                else:
                    record = crawler.crawl_company(task.company_name)
                self._save(record, task)
                with self._result_lock:
                    self._succeeded.append(task.company_name)
                if record.errors:
                    LOGGER.warning("%s 部分栏目失败：%s", task.company_name, record.errors)
                else:
                    LOGGER.info("采集完成：%s", task.company_name)
            except Exception as exc:
                name = task.company_name if task else "<worker>"
                LOGGER.error("采集失败：%s：%s", name, exc)
                LOGGER.debug("采集失败堆栈：%s", name, exc_info=True)
                with self._result_lock:
                    self._failed.append((name, str(exc)))
            finally:
                self._queue.task_done()
            if self.delay_max:
                time.sleep(random.uniform(self.delay_min, self.delay_max))

    def run(self, tasks: Sequence[CrawlTask]) -> RunSummary:
        started = time.perf_counter()
        seen = set()
        for task in tasks:
            marker = (task.company_name, task.encry_str)
            if marker not in seen:
                seen.add(marker)
                self._queue.put(task)

        threads = []
        for index in range(self.workers):
            thread = threading.Thread(
                target=self._worker,
                name="采集线程%d号" % (index + 1),
                daemon=False,
            )
            thread.start()
            threads.append(thread)

        for _ in threads:
            self._queue.put(None)
        self._queue.join()
        for thread in threads:
            thread.join()

        return RunSummary(
            succeeded=list(self._succeeded),
            failed=list(self._failed),
            elapsed_seconds=time.perf_counter() - started,
        )
