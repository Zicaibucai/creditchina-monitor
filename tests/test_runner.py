import unittest

from creditchina_merged.crawler import EnterpriseRecord
from creditchina_merged.runner import CrawlRunner, CrawlTask


class FakeCrawler:
    def crawl_company(self, name):
        return EnterpriseRecord(name=name, encry_str="key-" + name, basic={"法人": "测试"})


class RecordingStorage:
    def __init__(self):
        self.names = []

    def save(self, record):
        self.names.append(record.name)


class RunnerTests(unittest.TestCase):
    def test_queue_workers_finish_and_deduplicate_tasks(self):
        storage = RecordingStorage()
        runner = CrawlRunner(lambda: FakeCrawler(), file_storage=storage, workers=3)
        summary = runner.run(
            [CrawlTask("企业甲"), CrawlTask("企业乙"), CrawlTask("企业甲")]
        )
        self.assertCountEqual(summary.succeeded, ["企业甲", "企业乙"])
        self.assertEqual(summary.failed, [])
        self.assertCountEqual(storage.names, ["企业甲", "企业乙"])


if __name__ == "__main__":
    unittest.main()

