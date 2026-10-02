#!/usr/bin/env python3
"""tx_indexer 单元测试。"""

import contextlib
import io
import json
import os
import tempfile
import unittest

import tx_indexer
from tx_indexer import (
    DuplicateTransactionError,
    InvalidCursorError,
    InvalidPageSizeError,
    InvalidTimeRangeError,
    InvalidTransactionError,
)

SAMPLE = [
    {"tx_hash": "0x03", "block_number": 2, "timestamp": 300,
     "from_address": "alice", "to_address": "bob", "method": "transfer",
     "amount": "30"},
    {"tx_hash": "0x01", "block_number": 1, "timestamp": 100,
     "from_address": "alice", "to_address": "carol", "method": "transfer",
     "amount": "10"},
    {"tx_hash": "0x02", "block_number": 1, "timestamp": 200,
     "from_address": "bob", "to_address": "carol", "method": "approve",
     "amount": "20"},
    {"tx_hash": "0x04", "block_number": 3, "timestamp": 400,
     "from_address": "dave", "to_address": "alice", "method": "transfer",
     "amount": "41"},
]


def write_jsonl(lines):
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line if isinstance(line, str) else json.dumps(line))
            fh.write("\n")
    return path


class TempFileMixin(unittest.TestCase):
    def setUp(self):
        self._paths = []

    def tearDown(self):
        for path in self._paths:
            os.unlink(path)

    def make_file(self, lines):
        path = write_jsonl(lines)
        self._paths.append(path)
        return path


class TestParse(TempFileMixin):
    def test_valid_file_loads_in_order(self):
        path = self.make_file(SAMPLE)
        txs = tx_indexer.load_transactions(path)
        self.assertEqual([t["tx_hash"] for t in txs],
                         ["0x03", "0x01", "0x02", "0x04"])

    def test_blank_lines_are_skipped(self):
        path = self.make_file([json.dumps(SAMPLE[0]), "", "   ", json.dumps(SAMPLE[1])])
        self.assertEqual(len(tx_indexer.load_transactions(path)), 2)

    def test_invalid_json(self):
        path = self.make_file(["{not json"])
        with self.assertRaises(InvalidTransactionError) as ctx:
            tx_indexer.load_transactions(path)
        self.assertEqual(ctx.exception.input_line, 1)
        self.assertEqual(ctx.exception.to_dict()["error"], "invalid_transaction")

    def test_missing_field(self):
        bad = dict(SAMPLE[0])
        del bad["method"]
        path = self.make_file([json.dumps(SAMPLE[1]), json.dumps(bad)])
        with self.assertRaises(InvalidTransactionError) as ctx:
            tx_indexer.load_transactions(path)
        self.assertEqual(ctx.exception.input_line, 2)

    def test_negative_block_number(self):
        bad = dict(SAMPLE[0], block_number=-1)
        path = self.make_file([json.dumps(bad)])
        with self.assertRaises(InvalidTransactionError):
            tx_indexer.load_transactions(path)

    def test_bool_timestamp_rejected(self):
        bad = dict(SAMPLE[0], timestamp=True)
        path = self.make_file([json.dumps(bad)])
        with self.assertRaises(InvalidTransactionError):
            tx_indexer.load_transactions(path)

    def test_empty_text_field_rejected(self):
        bad = dict(SAMPLE[0], to_address="")
        path = self.make_file([json.dumps(bad)])
        with self.assertRaises(InvalidTransactionError):
            tx_indexer.load_transactions(path)

    def test_amount_must_be_decimal_string(self):
        for bad_amount in ("-1", "1.5", "0x10", "", 10, "1e3"):
            bad = dict(SAMPLE[0], amount=bad_amount)
            path = self.make_file([json.dumps(bad)])
            with self.assertRaises(InvalidTransactionError, msg=repr(bad_amount)):
                tx_indexer.load_transactions(path)

    def test_amount_leading_zeros_accepted(self):
        ok = dict(SAMPLE[0], amount="007")
        path = self.make_file([json.dumps(ok)])
        self.assertEqual(tx_indexer.load_transactions(path)[0]["amount"], "007")

    def test_duplicate_tx_hash(self):
        path = self.make_file([json.dumps(SAMPLE[0]), json.dumps(SAMPLE[0])])
        with self.assertRaises(DuplicateTransactionError) as ctx:
            tx_indexer.load_transactions(path)
        self.assertEqual(ctx.exception.input_line, 2)
        self.assertEqual(ctx.exception.to_dict()["error"], "duplicate_transaction")


