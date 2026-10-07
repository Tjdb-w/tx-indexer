"""method-time-series（method_time_series）测试。

覆盖：连续 hour/day 桶 × method 序列与零填充、纪元对齐、左闭右开、
成功/失败计数与金额字符串、沿用 query 全部筛选、total_points /
total_methods / total_buckets、游标分页（不重复不遗漏、绑定筛选 /
时间窗 / 桶粒度、篡改 / 跨命令复用拒绝），以及三类独立序列异常。
"""

import base64
import json
import unittest

from tx_indexer.cursor import (
    encode_method_stats_cursor,
    encode_method_time_series_cursor,
    encode_time_bucket_aggregation_cursor,
    encode_time_stats_cursor,
)
from tx_indexer.engine import (
    DEFAULT_PAGE_SIZE,
    TxIndexer,
    normalize_filters,
)
from tx_indexer.errors import (
    InvalidPageSizeError,
    InvalidSeriesCursorError,
    InvalidSeriesRangeError,
    UnsupportedSeriesBucketError,
)

HOUR = 3600
DAY = 86400


def rec(tx_hash, timestamp, frm="alice", to="bob", method="transfer",
        amount="10", block_number=1, success=True):
    return {
        "tx_hash": tx_hash,
        "block_number": block_number,
        "timestamp": timestamp,
        "from_address": frm,
        "to_address": to,
        "method": method,
        "amount": amount,
        "success": success,
    }


def jsonl_rec(tx_hash, timestamp, **kwargs):
    """模拟 JSONL 记录：不含 success 字段。"""
    record = rec(tx_hash, timestamp, **kwargs)
    del record["success"]
    return record


