"""区块高度筛选（--min-block / --max-block 与 min_block / max_block）测试。"""

import io
import json
import os
import sys
import tempfile
import unittest

from tx_indexer.cli import main
from tx_indexer.cursor import _encode_payload
from tx_indexer.engine import TxIndexer, normalize_filters
from tx_indexer.errors import (
    InvalidBlockFilterError,
    InvalidBlockRangeError,
    InvalidCursorError,
)

DATA_LINES = [
    {"tx_hash": "h1", "block_number": 1, "timestamp": 10,
     "from_address": "alice", "to_address": "bob",
     "method": "transfer", "amount": "10"},
    {"tx_hash": "h2", "block_number": 2, "timestamp": 20,
     "from_address": "bob", "to_address": "alice",
     "method": "approve", "amount": "21"},
    {"tx_hash": "h3", "block_number": 3, "timestamp": 30,
     "from_address": "alice", "to_address": "carol",
     "method": "transfer", "amount": "5"},
    {"tx_hash": "h4", "block_number": 4, "timestamp": 40,
     "from_address": "carol", "to_address": "alice",
     "method": "transfer", "amount": "8"},
    {"tx_hash": "h5", "block_number": 5, "timestamp": 50,
     "from_address": "bob", "to_address": "carol",
     "method": "approve", "amount": "7"},
]


def _records():
    return [dict(obj) for obj in DATA_LINES]


def _hashes(page):
    return [t["tx_hash"] for t in page["transactions"]]


class NormalizeFiltersBlockTest(unittest.TestCase):
    def test_bounds_accepted(self):
        filters = normalize_filters(min_block=2, max_block=4)
        self.assertEqual(filters["min_block"], 2)
        self.assertEqual(filters["max_block"], 4)

    def test_bounds_default_none(self):
        filters = normalize_filters()
        self.assertIsNone(filters["min_block"])
        self.assertIsNone(filters["max_block"])

    def test_zero_bound_accepted(self):
        filters = normalize_filters(min_block=0, max_block=0)
        self.assertEqual(filters["min_block"], 0)
        self.assertEqual(filters["max_block"], 0)

    def test_non_integer_values_rejected(self):
        for bad in ("3", "007", "", "  ", "0x10", "+3", "-3", "1.5",
                    1.5, 3.0, True, False, [1], None.__class__):
            with self.assertRaises(InvalidBlockFilterError):
                normalize_filters(min_block=bad)
            with self.assertRaises(InvalidBlockFilterError):
                normalize_filters(max_block=bad)

    def test_negative_rejected(self):
        with self.assertRaises(InvalidBlockFilterError):
            normalize_filters(min_block=-1)

    def test_inverted_range_rejected(self):
        with self.assertRaises(InvalidBlockRangeError):
            normalize_filters(min_block=5, max_block=2)

    def test_equal_bounds_allowed(self):
        filters = normalize_filters(min_block=3, max_block=3)
        self.assertEqual(filters["min_block"], 3)


