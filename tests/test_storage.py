import json
import tempfile
import unittest
from pathlib import Path

from creditchina_merged.crawler import EnterpriseRecord
from creditchina_merged.storage import FileStorage, safe_filename


class StorageTests(unittest.TestCase):
    def test_file_storage_writes_json_and_compatible_text(self):
        record = EnterpriseRecord(
            name="示例/企业",
            encry_str="key",
            basic={"法人": "张三"},
            permissions=[{"行政许可决定书文号": "1"}],
        )
        with tempfile.TemporaryDirectory() as directory:
            storage = FileStorage(Path(directory))
            storage.save(record)
            json_path = Path(directory) / "示例_企业.json"
            text_path = Path(directory) / "示例_企业.txt"
            self.assertTrue(json_path.exists())
            self.assertTrue(text_path.exists())
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["法人"], "张三")
            self.assertIn("行政许可：", text_path.read_text(encoding="utf-8"))

    def test_safe_filename(self):
        self.assertEqual(safe_filename('a/b:c*?"<>|'), "a_b_c______")


if __name__ == "__main__":
    unittest.main()

