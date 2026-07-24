import unittest
from urllib.parse import parse_qs, urlparse

from creditchina_merged.config import ApiConfig
from creditchina_merged.crawler import CreditChinaCrawler


class CurrentApiClient:
    def __init__(self):
        self.urls = []

    @staticmethod
    def wrapped(table_name, entity, labels, source):
        return {
            "table_name": table_name,
            "entity": entity,
            "columnList": list(entity),
            "sencesMap": labels,
            "dataSource": source,
        }

    def get_json(self, url):
        self.urls.append(url)
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        if parsed.path.endswith("catalogSearchHome"):
            return {
                "status": 200,
                "data": {
                    "total": 1,
                    "totalSize": 1,
                    "page": 1,
                    "list": [
                        {
                            "accurate_entity_name_query": "上海颐景建筑设计有限公司",
                            "accurate_entity_code": "91310000TEST",
                            "entityType": 1,
                            "uuid": "uuid-1",
                        }
                    ],
                },
            }
        if parsed.path.endswith("getTyshxydmDetailsContent"):
            return {
                "status": 200,
                "data": {
                    "headEntity": {
                        "jgmc": "上海颐景建筑设计有限公司",
                        "tyshxydm": "91310000TEST",
                        "status": "存续",
                    },
                    "data": {
                        "entity": {
                            "fddbr": "张三",
                            "clrq": "2020-01-02",
                            "dz": "上海市",
                        },
                        "columnList": ["fddbr", "clrq", "dz"],
                        "sencesMap": {
                            "fddbr": "法定代表人",
                            "clrq": "成立日期",
                            "dz": "地址",
                        },
                    },
                },
            }
        if parsed.path.endswith("typeSourceSearch"):
            category = query["type"][0]
            if category == "行政管理":
                rows = [
                    self.wrapped(
                        "credit_xyzx_fr_xzxk_new",
                        {"xk_wsh": "许可1", "xk_nr": "准予许可", "xk_xzjg": "许可机关"},
                        {"xk_wsh": "许可决定文书号", "xk_nr": "许可内容", "xk_xzjg": "许可机关"},
                        "双公示",
                    ),
                    self.wrapped(
                        "credit_xyzx_fr_xzcf_2026",
                        {"cf_wsh": "处罚1", "cf_jg": "警告", "cf_cfjg": "处罚机关"},
                        {"cf_wsh": "行政处罚决定书文号", "cf_jg": "处罚内容", "cf_cfjg": "处罚机关"},
                        "双公示",
                    ),
                ]
            elif category == "诚实守信":
                rows = [self.wrapped("credit_good", {"honor": "A级"}, {"honor": "荣誉"}, "税务")]
            elif category == "经营异常":
                rows = [self.wrapped("credit_abnormal", {"reason": "未年报"}, {"reason": "列入原因"}, "市场监管")]
            else:
                rows = [self.wrapped("credit_bad", {"case": "CASE-1"}, {"case": "案号"}, "法院")]
            return {"status": 200, "data": {"total": len(rows), "page": 1, "list": rows}}
        raise AssertionError("unexpected URL: " + url)


class CurrentCrawlerTests(unittest.TestCase):
    def test_current_api_contract_and_dynamic_fields(self):
        client = CurrentApiClient()
        crawler = CreditChinaCrawler(client, ApiConfig(page_size=100, max_pages=2))
        record = crawler.crawl_company("上海颐景建筑设计有限公司")
        payload = record.to_dict()

        self.assertEqual(payload["统一社会信用代码"], "91310000TEST")
        self.assertEqual(payload["企业状态"], "存续")
        self.assertEqual(payload["法人"], "张三")
        self.assertEqual(payload["行政许可"][0]["行政许可决定书文号"], "许可1")
        self.assertEqual(payload["行政处罚"][0]["决定书文号"], "处罚1")
        self.assertEqual(payload["守信红名单"][0]["荣誉"], "A级")
        self.assertEqual(payload["重点关注名单"][0]["列入原因"], "未年报")
        self.assertEqual(payload["黑名单"][0]["案号"], "CASE-1")
        self.assertNotIn("采集错误", payload)
        self.assertIn("scenes=defaultScenario", client.urls[0])


if __name__ == "__main__":
    unittest.main()

