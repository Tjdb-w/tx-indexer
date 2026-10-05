"""时间分桶聚合（time_bucket_stats）测试。"""

import unittest

from tx_indexer.engine import TxIndexer
from tx_indexer.errors import (
    InvalidAggregationCursor,
    InvalidAggregationFilter,
    InvalidAggregationRange,
    InvalidPageSizeError,
    UnsupportedAggregationBucket,
)


def rec(tx_hash, block_number, timestamp, frm, to, method, amount,
        success=None):
    record = {
        "tx_hash": tx_hash,
        "block_number": block_number,
        "timestamp": timestamp,
        "from_address": frm,
        "to_address": to,
        "method": method,
        "amount": amount,
    }
    if success is not None:
        record["success"] = success
    return record


HOUR = 3600
DAY = 86400


class TimeBucketStatsTest(unittest.TestCase):
    def setUp(self):
        self.records = [
            rec("h1", 1, 10, "alice", "bob", "transfer", "10", success=True),
            rec("h2", 2, 100, "alice", "carol", "approve", "20",
                success=False),
            rec("h3", 3, 3599, "bob", "alice", "transfer", "30"),
            rec("h4", 4, 3600, "carol", "dave", "transfer", "40",
                success=False),
            rec("h5", 5, 7200, "dave", "alice", "approve", "50",
                success=True),
            rec("h6", 6, DAY + 100, "alice", "bob", "transfer", "60",
                success=False),
        ]
        self.idx = TxIndexer(self.records)

    def test_hour_buckets_continuous_with_zero_fill(self):
        result = self.idx.time_bucket_stats(0, 3 * HOUR, bucket="hour")
        self.assertEqual(result["total_buckets"], 3)
        self.assertIsNone(result["next_cursor"])
        self.assertEqual(
            result["buckets"],
            [
                {"bucket_start": 0, "total_count": 3,
                 "success_count": 2, "failure_count": 1},
                {"bucket_start": 3600, "total_count": 1,
                 "success_count": 0, "failure_count": 1},
                {"bucket_start": 7200, "total_count": 1,
                 "success_count": 1, "failure_count": 0},
            ],
        )

    def test_empty_bucket_in_range_returned_with_zeros(self):
        result = self.idx.time_bucket_stats(0, 4 * HOUR, bucket="hour")
        self.assertEqual(result["buckets"][3],
                         {"bucket_start": 10800, "total_count": 0,
                          "success_count": 0, "failure_count": 0})

    def test_day_buckets_utc_aligned(self):
        result = self.idx.time_bucket_stats(0, 2 * DAY, bucket="day")
        self.assertEqual(
            [b["bucket_start"] for b in result["buckets"]],
            [0, DAY],
        )
        self.assertEqual(result["buckets"][0]["total_count"], 5)
        self.assertEqual(result["buckets"][1]["total_count"], 1)
        self.assertEqual(result["buckets"][1]["failure_count"], 1)

    def test_start_inclusive_end_exclusive(self):
        # 起点落在桶内：首桶只统计 >= start_time 的交易；
        # 终点恰好等于交易时间：该交易不计入
        result = self.idx.time_bucket_stats(100, 3600, bucket="hour")
        self.assertEqual(result["total_buckets"], 1)
        self.assertEqual(result["buckets"][0]["bucket_start"], 0)
        self.assertEqual(result["buckets"][0]["total_count"], 2)
        self.assertEqual(result["buckets"][0]["failure_count"], 1)

    def test_first_bucket_aligned_to_hour_not_to_start(self):
        result = self.idx.time_bucket_stats(5000, 8000, bucket="hour")
        self.assertEqual(
            [b["bucket_start"] for b in result["buckets"]],
            [3600, 7200],
        )

    def test_missing_success_field_counts_as_success(self):
        result = self.idx.time_bucket_stats(0, HOUR, bucket="hour")
        # h3 无 success 字段，按成功计
        self.assertEqual(result["buckets"][0]["success_count"], 2)

    def test_address_filter_matches_either_side(self):
        result = self.idx.time_bucket_stats(
            0, 3 * HOUR, address="carol", bucket="hour"
        )
        counts = [b["total_count"] for b in result["buckets"]]
        self.assertEqual(counts, [1, 1, 0])

    def test_method_filter(self):
        result = self.idx.time_bucket_stats(
            0, 3 * HOUR, method="approve", bucket="hour"
        )
        counts = [b["total_count"] for b in result["buckets"]]
        self.assertEqual(counts, [1, 0, 1])

    def test_no_match_returns_zero_filled_buckets(self):
        result = self.idx.time_bucket_stats(
            0, 2 * HOUR, address="nobody", bucket="hour"
        )
        self.assertEqual(result["total_buckets"], 2)
        for bucket in result["buckets"]:
            self.assertEqual(
                bucket,
                {"bucket_start": bucket["bucket_start"], "total_count": 0,
                 "success_count": 0, "failure_count": 0},
            )

    def test_pagination_no_overlap_no_gap(self):
        seen = []
        cursor = None
        pages = 0
        while True:
            result = self.idx.time_bucket_stats(
                0, 5 * HOUR, bucket="hour", page_size=2, cursor=cursor
            )
            seen.extend(b["bucket_start"] for b in result["buckets"])
            pages += 1
            cursor = result["next_cursor"]
            if cursor is None:
                break
            self.assertLess(pages, 10)
        self.assertEqual(
            seen, [0, 3600, 7200, 10800, 14400]
        )
        self.assertEqual(result["total_buckets"], 5)

    def test_page_size_not_bound_to_cursor(self):
        first = self.idx.time_bucket_stats(0, 5 * HOUR, bucket="hour",
                                           page_size=1)
        rest = self.idx.time_bucket_stats(0, 5 * HOUR, bucket="hour",
                                          page_size=100,
                                          cursor=first["next_cursor"])
        self.assertEqual(len(rest["buckets"]), 4)
        self.assertIsNone(rest["next_cursor"])

    def test_cursor_beyond_end_returns_empty_page(self):
        first = self.idx.time_bucket_stats(0, 2 * HOUR, bucket="hour",
                                           page_size=2)
        self.assertIsNone(first["next_cursor"])
        # 用最后一桶之后的游标续页：空页、空 next_cursor
        from tx_indexer.cursor import encode_time_bucket_stats_cursor
        cursor = encode_time_bucket_stats_cursor(
            {"address": None, "method": None, "start_time": 0,
             "end_time": 2 * HOUR},
            "hour",
            10 * HOUR,
        )
        result = self.idx.time_bucket_stats(0, 2 * HOUR, bucket="hour",
                                            cursor=cursor)
        self.assertEqual(result["buckets"], [])
        self.assertIsNone(result["next_cursor"])

    def test_cursor_binds_filters_and_bucket(self):
        first = self.idx.time_bucket_stats(0, 5 * HOUR, bucket="hour",
                                           page_size=2)
        cursor = first["next_cursor"]
        with self.assertRaises(InvalidAggregationCursor):
            self.idx.time_bucket_stats(0, 5 * HOUR, bucket="day",
                                       cursor=cursor)
        with self.assertRaises(InvalidAggregationCursor):
            self.idx.time_bucket_stats(0, 6 * HOUR, bucket="hour",
                                       cursor=cursor)
        with self.assertRaises(InvalidAggregationCursor):
            self.idx.time_bucket_stats(0, 5 * HOUR, address="alice",
                                       bucket="hour", cursor=cursor)
        with self.assertRaises(InvalidAggregationCursor):
            self.idx.time_bucket_stats(0, 5 * HOUR, method="transfer",
                                       bucket="hour", cursor=cursor)

    def test_cursor_tampered_or_garbage(self):
        with self.assertRaises(InvalidAggregationCursor):
            self.idx.time_bucket_stats(0, 5 * HOUR, cursor="not-a-cursor")
        with self.assertRaises(InvalidAggregationCursor):
            self.idx.time_bucket_stats(0, 5 * HOUR, cursor="")
        with self.assertRaises(InvalidAggregationCursor):
            self.idx.time_bucket_stats(0, 5 * HOUR, cursor=123)
        first = self.idx.time_bucket_stats(0, 5 * HOUR, bucket="hour",
                                           page_size=2)
        tampered = first["next_cursor"][:-2] + "xx"
        with self.assertRaises(InvalidAggregationCursor):
            self.idx.time_bucket_stats(0, 5 * HOUR, bucket="hour",
                                       cursor=tampered)

    def test_cross_command_cursor_rejected(self):
        from tx_indexer.engine import normalize_filters
        page = self.idx.query(normalize_filters(), page_size=2)
        with self.assertRaises(InvalidAggregationCursor):
            self.idx.time_bucket_stats(0, 5 * HOUR,
                                       cursor=page["next_cursor"])
        stats_page = self.idx.time_stats(
            normalize_filters(), 3600, page_size=1
        )
        with self.assertRaises(InvalidAggregationCursor):
            self.idx.time_bucket_stats(0, 5 * HOUR,
                                       cursor=stats_page["next_cursor"])

    def test_invalid_range(self):
        with self.assertRaises(InvalidAggregationRange):
            self.idx.time_bucket_stats(100, 100)
        with self.assertRaises(InvalidAggregationRange):
            self.idx.time_bucket_stats(200, 100)
        with self.assertRaises(InvalidAggregationRange):
            self.idx.time_bucket_stats(-1, 100)
        with self.assertRaises(InvalidAggregationRange):
            self.idx.time_bucket_stats(0, "100")
        with self.assertRaises(InvalidAggregationRange):
            self.idx.time_bucket_stats(0, True)

    def test_unsupported_bucket(self):
        for bad in ("minute", "HOUR", "", 3600, None):
            with self.assertRaises(UnsupportedAggregationBucket):
                self.idx.time_bucket_stats(0, 100, bucket=bad)

    def test_invalid_filter(self):
        for bad in ("", "   ", 7):
            with self.assertRaises(InvalidAggregationFilter):
                self.idx.time_bucket_stats(0, 100, address=bad)
            with self.assertRaises(InvalidAggregationFilter):
                self.idx.time_bucket_stats(0, 100, method=bad)

    def test_invalid_page_size(self):
        with self.assertRaises(InvalidPageSizeError):
            self.idx.time_bucket_stats(0, 100, page_size=0)

    def test_existing_queries_unaffected(self):
        from tx_indexer.engine import normalize_filters
        page = self.idx.query(normalize_filters())
        self.assertEqual(page["total"], 6)
        stats = self.idx.stats(normalize_filters())
        self.assertEqual(stats["total_count"], 6)
        self.assertEqual(stats["total_amount"], "210")


if __name__ == "__main__":
    unittest.main()
