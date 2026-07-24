import unittest

from creditchina_merged.config import DatabaseConfig
from creditchina_merged.crawler import EnterpriseRecord
from creditchina_merged.storage import MySQLRepository


class FakeCursor:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql, params=None):
        if params is not None:
            assert sql.count("%s") == len(params), (sql, params)
        self.calls.append(("execute", sql, params))

    def executemany(self, sql, values):
        assert values
        assert sql.count("%s") == len(values[0]), (sql, values[0])
        self.calls.append(("executemany", sql, values))


class FakeConnection:
    def __init__(self):
        self.fake_cursor = FakeCursor()
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self.fake_cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class FakeRepository(MySQLRepository):
    def __init__(self):
        super().__init__(DatabaseConfig())
        self.connection = FakeConnection()

    def connect(self, include_database=True):
        return self.connection


class MySQLStorageTests(unittest.TestCase):
    def test_all_six_sections_use_parameterized_writes_in_one_transaction(self):
        record = EnterpriseRecord(
            name="示例有限公司",
            encry_str="key",
            basic={"法人": "张三", "统一社会信用代码": "CODE"},
            permissions=[{"行政许可决定书文号": "许可1"}],
            penalties=[{"决定书文号": "处罚1"}],
            red_list=[{"序号": "1"}],
            watch_list=[{"注册号": "REG"}],
            black_list=[{"案号": "CASE"}],
        )
        repository = FakeRepository()
        repository.save(record, mark_task_completed=True)

        calls = repository.connection.fake_cursor.calls
        self.assertEqual(sum(call[0] == "execute" for call in calls), 3)
        self.assertEqual(sum(call[0] == "executemany" for call in calls), 6)
        self.assertTrue(any("INSERT INTO crawl_history_runs" in call[1] for call in calls))
        self.assertTrue(any("INSERT INTO company_history" in call[1] for call in calls))
        self.assertFalse(any("DELETE" in call[1].upper() for call in calls))
        self.assertIn("UPDATE company_test", calls[-1][1])
        self.assertTrue(repository.connection.committed)
        self.assertFalse(repository.connection.rolled_back)
        self.assertTrue(repository.connection.closed)


if __name__ == "__main__":
    unittest.main()
