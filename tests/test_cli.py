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
        code, page1, _ = self._run(
            ["method-stats", self.path, "--page-size", "1"])
        self.assertEqual(code, 0)
        self.assertEqual(page1["total_groups"], 2)
        self.assertEqual(page1["groups"], [{
            "method": "approve",
            "total_count": 1,
            "total_amount": "21",
            "avg_amount": "21",
        }])
        self.assertIsNotNone(page1["next_cursor"])

        code, page2, _ = self._run([
            "method-stats", self.path, "--page-size", "1",
            "--cursor", page1["next_cursor"],
        ])
        self.assertEqual(code, 0)
        self.assertEqual(page2["groups"], [{
            "method": "transfer",
            "total_count": 2,
            "total_amount": "15",
            "avg_amount": "7",
        }])
        self.assertEqual(page2["total_groups"], 2)
        self.assertIsNone(page2["next_cursor"])

    def test_method_stats_walk_all_pages(self):
        collected = []
        cursor = None
        while True:
            argv = ["method-stats", self.path, "--page-size", "1"]
            if cursor is not None:
                argv += ["--cursor", cursor]
            code, page, _ = self._run(argv)
            self.assertEqual(code, 0)
            collected.extend(g["method"] for g in page["groups"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(collected, ["approve", "transfer"])

    def test_method_stats_no_match(self):
        code, page, _ = self._run(
            ["method-stats", self.path, "--method", "x"])
        self.assertEqual(code, 0)
        self.assertEqual(page, {
            "groups": [],
            "total_groups": 0,
            "next_cursor": None,
        })

    def test_method_stats_filters(self):
        code, page, _ = self._run([
            "method-stats", self.path,
            "--from-address", "alice",
            "--start-time", "15",
        ])
        self.assertEqual(code, 0)
        self.assertEqual(
            [g["method"] for g in page["groups"]], ["transfer"]
        )
        self.assertEqual(page["groups"][0]["total_count"], 1)
        self.assertEqual(page["groups"][0]["total_amount"], "5")

    def test_method_stats_invalid_page_size_exit_2(self):
        code, out, err = self._run(
            ["method-stats", self.path, "--page-size", "5000"])
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_page_size")
        self.assertIsNone(payload["input_line"])

    def test_method_stats_cross_command_cursor_exit_2(self):
        code, query_page, _ = self._run(
            ["query", self.path, "--page-size", "1"])
        self.assertEqual(code, 0)
        code, out, err = self._run([
            "method-stats", self.path,
            "--cursor", query_page["next_cursor"],
        ])
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_cursor")
        self.assertIsNone(payload["input_line"])

        code, method_page, _ = self._run(
            ["method-stats", self.path, "--page-size", "1"])
        self.assertEqual(code, 0)
        code, out, err = self._run([
            "query", self.path, "--cursor", method_page["next_cursor"],
        ])
        self.assertEqual(code, 2)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_cursor")

    def test_method_stats_cursor_bound_to_filters(self):
        code, page1, _ = self._run([
            "method-stats", self.path,
            "--address", "alice", "--page-size", "1"])
        self.assertEqual(code, 0)
        self.assertIsNotNone(page1["next_cursor"])

        # 改变筛选复用旧游标 → invalid_cursor
        code, out, err = self._run([
            "method-stats", self.path,
            "--address", "bob", "--page-size", "1",
            "--cursor", page1["next_cursor"]])
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_cursor")
        self.assertIsNone(payload["input_line"])

    def test_method_stats_data_error_has_line_no(self):
        bad = os.path.join(self.tmp.name, "bad.jsonl")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(DATA_LINES[0]) + "\n")
            fh.write("{broken\n")
        code, _, err = self._run(["method-stats", bad])
        self.assertEqual(code, 2)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_transaction")
        self.assertEqual(payload["input_line"], 2)

    def test_address_stats(self):
        code, page1, _ = self._run(
            ["address-stats", self.path, "--page-size", "2"])
        self.assertEqual(code, 0)
        # h1 alice→bob 10、h2 bob→alice 21、h3 alice→carol 5
        self.assertEqual(page1["total_groups"], 3)
        self.assertEqual(page1["groups"], [
            {"address": "alice", "send_count": 2, "receive_count": 1,
             "total_count": 3, "total_amount": "36", "avg_amount": "12"},
            {"address": "bob", "send_count": 1, "receive_count": 1,
             "total_count": 2, "total_amount": "31", "avg_amount": "15"},
        ])
        self.assertIsNotNone(page1["next_cursor"])

        code, page2, _ = self._run([
            "address-stats", self.path, "--page-size", "2",
            "--cursor", page1["next_cursor"],
        ])
        self.assertEqual(code, 0)
        self.assertEqual(page2["groups"], [
            {"address": "carol", "send_count": 0, "receive_count": 1,
             "total_count": 1, "total_amount": "5", "avg_amount": "5"},
        ])
        self.assertEqual(page2["total_groups"], 3)
        self.assertIsNone(page2["next_cursor"])

    def test_address_stats_walk_all_pages(self):
        collected = []
        cursor = None
        while True:
            argv = ["address-stats", self.path, "--page-size", "1"]
            if cursor is not None:
                argv += ["--cursor", cursor]
            code, page, _ = self._run(argv)
            self.assertEqual(code, 0)
            collected.extend(g["address"] for g in page["groups"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(collected, ["alice", "bob", "carol"])

    def test_address_stats_self_transfer(self):
        path = os.path.join(self.tmp.name, "self.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "tx_hash": "s1", "block_number": 1, "timestamp": 1,
                "from_address": "eva", "to_address": "eva",
                "method": "m", "amount": "100",
            }) + "\n")
        code, page, _ = self._run(["address-stats", path])
        self.assertEqual(code, 0)
        self.assertEqual(page["total_groups"], 1)
        self.assertEqual(page["groups"], [{
            "address": "eva",
            "send_count": 1,
            "receive_count": 1,
            "total_count": 1,
            "total_amount": "100",
            "avg_amount": "100",
        }])
        self.assertIsNone(page["next_cursor"])

    def test_address_stats_no_match(self):
        code, page, _ = self._run(
            ["address-stats", self.path, "--method", "x"])
        self.assertEqual(code, 0)
        self.assertEqual(page, {
            "groups": [],
            "total_groups": 0,
            "next_cursor": None,
        })

    def test_address_stats_default_page_size_is_100(self):
        code, page, _ = self._run(["address-stats", self.path])
        self.assertEqual(code, 0)
        self.assertEqual(page["total_groups"], 3)
        self.assertIsNone(page["next_cursor"])

    def test_address_stats_filters(self):
        code, page, _ = self._run([
            "address-stats", self.path,
            "--from-address", "alice",
            "--start-time", "15",
        ])
        self.assertEqual(code, 0)
        # 仅 h3 alice→carol 5 命中
        self.assertEqual(
            [g["address"] for g in page["groups"]], ["alice", "carol"]
        )
        self.assertEqual(page["groups"][0]["total_amount"], "5")

    def test_address_stats_invalid_page_size_exit_2(self):
        code, out, err = self._run(
            ["address-stats", self.path, "--page-size", "0"])
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_page_size")
        self.assertIsNone(payload["input_line"])

    def test_address_stats_cross_command_cursor_exit_2(self):
        for source in ("query", "method-stats"):
            code, src_page, _ = self._run(
                [source, self.path, "--page-size", "1"])
            self.assertEqual(code, 0)
            code, out, err = self._run([
                "address-stats", self.path,
                "--cursor", src_page["next_cursor"],
            ])
            self.assertEqual(code, 2)
            self.assertIsNone(out)
            payload = json.loads(err)
            self.assertEqual(payload["error"], "invalid_cursor")
            self.assertIsNone(payload["input_line"])

        # 反向：address-stats 游标用于其他命令同样拒绝
        code, addr_page, _ = self._run(
            ["address-stats", self.path, "--page-size", "1"])
        self.assertEqual(code, 0)
        for target in ("query", "method-stats"):
            code, out, err = self._run([
                target, self.path,
                "--cursor", addr_page["next_cursor"],
            ])
            self.assertEqual(code, 2)
            self.assertEqual(
                json.loads(err)["error"], "invalid_cursor"
            )

    def test_address_stats_cursor_bound_to_filters(self):
        code, page1, _ = self._run([
            "address-stats", self.path,
            "--address", "alice", "--page-size", "1"])
        self.assertEqual(code, 0)
        self.assertIsNotNone(page1["next_cursor"])

        code, out, err = self._run([
            "address-stats", self.path,
            "--address", "bob", "--page-size", "1",
            "--cursor", page1["next_cursor"]])
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_cursor")
        self.assertIsNone(payload["input_line"])

    def test_counterparty_stats(self):
        code, page1, _ = self._run([
            "counterparty-stats", self.path,
            "--address", "alice", "--page-size", "1",
        ])
        self.assertEqual(code, 0)
        # h1 alice→bob 10、h2 bob→alice 21、h3 alice→carol 5
        self.assertEqual(page1["address"], "alice")
        self.assertEqual(page1["total_groups"], 2)
        self.assertEqual(page1["groups"], [
            {"counterparty": "bob", "send_count": 1, "receive_count": 1,
             "total_count": 2, "total_amount": "31", "avg_amount": "15"},
        ])
        self.assertIsNotNone(page1["next_cursor"])

        code, page2, _ = self._run([
            "counterparty-stats", self.path,
            "--address", "alice", "--page-size", "1",
            "--cursor", page1["next_cursor"],
        ])
        self.assertEqual(code, 0)
        self.assertEqual(page2["groups"], [
            {"counterparty": "carol", "send_count": 0, "receive_count": 1,
             "total_count": 1, "total_amount": "5", "avg_amount": "5"},
        ])
        self.assertEqual(page2["total_groups"], 2)
        self.assertIsNone(page2["next_cursor"])

    def test_counterparty_stats_walk_all_pages(self):
        collected = []
        cursor = None
        while True:
            argv = ["counterparty-stats", self.path,
                    "--address", "alice", "--page-size", "1"]
            if cursor is not None:
                argv += ["--cursor", cursor]
            code, page, _ = self._run(argv)
            self.assertEqual(code, 0)
            collected.extend(g["counterparty"] for g in page["groups"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(collected, ["bob", "carol"])

    def test_counterparty_stats_no_match_keeps_address(self):
        code, page, _ = self._run([
            "counterparty-stats", self.path, "--address", "nobody"])
        self.assertEqual(code, 0)
        self.assertEqual(page, {
            "address": "nobody",
            "groups": [],
            "total_groups": 0,
            "next_cursor": None,
        })

    def test_counterparty_stats_missing_address_exit_2_before_read(self):
        # 数据文件不存在：若先读文件则不是 invalid_filter
        missing = os.path.join(self.tmp.name, "missing.jsonl")
        code, out, err = self._run(["counterparty-stats", missing])
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_filter")
        self.assertIsNone(payload["input_line"])

    def test_counterparty_stats_address_conflict_exit_2(self):
        code, out, err = self._run([
            "counterparty-stats", self.path,
            "--address", "alice", "--from-address", "bob"])
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_filter")
        self.assertIsNone(payload["input_line"])

    def test_counterparty_stats_blank_address_exit_2(self):
        code, out, err = self._run([
            "counterparty-stats", self.path, "--address", "  "])
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_filter")
        self.assertIsNone(payload["input_line"])

    def test_counterparty_stats_invalid_page_size_exit_2(self):
        code, out, err = self._run([
            "counterparty-stats", self.path,
            "--address", "alice", "--page-size", "0"])
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_page_size")
        self.assertIsNone(payload["input_line"])

    def test_counterparty_stats_cross_command_cursor_exit_2(self):
        for source in ("query", "method-stats", "address-stats"):
            code, src_page, _ = self._run([source, self.path, "--page-size", "1"])
            self.assertEqual(code, 0)
            code, out, err = self._run([
                "counterparty-stats", self.path,
                "--address", "alice",
                "--cursor", src_page["next_cursor"],
            ])
            self.assertEqual(code, 2)
            self.assertIsNone(out)
            payload = json.loads(err)
            self.assertEqual(payload["error"], "invalid_cursor")
            self.assertIsNone(payload["input_line"])

        # 反向：counterparty-stats 游标用于其他命令同样拒绝
        code, cp_page, _ = self._run([
            "counterparty-stats", self.path,
            "--address", "alice", "--page-size", "1"])
        self.assertEqual(code, 0)
        for target in ("query", "method-stats", "address-stats"):
            code, out, err = self._run([
                target, self.path,
                "--cursor", cp_page["next_cursor"],
            ])
            self.assertEqual(code, 2)
            self.assertEqual(
                json.loads(err)["error"], "invalid_cursor"
            )

    def test_counterparty_stats_cursor_bound_to_address(self):
        code, page1, _ = self._run([
            "counterparty-stats", self.path,
            "--address", "alice", "--page-size", "1"])
        self.assertEqual(code, 0)
        self.assertIsNotNone(page1["next_cursor"])

        # 改变观察地址复用旧游标 → invalid_cursor
        code, out, err = self._run([
            "counterparty-stats", self.path,
            "--address", "bob", "--page-size", "1",
            "--cursor", page1["next_cursor"]])
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_cursor")
        self.assertIsNone(payload["input_line"])

    def test_counterparty_stats_data_error_has_line_no(self):
        bad = os.path.join(self.tmp.name, "bad_cp.jsonl")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(DATA_LINES[0]) + "\n")
            fh.write("{broken\n")
        code, _, err = self._run(
            ["counterparty-stats", bad, "--address", "alice"])
        self.assertEqual(code, 2)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_transaction")
        self.assertEqual(payload["input_line"], 2)

    def test_counterparty_stats_time_range_exit_2(self):
        code, out, err = self._run([
            "counterparty-stats", self.path,
            "--address", "alice",
            "--start-time", "100", "--end-time", "1"])
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_time_range")
        self.assertIsNone(payload["input_line"])

    def test_address_stats_data_error_has_line_no(self):
        bad = os.path.join(self.tmp.name, "bad_addr.jsonl")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(DATA_LINES[0]) + "\n")
            fh.write("{broken\n")
        code, _, err = self._run(["address-stats", bad])
        self.assertEqual(code, 2)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_transaction")
        self.assertEqual(payload["input_line"], 2)

    def test_address_stats_invalid_filter_before_file_read(self):
        missing = os.path.join(self.tmp.name, "missing.jsonl")
        code, out, err = self._run(
            ["address-stats", missing,
             "--address", "a", "--from-address", "b"])
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_filter")
        self.assertIsNone(payload["input_line"])

    def test_address_stats_invalid_time_range_before_file_read(self):
        missing = os.path.join(self.tmp.name, "missing.jsonl")
        code, out, err = self._run(
            ["address-stats", missing,
             "--start-time", "100", "--end-time", "1"])
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_time_range")
        self.assertIsNone(payload["input_line"])

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

    def test_time_stats(self):
        code, result, _ = self._run(
            ["time-stats", self.path, "--bucket-size", "15"])
        self.assertEqual(code, 0)
        self.assertEqual(result["groups"], [
            {"bucket_start": 0, "bucket_end_exclusive": 15,
             "total_count": 1, "total_amount": "10", "avg_amount": "10"},
            {"bucket_start": 15, "bucket_end_exclusive": 30,
             "total_count": 1, "total_amount": "21", "avg_amount": "21"},
            {"bucket_start": 30, "bucket_end_exclusive": 45,
             "total_count": 1, "total_amount": "5", "avg_amount": "5"},
        ])
        self.assertEqual(result["total_groups"], 3)
        self.assertIsNone(result["next_cursor"])

    def test_time_stats_single_bucket_avg_floor(self):
        code, result, _ = self._run(
            ["time-stats", self.path, "--bucket-size", "60"])
        self.assertEqual(code, 0)
        self.assertEqual(result["groups"], [
            {"bucket_start": 0, "bucket_end_exclusive": 60,
             "total_count": 3, "total_amount": "36", "avg_amount": "12"},
        ])
        self.assertEqual(result["total_groups"], 1)

    def test_time_stats_walk_all_pages(self):
        collected = []
        cursor = None
        pages = 0
        while True:
            argv = ["time-stats", self.path, "--bucket-size", "15",
                    "--page-size", "2"]
            if cursor is not None:
                argv += ["--cursor", cursor]
            code, page, _ = self._run(argv)
            self.assertEqual(code, 0)
            pages += 1
            collected.extend(g["bucket_start"] for g in page["groups"])
            self.assertEqual(page["total_groups"], 3)
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(pages, 2)
        self.assertEqual(collected, [0, 15, 30])

    def test_time_stats_no_match(self):
        code, result, _ = self._run([
            "time-stats", self.path, "--bucket-size", "15",
            "--method", "nonexistent"])
        self.assertEqual(code, 0)
        self.assertEqual(
            result, {"groups": [], "total_groups": 0, "next_cursor": None})

    def test_time_stats_missing_bucket_size_exit_2_before_read(self):
        # 数据文件不存在：若先读文件则不是 invalid_bucket_size
        missing = os.path.join(self.tmp.name, "missing.jsonl")
        code, out, err = self._run(["time-stats", missing])
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_bucket_size")
        self.assertIsNone(payload["input_line"])

    def test_time_stats_invalid_bucket_size_exit_2(self):
        for bad in ("abc", "0", "-5", "1.5", ""):
            code, out, err = self._run([
                "time-stats", self.path, "--bucket-size", bad])
            self.assertEqual(code, 2)
            self.assertIsNone(out)
            payload = json.loads(err)
            self.assertEqual(payload["error"], "invalid_bucket_size")
            self.assertIsNone(payload["input_line"])

    def test_time_stats_invalid_page_size_exit_2(self):
        code, out, err = self._run([
            "time-stats", self.path, "--bucket-size", "15",
            "--page-size", "0"])
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_page_size")
        self.assertIsNone(payload["input_line"])

    def test_time_stats_cross_command_cursor_exit_2(self):
        for source in ("query", "method-stats", "address-stats"):
            code, src_page, _ = self._run(
                [source, self.path, "--page-size", "1"])
            self.assertEqual(code, 0)
            self.assertIsNotNone(src_page["next_cursor"])
            code, out, err = self._run([
                "time-stats", self.path, "--bucket-size", "15",
                "--cursor", src_page["next_cursor"]])
            self.assertEqual(code, 2)
            self.assertIsNone(out)
            payload = json.loads(err)
            self.assertEqual(payload["error"], "invalid_cursor")
            self.assertIsNone(payload["input_line"])

    def test_time_stats_cursor_bound_to_bucket_size(self):
        code, page1, _ = self._run([
            "time-stats", self.path, "--bucket-size", "15",
            "--page-size", "1"])
        self.assertEqual(code, 0)
        self.assertIsNotNone(page1["next_cursor"])

        # 相同 bucket_size 续页正常
        code, page2, _ = self._run([
            "time-stats", self.path, "--bucket-size", "15",
            "--page-size", "1", "--cursor", page1["next_cursor"]])
        self.assertEqual(code, 0)
        self.assertEqual(
            [g["bucket_start"] for g in page2["groups"]], [15])

        # 改变 bucket_size 复用旧游标 → invalid_cursor
        code, out, err = self._run([
            "time-stats", self.path, "--bucket-size", "30",
            "--page-size", "1", "--cursor", page1["next_cursor"]])
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_cursor")
        self.assertIsNone(payload["input_line"])

    def test_time_stats_cursor_bound_to_filters(self):
        code, page1, _ = self._run([
            "time-stats", self.path, "--bucket-size", "15",
            "--page-size", "1"])
        self.assertEqual(code, 0)
        code, out, err = self._run([
            "time-stats", self.path, "--bucket-size", "15",
            "--method", "transfer",
            "--cursor", page1["next_cursor"]])
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_cursor")
        self.assertIsNone(payload["input_line"])

    def test_time_stats_cursor_not_bound_to_page_size(self):
        code, page1, _ = self._run([
            "time-stats", self.path, "--bucket-size", "15",
            "--page-size", "1"])
        self.assertEqual(code, 0)
        code, page2, _ = self._run([
            "time-stats", self.path, "--bucket-size", "15",
            "--page-size", "100", "--cursor", page1["next_cursor"]])
        self.assertEqual(code, 0)
        self.assertEqual(
            [g["bucket_start"] for g in page2["groups"]], [15, 30])
        self.assertIsNone(page2["next_cursor"])

    def test_time_stats_data_error_has_line_no(self):
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write("{}\n")
        code, out, err = self._run(
            ["time-stats", self.path, "--bucket-size", "15"])
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_transaction")
        self.assertEqual(payload["input_line"], 4)

    def test_pair_stats(self):
        code, page1, _ = self._run(
            ["pair-stats", self.path, "--page-size", "2"])
        self.assertEqual(code, 0)
        self.assertEqual(page1["groups"], [
            {"from_address": "bob", "to_address": "alice",
             "total_count": 1, "total_amount": "21", "avg_amount": "21"},
            {"from_address": "alice", "to_address": "bob",
             "total_count": 1, "total_amount": "10", "avg_amount": "10"},
        ])
        self.assertEqual(page1["total_groups"], 3)
        self.assertIsNotNone(page1["next_cursor"])

        code, page2, _ = self._run([
            "pair-stats", self.path, "--page-size", "2",
            "--cursor", page1["next_cursor"],
        ])
        self.assertEqual(code, 0)
        self.assertEqual(page2["groups"], [
            {"from_address": "alice", "to_address": "carol",
             "total_count": 1, "total_amount": "5", "avg_amount": "5"},
        ])
        self.assertEqual(page2["total_groups"], 3)
        self.assertIsNone(page2["next_cursor"])

    def test_pair_stats_walk_all_pages(self):
        collected = []
        cursor = None
        while True:
            argv = ["pair-stats", self.path, "--page-size", "1"]
            if cursor is not None:
                argv += ["--cursor", cursor]
            code, page, _ = self._run(argv)
            self.assertEqual(code, 0)
            collected.extend(
                (g["from_address"], g["to_address"]) for g in page["groups"]
            )
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(
            collected,
            [("bob", "alice"), ("alice", "bob"), ("alice", "carol")],
        )

    def test_pair_stats_self_transfer(self):
        path = os.path.join(self.tmp.name, "self_pair.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "tx_hash": "s1", "block_number": 1, "timestamp": 1,
                "from_address": "eva", "to_address": "eva",
                "method": "m", "amount": "100",
            }) + "\n")
            fh.write(json.dumps({
                "tx_hash": "s2", "block_number": 2, "timestamp": 2,
                "from_address": "eva", "to_address": "eva",
                "method": "m", "amount": "11",
            }) + "\n")
        code, page, _ = self._run(["pair-stats", path])
        self.assertEqual(code, 0)
        self.assertEqual(page["total_groups"], 1)
        self.assertEqual(page["groups"], [{
            "from_address": "eva", "to_address": "eva",
            "total_count": 2, "total_amount": "111", "avg_amount": "55",
        }])
        self.assertIsNone(page["next_cursor"])

    def test_pair_stats_no_match(self):
        code, page, _ = self._run(
            ["pair-stats", self.path, "--method", "x"])
        self.assertEqual(code, 0)
        self.assertEqual(page, {
            "groups": [],
            "total_groups": 0,
            "next_cursor": None,
        })

    def test_pair_stats_default_page_size_is_100(self):
        code, page, _ = self._run(["pair-stats", self.path])
        self.assertEqual(code, 0)
        self.assertEqual(page["total_groups"], 3)
        self.assertIsNone(page["next_cursor"])

    def test_pair_stats_filters(self):
        code, page, _ = self._run([
            "pair-stats", self.path,
            "--from-address", "alice",
            "--to-address", "carol",
            "--start-time", "15",
        ])
        self.assertEqual(code, 0)
        # 仅 h3 alice→carol 5 命中
        self.assertEqual(page["groups"], [{
            "from_address": "alice", "to_address": "carol",
            "total_count": 1, "total_amount": "5", "avg_amount": "5",
        }])
        self.assertEqual(page["total_groups"], 1)

    def test_pair_stats_invalid_page_size_exit_2(self):
        for bad in ("0", "1001", "abc"):
            code, out, err = self._run(
                ["pair-stats", self.path, "--page-size", bad])
            self.assertEqual(code, 2)
            self.assertIsNone(out)
            payload = json.loads(err)
            self.assertEqual(payload["error"], "invalid_page_size")
            self.assertIsNone(payload["input_line"])

    def test_pair_stats_cross_command_cursor_exit_2(self):
        for source in ("query", "method-stats", "address-stats",
                       "counterparty-stats", "time-stats"):
            argv = [source, self.path, "--page-size", "1"]
            if source == "counterparty-stats":
                argv += ["--address", "alice"]
            if source == "time-stats":
                argv += ["--bucket-size", "15"]
            code, src_page, _ = self._run(argv)
            self.assertEqual(code, 0)
            code, out, err = self._run([
                "pair-stats", self.path,
                "--cursor", src_page["next_cursor"],
            ])
            self.assertEqual(code, 2)
            self.assertIsNone(out)
            payload = json.loads(err)
            self.assertEqual(payload["error"], "invalid_cursor")
            self.assertIsNone(payload["input_line"])

        # 反向：pair-stats 游标用于其他命令同样拒绝
        code, pair_page, _ = self._run(
            ["pair-stats", self.path, "--page-size", "1"])
        self.assertEqual(code, 0)
        code, out, err = self._run([
            "method-stats", self.path,
            "--cursor", pair_page["next_cursor"],
        ])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error"], "invalid_cursor")

    def test_pair_stats_cursor_bound_to_filters(self):
        code, page1, _ = self._run([
            "pair-stats", self.path,
            "--address", "alice", "--page-size", "1"])
        self.assertEqual(code, 0)
        self.assertIsNotNone(page1["next_cursor"])

        code, out, err = self._run([
            "pair-stats", self.path,
            "--address", "bob", "--page-size", "1",
            "--cursor", page1["next_cursor"]])
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_cursor")
        self.assertIsNone(payload["input_line"])

    def test_pair_stats_cursor_not_bound_to_page_size(self):
        code, page1, _ = self._run(
            ["pair-stats", self.path, "--page-size", "1"])
        self.assertEqual(code, 0)
        self.assertEqual(
            [(g["from_address"], g["to_address"])
             for g in page1["groups"]],
            [("bob", "alice")],
        )
        code, page2, _ = self._run([
            "pair-stats", self.path, "--page-size", "100",
            "--cursor", page1["next_cursor"],
        ])
        self.assertEqual(code, 0)
        self.assertEqual(
            [(g["from_address"], g["to_address"])
             for g in page2["groups"]],
            [("alice", "bob"), ("alice", "carol")],
        )
        self.assertIsNone(page2["next_cursor"])

    def test_pair_stats_data_error_has_line_no(self):
        bad = os.path.join(self.tmp.name, "bad_pair.jsonl")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(DATA_LINES[0]) + "\n")
            fh.write("{broken\n")
        code, _, err = self._run(["pair-stats", bad])
        self.assertEqual(code, 2)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_transaction")
        self.assertEqual(payload["input_line"], 2)

    def test_pair_stats_duplicate_transaction_has_line_no(self):
        dup = os.path.join(self.tmp.name, "dup_pair.jsonl")
        with open(dup, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(DATA_LINES[0]) + "\n")
            fh.write(json.dumps(DATA_LINES[0]) + "\n")
        code, _, err = self._run(["pair-stats", dup])
        self.assertEqual(code, 2)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "duplicate_transaction")
        self.assertEqual(payload["input_line"], 2)

    def test_pair_stats_invalid_time_range_before_file_read(self):
        missing = os.path.join(self.tmp.name, "missing.jsonl")
        code, out, err = self._run(
            ["pair-stats", missing,
             "--start-time", "100", "--end-time", "1"])
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_time_range")
        self.assertIsNone(payload["input_line"], None)

    def test_address_time_stats(self):
        code, page1, _ = self._run([
            "address-time-stats", self.path,
            "--bucket-size", "60", "--page-size", "2"])
        self.assertEqual(code, 0)
        # bucket 0：alice 总 36(count3)、bob 总 31(count2)、carol 5
        self.assertEqual(page1["total_groups"], 3)
        self.assertEqual(page1["groups"], [
            {"address": "alice", "bucket_start": 0,
             "bucket_end_exclusive": 60,
             "send_count": 2, "receive_count": 1, "total_count": 3,
             "total_amount": "36", "avg_amount": "12"},
            {"address": "bob", "bucket_start": 0,
             "bucket_end_exclusive": 60,
             "send_count": 1, "receive_count": 1, "total_count": 2,
             "total_amount": "31", "avg_amount": "15"},
        ])
        self.assertIsNotNone(page1["next_cursor"])

        code, page2, _ = self._run([
            "address-time-stats", self.path,
            "--bucket-size", "60", "--page-size", "2",
            "--cursor", page1["next_cursor"],
        ])
        self.assertEqual(code, 0)
        self.assertEqual(page2["groups"], [
            {"address": "carol", "bucket_start": 0,
             "bucket_end_exclusive": 60,
             "send_count": 0, "receive_count": 1, "total_count": 1,
             "total_amount": "5", "avg_amount": "5"},
        ])
        self.assertEqual(page2["total_groups"], 3)
        self.assertIsNone(page2["next_cursor"])

    def test_address_time_stats_buckets_split(self):
        code, result, _ = self._run([
            "address-time-stats", self.path, "--bucket-size", "15"])
        self.assertEqual(code, 0)
        self.assertEqual(result["groups"], [
            {"address": "alice", "bucket_start": 0,
             "bucket_end_exclusive": 15,
             "send_count": 1, "receive_count": 0, "total_count": 1,
             "total_amount": "10", "avg_amount": "10"},
            {"address": "bob", "bucket_start": 0,
             "bucket_end_exclusive": 15,
             "send_count": 0, "receive_count": 1, "total_count": 1,
             "total_amount": "10", "avg_amount": "10"},
            {"address": "bob", "bucket_start": 15,
             "bucket_end_exclusive": 30,
             "send_count": 1, "receive_count": 0, "total_count": 1,
             "total_amount": "21", "avg_amount": "21"},
            {"address": "alice", "bucket_start": 15,
             "bucket_end_exclusive": 30,
             "send_count": 0, "receive_count": 1, "total_count": 1,
             "total_amount": "21", "avg_amount": "21"},
            {"address": "alice", "bucket_start": 30,
             "bucket_end_exclusive": 45,
             "send_count": 1, "receive_count": 0, "total_count": 1,
             "total_amount": "5", "avg_amount": "5"},
            {"address": "carol", "bucket_start": 30,
             "bucket_end_exclusive": 45,
             "send_count": 0, "receive_count": 1, "total_count": 1,
             "total_amount": "5", "avg_amount": "5"},
        ])
        self.assertEqual(result["total_groups"], 6)
        self.assertIsNone(result["next_cursor"])

    def test_address_time_stats_self_transfer(self):
        path = os.path.join(self.tmp.name, "self_ats.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "tx_hash": "s1", "block_number": 1, "timestamp": 1,
                "from_address": "eva", "to_address": "eva",
                "method": "m", "amount": "100",
            }) + "\n")
        code, page, _ = self._run(
            ["address-time-stats", path, "--bucket-size", "60"])
        self.assertEqual(code, 0)
        self.assertEqual(page["total_groups"], 1)
        self.assertEqual(page["groups"], [{
            "address": "eva", "bucket_start": 0,
            "bucket_end_exclusive": 60,
            "send_count": 1, "receive_count": 1, "total_count": 1,
            "total_amount": "100", "avg_amount": "100",
        }])
        self.assertIsNone(page["next_cursor"])

    def test_address_time_stats_walk_all_pages(self):
        collected = []
        cursor = None
        while True:
            argv = ["address-time-stats", self.path,
                    "--bucket-size", "15", "--page-size", "2"]
            if cursor is not None:
                argv += ["--cursor", cursor]
            code, page, _ = self._run(argv)
            self.assertEqual(code, 0)
            collected.extend(
                (g["bucket_start"], g["address"]) for g in page["groups"]
            )
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(collected, [
            (0, "alice"), (0, "bob"),
            (15, "bob"), (15, "alice"),
            (30, "alice"), (30, "carol"),
        ])

    def test_address_time_stats_no_match(self):
        code, page, _ = self._run([
            "address-time-stats", self.path, "--bucket-size", "60",
            "--method", "x"])
        self.assertEqual(code, 0)
        self.assertEqual(page, {
            "groups": [], "total_groups": 0, "next_cursor": None,
        })

    def test_address_time_stats_missing_bucket_size_exit_2_before_read(self):
        # 数据文件不存在：若先读文件则不是 invalid_bucket_size
        missing = os.path.join(self.tmp.name, "missing.jsonl")
        code, out, err = self._run(["address-time-stats", missing])
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_bucket_size")
        self.assertIsNone(payload["input_line"])

    def test_address_time_stats_invalid_bucket_size_exit_2(self):
        for bad in ("abc", "0", "-5", "1.5", ""):
            code, out, err = self._run([
                "address-time-stats", self.path, "--bucket-size", bad])
            self.assertEqual(code, 2)
            self.assertIsNone(out)
            payload = json.loads(err)
            self.assertEqual(payload["error"], "invalid_bucket_size")
            self.assertIsNone(payload["input_line"])

    def test_address_time_stats_invalid_page_size_exit_2(self):
        code, out, err = self._run([
            "address-time-stats", self.path, "--bucket-size", "60",
            "--page-size", "0"])
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_page_size")
        self.assertIsNone(payload["input_line"])

    def test_address_time_stats_invalid_filter_before_file_read(self):
        missing = os.path.join(self.tmp.name, "missing.jsonl")
        code, out, err = self._run([
            "address-time-stats", missing, "--bucket-size", "60",
            "--address", "a", "--from-address", "b"])
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_filter")
        self.assertIsNone(payload["input_line"])

    def test_address_time_stats_invalid_time_range_before_file_read(self):
        missing = os.path.join(self.tmp.name, "missing.jsonl")
        code, out, err = self._run([
            "address-time-stats", missing, "--bucket-size", "60",
            "--start-time", "100", "--end-time", "1"])
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_time_range")
        self.assertIsNone(payload["input_line"])

    def test_address_time_stats_cross_command_cursor_exit_2(self):
        for source in ("query", "method-stats", "address-stats",
                       "counterparty-stats", "time-stats", "pair-stats"):
            argv = [source, self.path, "--page-size", "1"]
            if source == "counterparty-stats":
                argv += ["--address", "alice"]
            if source == "time-stats":
                argv += ["--bucket-size", "15"]
            code, src_page, _ = self._run(argv)
            self.assertEqual(code, 0)
            self.assertIsNotNone(src_page["next_cursor"])
            code, out, err = self._run([
                "address-time-stats", self.path, "--bucket-size", "60",
                "--cursor", src_page["next_cursor"]])
            self.assertEqual(code, 2)
            self.assertIsNone(out)
            payload = json.loads(err)
            self.assertEqual(payload["error"], "invalid_cursor")
            self.assertIsNone(payload["input_line"])

        # 反向：address-time-stats 游标用于其他命令同样拒绝
        code, ats_page, _ = self._run([
            "address-time-stats", self.path,
            "--bucket-size", "60", "--page-size", "1"])
        self.assertEqual(code, 0)
        for target, extra in (
            ("time-stats", ["--bucket-size", "60"]),
            ("address-stats", []),
        ):
            code, out, err = self._run(
                [target, self.path] + extra
                + ["--cursor", ats_page["next_cursor"]])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(err)["error"], "invalid_cursor")

    def test_address_time_stats_cursor_bound_to_bucket_size(self):
        code, page1, _ = self._run([
            "address-time-stats", self.path, "--bucket-size", "15",
            "--page-size", "1"])
        self.assertEqual(code, 0)

        # 相同 bucket_size 续页正常
        code, page2, _ = self._run([
            "address-time-stats", self.path, "--bucket-size", "15",
            "--page-size", "1", "--cursor", page1["next_cursor"]])
        self.assertEqual(code, 0)
        self.assertEqual(
            [(g["bucket_start"], g["address"]) for g in page2["groups"]],
            [(0, "bob")],
        )

        # 改变 bucket_size 复用旧游标 → invalid_cursor
        code, out, err = self._run([
            "address-time-stats", self.path, "--bucket-size", "30",
            "--page-size", "1", "--cursor", page1["next_cursor"]])
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_cursor")
        self.assertIsNone(payload["input_line"])

    def test_address_time_stats_cursor_not_bound_to_page_size(self):
        code, page1, _ = self._run([
            "address-time-stats", self.path, "--bucket-size", "15",
            "--page-size", "1"])
        self.assertEqual(code, 0)
        code, page2, _ = self._run([
            "address-time-stats", self.path, "--bucket-size", "15",
            "--page-size", "100", "--cursor", page1["next_cursor"]])
        self.assertEqual(code, 0)
        self.assertEqual(
            [(g["bucket_start"], g["address"]) for g in page2["groups"]],
            [(0, "bob"), (15, "bob"), (15, "alice"),
             (30, "alice"), (30, "carol")],
        )
        self.assertIsNone(page2["next_cursor"])

    def test_address_time_stats_data_error_has_line_no(self):
        bad = os.path.join(self.tmp.name, "bad_ats.jsonl")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(DATA_LINES[0]) + "\n")
            fh.write("{broken\n")
        code, _, err = self._run(
            ["address-time-stats", bad, "--bucket-size", "60"])
        self.assertEqual(code, 2)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_transaction")
        self.assertEqual(payload["input_line"], 2)


