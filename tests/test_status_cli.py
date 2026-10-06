"""--status 筛选与 status-stats 命令的 CLI 端到端测试。"""

import io
import json
import os
import sys
import tempfile
import unittest

from tx_indexer.cli import main

DATA_LINES = [
    {"tx_hash": "h1", "block_number": 1, "timestamp": 10,
     "from_address": "alice", "to_address": "bob",
     "method": "transfer", "amount": "10"},
    {"tx_hash": "h2", "block_number": 2, "timestamp": 20,
     "from_address": "bob", "to_address": "alice",
     "method": "approve", "amount": "21", "success": True},
    {"tx_hash": "h3", "block_number": 2, "timestamp": 30,
     "from_address": "alice", "to_address": "carol",
     "method": "transfer", "amount": "5", "success": False},
    {"tx_hash": "h4", "block_number": 3, "timestamp": 40,
     "from_address": "bob", "to_address": "carol",
     "method": "transfer", "amount": "7", "success": False},
]


class StatusCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "data.jsonl")
        with open(self.path, "w", encoding="utf-8") as fh:
            for obj in DATA_LINES:
                fh.write(json.dumps(obj) + "\n")
        self._stdout, self._stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()

    def tearDown(self):
        sys.stdout, sys.stderr = self._stdout, self._stderr
        self.tmp.cleanup()

    def _run(self, argv):
        sys.stdout.seek(0)
        sys.stdout.truncate(0)
        sys.stderr.seek(0)
        sys.stderr.truncate(0)
        code = main(argv)
        out = sys.stdout.getvalue()
        err = sys.stderr.getvalue()
        return (
            code,
            json.loads(out) if out.strip() else None,
            json.loads(err.strip().splitlines()[-1]) if err.strip() else None,
        )

    def test_status_stats_without_filter(self):
        code, result, err = self._run(["status-stats", self.path])
        self.assertEqual(code, 0)
        self.assertIsNone(err)
        self.assertEqual(
            result,
            {
                "total_count": 4,
                "success_count": 2,
                "failure_count": 2,
                "success_amount": "31",
                "failure_amount": "12",
            },
        )

    def test_status_stats_success(self):
        code, result, err = self._run(
            ["status-stats", self.path, "--status", "success"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(result["total_count"], 2)
        self.assertEqual(result["success_count"], 2)
        self.assertEqual(result["failure_count"], 0)
        self.assertEqual(result["success_amount"], "31")
        self.assertEqual(result["failure_amount"], "0")

    def test_status_stats_failure(self):
        code, result, err = self._run(
            ["status-stats", self.path, "--status", "failure"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(result["total_count"], 2)
        self.assertEqual(result["success_count"], 0)
        self.assertEqual(result["failure_count"], 2)
        self.assertEqual(result["success_amount"], "0")
        self.assertEqual(result["failure_amount"], "12")

    def test_status_stats_empty_result(self):
        code, result, err = self._run(
            ["status-stats", self.path, "--method", "missing"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(result["total_count"], 0)
        self.assertEqual(result["success_count"], 0)
        self.assertEqual(result["failure_count"], 0)
        self.assertEqual(result["success_amount"], "0")
        self.assertEqual(result["failure_amount"], "0")

    def test_query_with_status(self):
        code, result, err = self._run(
            ["query", self.path, "--status", "failure"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            [t["tx_hash"] for t in result["transactions"]], ["h3", "h4"]
        )
        self.assertEqual(result["total"], 2)

    def test_invalid_status_values(self):
        for value in ("Success", "FAILURE", "ok", "true", "  ", ""):
            code, result, err = self._run(
                ["query", self.path, "--status", value]
            )
            self.assertEqual(code, 2)
            self.assertIsNone(result)
            self.assertEqual(err["error"], "invalid_status_filter")
            self.assertIsNone(err["input_line"])

    def test_invalid_status_before_reading_data(self):
        # 数据文件不存在也应先报状态错误
        code, result, err = self._run(
            ["query",
             os.path.join(self.tmp.name, "missing.jsonl"),
             "--status", "Success"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(err["error"], "invalid_status_filter")

    def test_cursor_bound_to_status(self):
        code, page1, err = self._run(
            ["query", self.path, "--page-size", "1",
             "--status", "success"]
        )
        self.assertEqual(code, 0)
        cursor = page1["next_cursor"]

        # 同条件续翻不跳过、不重复
        code, page2, err = self._run(
            ["query", self.path, "--page-size", "1",
             "--status", "success", "--cursor", cursor]
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            [t["tx_hash"] for t in page2["transactions"]], ["h2"]
        )

        # 去掉 --status 续翻报 invalid_cursor
        code, result, err = self._run(
            ["query", self.path, "--page-size", "1",
             "--cursor", cursor]
        )
        self.assertEqual(code, 2)
        self.assertEqual(err["error"], "invalid_cursor")

        # 改成 failure 续翻报 invalid_cursor
        code, result, err = self._run(
            ["query", self.path, "--page-size", "1",
             "--status", "failure", "--cursor", cursor]
        )
        self.assertEqual(code, 2)
        self.assertEqual(err["error"], "invalid_cursor")


if __name__ == "__main__":
    unittest.main()
