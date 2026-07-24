"""企业搜索、六类信用信息采集与结构化解析。"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import urlencode

from .config import ApiConfig
from .http_client import AccessIntercepted, ProxyUnavailable

LOGGER = logging.getLogger(__name__)


class ApiResponseError(RuntimeError):
    pass


class CaptchaRequired(ApiResponseError):
    pass


def ensure_api_payload(payload: Any) -> None:
    if not isinstance(payload, dict):
        return
    status = payload.get("status")
    code = payload.get("code")
    message = text(payload.get("message") or payload.get("msg"))
    if any(str(item) == "40001" for item in (status, code) if item is not None):
        raise CaptchaRequired(message or "官网要求验证码")
    if "验证码" in message or "刷新后重试" in message:
        raise CaptchaRequired(message)
    if any(str(item) in {"403", "412", "429"} for item in (status, code) if item is not None):
        raise AccessIntercepted(message or "官网触发访问风控")
    if any(
        marker in message.lower()
        for marker in (
            "too many requests",
            "rate limit",
            "访问频繁",
            "请求频繁",
            "操作频繁",
            "访问受限",
            "请稍后再试",
            "请求过多",
        )
    ):
        raise AccessIntercepted(message)
    if code not in (None, 0, "0", 200, "200"):
        raise ApiResponseError("接口错误 %s：%s" % (code, message))


def normalize_company_name(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).replace("（", "(").replace("）", ")")


def text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\r", "").replace("\n", " ").strip()


def value(data: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        if key in data and data[key] is not None:
            candidate = text(data[key])
            if candidate:
                return candidate
    return ""


def nested(data: Any, path: Sequence[str]) -> Any:
    current = data
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    return current


def first_list(data: Any, paths: Sequence[Sequence[str]]) -> List[Dict[str, Any]]:
    for path in paths:
        candidate = nested(data, path)
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def first_dict(data: Any, paths: Sequence[Sequence[str]]) -> Dict[str, Any]:
    for path in paths:
        candidate = nested(data, path)
        if isinstance(candidate, dict):
            return candidate
    return data if isinstance(data, dict) else {}


def total_count(data: Any) -> Optional[int]:
    for path in (
        ("data", "total"),
        ("data", "totalCount"),
        ("result", "total"),
        ("result", "totalCount"),
        ("total",),
    ):
        candidate = nested(data, path)
        try:
            if candidate is not None:
                return int(candidate)
        except (TypeError, ValueError):
            continue
    return None


@dataclass(frozen=True)
class SearchHit:
    name: str
    encry_str: str = ""
    company_code: str = ""
    entity_type: str = "1"
    uuid: str = ""


@dataclass
class EnterpriseRecord:
    name: str
    encry_str: str
    basic: Dict[str, Any] = field(default_factory=dict)
    permissions: List[Dict[str, Any]] = field(default_factory=list)
    penalties: List[Dict[str, Any]] = field(default_factory=list)
    red_list: List[Dict[str, Any]] = field(default_factory=list)
    watch_list: List[Dict[str, Any]] = field(default_factory=list)
    black_list: List[Dict[str, Any]] = field(default_factory=list)
    errors: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "企业名": self.name,
            "encryStr": self.encry_str,
        }
        result.update(self.basic)
        result.update(
            {
                "行政许可": self.permissions,
                "行政处罚": self.penalties,
                "守信红名单": self.red_list,
                "重点关注名单": self.watch_list,
                "黑名单": self.black_list,
            }
        )
        if self.errors:
            result["采集错误"] = self.errors
        return result


class CreditChinaCrawler:
    def __init__(self, client: Any, api: Optional[ApiConfig] = None) -> None:
        self.client = client
        self.api = api or ApiConfig()
        self._current_cache: Dict[tuple, List[Dict[str, Any]]] = {}
        self._current_cache_errors: Dict[tuple, str] = {}

    def _url(self, path: str, params: Mapping[str, Any]) -> str:
        path = path if path.startswith("/") else "/" + path
        return "%s%s?%s" % (self.api.base_url.rstrip("/"), path, urlencode(params))

    def search(self, keyword: str) -> List[SearchHit]:
        hits: List[SearchHit] = []
        seen = set()
        for page in range(1, self.api.max_pages + 1):
            params: Dict[str, Any] = {
                "keyword": keyword,
                "templateId": "",
                "page": page,
                "pageSize": self.api.page_size,
            }
            if self.api.mode == "current":
                params.update(
                    {
                        "scenes": "defaultScenario",
                        "tableName": "credit_xyzx_tyshxydm",
                        "searchState": 2,
                        "entityType": "1,2,4,5,6,7,8",
                    }
                )
            url = self._url(
                self.api.search_path,
                params,
            )
            payload = self.client.get_json(url)
            ensure_api_payload(payload)
            if self.api.mode == "current":
                rows = first_list(payload, (("data", "list"), ("data", "results")))
            else:
                rows = first_list(
                    payload,
                    (("data", "results"), ("result", "results"), ("results",)),
                )
            for row in rows:
                name = value(
                    row,
                    "accurate_entity_name_query",
                    "name",
                    "entName",
                    "企业名称",
                )
                encry_str = value(row, "encryStr", "encry_str").replace("\n", "")
                company_code = value(row, "accurate_entity_code", "creditCode", "tyshxydm")
                entity_type = value(row, "entityType", "entity_type") or "1"
                uuid = value(row, "uuid")
                if self.api.mode == "current" and not encry_str:
                    encry_str = uuid or company_code
                marker = (normalize_company_name(name), encry_str)
                if name and (encry_str or self.api.mode == "current") and marker not in seen:
                    seen.add(marker)
                    hits.append(
                        SearchHit(
                            name=name,
                            encry_str=encry_str,
                            company_code=company_code,
                            entity_type=entity_type,
                            uuid=uuid,
                        )
                    )
            total = total_count(payload)
            if not rows or len(rows) < self.api.page_size:
                break
            if total is not None and page * self.api.page_size >= total:
                break
        return hits

    def find_exact(self, company_name: str) -> SearchHit:
        normalized = normalize_company_name(company_name)
        matches = [hit for hit in self.search(company_name) if normalize_company_name(hit.name) == normalized]
        if not matches:
            raise LookupError("未找到精确匹配企业：%s" % company_name)
        # 沿用项目二“多条同名记录取最后一条”的规则。
        return matches[-1]

    def _paged_rows(
        self,
        path: str,
        base_params: Mapping[str, Any],
        page_key: str,
        result_paths: Sequence[Sequence[str]],
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        seen = set()
        for page in range(1, self.api.max_pages + 1):
            params = dict(base_params)
            params[page_key] = page
            params["pageSize"] = self.api.page_size
            payload = self.client.get_json(self._url(path, params))
            ensure_api_payload(payload)
            page_rows = first_list(payload, result_paths)
            for row in page_rows:
                marker = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
                if marker not in seen:
                    seen.add(marker)
                    rows.append(row)
            total = total_count(payload)
            if not page_rows or len(page_rows) < self.api.page_size:
                break
            if total is not None and page * self.api.page_size >= total:
                break
        return rows

    @staticmethod
    def _current_labeled_record(item: Dict[str, Any]) -> Dict[str, Any]:
        entity = item.get("entity") if isinstance(item.get("entity"), dict) else item
        columns = item.get("columnList")
        if not isinstance(columns, list):
            columns = list(entity.keys())
        labels = item.get("sencesMap") if isinstance(item.get("sencesMap"), dict) else {}
        result: Dict[str, Any] = {}
        for key in columns:
            rendered = text(entity.get(key))
            if rendered:
                result[text(labels.get(key)) or str(key)] = rendered
        table_name = value(item, "table_name", "tableName")
        data_source = value(item, "dataSource", "data_source")
        if table_name:
            result["_表名"] = table_name
        if data_source:
            result["数据来源"] = data_source
        result["_原始字段"] = dict(entity)
        return result

    @staticmethod
    def _current_value(row: Mapping[str, Any], *keys: str) -> str:
        direct = value(row, *keys)
        if direct:
            return direct
        raw = row.get("_原始字段")
        return value(raw, *keys) if isinstance(raw, Mapping) else ""

    def _current_detail(self, hit: SearchHit) -> Dict[str, Any]:
        payload = self.client.get_json(
            self._url(
                self.api.detail_path,
                {
                    "keyword": hit.name,
                    "scenes": "defaultscenario",
                    "entityType": hit.entity_type or "1",
                    "searchState": 1,
                    "uuid": hit.uuid,
                    "tyshxydm": hit.company_code,
                },
            )
        )
        ensure_api_payload(payload)
        data = first_dict(payload, (("data",),))
        head = data.get("headEntity") if isinstance(data.get("headEntity"), dict) else {}
        detail = data.get("data") if isinstance(data.get("data"), dict) else {}
        labeled = self._current_labeled_record(detail) if detail else {}
        raw = labeled.get("_原始字段") if isinstance(labeled.get("_原始字段"), dict) else {}

        def pick(*keys: str) -> str:
            return value(labeled, *keys) or value(raw, *keys) or value(head, *keys)

        basic: Dict[str, Any] = {
            "工商注册号": pick("工商注册号", "注册号", "regno", "reg_no"),
            "法人": pick("法定代表人", "法人", "负责人", "legalPerson", "fddbr", "name"),
            "企业状态": pick("企业状态", "登记状态", "status", "entstatus"),
            "成立日期": pick("成立日期", "注册日期", "esdate", "clrq")[:10],
            "企业类型": pick("企业类型", "主体类型", "enttype", "entity_type"),
            "地址": pick("注册地址", "住所", "地址", "dom"),
            "登记机关": pick("登记机关", "regorg"),
            "统一社会信用代码": pick(
                "统一社会信用代码", "tyshxydm", "creditCode"
            )
            or hit.company_code,
            "系统数据更新时间": pick(
                "数据更新时间", "系统数据更新时间", "sysUpdateTime", "update_time"
            ),
        }
        # 新版接口动态返回字段清单，全部保留，避免只保存旧项目已知字段。
        for key, item in labeled.items():
            if not key.startswith("_") and key not in basic:
                basic[key] = item
        return basic

    def _current_records(self, hit: SearchHit, category: str) -> List[Dict[str, Any]]:
        cache_key = (hit.name, hit.company_code, hit.uuid, category)
        if cache_key in self._current_cache:
            return self._current_cache[cache_key]
        records: List[Dict[str, Any]] = []
        seen = set()
        for page in range(1, self.api.max_pages + 1):
            try:
                payload = self.client.get_json(
                    self._url(
                        self.api.category_path,
                        {
                            "source": "",
                            "type": category,
                            "searchState": 1,
                            "entityType": hit.entity_type or "1",
                            "scenes": "defaultscenario",
                            "keyword": hit.name,
                            "tyshxydm": hit.company_code,
                            "page": page,
                            "pageSize": self.api.page_size,
                            "pubSort": "desc",
                        },
                    )
                )
            except (AccessIntercepted, ProxyUnavailable) as exc:
                if page == 1 or not records:
                    raise
                error = (
                    "%s第 %d 页触发官网风控；已保留前 %d 条，本次分页不完整"
                    % (category, page, len(records))
                )
                self._current_cache_errors[cache_key] = error
                LOGGER.warning("%s %s：%s", hit.name, error, exc)
                break
            ensure_api_payload(payload)
            rows = first_list(payload, (("data", "list"),))
            for item in rows:
                row = self._current_labeled_record(item)
                marker = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
                if marker not in seen:
                    seen.add(marker)
                    records.append(row)
            total = total_count(payload)
            if not rows or len(rows) < self.api.page_size:
                break
            if total is not None and page * self.api.page_size >= total:
                break
        self._current_cache[cache_key] = records
        return records

    def _apply_current_cache_errors(
        self,
        record: EnterpriseRecord,
        hit: SearchHit,
    ) -> None:
        if self.api.mode != "current":
            return
        administrative_key = (
            hit.name,
            hit.company_code,
            hit.uuid,
            "行政管理",
        )
        administrative_error = self._current_cache_errors.get(administrative_key)
        if administrative_error:
            # 行政许可和行政处罚来自同一分页接口。两个栏目都标错，
            # 可防止部分第一页结果覆盖上次完整快照或产生“已删除”公告。
            record.errors.setdefault("行政许可", administrative_error)
            record.errors.setdefault("行政处罚", administrative_error)

    def _detail(self, hit: SearchHit) -> Dict[str, Any]:
        if self.api.mode == "current":
            return self._current_detail(hit)
        payload = self.client.get_json(
            self._url(self.api.detail_path, {"encryStr": hit.encry_str})
        )
        row = first_dict(payload, (("result",), ("data", "result"), ("data",)))
        status_raw = value(row, "entstatus", "entStatus", "status")
        status = {"1": "续存", "2": "吊销", "3": "注销", "4": "迁出"}.get(
            status_raw, status_raw
        )
        return {
            "工商注册号": value(row, "regno", "regNo"),
            "法人": value(row, "legalPerson", "legalRepresentative"),
            "企业状态": status,
            "成立日期": value(row, "esdate", "establishDate")[:10],
            "企业类型": value(row, "enttype", "entType"),
            "地址": value(row, "dom", "address"),
            "登记机关": value(row, "regorg", "regOrg"),
            "统一社会信用代码": value(row, "creditCode", "unifiedSocialCreditCode"),
            "系统数据更新时间": value(row, "sysUpdateTime", "updateTime"),
        }

    def _permissions(self, hit: SearchHit) -> List[Dict[str, Any]]:
        if self.api.mode == "current":
            result = []
            for row in self._current_records(hit, "行政管理"):
                table_name = self._current_value(row, "_表名")
                if "xzxk" not in table_name.lower():
                    continue
                item = dict(row)
                item.update(
                    {
                        "许可主体": self._current_value(row, "许可主体", "行政相对人名称", "xk_xdr")
                        or hit.name,
                        "统一社会信用代码": self._current_value(
                            row, "统一社会信用代码", "行政相对人代码", "xk_xdr_shxym"
                        )
                        or hit.company_code,
                        "行政许可决定书文号": self._current_value(
                            row, "行政许可决定书文号", "许可决定文书号", "xk_wsh"
                        ),
                        "许可项目名称": self._current_value(
                            row, "许可项目名称", "行政许可决定文书名称", "xk_xmmc"
                        ),
                        "审核类型": self._current_value(row, "审核类型", "xk_splb"),
                        "许可法人": self._current_value(row, "法定代表人", "许可法人", "xk_fr"),
                        "内容许可": self._current_value(row, "许可内容", "内容许可", "xk_nr"),
                        "许可有效期": self._current_value(row, "许可有效期", "xk_yxq"),
                        "许可决定日期": self._current_value(row, "许可决定日期", "xk_jdrq"),
                        "许可截止日期": self._current_value(row, "许可截止日期", "xk_jzq"),
                        "省份编码": self._current_value(row, "省份编码", "area_code"),
                        "地方编码": self._current_value(row, "地方编码", "xk_dfbm"),
                        "许可机关": self._current_value(row, "许可机关", "许可决定机关", "xk_xzjg"),
                        "数据更新时间": self._current_value(row, "数据更新时间", "xk_sjc")[:10],
                    }
                )
                result.append(item)
            return result
        rows = self._paged_rows(
            self.api.permission_path,
            {"name": hit.name},
            "page",
            (("result", "results"), ("data", "results"), ("results",)),
        )
        return [
            {
                "许可主体": value(row, "xkXdr") or hit.name,
                "统一社会信用代码": value(row, "xkXdrShxym"),
                "行政许可决定书文号": value(row, "xkWsh"),
                "许可项目名称": value(row, "xkXmmc"),
                "审核类型": value(row, "xkSplb"),
                "许可法人": value(row, "xkFr"),
                "内容许可": value(row, "xkNr", "xkXmmc"),
                "许可有效期": value(row, "xkYxq"),
                "许可决定日期": value(row, "xkJdrq"),
                "许可截止日期": value(row, "xkJzq"),
                "省份编码": value(row, "areaCode"),
                "地方编码": value(row, "xkDfbm"),
                "许可机关": value(row, "xkXzjg"),
                "数据更新时间": value(row, "xkSjc")[:10],
            }
            for row in rows
        ]

    def _penalties(self, hit: SearchHit) -> List[Dict[str, Any]]:
        if self.api.mode == "current":
            result = []
            for row in self._current_records(hit, "行政管理"):
                table_name = self._current_value(row, "_表名")
                if "xzcf" not in table_name.lower():
                    continue
                item = dict(row)
                item.update(
                    {
                        "处罚企业名称": self._current_value(
                            row, "行政相对人名称", "处罚企业名称", "cf_xdr_mc"
                        )
                        or hit.name,
                        "统一社会信用代码": self._current_value(
                            row, "统一社会信用代码", "行政相对人代码", "cf_xdr_shxym"
                        )
                        or hit.company_code,
                        "决定书文号": self._current_value(
                            row, "行政处罚决定书文号", "决定书文号", "cf_wsh"
                        ),
                        "处罚名称": self._current_value(
                            row, "行政处罚决定文书名称", "处罚名称", "cf_cfmc"
                        ),
                        "法人代表": self._current_value(row, "法定代表人", "法人代表", "cf_fr"),
                        "处罚类别": self._current_value(row, "处罚类别", "cf_cflb1"),
                        "处罚结果": self._current_value(row, "处罚内容", "处罚结果", "cf_jg"),
                        "处罚事由": self._current_value(row, "处罚事由", "cf_sy"),
                        "处罚依据": self._current_value(row, "处罚依据", "cf_yj"),
                        "处罚机关": self._current_value(row, "处罚机关", "处罚决定机关", "cf_cfjg"),
                        "处罚决定日期": self._current_value(row, "处罚决定日期", "cf_jdrq"),
                        "处罚期限": self._current_value(row, "处罚期限", "cf_qx"),
                        "数据更新时间": self._current_value(row, "数据更新时间", "cf_sjc"),
                    }
                )
                result.append(item)
            return result
        rows = self._paged_rows(
            self.api.penalty_path,
            {"name": hit.name},
            "page",
            (("result", "results"), ("data", "results"), ("results",)),
        )
        return [
            {
                "处罚企业名称": value(row, "cfXdrMc") or hit.name,
                "统一社会信用代码": value(row, "cfXdrShxym"),
                "决定书文号": value(row, "cfWsh"),
                "处罚名称": value(row, "cfCfmc") or hit.name,
                "法人代表": value(row, "cfFr"),
                "处罚类别": value(row, "cfCflb1"),
                "处罚结果": value(row, "cfJg"),
                "处罚事由": value(row, "cfSy"),
                "处罚依据": value(row, "cfYj"),
                "处罚机关": value(row, "cfXzjg"),
                "处罚决定日期": value(row, "cfJdrq"),
                "处罚期限": value(row, "cfQx"),
                "数据更新时间": value(row, "cfSjc"),
            }
            for row in rows
        ]

    def _records(self, hit: SearchHit, credit_type: int) -> List[Dict[str, Any]]:
        return self._paged_rows(
            self.api.record_path,
            {"encryStr": hit.encry_str, "creditType": credit_type, "dataSource": 0},
            "pageNum",
            (("result",), ("result", "results"), ("data", "results"), ("data",)),
        )

    def _red_list(self, hit: SearchHit) -> List[Dict[str, Any]]:
        if self.api.mode == "current":
            result = []
            for row in self._current_records(hit, "诚实守信"):
                item = dict(row)
                item.setdefault("数据类别", "诚实守信")
                item.setdefault("纳税人名称", hit.name)
                result.append(item)
            return result
        return [
            {
                "数据类别": value(row, "数据类别"),
                "纳税人名称": value(row, "纳税人名称") or hit.name,
                "数据来源": value(row, "数据来源"),
                "序号": value(row, "序号"),
                "评价年度": value(row, "评价年度"),
                "最新更新日期": value(row, "最新更新日期"),
                "文件名": value(row, "文件名"),
            }
            for row in self._records(hit, 2)
        ]

    def _watch_list(self, hit: SearchHit) -> List[Dict[str, Any]]:
        if self.api.mode == "current":
            result = []
            for row in self._current_records(hit, "经营异常"):
                item = dict(row)
                item.setdefault("数据类别", "经营异常")
                item.setdefault("企业名称", hit.name)
                item.setdefault("注册号", hit.company_code)
                result.append(item)
            return result
        return [
            {
                "数据类别": value(row, "数据类别"),
                "企业名称": value(row, "企业名称") or hit.name,
                "数据来源": value(row, "数据来源"),
                "注册号": value(row, "注册号"),
                "法定代表人": value(row, "法定代表人"),
                "列入经营异常名录原因类型名称": value(row, "列入经营异常名录原因类型名称"),
                "设立日期": value(row, "设立日期"),
                "列入决定机关名称": value(row, "列入决定机关名称"),
                "最新更新日期": value(row, "最新更新日期"),
            }
            for row in self._records(hit, 4)
        ]

    def _black_list(self, hit: SearchHit) -> List[Dict[str, Any]]:
        if self.api.mode == "current":
            result = []
            for row in self._current_records(hit, "严重失信主体名单"):
                item = dict(row)
                item.setdefault("数据类别", "严重失信主体名单")
                item.setdefault("失信被执行人名称", hit.name)
                item.setdefault("案号", self._current_value(row, "案号", "case_number"))
                result.append(item)
            return result
        keys = (
            "数据类别",
            "失信被执行人名称",
            "数据来源",
            "案号",
            "企业法人姓名",
            "执行法院",
            "地域名称",
            "执行依据文号",
            "作出执行依据单位",
            "法律生效文书确定的义务",
            "被执行人的履行情况",
            "失信被执行人具体情形",
            "发布时间",
            "立案时间",
            "已履行部分",
            "未履行部分",
            "最新更新日期",
        )
        result = []
        for row in self._records(hit, 8):
            item = {key: value(row, key) for key in keys}
            if not item["失信被执行人名称"]:
                item["失信被执行人名称"] = hit.name
            result.append(item)
        return result

    def crawl_hit(self, hit: SearchHit) -> EnterpriseRecord:
        record = EnterpriseRecord(name=hit.name, encry_str=hit.encry_str)
        operations = (
            ("基本信息", "basic", self._detail),
            ("行政许可", "permissions", self._permissions),
            ("行政处罚", "penalties", self._penalties),
            ("守信红名单", "red_list", self._red_list),
            ("重点关注名单", "watch_list", self._watch_list),
            ("黑名单", "black_list", self._black_list),
        )
        for label, attribute, operation in operations:
            LOGGER.info("%s 正在采集：%s", hit.name, label)
            try:
                value = operation(hit)
                setattr(record, attribute, value)
                if isinstance(value, list):
                    LOGGER.info("%s %s完成：%d 条", hit.name, label, len(value))
                else:
                    LOGGER.info("%s %s完成", hit.name, label)
            except (AccessIntercepted, ProxyUnavailable):
                raise
            except Exception as exc:
                record.errors[label] = str(exc)
                LOGGER.warning("%s %s失败：%s", hit.name, label, exc)
        self._apply_current_cache_errors(record, hit)
        return record

    def crawl_company(self, company_name: str) -> EnterpriseRecord:
        return self.crawl_hit(self.find_exact(company_name))

    def crawl_administration_company(self, company_name: str) -> EnterpriseRecord:
        """仅采集企业识别信息和官网“行政管理”栏目。

        行政许可和行政处罚来自同一次“行政管理”栏目请求，内部缓存会避免
        重复访问；守信、经营异常、重点关注及黑名单均不会请求。
        """

        hit = self.find_exact(company_name)
        record = EnterpriseRecord(name=hit.name, encry_str=hit.encry_str)
        operations = (
            ("基本信息", "basic", self._detail),
            ("行政许可", "permissions", self._permissions),
            ("行政处罚", "penalties", self._penalties),
        )
        for label, attribute, operation in operations:
            LOGGER.info("%s 正在采集：%s", hit.name, label)
            try:
                value = operation(hit)
                setattr(record, attribute, value)
                if isinstance(value, list):
                    LOGGER.info("%s %s完成：%d 条", hit.name, label, len(value))
                else:
                    LOGGER.info("%s %s完成", hit.name, label)
            except (AccessIntercepted, ProxyUnavailable):
                raise
            except Exception as exc:
                record.errors[label] = str(exc)
                LOGGER.warning("%s %s失败：%s", hit.name, label, exc)
        self._apply_current_cache_errors(record, hit)
        return record
