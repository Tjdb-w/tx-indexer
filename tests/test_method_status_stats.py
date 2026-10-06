"""method-status-stats：按方法拆分成功/失败的分页统计测试。"""

import io
import json
import os
import sys
import tempfile
import unittest

from tx_indexer.cli import main
from tx_indexer.engine import TxIndexer, normalize_filters
from tx_indexer.errors import (
    InvalidCursorError,
    InvalidPageSizeError,
    InvalidStatusFilterError,
)


def rec(tx_hash, block_number, timestamp, frm, to, method, amount,
        success="__absent__"):
    record = {
        "tx_hash": tx_hash,
        "block_number": block_number,
        "timestamp": timestamp,
        "from_address": frm,
        "to_address": to,
        "method": method,
        "amount": amount,
    }
    # 缺省 success 时不写入该字段，语义为成功
    if success != "__absent__":
        record["success"] = success
    return record


class MethodStatusStatsTest(unittest.TestCase):
    def setUp(self):
        self.records = [
            # transfer：10(成功,缺省) + 5(失败) + 7(失败) = 22，3 笔
            rec("h1", 1, 10, "alice", "bob", "transfer", "10"),
            rec("h3", 2, 30, "alice", "carol", "transfer", "5",
                success=False),
            rec("h4", 3, 40, "bob", "carol", "transfer", "7",
                success=False),
            # approve：21 成功，1 笔
            rec("h2", 2, 20, "bob", "alice", "approve", "21",
                success=True),
        ]
        self.idx = TxIndexer(self.records)

    def test_aggregation_and_field_shape(self):
        result = self.idx.method_status_stats(normalize_filters())
        self.assertEqual(result["total_groups"], 2)
        self.assertIsNone(result["next_cursor"])
        self.assertEqual(result["groups"][0], {
            "method": "transfer",
            "total_count": 3,
            "total_amount": "22",
            "avg_amount": "7",
            "success_count": 1,
            "failure_count": 2,
            "success_amount": "10",
            "failure_amount": "12",
        })
        self.assertEqual(result["groups"][1], {
            "method": "approve",
            "total_count": 1,
            "total_amount": "21",
            "avg_amount": "21",
            "success_count": 1,
            "failure_count": 0,
            "success_amount": "21",
            "failure_amount": "0",
        })

    def test_amounts_are_no_leading_zero_strings(self):
        result = self.idx.method_status_stats(normalize_filters())
        for group in result["groups"]:
            for key in (
                "total_amount", "avg_amount",
                "success_amount", "failure_amount",
            ):
                value = group[key]
                self.assertIsInstance(value, str)
                self.assertEqual(value, str(int(value)))
            for key in (
                "total_count", "success_count", "failure_count",
            ):
                self.assertIsInstance(group[key], int)

    def test_count_and_amount_invariants(self):
        for group in self.idx.method_status_stats(
            normalize_filters()
        )["groups"]:
            self.assertEqual(
                group["total_count"],
                group["success_count"] + group["failure_count"],
            )
            self.assertEqual(
                int(group["total_amount"]),
                int(group["success_amount"])
                + int(group["failure_amount"]),
            )

    def test_avg_floor_division(self):
        records = [
            rec("a1", 1, 1, "x", "y", "m", "10", success=True),
            rec("a2", 2, 2, "x", "y", "m", "1", success=False),
            rec("a3", 3, 3, "x", "y", "m", "0", success=False),
        ]  # 合计 11，3 笔 -> avg 3
        result = TxIndexer(records).method_status_stats(
            normalize_filters()
        )
        group = result["groups"][0]
        self.assertEqual(group["total_amount"], "11")
        self.assertEqual(group["avg_amount"], "3")
        self.assertEqual(group["success_amount"], "10")
        self.assertEqual(group["failure_amount"], "1")

    def test_ordering_tie_breakers(self):
        records = [
            # aa 与 bb：total=10,count=2；aa 成功 2、bb 成功 1 -> aa 在前
            rec("aa1", 1, 1, "x", "y", "aa", "8", success=True),
            rec("aa2", 2, 2, "x", "y", "aa", "2", success=True),
            rec("bb1", 3, 3, "x", "y", "bb", "8", success=True),
            rec("bb2", 4, 4, "x", "y", "bb", "2", success=False),
            # cc 与 dd：total/count/success/failure 全相同（各 1 成功），
            # 按码点升序 cc 在前
            rec("cc1", 5, 5, "x", "y", "cc", "5", success=True),
            rec("dd1", 6, 6, "x", "y", "dd", "5", success=True),
            # zz：total=20 最大，排最前
            rec("zz1", 7, 7, "x", "y", "zz", "20", success=False),
        ]
        result = TxIndexer(records).method_status_stats(
            normalize_filters()
        )
        self.assertEqual(
            [g["method"] for g in result["groups"]],
            ["zz", "aa", "bb", "cc", "dd"],
        )

    def test_failure_count_tie_breaker(self):
        records = [
            # pp / qq：total=10,count=2,success=1；pp 失败金额对应
            # failure_count 相同 -> 再看 method 码点
            rec("pp1", 1, 1, "x", "y", "pp", "9", success=False),
            rec("pp2", 2, 2, "x", "y", "pp", "1", success=True),
            rec("qq1", 3, 3, "x", "y", "qq", "1", success=True),
            rec("qq2", 4, 4, "x", "y", "qq", "9", success=False),
        ]
        result = TxIndexer(records).method_status_stats(
            normalize_filters()
        )
        self.assertEqual(
            [g["method"] for g in result["groups"]], ["pp", "qq"]
        )

    def test_empty_result(self):
        result = self.idx.method_status_stats(
            normalize_filters(method="missing")
        )
        self.assertEqual(
            result,
            {"groups": [], "total_groups": 0, "next_cursor": None},
        )

    def test_status_success_zeroes_failure_side(self):
        result = self.idx.method_status_stats(
            normalize_filters(status="success")
        )
        for group in result["groups"]:
            self.assertEqual(group["failure_count"], 0)
            self.assertEqual(group["failure_amount"], "0")
            self.assertEqual(
                group["total_count"], group["success_count"]
            )
        methods = {g["method"] for g in result["groups"]}
        self.assertEqual(methods, {"transfer", "approve"})

    def test_status_failure_zeroes_success_side(self):
        result = self.idx.method_status_stats(
            normalize_filters(status="failure")
        )
        self.assertEqual(
            [g["method"] for g in result["groups"]], ["transfer"]
        )
        group = result["groups"][0]
        self.assertEqual(group["success_count"], 0)
        self.assertEqual(group["success_amount"], "0")
        self.assertEqual(group["failure_count"], 2)
        self.assertEqual(group["failure_amount"], "12")
        self.assertEqual(group["total_count"], 2)
        self.assertEqual(group["total_amount"], "12")
        self.assertEqual(group["avg_amount"], "6")

    def test_invalid_status_filter(self):
        with self.assertRaises(InvalidStatusFilterError):
            normalize_filters(status="Success")

    def test_filters_intersect(self):
        # 只看 alice 参与的：transfer 命中 h1(10 成功)、h3(5 失败)
        result = self.idx.method_status_stats(
            normalize_filters(address="alice", method="transfer",
                              min_amount="5", max_amount="10")
        )
        self.assertEqual(result["total_groups"], 1)
        group = result["groups"][0]
        self.assertEqual(group["method"], "transfer")
        self.assertEqual(group["total_count"], 2)
        self.assertEqual(group["total_amount"], "15")
        self.assertEqual(group["success_amount"], "10")
        self.assertEqual(group["failure_amount"], "5")

    def test_block_filter(self):
        result = self.idx.method_status_stats(
            normalize_filters(min_block=3)
        )
        self.assertEqual([g["method"] for g in result["groups"]],
                         ["transfer"])
        self.assertEqual(result["groups"][0]["total_count"], 1)

    def test_pagination_no_skip_no_duplicate(self):
        filters = normalize_filters()
        collected = []
        cursor = None
        pages = 0
        while True:
            page = self.idx.method_status_stats(
                filters, page_size=1, cursor=cursor
            )
            pages += 1
            self.assertEqual(page["total_groups"], 2)
            collected.extend(g["method"] for g in page["groups"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(pages, 2)
        self.assertEqual(collected, ["transfer", "approve"])

    def test_last_page_has_null_cursor(self):
        filters = normalize_filters()
        page = self.idx.method_status_stats(filters, page_size=1)
        cursor = page["next_cursor"]
        last = self.idx.method_status_stats(
            filters, page_size=1, cursor=cursor
        )
        self.assertEqual(
            [g["method"] for g in last["groups"]], ["approve"]
        )
        self.assertIsNone(last["next_cursor"])

    def test_cursor_not_bound_to_page_size(self):
        filters = normalize_filters()
        page = self.idx.method_status_stats(
            filters, page_size=1
        )
        cursor = page["next_cursor"]
        # 换更大 page_size 续翻仍然合法
        page2 = self.idx.method_status_stats(
            filters, page_size=100, cursor=cursor
        )
        self.assertEqual(
            [g["method"] for g in page2["groups"]], ["approve"]
        )

    def test_cross_command_cursor_rejected(self):
        # query / method-stats 游标均不能用于 method-status-stats
        query_page = self.idx.query(
            normalize_filters(), page_size=1
        )
        with self.assertRaises(InvalidCursorError):
            self.idx.method_status_stats(
                normalize_filters(), cursor=query_page["next_cursor"]
            )
        method_page = self.idx.method_stats(
            normalize_filters(), page_size=1
        )
        with self.assertRaises(InvalidCursorError):
            self.idx.method_status_stats(
                normalize_filters(), cursor=method_page["next_cursor"]
            )

    def test_method_status_cursor_rejected_by_other_commands(self):
        page = self.idx.method_status_stats(
            normalize_filters(), page_size=1
        )
        cursor = page["next_cursor"]
        with self.assertRaises(InvalidCursorError):
            self.idx.method_stats(
                normalize_filters(), page_size=1, cursor=cursor
            )
        with self.assertRaises(InvalidCursorError):
            self.idx.query(
                normalize_filters(), page_size=1, cursor=cursor
            )

    def test_cursor_bound_to_filters_and_status(self):
        page = self.idx.method_status_stats(
            normalize_filters(status="success"), page_size=1
        )
        cursor = page["next_cursor"]
        with self.assertRaises(InvalidCursorError):
            self.idx.method_status_stats(
                normalize_filters(status="failure"), cursor=cursor
            )
        with self.assertRaises(InvalidCursorError):
            self.idx.method_status_stats(
                normalize_filters(), cursor=cursor
            )
        with self.assertRaises(InvalidCursorError):
            self.idx.method_status_stats(
                normalize_filters(status="success", method="approve"),
                cursor=cursor,
            )

    def test_cursor_amount_numeric_equivalence(self):
        page = self.idx.method_status_stats(
            normalize_filters(min_amount="05"), page_size=1
        )
        cursor = page["next_cursor"]
        # 仅前导零不同：等价筛选，可续翻
        resumed = self.idx.method_status_stats(
            normalize_filters(min_amount="5"), page_size=1, cursor=cursor
        )
        self.assertIn("groups", resumed)

    def test_tampered_or_undecodable_cursor(self):
        page = self.idx.method_status_stats(
            normalize_filters(), page_size=1
        )
        cursor = page["next_cursor"]
        for bad in (cursor + "x", "!!!", "", "not-base64"):
            with self.assertRaises(InvalidCursorError):
                self.idx.method_status_stats(
                    normalize_filters(), cursor=bad
                )

    def test_invalid_page_size(self):
        for bad in (0, -1, 1001):
            with self.assertRaises(InvalidPageSizeError):
                self.idx.method_status_stats(
                    normalize_filters(), page_size=bad
                )


class MethodStatusStatsCliTest(unittest.TestCase):
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

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "data.jsonl")
        with open(self.path, "w", encoding="utf-8") as fh:
            for obj in self.DATA_LINES:
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

    def test_cli_basic(self):
        code, result, err = self._run(
            ["method-status-stats", self.path]
        )
        self.assertEqual(code, 0)
        self.assertIsNone(err)
        self.assertEqual(result["total_groups"], 2)
        self.assertEqual(
            [g["method"] for g in result["groups"]],
            ["transfer", "approve"],
        )

    def test_cli_pagination_and_cross_command(self):
        code, page1, err = self._run(
            ["method-status-stats", self.path, "--page-size", "1"]
        )
        self.assertEqual(code, 0)
        cursor = page1["next_cursor"]

        code, page2, err = self._run(
            ["method-status-stats", self.path, "--page-size", "1",
             "--cursor", cursor]
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            [g["method"] for g in page2["groups"]], ["approve"]
        )
        self.assertIsNone(page2["next_cursor"])

        code, result, err = self._run(
            ["method-stats", self.path, "--page-size", "1",
             "--cursor", cursor]
        )
        self.assertEqual(code, 2)
        self.assertEqual(err["error"], "invalid_cursor")

    def test_cli_invalid_filter_codes(self):
        for argv, error in (
            (["method-status-stats", self.path, "--status", "nope"],
             "invalid_status_filter"),
            (["method-status-stats", self.path, "--min-amount", "-1"],
             "invalid_amount_filter"),
            (["method-status-stats", self.path,
              "--min-amount", "9", "--max-amount", "1"],
             "invalid_amount_range"),
            (["method-status-stats", self.path, "--min-block", "x"],
             "invalid_block_filter"),
            (["method-status-stats", self.path,
              "--min-block", "9", "--max-block", "1"],
             "invalid_block_range"),
            (["method-status-stats", self.path,
              "--start-time", "9", "--end-time", "1"],
             "invalid_time_range"),
            (["method-status-stats", self.path, "--address", "  "],
             "invalid_filter"),
        ):
            code, result, err = self._run(argv)
            self.assertEqual(code, 2, argv)
            self.assertIsNone(result)
            self.assertEqual(err["error"], error, argv)

    def test_cli_invalid_data(self):
        bad_path = os.path.join(self.tmp.name, "bad.jsonl")
        with open(bad_path, "w", encoding="utf-8") as fh:
            fh.write("{not json}\n")
        code, result, err = self._run(
            ["method-status-stats", bad_path]
        )
        self.assertEqual(code, 2)
        self.assertEqual(err["error"], "invalid_transaction")
        self.assertEqual(err["input_line"], 1)


if __name__ == "__main__":
    unittest.main()