class AmountFilterCliTest(unittest.TestCase):
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

    def test_query_amount_closed_interval(self):
        code, page, err = self._run(
            ["query", self.path, "--min-amount", "10",
             "--max-amount", "21"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual([t["tx_hash"] for t in page["transactions"]],
                         ["h1", "h2"])
        self.assertEqual(page["total"], 2)

    def test_query_single_bound_and_zero(self):
        code, page, _ = self._run(
            ["query", self.path, "--max-amount", "10"])
        self.assertEqual(code, 0)
        self.assertEqual([t["tx_hash"] for t in page["transactions"]],
                         ["h1", "h3"])

        code, page, _ = self._run(
            ["query", self.path, "--min-amount", "21"])
        self.assertEqual(code, 0)
        self.assertEqual([t["tx_hash"] for t in page["transactions"]], ["h2"])

        code, page, _ = self._run(
            ["query", self.path, "--min-amount", "0",
             "--max-amount", "0"])
        self.assertEqual(code, 0)
        self.assertEqual(page["transactions"], [])
        self.assertEqual(page["total"], 0)

    def test_leading_zeros_numeric_equivalence(self):
        code, page, _ = self._run(
            ["query", self.path, "--min-amount", "0010",
             "--max-amount", "021"])
        self.assertEqual(code, 0)
        self.assertEqual([t["tx_hash"] for t in page["transactions"]],
                         ["h1", "h2"])

    def test_amount_intersects_other_filters(self):
        code, page, _ = self._run([
            "query", self.path,
            "--address", "alice", "--method", "transfer",
            "--min-amount", "6", "--max-amount", "10",
        ])
        self.assertEqual(code, 0)
        self.assertEqual([t["tx_hash"] for t in page["transactions"]], ["h1"])

    def test_stats_and_groups_recomputed(self):
        code, stats, _ = self._run(
            ["stats", self.path, "--min-amount", "10"])
        self.assertEqual(code, 0)
        self.assertEqual(stats, {
            "total_count": 2,
            "total_amount": "31",
            "min_amount": "10",
            "max_amount": "21",
            "avg_amount": "15",
        })

        for command, extra in (
            ("method-stats", []),
            ("address-stats", []),
            ("counterparty-stats", ["--address", "alice"]),
            ("time-stats", ["--bucket-size", "60"]),
            ("pair-stats", []),
            ("address-time-stats", ["--bucket-size", "60"]),
        ):
            code, result, _ = self._run(
                [command, self.path, "--min-amount", "100"] + extra)
            self.assertEqual(code, 0)
            self.assertEqual(result["groups"], [])
            self.assertEqual(result["total_groups"], 0)
            self.assertIsNone(result["next_cursor"])

    def test_invalid_amount_filter_exit_2_single_line_json(self):
        missing = os.path.join(self.tmp.name, "missing.jsonl")
        bad_values = ("", " ", "-1", "+1", "1.0", ".5", "abc",
                      "1e3", "0x1", " 10", "1_0")
        for flag in ("--min-amount", "--max-amount"):
            for bad in bad_values:
                code, out, err = self._run(
                    ["query", missing, flag, bad])
                self.assertEqual(code, 2, (flag, bad))
                self.assertIsNone(out)
                # stderr 为单行 JSON
                self.assertEqual(err.strip().count("\n"), 0, (flag, bad))
                payload = json.loads(err)
                self.assertEqual(payload["error"], "invalid_amount_filter")
                self.assertIsNone(payload["input_line"])

    def test_invalid_amount_range_exit_2(self):
        missing = os.path.join(self.tmp.name, "missing.jsonl")
        code, out, err = self._run([
            "query", missing,
            "--min-amount", "11", "--max-amount", "10"])
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        self.assertEqual(err.strip().count("\n"), 0)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_amount_range")
        self.assertIsNone(payload["input_line"])

        # 前导零数值比较：0011 > 10 倒置
        code, _, err = self._run([
            "query", missing,
            "--min-amount", "0011", "--max-amount", "10"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error"], "invalid_amount_range")

        # 相等为合法闭区间（前导零等价）：正常查询而非金额报错
        code, page, err = self._run([
            "query", self.path,
            "--min-amount", "10", "--max-amount", "010"])
        self.assertEqual(code, 0)
        self.assertEqual([t["tx_hash"] for t in page["transactions"]], ["h1"])

    def test_cursor_bound_to_amount_bounds(self):
        code, page1, _ = self._run([
            "query", self.path, "--min-amount", "5", "--page-size", "1"])
        self.assertEqual(code, 0)
        self.assertIsNotNone(page1["next_cursor"])

        # 相同边界续页正常
        code, page2, _ = self._run([
            "query", self.path, "--min-amount", "5", "--page-size", "1",
            "--cursor", page1["next_cursor"]])
        self.assertEqual(code, 0)
        self.assertEqual([t["tx_hash"] for t in page2["transactions"]], ["h2"])

        # 只调整前导零可继续翻页
        code, page2, _ = self._run([
            "query", self.path, "--min-amount", "005", "--page-size", "1",
            "--cursor", page1["next_cursor"]])
        self.assertEqual(code, 0)
        self.assertEqual([t["tx_hash"] for t in page2["transactions"]], ["h2"])

        # 改变/增加/删除任一边界 → invalid_cursor
        for extra in (
            ["--min-amount", "10"],
            ["--min-amount", "5", "--max-amount", "21"],
            [],
        ):
            code, out, err = self._run(
                ["query", self.path, "--page-size", "1",
                 "--cursor", page1["next_cursor"]] + extra)
            self.assertEqual(code, 2)
            self.assertIsNone(out)
            self.assertEqual(json.loads(err)["error"], "invalid_cursor")

    def test_old_cursor_valid_without_amount_bounds(self):
        # 未指定金额边界时签发与续用都不带金额，行为与既有游标一致
        code, page1, _ = self._run(
            ["query", self.path, "--page-size", "1"])
        code, page2, _ = self._run([
            "query", self.path, "--page-size", "1",
            "--cursor", page1["next_cursor"]])
        self.assertEqual(code, 0)
        self.assertEqual([t["tx_hash"] for t in page2["transactions"]], ["h2"])


if __name__ == "__main__":
    unittest.main()