class MethodTimeSeriesTest(unittest.TestCase):
    def setUp(self):
        self.records = [
            rec("a", 0, success=True),
            rec("b", HOUR - 1, "alice", "carol", "approve", "5",
                success=False),
            rec("c", HOUR, "bob", "alice", "transfer", "7", success=True),
            rec("d", 2 * HOUR, "dave", "alice", "transfer", "3",
                success=False),
            # 第四小时只有 approve 的失败交易
            rec("e", 3 * HOUR + 10, "alice", "bob", "approve", "9",
                success=False),
        ]
        self.idx = TxIndexer(self.records)

    def series(self, *args, **kwargs):
        return self.idx.method_time_series(
            normalize_filters(), *args, **kwargs
        )

    def test_hour_series_continuous_zero_filled(self):
        result = self.series(0, 5 * HOUR, "hour")
        self.assertEqual(result["total_methods"], 2)
        self.assertEqual(result["total_buckets"], 5)
        self.assertEqual(result["total_points"], 10)
        self.assertIsNone(result["next_cursor"])
        # bucket_start 升序、同桶 method 码点升序（approve < transfer）
        keys = [(p["bucket_start"], p["method"]) for p in result["series"]]
        self.assertEqual(
            keys,
            [(bucket, method)
             for bucket in range(0, 5 * HOUR, HOUR)
             for method in ("approve", "transfer")],
        )
        # 空桶计数为 0、金额为 "0"
        empty = result["series"][4]  # (2*HOUR, approve)
        self.assertEqual(
            empty,
            {"method": "approve", "bucket_start": 2 * HOUR,
             "total_count": 0, "total_amount": "0",
             "success_count": 0, "failure_count": 0},
        )

    def test_point_fields_and_order(self):
        result = self.series(0, HOUR, "hour")
        self.assertEqual(
            list(result.keys()),
            ["series", "total_points", "total_methods", "total_buckets",
             "next_cursor"],
        )
        self.assertEqual(
            list(result["series"][0].keys()),
            ["method", "bucket_start", "total_count", "total_amount",
             "success_count", "failure_count"],
        )
        # 第 0 桶：approve 1 笔失败 5，transfer 1 笔成功 10
        approve, transfer = result["series"]
        self.assertEqual(
            approve,
            {"method": "approve", "bucket_start": 0,
             "total_count": 1, "total_amount": "5",
             "success_count": 0, "failure_count": 1},
        )
        self.assertEqual(
            transfer,
            {"method": "transfer", "bucket_start": 0,
             "total_count": 1, "total_amount": "10",
             "success_count": 1, "failure_count": 0},
        )

    def test_amounts_are_plain_decimal_strings(self):
        records = [
            rec("a", 0, method="m", amount="007"),
            rec("b", 1, method="m", amount="13"),
        ]
        result = TxIndexer(records).method_time_series(
            normalize_filters(), 0, HOUR, "hour"
        )
        self.assertEqual(result["series"][0]["total_amount"], "20")

    def test_start_inclusive_end_exclusive(self):
        # timestamp == start_time 计入；timestamp == end_time 不计入
        result = self.series(HOUR, 2 * HOUR, "hour")
        self.assertEqual(result["total_methods"], 1)
        self.assertEqual(result["total_buckets"], 1)
        point = result["series"][0]
        self.assertEqual(point["method"], "transfer")
        self.assertEqual(point["bucket_start"], HOUR)
        self.assertEqual(point["total_count"], 1)
        self.assertEqual(point["total_amount"], "7")

    def test_first_bucket_aligned_before_start(self):
        # start 落在小时中间：首桶仍按纪元整点对齐（可早于 start），
        # 且只统计 start 之后的数据
        result = self.series(HOUR + 10, 3 * HOUR, "hour")
        self.assertEqual(result["total_buckets"], 2)
        self.assertEqual(
            [(p["bucket_start"], p["method"], p["total_count"])
             for p in result["series"]],
            [(HOUR, "transfer", 0), (2 * HOUR, "transfer", 1)],
        )

    def test_last_bucket_covers_only_before_end(self):
        # end 落在小时中间：末桶只覆盖 end 之前的数据，
        # e（3*HOUR+10）在 end=3*HOUR+5 之外，但 d（2*HOUR）在窗内
        result = self.series(2 * HOUR, 3 * HOUR + 5, "hour")
        self.assertEqual(result["total_buckets"], 2)
        self.assertEqual(result["total_methods"], 1)
        by_bucket = {p["bucket_start"]: p for p in result["series"]}
        self.assertEqual(by_bucket[2 * HOUR]["total_count"], 1)
        self.assertEqual(by_bucket[2 * HOUR]["total_amount"], "3")
        self.assertEqual(by_bucket[3 * HOUR]["total_count"], 0)
        self.assertEqual(by_bucket[3 * HOUR]["total_amount"], "0")

    def test_day_buckets_utc(self):
        records = [
            rec("a", 0, method="transfer", success=True),
            rec("b", DAY - 1, method="approve", success=False),
            rec("c", DAY, method="transfer", success=True),
            rec("d", 2 * DAY + 100, method="approve", success=False),
        ]
        result = TxIndexer(records).method_time_series(
            normalize_filters(), 0, 3 * DAY, "day"
        )
        self.assertEqual(result["total_buckets"], 3)
        self.assertEqual(result["total_methods"], 2)
        self.assertEqual(result["total_points"], 6)
        by_key = {
            (p["bucket_start"], p["method"]): p for p in result["series"]
        }
        self.assertEqual(by_key[(0, "transfer")]["total_count"], 1)
        self.assertEqual(by_key[(0, "approve")]["failure_count"], 1)
        self.assertEqual(by_key[(DAY, "approve")]["total_count"], 0)
        self.assertEqual(by_key[(2 * DAY, "approve")]["failure_count"], 1)

    def test_day_alignment_with_unaligned_range(self):
        result = self.series(HOUR, DAY + HOUR, "day")
        starts = sorted({p["bucket_start"] for p in result["series"]})
        self.assertEqual(starts, [0, DAY])
        self.assertEqual(result["total_buckets"], 2)

    def test_no_transactions_empty_series(self):
        result = self.series(10 * HOUR, 12 * HOUR, "hour")
        self.assertEqual(result["series"], [])
        self.assertEqual(result["total_methods"], 0)
        self.assertEqual(result["total_points"], 0)
        self.assertEqual(result["total_buckets"], 2)
        self.assertIsNone(result["next_cursor"])

    def test_no_match_after_filter_empty_series(self):
        result = self.idx.method_time_series(
            normalize_filters(address="nobody"), 0, 3 * HOUR, "hour"
        )
        self.assertEqual(result["series"], [])
        self.assertEqual(result["total_methods"], 0)
        self.assertEqual(result["total_points"], 0)

    def test_jsonl_records_without_success_count_as_success(self):
        idx = TxIndexer([
            jsonl_rec("a", 0),
            jsonl_rec("b", 100),
        ])
        result = idx.method_time_series(normalize_filters(), 0, HOUR, "hour")
        point = result["series"][0]
        self.assertEqual(
            (point["total_count"], point["success_count"],
             point["failure_count"]),
            (2, 2, 0),
        )

    def test_query_filters_reused(self):
        # address / method / 金额 / 区块 / status 均沿用 query 筛选语义
        result = self.idx.method_time_series(
            normalize_filters(address="alice"), 0, 4 * HOUR, "hour"
        )
        by_key = {
            (p["bucket_start"], p["method"]): p for p in result["series"]
        }
        # a(alice→bob transfer)、b(alice→carol approve)、
        # c(bob→alice transfer)、d(dave→alice transfer)、e(alice→bob approve)
        self.assertEqual(by_key[(0, "transfer")]["total_count"], 1)
        self.assertEqual(by_key[(0, "approve")]["total_count"], 1)
        self.assertEqual(by_key[(HOUR, "transfer")]["total_count"], 1)
        self.assertEqual(by_key[(2 * HOUR, "transfer")]["total_count"], 1)
        self.assertEqual(by_key[(3 * HOUR, "approve")]["total_count"], 1)

        result = self.idx.method_time_series(
            normalize_filters(method="approve"), 0, 4 * HOUR, "hour"
        )
        self.assertEqual(result["total_methods"], 1)
        self.assertEqual(result["total_points"], 4)
        counts = [p["total_count"] for p in result["series"]]
        self.assertEqual(counts, [1, 0, 0, 1])

        result = self.idx.method_time_series(
            normalize_filters(min_amount="6"), 0, 4 * HOUR, "hour"
        )
        # 只有 a(10)、c(7)、e(9) 命中
        self.assertEqual(result["total_methods"], 2)
        by_key = {
            (p["bucket_start"], p["method"]): p for p in result["series"]
        }
        self.assertEqual(by_key[(0, "transfer")]["total_amount"], "10")
        self.assertEqual(by_key[(HOUR, "transfer")]["total_amount"], "7")
        self.assertEqual(by_key[(3 * HOUR, "approve")]["total_amount"], "9")

        result = self.idx.method_time_series(
            normalize_filters(min_block=2), 0, 4 * HOUR, "hour"
        )
        self.assertEqual(result["total_methods"], 0)

        result = self.idx.method_time_series(
            normalize_filters(status="failure"), 0, 4 * HOUR, "hour"
        )
        for point in result["series"]:
            self.assertEqual(point["success_count"], 0)
        self.assertEqual(
            sum(p["failure_count"] for p in result["series"]), 3
        )

    def test_method_original_string_kept(self):
        records = [
            rec("a", 0, method="Transfer"),
            rec("b", 1, method="transfer"),
            rec("c", 2, method="转账"),
        ]
        result = TxIndexer(records).method_time_series(
            normalize_filters(), 0, HOUR, "hour"
        )
        # 原字符串入选，按 Unicode 码点升序：Transfer < transfer < 转账
        self.assertEqual(
            [p["method"] for p in result["series"]],
            ["Transfer", "transfer", "转账"],
        )

    def test_pagination_no_skip_no_dup_across_empty_buckets(self):
        # 2 个 method × 5 个桶 = 10 点，page_size=3 → 4 页
        collected = []
        cursor = None
        pages = 0
        while True:
            page = self.series(0, 5 * HOUR, "hour", page_size=3,
                               cursor=cursor)
            pages += 1
            self.assertEqual(page["total_points"], 10)
            self.assertEqual(page["total_methods"], 2)
            self.assertEqual(page["total_buckets"], 5)
            collected.extend(page["series"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(pages, 4)
        self.assertEqual(
            [(p["bucket_start"], p["method"]) for p in collected],
            [(bucket, method)
             for bucket in range(0, 5 * HOUR, HOUR)
             for method in ("approve", "transfer")],
        )

    def test_page_beyond_last_is_empty_with_null_cursor(self):
        page1 = self.series(0, 2 * HOUR, "hour", page_size=100)
        self.assertIsNone(page1["next_cursor"])
        # 手工构造指向最后一点的游标：下一页必须是空页 + 空游标
        cursor = encode_method_time_series_cursor(
            normalize_filters(), 0, 2 * HOUR, "hour", HOUR, "transfer"
        )
        page = self.series(0, 2 * HOUR, "hour", page_size=100,
                           cursor=cursor)
        self.assertEqual(page["series"], [])
        self.assertEqual(page["total_points"], 4)
        self.assertIsNone(page["next_cursor"])

    def test_cursor_not_bound_to_page_size(self):
        page1 = self.series(0, 5 * HOUR, "hour", page_size=1)
        self.assertEqual(
            [(p["bucket_start"], p["method"]) for p in page1["series"]],
            [(0, "approve")],
        )
        page2 = self.series(0, 5 * HOUR, "hour", page_size=100,
                            cursor=page1["next_cursor"])
        self.assertEqual(len(page2["series"]), 9)
        self.assertIsNone(page2["next_cursor"])

    def test_cursor_bound_to_filters(self):
        filters = normalize_filters(address="alice")
        page1 = self.idx.method_time_series(
            filters, 0, 5 * HOUR, "hour", page_size=1
        )
        with self.assertRaises(InvalidSeriesCursorError):
            self.idx.method_time_series(
                normalize_filters(address="bob"), 0, 5 * HOUR, "hour",
                page_size=1, cursor=page1["next_cursor"],
            )
        with self.assertRaises(InvalidSeriesCursorError):
            self.series(0, 5 * HOUR, "hour", page_size=1,
                        cursor=page1["next_cursor"])
        # 状态筛选同样进入绑定
        page2 = self.idx.method_time_series(
            normalize_filters(status="success"), 0, 5 * HOUR, "hour",
            page_size=1,
        )
        with self.assertRaises(InvalidSeriesCursorError):
            self.idx.method_time_series(
                normalize_filters(status="failure"), 0, 5 * HOUR, "hour",
                page_size=1, cursor=page2["next_cursor"],
            )

    def test_cursor_bound_to_time_range_and_bucket(self):
        page1 = self.series(0, 5 * HOUR, "hour", page_size=1)
        with self.assertRaises(InvalidSeriesCursorError):
            self.series(0, 6 * HOUR, "hour", page_size=1,
                        cursor=page1["next_cursor"])
        with self.assertRaises(InvalidSeriesCursorError):
            self.series(HOUR, 5 * HOUR, "hour", page_size=1,
                        cursor=page1["next_cursor"])
        with self.assertRaises(InvalidSeriesCursorError):
            self.series(0, 5 * HOUR, "day", page_size=1,
                        cursor=page1["next_cursor"])

    def test_cross_command_cursor_rejected(self):
        filters = normalize_filters()
        tokens = [
            encode_time_stats_cursor(filters, HOUR, 0),
            encode_method_stats_cursor(filters, 10, 1, "m"),
            encode_time_bucket_aggregation_cursor(
                {"address": None, "method": None,
                 "start_time": 0, "end_time": HOUR},
                "hour", 0,
            ),
        ]
        for token in tokens:
            with self.assertRaises(InvalidSeriesCursorError):
                self.series(0, HOUR, "hour", cursor=token)

    def test_cursor_garbage(self):
        for bad in ("", "not-base64!!!", "bm9wZQ", "%%%"):
            with self.assertRaises(InvalidSeriesCursorError):
                self.series(0, HOUR, "hour", cursor=bad)

    def test_cursor_tampered(self):
        page1 = self.series(0, 5 * HOUR, "hour", page_size=1)
        token = page1["next_cursor"]
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        payload = json.loads(raw.decode("utf-8"))

        def reencode(payload):
            return base64.urlsafe_b64encode(
                json.dumps(
                    payload, separators=(",", ":"), sort_keys=True
                ).encode("utf-8")
            ).rstrip(b"=").decode("ascii")

        # 篡改 marker 到错位/越界值
        tampered = dict(payload)
        tampered["after"] = [7, "approve"]
        with self.assertRaises(InvalidSeriesCursorError):
            self.series(0, 5 * HOUR, "hour", cursor=reencode(tampered))
        tampered = dict(payload)
        tampered["after"] = [100 * HOUR, "approve"]
        with self.assertRaises(InvalidSeriesCursorError):
            self.series(0, 5 * HOUR, "hour", cursor=reencode(tampered))
        # 篡改桶粒度
        tampered = dict(payload)
        tampered["g"] = "day"
        with self.assertRaises(InvalidSeriesCursorError):
            self.series(0, 5 * HOUR, "hour", cursor=reencode(tampered))
        # 篡改时间窗
        tampered = dict(payload)
        tampered["w"] = [0, 6 * HOUR]
        with self.assertRaises(InvalidSeriesCursorError):
            self.series(0, 5 * HOUR, "hour", cursor=reencode(tampered))
        # 篡改筛选
        tampered = dict(payload)
        tampered["f"] = dict(payload["f"], address="mallory")
        with self.assertRaises(InvalidSeriesCursorError):
            self.series(0, 5 * HOUR, "hour", cursor=reencode(tampered))
        # 篡改版本
        tampered = dict(payload)
        tampered["v"] = 99
        with self.assertRaises(InvalidSeriesCursorError):
            self.series(0, 5 * HOUR, "hour", cursor=reencode(tampered))

    def test_invalid_range(self):
        for args in (
            (10, 10, "hour"),
            (100, 10, "hour"),
            (-1, 10, "hour"),
            (0, -10, "hour"),
            (True, 10, "hour"),
            (0, 1.5, "hour"),
            ("0", 10, "hour"),
            (None, 10, "hour"),
            (0, None, "hour"),
        ):
            with self.assertRaises(InvalidSeriesRangeError):
                self.series(*args)

    def test_unsupported_bucket(self):
        for bad in ("week", "HOUR", "Hour", "days", "", None, 3600,
                    1.0, True, ["hour"], {"hour": 1}):
            with self.assertRaises(UnsupportedSeriesBucketError):
                self.series(0, HOUR, bad)

    def test_invalid_page_size_remains_page_size_error(self):
        for bad in (0, -1, 1001, "10", None, True):
            with self.assertRaises(InvalidPageSizeError):
                self.series(0, HOUR, "hour", page_size=bad)

    def test_error_raises_before_any_page_data(self):
        with self.assertRaises(InvalidSeriesRangeError):
            self.series(5, 5, "hour")
        with self.assertRaises(InvalidSeriesCursorError):
            self.series(0, HOUR, "hour", cursor="@@@not-a-cursor@@@")

    def test_default_page_size_constant(self):
        self.assertEqual(DEFAULT_PAGE_SIZE, 100)

    def test_single_second_range(self):
        # 最小非空范围：[0,1)，只有 timestamp 0 的 a（transfer）命中
        result = self.series(0, 1, "hour")
        self.assertEqual(result["total_buckets"], 1)
        self.assertEqual(result["total_methods"], 1)
        self.assertEqual(result["total_points"], 1)
        point = result["series"][0]
        self.assertEqual(point["method"], "transfer")
        self.assertEqual(point["bucket_start"], 0)
        self.assertEqual(point["total_count"], 1)

    def test_existing_query_and_stats_unchanged(self):
        # 新功能不改变既有入口：query 左闭右闭、stats 口径保持
        page = self.idx.query(normalize_filters())
        self.assertEqual(page["total"], 5)
        stats = self.idx.stats(normalize_filters())
        self.assertEqual(stats["total_count"], 5)
        self.assertEqual(stats["total_amount"], "34")


if __name__ == "__main__":
    unittest.main()
