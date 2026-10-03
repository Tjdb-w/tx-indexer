"""CLI 端到端测试（含退出码与错误 JSON）。"""

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
     "method": "approve", "amount": "21"},
    {"tx_hash": "h3", "block_number": 2, "timestamp": 30,
     "from_address": "alice", "to_address": "carol",
     "method": "transfer", "amount": "5"},
]


class CliTest(unittest.TestCase):
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
        return code, json.loads(out) if out.strip() else None, err

    def test_query_pagination(self):
        code, page1, err = self._run(
            ["query", self.path, "--page-size", "2"])
        self.assertEqual(code, 0)
        self.assertEqual([t["tx_hash"] for t in page1["transactions"]],
                         ["h1", "h2"])
        self.assertEqual(page1["total"], 3)
        self.assertIsNotNone(page1["next_cursor"])

        code, page2, err = self._run(
            ["query", self.path, "--page-size", "2",
             "--cursor", page1["next_cursor"]])
        self.assertEqual(code, 0)
        self.assertEqual([t["tx_hash"] for t in page2["transactions"]],
                         ["h3"])
        self.assertIsNone(page2["next_cursor"])

    def test_query_default_page_size(self):
        code, page, _ = self._run(["query", self.path])
        self.assertEqual(code, 0)
        self.assertEqual(page["total"], 3)

    def test_stats(self):
        code, stats, _ = self._run(["stats", self.path,
                                    "--method", "transfer"])
        self.assertEqual(code, 0)
        self.assertEqual(stats, {
            "total_count": 2,
            "total_amount": "15",
            "min_amount": "5",
            "max_amount": "10",
            "avg_amount": "7",
        })

    def test_stats_no_match(self):
        code, stats, _ = self._run(["stats", self.path, "--method", "x"])
        self.assertEqual(code, 0)
        self.assertEqual(stats["total_count"], 0)
        self.assertEqual(stats["total_amount"], "0")
        self.assertIsNone(stats["min_amount"])

    def test_method_stats(self):
        code, result, err = self._run(["method-stats", self.path])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        # transfer: 10+5=15/2=7；approve: 21/1=21
        self.assertEqual(result["groups"], [
            {"method": "approve", "total_count": 1,
             "total_amount": "21", "avg_amount": "21"},
            {"method": "transfer", "total_count": 2,
             "total_amount": "15", "avg_amount": "7"},
        ])
        self.assertEqual(result["total_groups"], 2)
        self.assertIsNone(result["next_cursor"])

    def test_method_stats_pagination(self):
        code, page1, _ = self._run(
            ["method-stats", self.path, "--page-size", "1"])
        self.assertEqual(code, 0)
        self.assertEqual([g["method"] for g in page1["groups"]], ["approve"])
        self.assertEqual(page1["total_groups"], 2)
        self.assertIsNotNone(page1["next_cursor"])

        code, page2, _ = self._run(
            ["method-stats", self.path, "--page-size", "1",
             "--cursor", page1["next_cursor"]])
        self.assertEqual(code, 0)
        self.assertEqual([g["method"] for g in page2["groups"]], ["transfer"])
        self.assertEqual(page2["total_groups"], 2)
        self.assertIsNone(page2["next_cursor"])

    def test_method_stats_no_match(self):
        code, result, _ = self._run(
            ["method-stats", self.path, "--method", "x"])
        self.assertEqual(code, 0)
        self.assertEqual(result, {
            "groups": [],
            "total_groups": 0,
            "next_cursor": None,
        })

    def test_method_stats_with_filters(self):
        code, result, _ = self._run([
            "method-stats", self.path,
            "--from-address", "alice",
            "--start-time", "15", "--end-time", "30",
        ])
        self.assertEqual(code, 0)
        # 仅 h3（alice→carol, transfer, 5）落入窗口
        self.assertEqual(result["groups"], [
            {"method": "transfer", "total_count": 1,
             "total_amount": "5", "avg_amount": "5"},
        ])

    def test_method_stats_invalid_page_size(self):
        code, out, err = self._run(
            ["method-stats", self.path, "--page-size", "0"])
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_page_size")
        self.assertIsNone(payload["input_line"])

    def test_method_stats_cross_command_cursor_rejected(self):
        code, query_page, _ = self._run(
            ["query", self.path, "--page-size", "1"])
        self.assertEqual(code, 0)
        code, out, err = self._run(
            ["method-stats", self.path, "--page-size", "1",
             "--cursor", query_page["next_cursor"]])
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_cursor")
        self.assertIsNone(payload["input_line"])

    def test_method_stats_cursor_bound_to_filters(self):
        code, page1, _ = self._run([
            "method-stats", self.path,
            "--address", "alice", "--page-size", "1"])
        self.assertEqual(code, 0)
        self.assertIsNotNone(page1["next_cursor"])

        # 相同筛选可续页：alice 命中 approve(21)/transfer(15) 两组
        code, page2, _ = self._run([
            "method-stats", self.path,
            "--address", "alice", "--page-size", "1",
            "--cursor", page1["next_cursor"]])
        self.assertEqual(code, 0)
        self.assertEqual([g["method"] for g in page2["groups"]], ["transfer"])
        self.assertIsNone(page2["next_cursor"])

        # 改变筛选复用 → invalid_cursor
        code, out, err = self._run([
            "method-stats", self.path,
            "--address", "bob", "--page-size", "1",
            "--cursor", page1["next_cursor"]])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error"], "invalid_cursor")

    def test_invalid_page_size_exit_2(self):
        code, out, err = self._run(
            ["query", self.path, "--page-size", "5000"])
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_page_size")
        self.assertIsNone(payload["input_line"])

    def test_invalid_time_range_exit_2(self):
        code, _, err = self._run(
            ["stats", self.path, "--start-time", "100", "--end-time", "1"])
        self.assertEqual(code, 2)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_time_range")
        self.assertIsNone(payload["input_line"])

    def test_invalid_cursor_exit_2(self):
        code, _, err = self._run(
            ["query", self.path, "--cursor", "garbage!!!"])
        self.assertEqual(code, 2)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_cursor")

    def test_invalid_transaction_has_line_no(self):
        bad = os.path.join(self.tmp.name, "bad.jsonl")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(DATA_LINES[0]) + "\n")
            fh.write("{broken\n")
        code, _, err = self._run(["stats", bad])
        self.assertEqual(code, 2)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_transaction")
        self.assertEqual(payload["input_line"], 2)

    def test_duplicate_transaction(self):
        dup = os.path.join(self.tmp.name, "dup.jsonl")
        with open(dup, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(DATA_LINES[0]) + "\n")
            fh.write(json.dumps(DATA_LINES[0]) + "\n")
        code, _, err = self._run(["query", dup])
        self.assertEqual(code, 2)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "duplicate_transaction")
        self.assertEqual(payload["input_line"], 2)

    def test_filter_by_address_and_time(self):
        code, page, _ = self._run([
            "query", self.path,
            "--address", "alice",
            "--start-time", "15", "--end-time", "30",
        ])
        self.assertEqual(code, 0)
        self.assertEqual([t["tx_hash"] for t in page["transactions"]],
                         ["h2", "h3"])

    def test_query_from_to_address_and_repeated_method(self):
        code, page, _ = self._run([
            "query", self.path,
            "--from-address", "alice", "--from-address", "bob",
            "--to-address", "alice", "--to-address", "carol",
            "--method", "transfer", "--method", "approve",
            "--method", "transfer",
        ])
        self.assertEqual(code, 0)
        self.assertEqual([t["tx_hash"] for t in page["transactions"]],
                         ["h2", "h3"])
        self.assertEqual(page["total"], 2)

    def test_stats_with_set_filters(self):
        code, stats, _ = self._run([
            "stats", self.path,
            "--from-address", "alice",
            "--method", "transfer", "--method", "approve",
        ])
        self.assertEqual(code, 0)
        self.assertEqual(stats, {
            "total_count": 2,
            "total_amount": "15",
            "min_amount": "5",
            "max_amount": "10",
            "avg_amount": "7",
        })

    def test_address_conflict_exit_2_before_file_read(self):
        # 数据文件不存在：若先读文件则不是 invalid_filter
        missing = os.path.join(self.tmp.name, "missing.jsonl")
        for extra in (["--from-address", "bob"], ["--to-address", "bob"]):
            code, out, err = self._run(
                ["query", missing, "--address", "alice"] + extra)
            self.assertEqual(code, 2)
            self.assertIsNone(out)
            payload = json.loads(err)
            self.assertEqual(payload["error"], "invalid_filter")
            self.assertIsNone(payload["input_line"])

    def test_blank_filter_value_exit_2_before_file_read(self):
        missing = os.path.join(self.tmp.name, "missing.jsonl")
        for extra in (["--address", "  "], ["--from-address", ""],
                      ["--to-address", " \t"], ["--method", ""]):
            code, out, err = self._run(["stats", missing] + extra)
            self.assertEqual(code, 2)
            self.assertIsNone(out)
            payload = json.loads(err)
            self.assertEqual(payload["error"], "invalid_filter")
            self.assertIsNone(payload["input_line"])

    def test_cursor_bound_to_new_filters(self):
        code, page1, _ = self._run([
            "query", self.path,
            "--from-address", "alice", "--page-size", "1"])
        self.assertEqual(code, 0)
        self.assertIsNotNone(page1["next_cursor"])

        # 相同筛选续页正常
        code, page2, _ = self._run([
            "query", self.path,
            "--from-address", "alice", "--page-size", "1",
            "--cursor", page1["next_cursor"]])
        self.assertEqual(code, 0)
        self.assertEqual([t["tx_hash"] for t in page2["transactions"]],
                         ["h3"])
        self.assertIsNone(page2["next_cursor"])

        # 改变新增筛选组合复用旧游标 → invalid_cursor
        code, out, err = self._run([
            "query", self.path,
            "--from-address", "bob", "--page-size", "1",
            "--cursor", page1["next_cursor"]])
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_cursor")
        self.assertIsNone(payload["input_line"])


if __name__ == "__main__":
    unittest.main()