class EngineBlockFilterTest(unittest.TestCase):
    def setUp(self):
        self.idx = TxIndexer(_records())

    def test_query_closed_range(self):
        page = self.idx.query(normalize_filters(min_block=2, max_block=4))
        self.assertEqual(_hashes(page), ["h2", "h3", "h4"])
        self.assertEqual(page["total"], 3)

    def test_query_min_only(self):
        page = self.idx.query(normalize_filters(min_block=4))
        self.assertEqual(_hashes(page), ["h4", "h5"])
        self.assertEqual(page["total"], 2)

    def test_query_max_only(self):
        page = self.idx.query(normalize_filters(max_block=1))
        self.assertEqual(_hashes(page), ["h1"])
        self.assertEqual(page["total"], 1)

    def test_query_inclusive_endpoints(self):
        page = self.idx.query(normalize_filters(min_block=3, max_block=3))
        self.assertEqual(_hashes(page), ["h3"])

    def test_query_intersects_other_filters(self):
        page = self.idx.query(normalize_filters(
            method="transfer", min_block=2, max_block=4))
        self.assertEqual(_hashes(page), ["h3", "h4"])
        self.assertEqual(page["total"], 2)

    def test_query_no_match(self):
        page = self.idx.query(normalize_filters(min_block=10))
        self.assertEqual(page["transactions"], [])
        self.assertEqual(page["total"], 0)
        self.assertIsNone(page["next_cursor"])

    def test_stats_with_bounds(self):
        stats = self.idx.stats(normalize_filters(min_block=2, max_block=4))
        self.assertEqual(stats, {
            "total_count": 3,
            "total_amount": "34",
            "min_amount": "5",
            "max_amount": "21",
            "avg_amount": "11",
        })

    def test_stats_no_match(self):
        stats = self.idx.stats(normalize_filters(min_block=10))
        self.assertEqual(stats["total_count"], 0)
        self.assertEqual(stats["total_amount"], "0")
        self.assertIsNone(stats["min_amount"])

    def test_method_stats_with_bounds(self):
        page = self.idx.method_stats(
            normalize_filters(min_block=2, max_block=4))
        self.assertEqual(page["total_groups"], 2)
        self.assertEqual(
            [(g["method"], g["total_count"]) for g in page["groups"]],
            [("approve", 1), ("transfer", 2)],
        )

    def test_address_stats_with_bounds(self):
        page = self.idx.address_stats(normalize_filters(min_block=5))
        self.assertEqual(page["total_groups"], 2)
        addresses = {g["address"] for g in page["groups"]}
        self.assertEqual(addresses, {"bob", "carol"})

    def test_counterparty_stats_with_bounds(self):
        page = self.idx.counterparty_stats(
            normalize_filters(address="alice", min_block=4))
        self.assertEqual(page["address"], "alice")
        self.assertEqual(page["total_groups"], 1)
        self.assertEqual(page["groups"][0]["counterparty"], "carol")

    def test_time_stats_with_bounds(self):
        page = self.idx.time_stats(
            normalize_filters(min_block=2, max_block=4), bucket_size=60)
        self.assertEqual(page["total_groups"], 1)
        self.assertEqual(page["groups"][0]["total_count"], 3)

    def test_pair_stats_with_bounds(self):
        page = self.idx.pair_stats(normalize_filters(max_block=1))
        self.assertEqual(page["total_groups"], 1)
        self.assertEqual(page["groups"][0]["from_address"], "alice")
        self.assertEqual(page["groups"][0]["to_address"], "bob")

    def test_address_time_stats_with_bounds(self):
        page = self.idx.address_time_stats(
            normalize_filters(min_block=5), bucket_size=60)
        self.assertEqual(page["total_groups"], 2)

    def test_query_cursor_pagination_with_bounds(self):
        filters = normalize_filters(min_block=1, max_block=5)
        seen = []
        cursor = None
        while True:
            page = self.idx.query(filters, page_size=2, cursor=cursor)
            seen.extend(_hashes(page))
            self.assertEqual(page["total"], 5)
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(seen, ["h1", "h2", "h3", "h4", "h5"])

    def test_cursor_invalidated_by_bound_change(self):
        filters = normalize_filters(min_block=1, max_block=5)
        page1 = self.idx.query(filters, page_size=2)
        cursor = page1["next_cursor"]
        for changed in (
            normalize_filters(min_block=2, max_block=5),
            normalize_filters(min_block=1, max_block=4),
            normalize_filters(max_block=5),
            normalize_filters(min_block=1),
            normalize_filters(),
        ):
            with self.assertRaises(InvalidCursorError):
                self.idx.query(changed, page_size=2, cursor=cursor)

    def test_cursor_survives_equivalent_bounds(self):
        filters = normalize_filters(min_block=1, max_block=5)
        page1 = self.idx.query(filters, page_size=2)
        page2 = self.idx.query(
            normalize_filters(min_block=1, max_block=5),
            page_size=2, cursor=page1["next_cursor"])
        self.assertEqual(_hashes(page2), ["h3", "h4"])

    def test_cursor_not_bound_to_page_size(self):
        filters = normalize_filters(min_block=1, max_block=5)
        page1 = self.idx.query(filters, page_size=2)
        page2 = self.idx.query(
            filters, page_size=3, cursor=page1["next_cursor"])
        self.assertEqual(_hashes(page2), ["h3", "h4", "h5"])
        self.assertIsNone(page2["next_cursor"])

    def test_legacy_cursor_without_block_fields(self):
        # 不带区块边界字段的旧游标：未指定边界时可续翻
        legacy = _encode_payload({
            "v": 1,
            "c": "query",
            "f": {
                "address": None,
                "from_address": None,
                "to_address": None,
                "method": None,
                "start_time": None,
                "end_time": None,
                "min_amount": None,
                "max_amount": None,
            },
            "after": [2, "h2"],
        })
        page = self.idx.query(
            normalize_filters(), page_size=2, cursor=legacy)
        self.assertEqual(_hashes(page), ["h3", "h4"])
        # 指定任一边界时报 invalid_cursor
        with self.assertRaises(InvalidCursorError):
            self.idx.query(
                normalize_filters(min_block=1), page_size=2, cursor=legacy)
        with self.assertRaises(InvalidCursorError):
            self.idx.query(
                normalize_filters(max_block=5), page_size=2, cursor=legacy)

    def test_method_stats_cursor_bound_to_block_bounds(self):
        filters = normalize_filters(min_block=1)
        page1 = self.idx.method_stats(filters, page_size=1)
        with self.assertRaises(InvalidCursorError):
            self.idx.method_stats(
                normalize_filters(min_block=2),
                page_size=1, cursor=page1["next_cursor"])

    def test_time_stats_cursor_bound_to_block_bounds(self):
        filters = normalize_filters(max_block=5)
        page1 = self.idx.time_stats(filters, bucket_size=20, page_size=1)
        self.assertIsNotNone(page1["next_cursor"])
        page2 = self.idx.time_stats(
            filters, bucket_size=20, page_size=1,
            cursor=page1["next_cursor"])
        self.assertEqual(page2["total_groups"], page1["total_groups"])
        with self.assertRaises(InvalidCursorError):
            self.idx.time_stats(
                normalize_filters(max_block=4), bucket_size=20,
                page_size=1, cursor=page1["next_cursor"])


