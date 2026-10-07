"""address-flow-time-series（address_flow_time_series）测试。

覆盖：必填观察地址、连续 hour/day 桶与零填充、纪元对齐、左闭右开、
发送/接收/自转账计数与金额字符串（net_amount 可负）、沿用 query 全部
筛选、total_points / total_buckets、游标分页（不重复不遗漏、绑定地址 /
筛选 / 时间窗 / 桶粒度、篡改 / 跨命令复用拒绝），以及四类独立序列异常。
"""

import base64
import json
import unittest

from tx_indexer.cursor import (
    encode_address_flow_time_series_cursor,
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
    InvalidFlowSeriesCursorError,
    InvalidFlowSeriesFilterError,
    InvalidFlowSeriesRangeError,
    InvalidPageSizeError,
    UnsupportedFlowSeriesBucketError,
)

HOUR = 3600
DAY = 86400

ZERO_POINT = {
    "sent_count": 0,
    "received_count": 0,
    "total_count": 0,
    "sent_amount": "0",
    "received_amount": "0",
    "net_amount": "0",
}


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


class AddressFlowTimeSeriesTest(unittest.TestCase):
    def setUp(self):
        self.records = [
            rec("a", 0, "alice", "bob", "transfer", "10"),
            rec("b", HOUR - 1, "alice", "carol", "approve", "5"),
            rec("c", HOUR, "bob", "alice", "transfer", "7"),
            rec("d", 2 * HOUR, "alice", "alice", "transfer", "3"),
            rec("e", 3 * HOUR + 10, "carol", "dave", "transfer", "9"),
        ]
        self.idx = TxIndexer(self.records)
        self.filters = normalize_filters(address="alice")

    def series(self, *args, **kwargs):
        return self.idx.address_flow_time_series(
            self.filters, *args, **kwargs
        )

    def test_hour_series_continuous_zero_filled(self):
        result = self.series(0, 4 * HOUR, "hour")
        self.assertEqual(result["total_points"], 4)
        self.assertEqual(result["total_buckets"], 4)
        self.assertIsNone(result["next_cursor"])
        self.assertEqual(
            [p["bucket_start"] for p in result["series"]],
            [0, HOUR, 2 * HOUR, 3 * HOUR],
        )
        # 空桶零填充
        self.assertEqual(
            result["series"][3],
            dict(ZERO_POINT, bucket_start=3 * HOUR),
        )

    def test_point_fields_and_order(self):
        result = self.series(0, HOUR, "hour")
        self.assertEqual(
            list(result.keys()),
            ["series", "total_points", "total_buckets", "next_cursor"],
        )
        self.assertEqual(
            list(result["series"][0].keys()),
            ["bucket_start", "sent_count", "received_count", "total_count",
             "sent_amount", "received_amount", "net_amount"],
        )

    def test_sent_received_and_net(self):
        result = self.series(0, 4 * HOUR, "hour")
        by_bucket = {p["bucket_start"]: p for p in result["series"]}
        # a(alice→bob 10) + b(alice→carol 5)：第 0 桶两笔发送
        self.assertEqual(
            by_bucket[0],
            {"bucket_start": 0, "sent_count": 2, "received_count": 0,
             "total_count": 2, "sent_amount": "15", "received_amount": "0",
             "net_amount": "-15"},
        )
        # c(bob→alice 7)：第 1 桶一笔接收
        self.assertEqual(
            by_bucket[HOUR],
            {"bucket_start": HOUR, "sent_count": 0, "received_count": 1,
             "total_count": 1, "sent_amount": "0", "received_amount": "7",
             "net_amount": "7"},
        )
        # d(alice→alice 3) 自转账：两侧各累计一次，total_count 一次
        self.assertEqual(
            by_bucket[2 * HOUR],
            {"bucket_start": 2 * HOUR, "sent_count": 1, "received_count": 1,
             "total_count": 1, "sent_amount": "3", "received_amount": "3",
             "net_amount": "0"},
        )

    def test_amounts_are_plain_decimal_strings(self):
        records = [
            rec("a", 0, "alice", "bob", amount="007"),
            rec("b", 1, "bob", "alice", amount="13"),
        ]
        result = TxIndexer(records).address_flow_time_series(
            normalize_filters(address="alice"), 0, HOUR, "hour"
        )
        point = result["series"][0]
        self.assertEqual(point["sent_amount"], "7")
        self.assertEqual(point["received_amount"], "13")
        self.assertEqual(point["net_amount"], "6")

    def test_start_inclusive_end_exclusive(self):
        # timestamp == start_time 计入；timestamp == end_time 不计入
        result = self.series(HOUR, 2 * HOUR, "hour")
        self.assertEqual(result["total_buckets"], 1)
        point = result["series"][0]
        self.assertEqual(point["bucket_start"], HOUR)
        self.assertEqual(point["received_count"], 1)
        self.assertEqual(point["received_amount"], "7")

    def test_first_bucket_aligned_before_start(self):
        # start 落在小时中间：首桶仍按纪元整点对齐（可早于 start），
        # 且只统计 start 之后的数据（a 在 0 被排除，b 在 HOUR-1 命中）
        result = self.series(10, 2 * HOUR, "hour")
        self.assertEqual(result["total_buckets"], 2)
        first = result["series"][0]
        self.assertEqual(first["bucket_start"], 0)
        self.assertEqual(first["sent_count"], 1)
        self.assertEqual(first["sent_amount"], "5")
        self.assertEqual(first["total_count"], 1)

    def test_last_bucket_covers_only_before_end(self):
        # end 落在小时中间：末桶只覆盖 end 之前的数据
        result = self.series(2 * HOUR, 3 * HOUR + 5, "hour")
        self.assertEqual(result["total_buckets"], 2)
        by_bucket = {p["bucket_start"]: p for p in result["series"]}
        self.assertEqual(by_bucket[2 * HOUR]["total_count"], 1)
        # e（3*HOUR+10）在 end 之外，末桶为空
        self.assertEqual(by_bucket[3 * HOUR]["total_count"], 0)

    def test_day_buckets_utc(self):
        records = [
            rec("a", 0, "alice", "bob", amount="10"),
            rec("b", DAY - 1, "bob", "alice", amount="4"),
            rec("c", DAY, "alice", "alice", amount="3"),
        ]
        result = TxIndexer(records).address_flow_time_series(
            normalize_filters(address="alice"), 0, 2 * DAY, "day"
        )
        self.assertEqual(result["total_buckets"], 2)
        first, second = result["series"]
        self.assertEqual(first["bucket_start"], 0)
        self.assertEqual(
            (first["sent_count"], first["received_count"],
             first["total_count"]),
            (1, 1, 2),
        )
        self.assertEqual(first["net_amount"], "-6")
        # 自转账日桶：两侧各一次、total_count 一次、net 为 0
        self.assertEqual(
            (second["sent_count"], second["received_count"],
             second["total_count"], second["net_amount"]),
            (1, 1, 1, "0"),
        )

    def test_day_alignment_with_unaligned_range(self):
        result = self.series(HOUR, DAY + HOUR, "day")
        self.assertEqual(
            [p["bucket_start"] for p in result["series"]], [0, DAY]
        )
        self.assertEqual(result["total_buckets"], 2)

    def test_no_hit_returns_zero_series(self):
        # 无命中交易：仍返回完整连续桶的零值序列
        result = self.idx.address_flow_time_series(
            normalize_filters(address="nobody"), 0, 2 * HOUR, "hour"
        )
        self.assertEqual(result["total_points"], 2)
        self.assertEqual(result["total_buckets"], 2)
        self.assertIsNone(result["next_cursor"])
        self.assertEqual(
            result["series"],
            [dict(ZERO_POINT, bucket_start=0),
             dict(ZERO_POINT, bucket_start=HOUR)],
        )

    def test_empty_window_range_returns_zero_series(self):
        result = self.series(10 * HOUR, 12 * HOUR, "hour")
        self.assertEqual(result["total_points"], 2)
        self.assertEqual(
            result["series"],
            [dict(ZERO_POINT, bucket_start=10 * HOUR),
             dict(ZERO_POINT, bucket_start=11 * HOUR)],
        )

    def test_query_filters_reused(self):
        # method / 金额 / 区块 / status 均沿用 query 筛选语义
        result = self.idx.address_flow_time_series(
            normalize_filters(address="alice", method="approve"),
            0, 4 * HOUR, "hour",
        )
        counts = [p["sent_count"] for p in result["series"]]
        self.assertEqual(counts, [1, 0, 0, 0])

        result = self.idx.address_flow_time_series(
            normalize_filters(address="alice", min_amount="6"),
            0, 4 * HOUR, "hour",
        )
        # 只有 a(10)、c(7) 命中
        self.assertEqual(result["series"][0]["sent_amount"], "10")
        self.assertEqual(result["series"][1]["received_amount"], "7")
        self.assertEqual(result["series"][2]["total_count"], 0)

        result = self.idx.address_flow_time_series(
            normalize_filters(address="alice", min_block=2),
            0, 4 * HOUR, "hour",
        )
        self.assertEqual(
            [p["total_count"] for p in result["series"]], [0, 0, 0, 0]
        )

        result = self.idx.address_flow_time_series(
            normalize_filters(address="alice", status="failure"),
            0, 4 * HOUR, "hour",
        )
        self.assertEqual(
            [p["total_count"] for p in result["series"]], [0, 0, 0, 0]
        )

    def test_pagination_no_skip_no_dup(self):
        collected = []
        cursor = None
        pages = 0
        while True:
            page = self.series(0, 5 * HOUR, "hour", page_size=2,
                               cursor=cursor)
            pages += 1
            self.assertEqual(page["total_points"], 5)
            self.assertEqual(page["total_buckets"], 5)
            collected.extend(page["series"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(pages, 3)
        self.assertEqual(
            [p["bucket_start"] for p in collected],
            list(range(0, 5 * HOUR, HOUR)),
        )

    def test_page_beyond_last_is_empty_with_null_cursor(self):
        page1 = self.series(0, 2 * HOUR, "hour", page_size=100)
        self.assertIsNone(page1["next_cursor"])
        # 手工构造指向最后一桶的游标：下一页必须是空页 + 空游标
        cursor = encode_address_flow_time_series_cursor(
            self.filters, 0, 2 * HOUR, "hour", HOUR
        )
        page = self.series(0, 2 * HOUR, "hour", page_size=100,
                           cursor=cursor)
        self.assertEqual(page["series"], [])
        self.assertEqual(page["total_points"], 2)
        self.assertIsNone(page["next_cursor"])

    def test_cursor_not_bound_to_page_size(self):
        page1 = self.series(0, 5 * HOUR, "hour", page_size=1)
        self.assertEqual(
            [p["bucket_start"] for p in page1["series"]], [0]
        )
        page2 = self.series(0, 5 * HOUR, "hour", page_size=100,
                            cursor=page1["next_cursor"])
        self.assertEqual(len(page2["series"]), 4)
        self.assertIsNone(page2["next_cursor"])

    def test_cursor_bound_to_address_and_filters(self):
        page1 = self.series(0, 5 * HOUR, "hour", page_size=1)
        # 改观察地址
        with self.assertRaises(InvalidFlowSeriesCursorError):
            self.idx.address_flow_time_series(
                normalize_filters(address="bob"), 0, 5 * HOUR, "hour",
                page_size=1, cursor=page1["next_cursor"],
            )
        # 加筛选
        with self.assertRaises(InvalidFlowSeriesCursorError):
            self.idx.address_flow_time_series(
                normalize_filters(address="alice", method="approve"),
                0, 5 * HOUR, "hour",
                page_size=1, cursor=page1["next_cursor"],
            )
        # 状态筛选同样进入绑定
        page2 = self.idx.address_flow_time_series(
            normalize_filters(address="alice", status="success"),
            0, 5 * HOUR, "hour", page_size=1,
        )
        with self.assertRaises(InvalidFlowSeriesCursorError):
            self.idx.address_flow_time_series(
                normalize_filters(address="alice", status="failure"),
                0, 5 * HOUR, "hour",
                page_size=1, cursor=page2["next_cursor"],
            )

    def test_cursor_bound_to_time_range_and_bucket(self):
        page1 = self.series(0, 5 * HOUR, "hour", page_size=1)
        with self.assertRaises(InvalidFlowSeriesCursorError):
            self.series(0, 6 * HOUR, "hour", page_size=1,
                        cursor=page1["next_cursor"])
        with self.assertRaises(InvalidFlowSeriesCursorError):
            self.series(HOUR, 5 * HOUR, "hour", page_size=1,
                        cursor=page1["next_cursor"])
        with self.assertRaises(InvalidFlowSeriesCursorError):
            self.series(0, 5 * HOUR, "day", page_size=1,
                        cursor=page1["next_cursor"])

    def test_cross_command_cursor_rejected(self):
        filters = normalize_filters()
        tokens = [
            encode_time_stats_cursor(filters, HOUR, 0),
            encode_time_bucket_aggregation_cursor(
                {"address": None, "method": None,
                 "start_time": 0, "end_time": HOUR},
                "hour", 0,
            ),
            encode_method_time_series_cursor(
                filters, 0, HOUR, "hour", 0, "transfer"
            ),
        ]
        for token in tokens:
            with self.assertRaises(InvalidFlowSeriesCursorError):
                self.series(0, HOUR, "hour", cursor=token)

    def test_cursor_garbage(self):
        for bad in ("", "not-base64!!!", "bm9wZQ", "%%%"):
            with self.assertRaises(InvalidFlowSeriesCursorError):
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
        tampered["after"] = 7
        with self.assertRaises(InvalidFlowSeriesCursorError):
            self.series(0, 5 * HOUR, "hour", cursor=reencode(tampered))
        tampered = dict(payload)
        tampered["after"] = 100 * HOUR
        with self.assertRaises(InvalidFlowSeriesCursorError):
            self.series(0, 5 * HOUR, "hour", cursor=reencode(tampered))
        # 篡改桶粒度
        tampered = dict(payload)
        tampered["g"] = "day"
        with self.assertRaises(InvalidFlowSeriesCursorError):
            self.series(0, 5 * HOUR, "hour", cursor=reencode(tampered))
        # 篡改时间窗
        tampered = dict(payload)
        tampered["w"] = [0, 6 * HOUR]
        with self.assertRaises(InvalidFlowSeriesCursorError):
            self.series(0, 5 * HOUR, "hour", cursor=reencode(tampered))
        # 篡改地址
        tampered = dict(payload)
        tampered["f"] = dict(payload["f"], address="mallory")
        with self.assertRaises(InvalidFlowSeriesCursorError):
            self.series(0, 5 * HOUR, "hour", cursor=reencode(tampered))
        # 篡改版本
        tampered = dict(payload)
        tampered["v"] = 99
        with self.assertRaises(InvalidFlowSeriesCursorError):
            self.series(0, 5 * HOUR, "hour", cursor=reencode(tampered))

    def test_missing_address_is_filter_error(self):
        for filters in (
            normalize_filters(),
            normalize_filters(method="transfer"),
        ):
            with self.assertRaises(InvalidFlowSeriesFilterError):
                self.idx.address_flow_time_series(
                    filters, 0, HOUR, "hour"
                )

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
            with self.assertRaises(InvalidFlowSeriesRangeError):
                self.series(*args)

    def test_unsupported_bucket(self):
        for bad in ("week", "HOUR", "Hour", "days", "", None, 3600,
                    1.0, True, ["hour"], {"hour": 1}):
            with self.assertRaises(UnsupportedFlowSeriesBucketError):
                self.series(0, HOUR, bad)

    def test_invalid_page_size_remains_page_size_error(self):
        for bad in (0, -1, 1001, "10", None, True):
            with self.assertRaises(InvalidPageSizeError):
                self.series(0, HOUR, "hour", page_size=bad)

    def test_error_raises_before_any_page_data(self):
        with self.assertRaises(InvalidFlowSeriesRangeError):
            self.series(5, 5, "hour")
        with self.assertRaises(InvalidFlowSeriesCursorError):
            self.series(0, HOUR, "hour", cursor="@@@not-a-cursor@@@")

    def test_default_page_size_constant(self):
        self.assertEqual(DEFAULT_PAGE_SIZE, 100)

    def test_single_second_range(self):
        # 最小非空范围：[0,1)，只有 timestamp 0 的 a（alice→bob 10）命中
        result = self.series(0, 1, "hour")
        self.assertEqual(result["total_buckets"], 1)
        point = result["series"][0]
        self.assertEqual(point["bucket_start"], 0)
        self.assertEqual(point["sent_count"], 1)
        self.assertEqual(point["sent_amount"], "10")
        self.assertEqual(point["net_amount"], "-10")

    def test_existing_query_and_stats_unchanged(self):
        # 新功能不改变既有入口：query 左闭右闭、stats 口径保持
        page = self.idx.query(normalize_filters())
        self.assertEqual(page["total"], 5)
        stats = self.idx.stats(normalize_filters())
        self.assertEqual(stats["total_count"], 5)
        self.assertEqual(stats["total_amount"], "34")


if __name__ == "__main__":
    unittest.main()
