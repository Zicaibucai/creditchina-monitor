import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from creditchina_merged.config import ApiConfig
from creditchina_merged.crawler import CreditChinaCrawler, SearchHit, normalize_company_name
from creditchina_merged.http_client import AccessIntercepted


class FakeClient:
    def __init__(self):
        self.urls = []

    def get_json(self, url):
        self.urls.append(url)
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        if parsed.path.endswith("credit_info_search"):
            return {
                "data": {
                    "results": [
                        {"name": "示例（北京）有限公司", "encryStr": "old\n"},
                        {"name": "示例(北京)有限公司", "encryStr": "latest\n"},
                    ],
                    "total": 2,
                }
            }
        if parsed.path.endswith("credit_info_detail"):
            return {
                "result": {
                    "regno": "REG-1",
                    "legalPerson": "张三",
                    "entstatus": "1",
                    "esdate": "2020-01-02 00:00:00",
                    "enttype": "有限责任公司",
                    "dom": "北京市",
                    "regorg": "登记机关",
                    "creditCode": "CODE-1",
                    "sysUpdateTime": "2026-01-01",
                }
            }
        if parsed.path.endswith("pub_permissions_name"):
            return {
                "result": {
                    "results": [
                        {
                            "xkXdr": "示例(北京)有限公司",
                            "xkFr": "张三",
                            "xkWsh": "许字1号",
                            "xkNr": "准予许可",
                            "xkSplb": "普通",
                            "xkJdrq": "2025-01-01",
                            "xkJzq": "2026-01-01",
                            "xkYxq": "一年",
                            "xkXzjg": "许可机关",
                            "areaCode": "110000",
                            "xkDfbm": "110100",
                            "xkSjc": "2025-01-02 10:00:00",
                        }
                    ]
                }
            }
        if parsed.path.endswith("pub_penalty_name"):
            return {
                "result": {
                    "results": [
                        {
                            "cfWsh": "罚字1号",
                            "cfCfmc": "行政处罚",
                            "cfFr": "张三",
                            "cfCflb1": "警告",
                            "cfJg": "责令整改\n并警告",
                            "cfSy": "测试事由",
                            "cfYj": "测试依据",
                            "cfXzjg": "处罚机关",
                            "cfJdrq": "2025-02-01",
                            "cfQx": "",
                            "cfSjc": "2025-02-02",
                        }
                    ]
                }
            }
        if parsed.path.endswith("record_param"):
            credit_type = query["creditType"][0]
            if credit_type == "2":
                return {
                    "result": [
                        {
                            "数据类别": "A级纳税人",
                            "纳税人名称": "示例(北京)有限公司",
                            "数据来源": "税务",
                            "序号": "1",
                            "评价年度": "2025",
                            "最新更新日期": "2026-01-01",
                            "文件名": "名单",
                        }
                    ]
                }
            if credit_type == "4":
                return {
                    "result": [
                        {
                            "数据类别": "经营异常",
                            "企业名称": "示例(北京)有限公司",
                            "数据来源": "市场监管",
                            "注册号": "REG-1",
                            "法定代表人": "张三",
                            "列入经营异常名录原因类型名称": "未年报",
                            "设立日期": "2020-01-02",
                            "列入决定机关名称": "登记机关",
                            "最新更新日期": "2026-01-02",
                        }
                    ]
                }
            return {
                "result": [
                    {
                        "数据类别": "失信被执行人",
                        "失信被执行人名称": "示例(北京)有限公司",
                        "数据来源": "法院",
                        "案号": "(2025)京01号",
                        "企业法人姓名": "张三",
                        "执行法院": "示例法院",
                        "地域名称": "北京",
                        "执行依据文号": "文书1",
                        "作出执行依据单位": "示例法院",
                        "法律生效文书确定的义务": "履行义务\n一项",
                        "被执行人的履行情况": "未履行",
                        "失信被执行人具体情形": "拒不履行",
                        "发布时间": "2025-03-01",
                        "立案时间": "2025-02-01",
                        "已履行部分": "无",
                        "未履行部分": "全部",
                        "最新更新日期": "2026-01-03",
                    }
                ]
            }
        raise AssertionError("unexpected URL: " + url)


class CrawlerTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.crawler = CreditChinaCrawler(
            self.client,
            ApiConfig.legacy(page_size=100, max_pages=2),
        )

    def test_normalize_company_name(self):
        self.assertEqual(normalize_company_name(" 示例（北京） 有限公司 "), "示例(北京)有限公司")

    def test_administration_crawl_does_not_swallow_access_interception(self):
        hit = SearchHit(name="示例(北京)有限公司", encry_str="key")
        with patch.object(self.crawler, "find_exact", return_value=hit), patch.object(
            self.crawler,
            "_detail",
            side_effect=AccessIntercepted("访问频繁"),
        ):
            with self.assertRaisesRegex(AccessIntercepted, "访问频繁"):
                self.crawler.crawl_administration_company(hit.name)

    def test_exact_match_uses_last_record_and_collects_all_sections(self):
        record = self.crawler.crawl_company("示例（北京）有限公司")
        payload = record.to_dict()
        self.assertEqual(record.encry_str, "latest")
        self.assertEqual(payload["企业状态"], "续存")
        self.assertEqual(payload["统一社会信用代码"], "CODE-1")
        self.assertEqual(len(payload["行政许可"]), 1)
        self.assertEqual(len(payload["行政处罚"]), 1)
        self.assertEqual(len(payload["守信红名单"]), 1)
        self.assertEqual(len(payload["重点关注名单"]), 1)
        self.assertEqual(len(payload["黑名单"]), 1)
        self.assertEqual(payload["行政处罚"][0]["处罚结果"], "责令整改 并警告")
        self.assertNotIn("采集错误", payload)


if __name__ == "__main__":
    unittest.main()
