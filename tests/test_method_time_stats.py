"""method-time-stats：时间区间 × method 联合汇总的分页统计测试。"""

import io
import json
import os
import sys
import tempfile
import unittest

from tx_indexer.cli import main
from tx_indexer.engine import TxIndexer, normalize_filters
from tx_indexer.errors import (
    InvalidBucketSizeError,
    InvalidCursorError,
    InvalidPageSizeError,
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


# bucket_size=60 时：
#   桶 0：  approve 21（成功 1）、transfer 15（10 成功 + 5 失败）
#   桶 60： transfer 7（失败 1）、approve 3（成功 1）
#   桶 120：zebra 100（缺省 success → 成功 1）、transfer 100（失败 1）
RECORDS = [
    rec("h1", 1, 0, "alice", "bob", "transfer", "10"),
    rec("h2", 1, 5, "bob", "alice", "approve", "21", success=True),
    rec("h3", 2, 30, "alice", "carol", "transfer", "5", success=False),
    rec("h4", 3, 60, "bob", "carol", "transfer", "7", success=False),
    rec("h5", 3, 61, "carol", "bob", "approve", "3", success=True),
    rec("h6", 4, 120, "alice", "bob", "zebra", "100"),
    rec("h7", 4, 125, "bob", "alice", "transfer", "100", success=False),
]

EXPECTED_ORDER = [
    {"bucket_start": 0, "method": "approve", "total_count": 1,
     "total_amount": "21", "success_count": 1, "failure_count": 0},
    {"bucket_start": 0, "method": "transfer", "total_count": 2,
     "total_amount": "15", "success_count": 1, "failure_count": 1},
    {"bucket_start": 60, "method": "transfer", "total_count": 1,
     "total_amount": "7", "success_count": 0, "failure_count": 1},
    {"bucket_start": 60, "method": "approve", "total_count": 1,
     "total_amount": "3", "success_count": 1, "failure_count": 0},
    # 同桶同 total_amount/count，按 success_count 降序：zebra 在 transfer 前
    {"bucket_start": 120, "method": "zebra", "total_count": 1,
     "total_amount": "100", "success_count": 1, "failure_count": 0},
    {"bucket_start": 120, "method": "transfer", "total_count": 1,
     "total_amount": "100", "success_count": 0, "failure_count": 1},
]


class MethodTimeStatsTest(unittest.TestCase):
    def setUp(self):
        self.idx = TxIndexer(RECORDS)

    def test_aggregation_field_shape_and_order(self):
        result = self.idx.method_time_stats(normalize_filters(), 60)
        self.assertEqual(result["total_groups"], 6)
        self.assertIsNone(result["next_cursor"])
        self.assertEqual(result["groups"], EXPECTED_ORDER)
        for group in result["groups"]:
            # 恰好六个公开字段，无 avg_amount / bucket_end_exclusive
            self.assertEqual(
                set(group),
                {"bucket_start", "method", "total_count", "total_amount",
                 "success_count", "failure_count"},
            )
            self.assertIsInstance(group["bucket_start"], int)
            self.assertIsInstance(group["total_amount"], str)
            self.assertEqual(
                group["total_count"],
                group["success_count"] + group["failure_count"],
            )

    def test_bucket_alignment_from_epoch(self):
        # bucket_size=100：ts=60 与 ts=0 同桶，ts=120 落入 100 桶
        result = self.idx.method_time_stats(normalize_filters(), 100)
        starts = sorted({g["bucket_start"] for g in result["groups"]})
        self.assertEqual(starts, [0, 100])
        self.assertTrue(all(s % 100 == 0 for s in starts))

    def test_method_unicode_codepoint_tiebreak(self):
        # 同桶、total/count/success/failure 全相同：按 method 码点升序
        records = [
            rec("u1", 1, 0, "a", "b", "中", "1"),
            rec("u2", 1, 1, "a", "b", "a", "1"),
        ]
        result = TxIndexer(records).method_time_stats(normalize_filters(), 60)
        self.assertEqual(
            [g["method"] for g in result["groups"]], ["a", "中"]
        )

    def test_no_match_returns_empty(self):
        result = self.idx.method_time_stats(
            normalize_filters(method="nonexistent"), 60
        )
        self.assertEqual(result, {
            "groups": [],
            "total_groups": 0,
            "next_cursor": None,
        })

    def test_status_failure_filter_other_side_zero(self):
        result = self.idx.method_time_stats(
            normalize_filters(status="failure"), 60
        )
        # 失败交易：h3(桶0 transfer 5)、h4(桶60 transfer 7)、h7(桶120 transfer 100)
        self.assertEqual(result["total_groups"], 3)
        self.assertEqual(result["groups"], [
            {"bucket_start": 0, "method": "transfer", "total_count": 1,
             "total_amount": "5", "success_count": 0, "failure_count": 1},
            {"bucket_start": 60, "method": "transfer", "total_count": 1,
             "total_amount": "7", "success_count": 0, "failure_count": 1},
            {"bucket_start": 120, "method": "transfer", "total_count": 1,
             "total_amount": "100", "success_count": 0, "failure_count": 1},
        ])

    def test_status_success_filter(self):
        result = self.idx.method_time_stats(
            normalize_filters(status="success"), 60
        )
        for group in result["groups"]:
            self.assertEqual(group["failure_count"], 0)
            self.assertEqual(group["total_count"], group["success_count"])
        self.assertEqual(
            [(g["bucket_start"], g["method"]) for g in result["groups"]],
            [(0, "approve"), (0, "transfer"), (60, "approve"), (120, "zebra")],
        )

    def test_filters_intersect(self):
        result = self.idx.method_time_stats(
            normalize_filters(method="approve", start_time=10, end_time=70),
            60,
        )
        # approve 且 10 <= ts <= 70（左闭右闭）：仅 h5（桶 60）
        self.assertEqual(result["total_groups"], 1)
        self.assertEqual(result["groups"][0]["bucket_start"], 60)
        self.assertEqual(result["groups"][0]["method"], "approve")

    def test_pagination_no_skip_no_duplicate(self):
        seen = []
        cursor = None
        for _ in range(10):
            page = self.idx.method_time_stats(
                normalize_filters(), 60, page_size=2, cursor=cursor
            )
            seen.extend(page["groups"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(seen, EXPECTED_ORDER)

    def test_cursor_not_bound_to_page_size(self):
        first = self.idx.method_time_stats(
            normalize_filters(), 60, page_size=3
        )
        self.assertIsNotNone(first["next_cursor"])
        # 用更大的 page_size 续翻游标仍合法
        second = self.idx.method_time_stats(
            normalize_filters(), 60, page_size=10, cursor=first["next_cursor"]
        )
        self.assertEqual(
            first["groups"] + second["groups"], EXPECTED_ORDER
        )
        self.assertIsNone(second["next_cursor"])

    def test_cursor_bound_to_bucket_size(self):
        page = self.idx.method_time_stats(
            normalize_filters(), 60, page_size=2
        )
        with self.assertRaises(InvalidCursorError):
            self.idx.method_time_stats(
                normalize_filters(), 120, page_size=2,
                cursor=page["next_cursor"],
            )

    def test_cursor_bound_to_filters(self):
        page = self.idx.method_time_stats(
            normalize_filters(), 60, page_size=2
        )
        with self.assertRaises(InvalidCursorError):
            self.idx.method_time_stats(
                normalize_filters(method="approve"), 60, page_size=2,
                cursor=page["next_cursor"],
            )
        with self.assertRaises(InvalidCursorError):
            self.idx.method_time_stats(
                normalize_filters(status="failure"), 60, page_size=2,
                cursor=page["next_cursor"],
            )
        with self.assertRaises(InvalidCursorError):
            self.idx.method_time_stats(
                normalize_filters(min_amount="1"), 60, page_size=2,
                cursor=page["next_cursor"],
            )
        with self.assertRaises(InvalidCursorError):
            self.idx.method_time_stats(
                normalize_filters(min_block=1), 60, page_size=2,
                cursor=page["next_cursor"],
            )

    def test_cursor_amount_numeric_equivalence_leading_zeros(self):
        page = self.idx.method_time_stats(
            normalize_filters(min_amount="005", max_amount="0100"),
            60, page_size=2,
        )
        # 仅前导零不同视为等价筛选，游标可续翻
        continued = self.idx.method_time_stats(
            normalize_filters(min_amount="5", max_amount="100"),
            60, page_size=10, cursor=page["next_cursor"],
        )
        all_groups = page["groups"] + continued["groups"]
        self.assertTrue(all(5 <= int(g["total_amount"]) <= 100
                            for g in all_groups))

    def test_cross_command_cursor_rejected(self):
        # time-stats / address-time-stats / method-status-stats 游标互不可复用
        time_page = self.idx.time_stats(
            normalize_filters(), 60, page_size=1
        )
        with self.assertRaises(InvalidCursorError):
            self.idx.method_time_stats(
                normalize_filters(), 60, page_size=2,
                cursor=time_page["next_cursor"],
            )
        mstatus_page = self.idx.method_status_stats(
            normalize_filters(), page_size=1
        )
        with self.assertRaises(InvalidCursorError):
            self.idx.method_time_stats(
                normalize_filters(), 60, page_size=2,
                cursor=mstatus_page["next_cursor"],
            )

    def test_tampered_or_garbage_cursor_rejected(self):
        page = self.idx.method_time_stats(
            normalize_filters(), 60, page_size=2
        )
        token = page["next_cursor"]
        for bad in (
            "not-base64!!!",
            token[:-1] + ("A" if token[-1] != "A" else "B"),
            "",
        ):
            with self.assertRaises(InvalidCursorError):
                self.idx.method_time_stats(
                    normalize_filters(), 60, cursor=bad
                )

    def test_invalid_bucket_size(self):
        for bad in (0, -1, True, 1.5, "60", None):
            with self.assertRaises(InvalidBucketSizeError):
                self.idx.method_time_stats(normalize_filters(), bad)

    def test_invalid_page_size(self):
        for bad in (0, -1, 1001, True, 1.5, "10"):
            with self.assertRaises(InvalidPageSizeError):
                self.idx.method_time_stats(normalize_filters(), 60,
                                           page_size=bad)


class MethodTimeStatsCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "data.jsonl")
        with open(self.path, "w", encoding="utf-8") as fh:
            for obj in RECORDS:
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
            json.loads(err) if err.strip() else None,
        )

    def _base(self, *extra):
        return ["method-time-stats", self.path,
                "--bucket-size", "60"] + list(extra)

    def test_cli_full_result(self):
        code, result, err = self._run(self._base())
        self.assertEqual(code, 0)
        self.assertIsNone(err)
        self.assertEqual(result["total_groups"], 6)
        self.assertEqual(result["groups"], EXPECTED_ORDER)

    def test_cli_pagination(self):
        code, page1, err = self._run(self._base("--page-size", "2"))
        self.assertEqual(code, 0)
        self.assertIsNone(err)
        self.assertEqual(len(page1["groups"]), 2)
        code, page2, err = self._run(self._base(
            "--page-size", "2", "--cursor", page1["next_cursor"]))
        self.assertEqual(code, 0)
        self.assertEqual(len(page2["groups"]), 2)
        seen = page1["groups"] + page2["groups"]
        cursor = page2["next_cursor"]
        while cursor is not None:
            code, page, _ = self._run(self._base(
                "--page-size", "2", "--cursor", cursor))
            seen.extend(page["groups"])
            cursor = page["next_cursor"]
        self.assertEqual(seen, EXPECTED_ORDER)

    def test_cli_missing_bucket_size(self):
        code, result, err = self._run(
            ["method-time-stats", self.path]
        )
        self.assertEqual(code, 2)
        self.assertIsNone(result)
        self.assertEqual(err["error"], "invalid_bucket_size")
        self.assertIsNone(err["input_line"])

    def test_cli_bad_bucket_size(self):
        # 重复的 --bucket-size 由 argparse 以后值覆盖，故直接在 _base 后追加
        for bad in ("0", "-3", "abc", "1.5"):
            code, _, err = self._run(self._base("--bucket-size", bad))
            self.assertEqual(code, 2)
            self.assertEqual(err["error"], "invalid_bucket_size")

    def test_cli_bad_page_size(self):
        for bad in ("0", "1001", "abc"):
            code, _, err = self._run(self._base(
                "--page-size", bad))
            self.assertEqual(code, 2)
            self.assertEqual(err["error"], "invalid_page_size")

    def test_cli_bad_cursor(self):
        code, _, err = self._run(self._base("--cursor", "garbage!"))
        self.assertEqual(code, 2)
        self.assertEqual(err["error"], "invalid_cursor")

    def test_cli_filters_and_status(self):
        code, result, err = self._run(self._base(
            "--method", "transfer", "--status", "failure"))
        self.assertEqual(code, 0)
        self.assertIsNone(err)
        self.assertEqual(result["total_groups"], 3)
        self.assertTrue(
            all(g["method"] == "transfer" and g["success_count"] == 0
                for g in result["groups"])
        )


if __name__ == "__main__":
    unittest.main()
