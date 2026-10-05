"""时间分桶聚合（time_bucket_aggregation）测试。

覆盖：连续 hour/day 桶与零填充、UTC 对齐、左闭右开、成功/失败计数、
地址与方法筛选、游标分页（不重复不遗漏、绑定完整查询条件与桶粒度、
篡改/跨命令复用拒绝），以及四类独立聚合异常。
"""

import base64
import json
import unittest

from tx_indexer.cursor import (
    encode_method_stats_cursor,
    encode_time_stats_cursor,
)
from tx_indexer.engine import (
    DEFAULT_PAGE_SIZE,
    TxIndexer,
    normalize_filters,
)
from tx_indexer.errors import (
    InvalidAggregationCursor,
    InvalidAggregationCursorError,
    InvalidAggregationFilter,
    InvalidAggregationRange,
    InvalidPageSizeError,
    UnsupportedAggregationBucket,
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


class TimeBucketAggregationTest(unittest.TestCase):
    def setUp(self):
        self.records = [
            rec("a", 0, success=True),
            rec("b", HOUR - 1, "alice", "carol", "approve", "5",
                success=False),
            rec("c", HOUR, "bob", "alice", "transfer", "7", success=True),
            rec("d", 2 * HOUR, "dave", "alice", "transfer", "3",
                success=False),
            # 第四小时只有失败交易
            rec("e", 3 * HOUR + 10, "alice", "bob", "approve", "9",
                success=False),
        ]
        self.idx = TxIndexer(self.records)

    def aggregate(self, *args, **kwargs):
        return self.idx.time_bucket_aggregation(*args, **kwargs)

    def test_hour_buckets_continuous_zero_filled(self):
        # 范围覆盖 4 个小时桶；第 4 个桶（7200..10799）只有失败交易，
        # 这里查询 5 个桶以验证中间不会静默跳过
        result = self.aggregate(0, 5 * HOUR, "hour")
        starts = [b["bucket_start"] for b in result["buckets"]]
        self.assertEqual(starts, [0, HOUR, 2 * HOUR, 3 * HOUR, 4 * HOUR])
        self.assertEqual(result["total_buckets"], 5)
        self.assertIsNone(result["next_cursor"])
        # 无交易的桶（4*HOUR）三个计数均为 0
        empty = result["buckets"][4]
        self.assertEqual(
            empty,
            {"bucket_start": 4 * HOUR, "total_count": 0,
             "success_count": 0, "failure_count": 0},
        )

    def test_success_and_failure_counts(self):
        result = self.aggregate(0, 4 * HOUR, "hour")
        by_start = {b["bucket_start"]: b for b in result["buckets"]}
        self.assertEqual(
            (by_start[0]["total_count"],
             by_start[0]["success_count"],
             by_start[0]["failure_count"]),
            (2, 1, 1),
        )
        self.assertEqual(
            (by_start[HOUR]["total_count"],
             by_start[HOUR]["success_count"],
             by_start[HOUR]["failure_count"]),
            (1, 1, 0),
        )
        self.assertEqual(
            (by_start[2 * HOUR]["total_count"],
             by_start[2 * HOUR]["success_count"],
             by_start[2 * HOUR]["failure_count"]),
            (1, 0, 1),
        )
        for bucket in result["buckets"]:
            self.assertEqual(
                bucket["total_count"],
                bucket["success_count"] + bucket["failure_count"],
            )

    def test_result_field_order(self):
        result = self.aggregate(0, HOUR, "hour")
        self.assertEqual(
            list(result.keys()),
            ["buckets", "total_buckets", "next_cursor"],
        )
        self.assertEqual(
            list(result["buckets"][0].keys()),
            ["bucket_start", "total_count", "success_count",
             "failure_count"],
        )

    def test_start_inclusive_end_exclusive(self):
        # timestamp == start_time 计入
        result = self.aggregate(0, HOUR, "hour")
        self.assertEqual(result["buckets"][0]["total_count"], 2)
        # timestamp == end_time 不计入：范围 [HOUR, 2*HOUR) 只含 c
        result = self.aggregate(HOUR, 2 * HOUR, "hour")
        self.assertEqual(len(result["buckets"]), 1)
        self.assertEqual(result["buckets"][0]["total_count"], 1)
        self.assertEqual(result["buckets"][0]["bucket_start"], HOUR)

    def test_first_bucket_aligned_before_start(self):
        # start 落在小时中间：首桶仍按纪元整点对齐（可早于 start）
        result = self.aggregate(HOUR + 10, 3 * HOUR, "hour")
        self.assertEqual(
            [b["bucket_start"] for b in result["buckets"]],
            [HOUR, 2 * HOUR],
        )
        self.assertEqual(result["total_buckets"], 2)
        # 首桶只统计 start 之后：c 在 HOUR（早于起点）不计，
        # 该桶 total_count 为 0
        self.assertEqual(result["buckets"][0]["total_count"], 0)
        self.assertEqual(result["buckets"][1]["total_count"], 1)

    def test_last_bucket_covers_only_before_end(self):
        # end 落在小时中间：末桶只覆盖 end 之前的数据，
        # e（3*HOUR+10）在 end=3*HOUR+5 之外
        result = self.aggregate(2 * HOUR, 3 * HOUR + 5, "hour")
        self.assertEqual(
            [b["bucket_start"] for b in result["buckets"]],
            [2 * HOUR, 3 * HOUR],
        )
        self.assertEqual(result["buckets"][1]["total_count"], 0)

    def test_day_buckets_utc(self):
        records = [
            rec("a", 0, success=True),
            rec("b", DAY - 1, success=False),
            rec("c", DAY, success=True),
            rec("d", 2 * DAY + 100, success=False),
        ]
        result = TxIndexer(records).time_bucket_aggregation(
            0, 3 * DAY, "day"
        )
        self.assertEqual(
            [b["bucket_start"] for b in result["buckets"]],
            [0, DAY, 2 * DAY],
        )
        self.assertEqual(result["total_buckets"], 3)
        self.assertEqual(
            (result["buckets"][0]["total_count"],
             result["buckets"][0]["success_count"],
             result["buckets"][0]["failure_count"]),
            (2, 1, 1),
        )
        self.assertEqual(result["buckets"][2]["failure_count"], 1)

    def test_day_alignment_with_unaligned_range(self):
        result = self.aggregate(HOUR, DAY + HOUR, "day")
        # 首桶 0（覆盖起点 HOUR）、末桶 DAY（覆盖 DAY+HOUR-1）
        self.assertEqual([b["bucket_start"] for b in result["buckets"]],
                         [0, DAY])

    def test_address_filter_matches_either_side(self):
        result = self.aggregate(0, 4 * HOUR, "hour", address="alice")
        counts = [b["total_count"] for b in result["buckets"]]
        # a(alice→bob)、b(alice→carol)、c(bob→alice)、d(dave→alice)、
        # e(alice→bob)
        self.assertEqual(counts, [2, 1, 1, 1])

    def test_method_filter_single_and_set(self):
        single = self.aggregate(0, 4 * HOUR, "hour", method="approve")
        as_set = self.aggregate(
            0, 4 * HOUR, "hour", method=["approve", "approve"]
        )
        # approve：b 在第 0 桶、e 在第 3 桶
        expected = [1, 0, 0, 1]
        self.assertEqual(
            [b["total_count"] for b in single["buckets"]], expected
        )
        self.assertEqual(
            [b["total_count"] for b in as_set["buckets"]], expected
        )
        # 集合内任一命中
        both = self.aggregate(
            0, 4 * HOUR, "hour", method=("transfer", "approve")
        )
        self.assertEqual([b["total_count"] for b in both["buckets"]],
                         [2, 1, 1, 1])

    def test_filters_intersect(self):
        result = self.aggregate(
            0, 4 * HOUR, "hour", address="alice", method="transfer"
        )
        # a: alice→bob transfer; b: approve 排除; c: bob→alice transfer;
        # d: dave→alice transfer; e: approve 排除
        self.assertEqual(
            [b["total_count"] for b in result["buckets"]], [1, 1, 1, 0]
        )

    def test_no_match_returns_all_zero_buckets(self):
        result = self.aggregate(
            0, 3 * HOUR, "hour", address="nobody"
        )
        self.assertEqual(result["total_buckets"], 3)
        self.assertEqual(
            [b["total_count"] for b in result["buckets"]], [0, 0, 0]
        )
        self.assertIsNone(result["next_cursor"])

    def test_jsonl_records_without_success_count_as_success(self):
        idx = TxIndexer([
            jsonl_rec("a", 0),
            jsonl_rec("b", 100),
        ])
        result = idx.time_bucket_aggregation(0, HOUR, "hour")
        bucket = result["buckets"][0]
        self.assertEqual(
            (bucket["total_count"], bucket["success_count"],
             bucket["failure_count"]),
            (2, 2, 0),
        )

    def test_pagination_no_skip_no_dup_across_empty_buckets(self):
        # 5 个桶、page_size=2，空桶也必须照常翻页：3 页
        collected = []
        cursor = None
        pages = 0
        while True:
            page = self.aggregate(
                0, 5 * HOUR, "hour", page_size=2, cursor=cursor
            )
            pages += 1
            self.assertEqual(page["total_buckets"], 5)
            collected.extend(page["buckets"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(pages, 3)
        self.assertEqual(
            [b["bucket_start"] for b in collected],
            [0, HOUR, 2 * HOUR, 3 * HOUR, 4 * HOUR],
        )

    def test_page_beyond_last_is_empty_with_null_cursor(self):
        page1 = self.aggregate(0, 2 * HOUR, "hour", page_size=5)
        self.assertIsNone(page1["next_cursor"])
        # 手工构造指向最后一桶的游标：下一页必须是空页 + 空游标
        from tx_indexer.cursor import (
            encode_time_bucket_aggregation_cursor,
        )
        filters = {"address": None, "method": None,
                   "start_time": 0, "end_time": 2 * HOUR}
        cursor = encode_time_bucket_aggregation_cursor(
            filters, "hour", HOUR
        )
        page = self.aggregate(0, 2 * HOUR, "hour", page_size=5,
                              cursor=cursor)
        self.assertEqual(page["buckets"], [])
        self.assertEqual(page["total_buckets"], 2)
        self.assertIsNone(page["next_cursor"])

    def test_cursor_not_bound_to_page_size(self):
        page1 = self.aggregate(0, 5 * HOUR, "hour", page_size=1)
        self.assertEqual(
            [b["bucket_start"] for b in page1["buckets"]], [0]
        )
        page2 = self.aggregate(
            0, 5 * HOUR, "hour", page_size=100,
            cursor=page1["next_cursor"],
        )
        self.assertEqual(
            [b["bucket_start"] for b in page2["buckets"]],
            [HOUR, 2 * HOUR, 3 * HOUR, 4 * HOUR],
        )
        self.assertIsNone(page2["next_cursor"])

    def test_cursor_bound_to_bucket_granularity(self):
        page1 = self.aggregate(0, 2 * DAY, "day", page_size=1)
        with self.assertRaises(InvalidAggregationCursor):
            self.aggregate(0, 2 * DAY, "hour", page_size=1,
                           cursor=page1["next_cursor"])

    def test_cursor_bound_to_time_range(self):
        page1 = self.aggregate(0, 5 * HOUR, "hour", page_size=1)
        with self.assertRaises(InvalidAggregationCursor):
            self.aggregate(0, 6 * HOUR, "hour", page_size=1,
                           cursor=page1["next_cursor"])
        with self.assertRaises(InvalidAggregationCursor):
            self.aggregate(HOUR, 5 * HOUR, "hour", page_size=1,
                           cursor=page1["next_cursor"])

    def test_cursor_bound_to_filters(self):
        page1 = self.aggregate(
            0, 5 * HOUR, "hour", address="alice", page_size=1
        )
        with self.assertRaises(InvalidAggregationCursor):
            self.aggregate(
                0, 5 * HOUR, "hour", address="bob", page_size=1,
                cursor=page1["next_cursor"],
            )
        page2 = self.aggregate(
            0, 5 * HOUR, "hour", method="transfer", page_size=1
        )
        with self.assertRaises(InvalidAggregationCursor):
            self.aggregate(
                0, 5 * HOUR, "hour", method="approve", page_size=1,
                cursor=page2["next_cursor"],
            )

    def test_cross_command_cursor_rejected(self):
        filters = normalize_filters()
        query_like = encode_time_stats_cursor(filters, HOUR, 0)
        method_like = encode_method_stats_cursor(filters, 10, 1, "m")
        for token in (query_like, method_like):
            with self.assertRaises(InvalidAggregationCursor):
                self.aggregate(0, HOUR, "hour", cursor=token)

    def test_cursor_garbage(self):
        for bad in ("", "not-base64!!!", "bm9wZQ", "%%%", None):
            if bad is None:
                continue
            with self.assertRaises(InvalidAggregationCursor):
                self.aggregate(0, HOUR, "hour", cursor=bad)

    def test_cursor_tampered(self):
        page1 = self.aggregate(0, 5 * HOUR, "hour", page_size=1)
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
        tampered["after"] = 7
        with self.assertRaises(InvalidAggregationCursor):
            self.aggregate(0, 5 * HOUR, "hour", cursor=reencode(tampered))
        # 篡改桶粒度
        tampered = dict(payload)
        tampered["g"] = "day"
        with self.assertRaises(InvalidAggregationCursor):
            self.aggregate(0, 5 * HOUR, "hour", cursor=reencode(tampered))
        # 篡改筛选
        tampered = dict(payload)
        tampered["f"] = dict(payload["f"], address="mallory")
        with self.assertRaises(InvalidAggregationCursor):
            self.aggregate(0, 5 * HOUR, "hour", cursor=reencode(tampered))
        # 篡改版本
        tampered = dict(payload)
        tampered["v"] = 99
        with self.assertRaises(InvalidAggregationCursor):
            self.aggregate(0, 5 * HOUR, "hour", cursor=reencode(tampered))

    def test_invalid_range(self):
        for kwargs in (
            dict(start_time=10, end_time=10, bucket="hour"),
            dict(start_time=100, end_time=10, bucket="hour"),
            dict(start_time=-1, end_time=10, bucket="hour"),
            dict(start_time=0, end_time=-10, bucket="hour"),
            dict(start_time=True, end_time=10, bucket="hour"),
            dict(start_time=0, end_time=1.5, bucket="hour"),
            dict(start_time="0", end_time=10, bucket="hour"),
            dict(start_time=None, end_time=10, bucket="hour"),
        ):
            with self.assertRaises(InvalidAggregationRange):
                self.aggregate(**kwargs)

    def test_unsupported_bucket(self):
        for bad in ("week", "HOUR", "Hour", "days", "", None, 3600,
                    1.0, True, ["hour"], {"hour": 1}):
            with self.assertRaises(UnsupportedAggregationBucket):
                self.aggregate(0, HOUR, bad)

    def test_invalid_filter(self):
        with self.assertRaises(InvalidAggregationFilter):
            self.aggregate(0, HOUR, "hour", address="   ")
        with self.assertRaises(InvalidAggregationFilter):
            self.aggregate(0, HOUR, "hour", address=123)
        with self.assertRaises(InvalidAggregationFilter):
            self.aggregate(0, HOUR, "hour", method="  ")
        with self.assertRaises(InvalidAggregationFilter):
            self.aggregate(0, HOUR, "hour", method=["ok", " "])
        with self.assertRaises(InvalidAggregationFilter):
            self.aggregate(0, HOUR, "hour", method=123)
        with self.assertRaises(InvalidAggregationFilter):
            self.aggregate(0, HOUR, "hour", method=["m", 5])

    def test_invalid_page_size_remains_page_size_error(self):
        for bad in (0, -1, 1001, "10", None, True):
            with self.assertRaises(InvalidPageSizeError):
                self.aggregate(0, HOUR, "hour", page_size=bad)

    def test_exception_aliases_are_identical(self):
        self.assertIs(InvalidAggregationCursorError,
                      InvalidAggregationCursor)
        from tx_indexer.errors import (
            InvalidAggregationFilterError,
            InvalidAggregationRangeError,
            UnsupportedAggregationBucketError,
        )
        self.assertIs(InvalidAggregationFilterError,
                      InvalidAggregationFilter)
        self.assertIs(InvalidAggregationRangeError,
                      InvalidAggregationRange)
        self.assertIs(UnsupportedAggregationBucketError,
                      UnsupportedAggregationBucket)

    def test_granularity_keyword_alias(self):
        result = self.aggregate(0, HOUR, granularity="hour")
        self.assertEqual(result["total_buckets"], 1)
        with self.assertRaises(TypeError):
            self.aggregate(0, HOUR, "hour", granularity="day")

    def test_default_page_size_constant(self):
        self.assertEqual(DEFAULT_PAGE_SIZE, 100)

    def test_single_second_range(self):
        # 最小非空范围：[0,1)，只有 timestamp 0 命中
        result = self.aggregate(0, 1, "hour")
        self.assertEqual(result["total_buckets"], 1)
        self.assertEqual(result["buckets"][0]["bucket_start"], 0)
        self.assertEqual(result["buckets"][0]["total_count"], 1)

    def test_existing_query_and_stats_unchanged(self):
        # 新功能不改变既有入口：query 左闭右闭、stats 口径保持
        page = self.idx.query(normalize_filters())
        self.assertEqual(page["total"], 5)
        stats = self.idx.stats(normalize_filters())
        self.assertEqual(stats["total_count"], 5)
        self.assertEqual(stats["total_amount"], "34")

    def test_error_raises_before_any_page_data(self):
        # 非法参数抛异常时不返回任何结构（调用方拿不到部分页）
        with self.assertRaises(InvalidAggregationRange):
            self.aggregate(5, 5, "hour")
        with self.assertRaises(InvalidAggregationCursor):
            self.aggregate(
                0, HOUR, "hour",
                cursor="@@@not-a-cursor@@@",
            )


if __name__ == "__main__":
    unittest.main()
