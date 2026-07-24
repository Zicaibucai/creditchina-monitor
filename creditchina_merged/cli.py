"""命令行入口。"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from .browser_client import BrowserClient
from .config import (
    ApiConfig,
    DatabaseConfig,
    HttpConfig,
    proxies_from_values,
)
from .crawler import CreditChinaCrawler
from .http_client import HttpClient
from .runner import CrawlRunner, CrawlTask
from .storage import FileStorage, MySQLRepository


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="合并项目一、项目二逻辑的信用中国企业信息采集器"
    )
    source = parser.add_argument_group("任务来源")
    source.add_argument("--company", action="append", default=[], help="精确企业名，可重复")
    source.add_argument("--companies-file", type=Path, help="企业名文本文件，每行一个")
    source.add_argument("--keyword", action="append", default=[], help="关键词搜索，采集全部结果")
    source.add_argument("--db-source", action="store_true", help="从 company_test 读取 crawl_flag=0 的企业")
    source.add_argument("--db-limit", type=int, help="数据库任务最大数量")

    request = parser.add_argument_group("请求")
    request.add_argument(
        "--api-mode",
        choices=("current", "legacy"),
        default=os.getenv("CREDITCHINA_API_MODE", "current"),
        help="current 使用官网新版接口；legacy 保留两原项目旧接口",
    )
    request.add_argument(
        "--transport",
        choices=("auto", "browser", "requests", "urllib"),
        default="auto",
        help="auto 在新版接口使用可见 Chrome，在旧版使用 requests",
    )
    request.add_argument("--proxy", action="append", help="静态代理 URL 或 http=...;https=...，可重复")
    request.add_argument("--timeout", type=float, default=10.0)
    request.add_argument("--retries", type=int, default=5)
    request.add_argument("--backoff", type=float, default=0.8)
    request.add_argument("--page-size", type=int, default=10)
    request.add_argument("--max-pages", type=int, default=20)
    request.add_argument(
        "--captcha-timeout",
        type=float,
        default=300.0,
        help="等待人工完成官网验证码的秒数",
    )
    request.add_argument(
        "--jfbym-token",
        default=os.getenv("JFBYM_TOKEN", ""),
        help="打码平台 (jfbym.com) Token，配置后将自动识别验证码",
    )
    request.add_argument(
        "--jfbym-type",
        default=os.getenv("JFBYM_TYPE", "10103"),
        help="打码平台验证码类型ID，默认为 10103",
    )

    execution = parser.add_argument_group("执行与输出")
    execution.add_argument("--workers", type=int, default=1)
    execution.add_argument("--delay-min", type=float, default=0.0)
    execution.add_argument("--delay-max", type=float, default=0.0)
    execution.add_argument("--output", type=Path, default=Path("output"))
    execution.add_argument("--no-json", action="store_true")
    execution.add_argument("--no-text", action="store_true")
    execution.add_argument(
        "--xlsx-current",
        action="store_true",
        help="为本次成功采集的每家企业生成全部信息 XLSX",
    )
    execution.add_argument("--write-db", action="store_true", help="同时写入 MySQL 六张业务表")
    execution.add_argument("--init-db", action="store_true", help="创建数据库和所需表")
    execution.add_argument(
        "--export-history-penalties",
        type=Path,
        metavar="FILE.xlsx",
        help="导出历史搜索（包含本次）的行政处罚",
    )
    execution.add_argument(
        "--export-history-all",
        type=Path,
        metavar="FILE.xlsx",
        help="导出历史搜索（包含本次）的全部信息",
    )
    execution.add_argument(
        "--export-company",
        help="可选：历史导出仅保留该企业全称",
    )
    execution.add_argument("--verbose", action="store_true")
    return parser


def _file_companies(path: Optional[Path]) -> List[str]:
    if not path:
        return []
    result = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        company = line.strip()
        if company and not company.startswith("#"):
            result.append(company)
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(threadName)s] %(levelname)s %(message)s",
    )

    try:
        proxy_specs = proxies_from_values(args.proxy)
        resolved_transport = args.transport
        if resolved_transport == "auto":
            resolved_transport = "browser" if args.api_mode == "current" else "requests"
        http_config = HttpConfig(
            transport="requests" if resolved_transport == "browser" else resolved_transport,
            timeout=args.timeout,
            retries=args.retries,
            backoff=args.backoff,
            proxies=proxy_specs,
            cookie=os.getenv("CREDITCHINA_COOKIE", ""),
            jfbym_token=args.jfbym_token,
            jfbym_type=args.jfbym_type,
        )
        api_config = ApiConfig.from_env(
            page_size=args.page_size,
            max_pages=args.max_pages,
            mode=args.api_mode,
        )
    except (TypeError, ValueError) as exc:
        parser.error(str(exc))

    history_export_requested = bool(
        args.export_history_penalties or args.export_history_all
    )
    should_write_database = args.write_db or args.db_source or history_export_requested
    database_needed = should_write_database or args.init_db
    repository = MySQLRepository(DatabaseConfig.from_env()) if database_needed else None
    schema_path = Path(__file__).resolve().parent / "schema.sql"
    if args.init_db:
        assert repository is not None
        repository.init_schema(schema_path)
        logging.info("数据库结构初始化完成")

    tasks = [CrawlTask(company) for company in args.company]
    tasks.extend(CrawlTask(company) for company in _file_companies(args.companies_file))

    if args.db_source:
        assert repository is not None
        companies = repository.load_pending_companies(args.db_limit)
        tasks.extend(CrawlTask(company, from_database=True) for company in companies)

    if not tasks and not args.keyword and not history_export_requested:
        if args.init_db:
            return 0
        parser.error("请至少提供 --company、--companies-file、--keyword 或 --db-source")

    if not tasks and not args.keyword and history_export_requested:
        assert repository is not None
        if args.export_history_penalties:
            exported = repository.export_history_penalties(
                args.export_history_penalties,
                company_name=args.export_company,
            )
            logging.info("历史行政处罚已导出：%s", exported)
        if args.export_history_all:
            exported = repository.export_history_all(
                args.export_history_all,
                company_name=args.export_company,
            )
            logging.info("历史全部信息已导出：%s", exported)
        return 0

    if (
        args.no_json
        and args.no_text
        and not args.xlsx_current
        and not should_write_database
    ):
        parser.error("TXT、JSON、XLSX、MySQL 均被关闭，没有可用输出")
    file_storage = None
    if not (args.no_json and args.no_text) or args.xlsx_current:
        file_storage = FileStorage(
            args.output,
            write_json=not args.no_json,
            write_text=not args.no_text,
            write_xlsx=args.xlsx_current,
        )

    if resolved_transport == "browser":
        shared_client = BrowserClient(
            http_config,
            api_config,
            captcha_timeout=args.captcha_timeout,
        )
    else:
        shared_client = HttpClient(http_config)

    try:
        def new_crawler() -> CreditChinaCrawler:
            return CreditChinaCrawler(shared_client, api_config)

        if args.keyword:
            search_crawler = new_crawler()
            for keyword in args.keyword:
                hits = search_crawler.search(keyword)
                logging.info("关键词“%s”匹配 %d 条企业记录", keyword, len(hits))
                tasks.extend(
                    CrawlTask(
                        company_name=hit.name,
                        encry_str=hit.encry_str,
                        company_code=hit.company_code,
                        entity_type=hit.entity_type,
                        uuid=hit.uuid,
                    )
                    for hit in hits
                )

        if not tasks:
            logging.warning("搜索没有返回可采集企业")
            return 0

        runner = CrawlRunner(
            crawler_factory=new_crawler,
            file_storage=file_storage,
            mysql_repository=repository if should_write_database else None,
            workers=args.workers,
            delay_min=args.delay_min,
            delay_max=args.delay_max,
        )
        summary = runner.run(tasks)
        logging.info(
            "运行结束：成功 %d，失败 %d，用时 %.2f 秒",
            len(summary.succeeded),
            len(summary.failed),
            summary.elapsed_seconds,
        )
        for company, error in summary.failed:
            logging.error("%s：%s", company, error)
        if args.export_history_penalties:
            assert repository is not None
            exported = repository.export_history_penalties(
                args.export_history_penalties,
                company_name=args.export_company,
            )
            logging.info("历史行政处罚已导出：%s", exported)
        if args.export_history_all:
            assert repository is not None
            exported = repository.export_history_all(
                args.export_history_all,
                company_name=args.export_company,
            )
            logging.info("历史全部信息已导出：%s", exported)
        return 1 if summary.failed else 0
    finally:
        shared_client.close()


if __name__ == "__main__":
    sys.exit(main())