class CliBlockFilterTest(unittest.TestCase):
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

    def _run_error(self, argv, expected_error):
        code, out, err = self._run(argv)
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        payload = json.loads(err)
        self.assertEqual(payload["error"], expected_error)
        self.assertIsNone(payload["input_line"])
        return payload

    def test_query_with_bounds(self):
        code, page, _ = self._run(
            ["query", self.path, "--min-block", "2", "--max-block", "4"])
        self.assertEqual(code, 0)
        self.assertEqual(
            [t["tx_hash"] for t in page["transactions"]],
            ["h2", "h3", "h4"])
        self.assertEqual(page["total"], 3)

    def test_query_min_only(self):
        code, page, _ = self._run(["query", self.path, "--min-block", "4"])
        self.assertEqual(code, 0)
        self.assertEqual(
            [t["tx_hash"] for t in page["transactions"]], ["h4", "h5"])

    def test_query_max_only(self):
        code, page, _ = self._run(["query", self.path, "--max-block", "1"])
        self.assertEqual(code, 0)
        self.assertEqual(
            [t["tx_hash"] for t in page["transactions"]], ["h1"])

    def test_leading_zeros_accepted(self):
        code, page, _ = self._run(
            ["query", self.path, "--min-block", "002", "--max-block", "04"])
        self.assertEqual(code, 0)
        self.assertEqual(page["total"], 3)

    def test_intersection_with_other_filters(self):
        code, page, _ = self._run([
            "query", self.path, "--method", "transfer",
            "--min-block", "2", "--max-block", "4",
        ])
        self.assertEqual(code, 0)
        self.assertEqual(
            [t["tx_hash"] for t in page["transactions"]], ["h3", "h4"])

    def test_stats_with_bounds(self):
        code, stats, _ = self._run(
            ["stats", self.path, "--min-block", "2", "--max-block", "4"])
        self.assertEqual(code, 0)
        self.assertEqual(stats["total_count"], 3)
        self.assertEqual(stats["total_amount"], "34")
        self.assertEqual(stats["avg_amount"], "11")

    def test_grouped_commands_accept_bounds(self):
        cases = [
            (["method-stats", self.path, "--min-block", "2"], "total_groups"),
            (["address-stats", self.path, "--max-block", "4"], "total_groups"),
            (["counterparty-stats", self.path, "--address", "alice",
              "--min-block", "1"], "total_groups"),
            (["time-stats", self.path, "--bucket-size", "60",
              "--max-block", "3"], "total_groups"),
            (["pair-stats", self.path, "--min-block", "5"], "total_groups"),
            (["address-time-stats", self.path, "--bucket-size", "60",
              "--min-block", "1", "--max-block", "5"], "total_groups"),
        ]
        for argv, key in cases:
            code, out, _ = self._run(argv)
            self.assertEqual(code, 0, argv)
            self.assertIn(key, out)

    def test_invalid_bound_values(self):
        for bad in ("", "  ", "abc", "0x10", "+3", "-3", "1.5", "1e3", "3."):
            self._run_error(
                ["query", self.path, "--min-block", bad],
                "invalid_block_filter")
            self._run_error(
                ["stats", self.path, "--max-block", bad],
                "invalid_block_filter")

    def test_invalid_bound_checked_before_reading_file(self):
        missing = os.path.join(self.tmp.name, "missing.jsonl")
        self._run_error(
            ["query", missing, "--min-block", "abc"],
            "invalid_block_filter")
        self._run_error(
            ["query", missing, "--min-block", "5", "--max-block", "2"],
            "invalid_block_range")

    def test_inverted_range(self):
        self._run_error(
            ["query", self.path, "--min-block", "5", "--max-block", "2"],
            "invalid_block_range")

    def test_cursor_bound_to_block_bounds(self):
        code, page1, _ = self._run([
            "query", self.path, "--page-size", "2",
            "--min-block", "1", "--max-block", "5",
        ])
        self.assertEqual(code, 0)
        cursor = page1["next_cursor"]
        self.assertIsNotNone(cursor)

        # 相同边界续翻
        code, page2, _ = self._run([
            "query", self.path, "--page-size", "2",
            "--min-block", "1", "--max-block", "5", "--cursor", cursor,
        ])
        self.assertEqual(code, 0)
        self.assertEqual(
            [t["tx_hash"] for t in page2["transactions"]], ["h3", "h4"])

        # 仅前导零不同：同条件，可续翻
        code, page2b, _ = self._run([
            "query", self.path, "--page-size", "2",
            "--min-block", "01", "--max-block", "05", "--cursor", cursor,
        ])
        self.assertEqual(code, 0)
        self.assertEqual(
            [t["tx_hash"] for t in page2b["transactions"]], ["h3", "h4"])

        # 边界集合变化：invalid_cursor
        for extra in (
            ["--min-block", "2", "--max-block", "5"],
            ["--min-block", "1"],
            ["--max-block", "5"],
            [],
        ):
            self._run_error(
                ["query", self.path, "--page-size", "2",
                 "--cursor", cursor] + extra,
                "invalid_cursor")

    def test_cursor_last_page_null(self):
        cursor = None
        seen = []
        while True:
            argv = ["query", self.path, "--page-size", "2",
                    "--min-block", "2", "--max-block", "4"]
            if cursor is not None:
                argv += ["--cursor", cursor]
            code, page, _ = self._run(argv)
            self.assertEqual(code, 0)
            seen.extend(t["tx_hash"] for t in page["transactions"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(seen, ["h2", "h3", "h4"])

    def test_time_bucket_aggregation_unchanged(self):
        code, out, _ = self._run([
            "time-bucket-aggregation", self.path,
            "--start-time", "0", "--end-time", "60", "--bucket", "hour",
        ])
        self.assertEqual(code, 0)
        self.assertEqual(out["total_buckets"], 1)
        self.assertEqual(out["buckets"][0]["total_count"], 5)


if __name__ == "__main__":
    unittest.main()
