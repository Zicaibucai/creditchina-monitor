"""上海市在沪施工企业信用评价得分采集器。

逻辑移植自同级 ``shanghai_zjw_crawler/sh_zjw_spider.py``：优先使用
统一社会信用代码查询，名称（含全角括号）作为回退，再按 reportId 获取明细。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional

import requests


LOGGER = logging.getLogger(__name__)

SEARCH_URL = (
    "https://ciac.zjw.sh.gov.cn/JGBEnterpriseCreditInterWeb/pc/v1/gz/"
    "creditreportShNew/getPageCreditreportShNewList"
)
DETAIL_URL = (
    "https://ciac.zjw.sh.gov.cn/JGBEnterpriseCreditInterWeb/pc/v1/gz/"
    "enterpriseDetail/getEnterpriseAllDetailList"
)

DEFAULT_HEADERS = {
    "Referer": "https://ciac.zjw.sh.gov.cn/JGBEnterpriseCreditInterWeb/pc/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
}

DEFAULT_COMPANY_CODES = {
    "中国建筑一局(集团)有限公司": "91110000101107173B",
    "中国建筑第二工程局有限公司": "91110000100024296D",
    "中建三局集团有限公司": "91420000757013137P",
    "中国建筑四工程局有限公司": "91440000214401707F",
    "中国建筑第四工程局有限公司": "91440000214401707F",
    "中国建筑第五工程局有限公司": "91430000183764483Y",
    "中国建筑第六工程局有限公司": "911201161030636028",
    "中国建筑第七工程局有限公司": "91410000169954619U",
    "中建科工集团有限公司": "914403006803525199",
    "中建新疆建工(集团)有限公司": "9165000022859700XU",
    "上海建工一建集团有限公司": "913101151324008074",
    "上海建工二建集团有限公司": "913100001337139443",
    "上海建工四建集团有限公司": "91310115132328227T",
    "上海建工五建集团有限公司": "9131011513230855XK",
    "上海建工七建集团有限公司": "91310115133504675F",
}


def normalized_company_name(value: str) -> str:
    return value.strip().replace("（", "(").replace("）", ")")


def default_company_code(company_name: str) -> str:
    normalized = normalized_company_name(company_name)
    return next(
        (
            code
            for name, code in DEFAULT_COMPANY_CODES.items()
            if normalized_company_name(name) == normalized
        ),
        "",
    )


class ShZjwSpider:
    """上海住建施工企业信用分采集器。"""

    def __init__(self, timeout: float = 15.0, proxy: Optional[str] = None) -> None:
        self.session = requests.Session()
        # 避免意外继承 macOS 中失效的 127.0.0.1 系统代理；如需代理，
        # 由 SH_ZJW_PROXY 明确传入并同时用于 HTTP/HTTPS。
        self.session.trust_env = False
        self.session.headers.update(DEFAULT_HEADERS)
        self.timeout = timeout
        self.proxies = {"http": proxy, "https": proxy} if proxy else None

    def close(self) -> None:
        self.session.close()

    def query_company_summary(
        self,
        company_name: str = "",
        company_code: str = "",
    ) -> Optional[Dict[str, Any]]:
        payload = {
            "pageNo": 1,
            "pageSize": 10,
            "enterpriseName": company_name,
            "enterpriseCode": company_code,
        }
        try:
            response = self.session.post(
                SEARCH_URL,
                json=payload,
                proxies=self.proxies,
                timeout=self.timeout,
            )
            if response.status_code != 200:
                LOGGER.error("上海住建查询列表接口返回 HTTP %d", response.status_code)
                return None
            result = response.json()
            if str(result.get("code")) == "200" and result.get("data"):
                items = result["data"].get("list", [])
                if items:
                    return dict(items[0])
            LOGGER.warning("上海住建查询接口响应异常: %s", result.get("message"))
        except Exception as exc:
            LOGGER.error("上海住建查询异常（%s | %s）: %s", company_name, company_code, exc)
        return None

    def query_company_details(self, report_id: str) -> Optional[Dict[str, Any]]:
        try:
            response = self.session.post(
                DETAIL_URL,
                json={"reportId": report_id},
                proxies=self.proxies,
                timeout=self.timeout,
            )
            if response.status_code != 200:
                LOGGER.error("上海住建明细接口返回 HTTP %d", response.status_code)
                return None
            result = response.json()
            if str(result.get("code")) == "200" and result.get("data"):
                return dict(result["data"])
            LOGGER.warning("上海住建明细接口响应异常: %s", result.get("message"))
        except Exception as exc:
            LOGGER.error("上海住建明细请求异常（reportId=%s）: %s", report_id, exc)
        return None

    def crawl(self, company_name: str, company_code: str = "") -> Optional[Dict[str, Any]]:
        summary = self.query_company_summary(company_code=company_code) if company_code else None
        if not summary:
            fullwidth_name = company_name.replace("(", "（").replace(")", "）")
            summary = self.query_company_summary(company_name=fullwidth_name)
            if not summary and fullwidth_name != company_name:
                summary = self.query_company_summary(company_name=company_name)
        if not summary:
            return None

        report_id = str(summary.get("reportId") or "").strip()
        if not report_id:
            return None
        details = self.query_company_details(report_id)
        if not details:
            return {
                "enterpriseName": summary.get("enterpriseName", company_name),
                "enterpriseCode": summary.get("enterpriseCode"),
                "scoreTotal": summary.get("scoreTotal"),
                "creditreportCreatetime": summary.get("creditreportCreatetime"),
                "creditreportEnddate": summary.get("creditreportEnddate"),
                "reportId": report_id,
                "detail_fetched": False,
            }

        raw_detail = details.get("creditreportShNewDetail", {})
        score_detail: Mapping[str, Any] = raw_detail if isinstance(raw_detail, Mapping) else {}
        return {
            "enterpriseName": score_detail.get("enterpriseName", summary.get("enterpriseName")),
            "enterpriseCode": score_detail.get("enterpriseCode", summary.get("enterpriseCode")),
            "enterpriseZzjgdm": score_detail.get("enterpriseZzjgdm"),
            "scoreTotal": score_detail.get("scoreTotal", summary.get("scoreTotal")),
            "scoreBasic": score_detail.get("scoreBasic"),
            "scoreAchievement": score_detail.get("scoreAchievement"),
            "scoreSafetystandards": score_detail.get("scoreSafetystandards"),
            "scoreAward": score_detail.get("scoreAward"),
            "scoreBlxyf": score_detail.get("scoreBlxyf"),
            "scorePunishment": score_detail.get("scorePunishment"),
            "scoreOther": score_detail.get("scoreOther"),
            "creditreportCreatetime": score_detail.get(
                "creditreportCreatetime", summary.get("creditreportCreatetime")
            ),
            "creditreportEnddate": score_detail.get(
                "creditreportEnddate", summary.get("creditreportEnddate")
            ),
            "reportId": report_id,
            "detail_fetched": True,
        }