class TestQuery(TempFileMixin):
    def setUp(self):
        super().setUp()
        self.path = self.make_file(SAMPLE)

    def test_ordering_by_block_then_hash(self):
        result = tx_indexer.run_query(self.path)
        self.assertEqual([t["tx_hash"] for t in result["transactions"]],
                         ["0x01", "0x02", "0x03", "0x04"])
        self.assertEqual(result["total"], 4)
        self.assertIsNone(result["next_cursor"])

    def test_address_matches_from_or_to(self):
        result = tx_indexer.run_query(self.path, address="alice")
        self.assertEqual([t["tx_hash"] for t in result["transactions"]],
                         ["0x01", "0x03", "0x04"])
        self.assertEqual(result["total"], 3)

    def test_method_exact_match(self):
        result = tx_indexer.run_query(self.path, method="transfer")
        self.assertEqual(result["total"], 3)
        result = tx_indexer.run_query(self.path, method="trans")
        self.assertEqual(result["total"], 0)

    def test_time_window_inclusive(self):
        result = tx_indexer.run_query(self.path, from_ts=100, to_ts=300)
        self.assertEqual([t["tx_hash"] for t in result["transactions"]],
                         ["0x01", "0x02", "0x03"])

    def test_filters_intersect(self):
        result = tx_indexer.run_query(
            self.path, address="alice", method="transfer", from_ts=150)
        self.assertEqual([t["tx_hash"] for t in result["transactions"]],
                         ["0x03", "0x04"])

    def test_pagination_walks_all_pages_without_gaps_or_dupes(self):
        seen = []
        cursor = None
        pages = 0
        while True:
            result = tx_indexer.run_query(self.path, page_size=1, cursor=cursor)
            self.assertEqual(result["total"], 4)
            seen.extend(t["tx_hash"] for t in result["transactions"])
            cursor = result["next_cursor"]
            pages += 1
            if cursor is None:
                break
        self.assertEqual(pages, 4)
        self.assertEqual(seen, ["0x01", "0x02", "0x03", "0x04"])

    def test_page_size_two(self):
        first = tx_indexer.run_query(self.path, page_size=2)
        self.assertEqual([t["tx_hash"] for t in first["transactions"]],
                         ["0x01", "0x02"])
        self.assertIsNotNone(first["next_cursor"])
        second = tx_indexer.run_query(self.path, page_size=2,
                                      cursor=first["next_cursor"])
        self.assertEqual([t["tx_hash"] for t in second["transactions"]],
                         ["0x03", "0x04"])
        self.assertIsNone(second["next_cursor"])

    def test_page_size_bounds(self):
        for bad in (0, -1, 1001):
            with self.assertRaises(InvalidPageSizeError):
                tx_indexer.run_query(self.path, page_size=bad)
        for ok in (1, 1000):
            tx_indexer.run_query(self.path, page_size=ok)

    def test_inverted_time_range(self):
        with self.assertRaises(InvalidTimeRangeError) as ctx:
            tx_indexer.run_query(self.path, from_ts=10, to_ts=9)
        self.assertEqual(ctx.exception.to_dict()["error"], "invalid_time_range")
        self.assertIsNone(ctx.exception.input_line)

    def test_malformed_cursor(self):
        for bad in ("!!!", "aGVsbG8=", ""):
            with self.assertRaises(InvalidCursorError, msg=repr(bad)):
                tx_indexer.run_query(self.path, cursor=bad)

    def test_cursor_filter_mismatch(self):
        first = tx_indexer.run_query(self.path, page_size=2)
        with self.assertRaises(InvalidCursorError) as ctx:
            tx_indexer.run_query(self.path, page_size=2,
                                 cursor=first["next_cursor"], method="transfer")
        self.assertEqual(ctx.exception.to_dict()["error"], "invalid_cursor")

    def test_cursor_reused_with_same_filters(self):
        first = tx_indexer.run_query(self.path, page_size=2, address="alice")
        self.assertEqual([t["tx_hash"] for t in first["transactions"]],
                         ["0x01", "0x03"])
        second = tx_indexer.run_query(self.path, page_size=2, address="alice",
                                      cursor=first["next_cursor"])
        self.assertEqual([t["tx_hash"] for t in second["transactions"]],
                         ["0x04"])
        self.assertIsNone(second["next_cursor"])


class TestStats(TempFileMixin):
    def setUp(self):
        super().setUp()
        self.path = self.make_file(SAMPLE)

    def test_full_aggregation(self):
        result = tx_indexer.run_stats(self.path)
        self.assertEqual(result, {
            "total_count": 4,
            "total_amount": "101",
            "min_amount": "10",
            "max_amount": "41",
            "avg_amount": "25",  # 101 // 4，向下取整
        })

    def test_filtered_aggregation(self):
        result = tx_indexer.run_stats(self.path, address="carol")
        self.assertEqual(result["total_count"], 2)
        self.assertEqual(result["total_amount"], "30")
        self.assertEqual(result["avg_amount"], "15")

    def test_empty_result(self):
        result = tx_indexer.run_stats(self.path, address="nobody")
        self.assertEqual(result, {
            "total_count": 0,
            "total_amount": "0",
            "min_amount": None,
            "max_amount": None,
            "avg_amount": None,
        })

    def test_stats_validates_time_range(self):
        with self.assertRaises(InvalidTimeRangeError):
            tx_indexer.run_stats(self.path, from_ts=5, to_ts=1)


class TestCli(TempFileMixin):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tx_indexer.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_query_exit_ok_and_json(self):
        path = self.make_file(SAMPLE)
        code, out, _ = self.run_cli(["query", "--file", path, "--page-size", "2"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(set(payload), {"transactions", "total", "next_cursor"})

    def test_stats_exit_ok(self):
        path = self.make_file(SAMPLE)
        code, out, _ = self.run_cli(["stats", path])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["total_count"], 4)

    def test_invalid_transaction_exit_2(self):
        path = self.make_file(["{bad"])
        code, _, err = self.run_cli(["query", "--file", path])
        self.assertEqual(code, 2)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_transaction")
        self.assertEqual(payload["input_line"], 1)
        self.assertIn("message", payload)

    def test_non_line_error_has_null_input_line(self):
        path = self.make_file(SAMPLE)
        code, _, err = self.run_cli(
            ["query", "--file", path, "--page-size", "0"])
        self.assertEqual(code, 2)
        payload = json.loads(err)
        self.assertEqual(payload["error"], "invalid_page_size")
        self.assertIsNone(payload["input_line"])


if __name__ == "__main__":
    unittest.main()
