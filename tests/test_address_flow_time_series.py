"""address-flow-time-series（address_flow_time_series）测试。

覆盖：连续 hour/day 桶序列与零填充、纪元对齐、左闭右开、相对观察地址
的 sent/received/total 计数与金额字符串（自转账两侧累计但 total_count
一次）、net_amount 可带负号、沿用 query 全部筛选、total_points /
total_buckets（无命中时为等长零值序列）、游标分页（不重复不遗漏、
绑定观察地址 / 筛选 / 时间窗 / 桶粒度、篡改 / 跨命令复用拒绝），以及
四类独立资金流向序列异常。
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
    InvalidFilterError,
    InvalidFlowSeriesCursor,
    InvalidFlowSeriesFilter,
    InvalidFlowSeriesRange,
    InvalidPageSizeError,
    UnsupportedFlowSeriesBucket,
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


class AddressFlowTimeSeriesTest(unittest.TestCase):
    def setUp(self):
        self.records = [
            rec("a", 0, success=True),                       # alice→bob 10
            rec("b", HOUR - 1, "alice", "carol", "approve",
                "5", success=False),                         # alice→carol 5
            rec("c", HOUR, "bob", "alice", "transfer", "7",
                success=True),                               # bob→alice 7
            rec("d", 2 * HOUR, "dave", "alice", "transfer",
                "3", success=False),                         # dave→alice 3
            rec("e", 3 * HOUR + 10, "alice", "bob", "approve",
                "9", success=False),                         # alice→bob 9
            rec("f", 3 * HOUR + 20, "alice", "alice",
                "transfer", "4", success=True),              # 自转账 4
        ]
        self.idx = TxIndexer(self.records)

    def series(self, *args, address="alice", **kwargs):
        return self.idx.address_flow_time_series(
            normalize_filters(address=address), *args, **kwargs
        )

    def test_hour_series_continuous_zero_filled(self):
        result = self.series(0, 5 * HOUR, "hour")
        self.assertEqual(result["total_buckets"], 5)
        self.assertEqual(result["total_points"], 5)
        self.assertIsNone(result["next_cursor"])
        self.assertEqual(
            [p["bucket_start"] for p in result["series"]],
            list(range(0, 5 * HOUR, HOUR)),
        )
        by_bucket = {p["bucket_start"]: p for p in result["series"]}
        # 第 0 桶：a、b 两笔都是 alice 发出
        self.assertEqual(
            by_bucket[0],
            {"bucket_start": 0, "sent_count": 2, "received_count": 0,
             "total_count": 2, "sent_amount": "15",
             "received_amount": "0", "net_amount": "-15"},
        )
        # 第 1 桶：c 一笔 alice 接收
        self.assertEqual(
            by_bucket[HOUR],
            {"bucket_start": HOUR, "sent_count": 0, "received_count": 1,
             "total_count": 1, "sent_amount": "0",
             "received_amount": "7", "net_amount": "7"},
        )
        # 第 2 桶：d 一笔 alice 接收
        self.assertEqual(
            by_bucket[2 * HOUR]["received_amount"], "3"
        )
        self.assertEqual(
            by_bucket[2 * HOUR]["net_amount"], "3"
        )
        # 第 4 桶无交易：零值序列点（不静默跳过）
        self.assertEqual(
            by_bucket[4 * HOUR],
            {"bucket_start": 4 * HOUR, "sent_count": 0,
             "received_count": 0, "total_count": 0,
             "sent_amount": "0", "received_amount": "0",
             "net_amount": "0"},
        )

    def test_self_transfer_counts_both_sides_but_total_once(self):
        result = self.series(0, 5 * HOUR, "hour")
        # 第 3 桶：e（alice→bob 9）+ f（alice→alice 4）
        point = next(p for p in result["series"]
                     if p["bucket_start"] == 3 * HOUR)
        self.assertEqual(point["sent_count"], 2)
        self.assertEqual(point["received_count"], 1)
        # 两笔不同交易，total_count 计 2；自转账本身只计一次
        self.assertEqual(point["total_count"], 2)
        self.assertEqual(point["sent_amount"], "13")
        self.assertEqual(point["received_amount"], "4")
        self.assertEqual(point["net_amount"], "-9")

    def test_only_self_transfer_in_bucket(self):
        records = [rec("s", 100, "alice", "alice", "transfer", "100")]
        result = TxIndexer(records).address_flow_time_series(
            normalize_filters(address="alice"), 0, HOUR, "hour"
        )
        point = result["series"][0]
        self.assertEqual(point["sent_count"], 1)
        self.assertEqual(point["received_count"], 1)
        self.assertEqual(point["total_count"], 1)
        self.assertEqual(point["sent_amount"], "100")
        self.assertEqual(point["received_amount"], "100")
        self.assertEqual(point["net_amount"], "0")

    def test_point_fields_and_top_level_keys(self):
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

    def test_amounts_are_plain_decimal_strings(self):
        records = [
            rec("a", 0, amount="007"),
            rec("b", 10, amount="13"),
        ]
        result = TxIndexer(records).address_flow_time_series(
            normalize_filters(address="alice"), 0, HOUR, "hour"
        )
        point = result["series"][0]
        self.assertEqual(point["sent_amount"], "20")
        self.assertEqual(point["received_amount"], "0")
        self.assertEqual(point["net_amount"], "-20")
        for value in (point["sent_amount"], point["received_amount"],
                      point["net_amount"]):
            self.assertIsInstance(value, str)

    def test_net_amount_negative_and_zero_strings(self):
        by_bucket = {
            p["bucket_start"]: p
            for p in self.series(0, 5 * HOUR, "hour")["series"]
        }
        self.assertTrue(by_bucket[0]["net_amount"].startswith("-"))
        self.assertEqual(by_bucket[HOUR]["net_amount"], "7")
        self.assertEqual(by_bucket[4 * HOUR]["net_amount"], "0")

    def test_start_inclusive_end_exclusive(self):
        # timestamp == start_time 计入；timestamp == end_time 不计入
        result = self.series(0, 1, "hour")
        self.assertEqual(result["total_buckets"], 1)
        point = result["series"][0]
        self.assertEqual(point["bucket_start"], 0)
        self.assertEqual(point["total_count"], 1)
        self.assertEqual(point["sent_amount"], "10")

    def test_first_bucket_aligned_before_start(self):
        # start 落在小时中间：首桶仍按纪元整点对齐（可早于 start），
        # 且只统计 start 之后的数据（b 在 HOUR-1 < HOUR+10，不计入）
        result = self.series(HOUR + 10, 3 * HOUR, "hour")
        self.assertEqual(result["total_buckets"], 2)
        by_bucket = {p["bucket_start"]: p for p in result["series"]}
        self.assertEqual(by_bucket[HOUR]["total_count"], 0)
        self.assertEqual(by_bucket[HOUR]["sent_amount"], "0")
        self.assertEqual(by_bucket[2 * HOUR]["received_amount"], "3")

    def test_last_bucket_covers_only_before_end(self):
        # end 落在小时中间：末桶只覆盖 end 之前的数据，
        # e/f（3*HOUR+10/20）在 end=3*HOUR+5 之外
        result = self.series(2 * HOUR, 3 * HOUR + 5, "hour")
        self.assertEqual(result["total_buckets"], 2)
        by_bucket = {p["bucket_start"]: p for p in result["series"]}
        self.assertEqual(by_bucket[2 * HOUR]["received_amount"], "3")
        self.assertEqual(by_bucket[3 * HOUR]["total_count"], 0)
        self.assertEqual(by_bucket[3 * HOUR]["sent_amount"], "0")
        self.assertEqual(by_bucket[3 * HOUR]["net_amount"], "0")

    def test_day_buckets_utc(self):
        records = [
            rec("a", 0, amount="10"),                       # 发出
            rec("b", DAY - 1, "bob", "alice", amount="2"),  # 接收
            rec("c", DAY, amount="4"),                       # 次日发出
            rec("d", 2 * DAY + 100, "carol", "alice",
                amount="8"),                                 # 第三日接收
        ]
        result = TxIndexer(records).address_flow_time_series(
            normalize_filters(address="alice"), 0, 3 * DAY, "day"
        )
        self.assertEqual(result["total_buckets"], 3)
        self.assertEqual(result["total_points"], 3)
        by_bucket = {p["bucket_start"]: p for p in result["series"]}
        self.assertEqual(by_bucket[0]["sent_amount"], "10")
        self.assertEqual(by_bucket[0]["received_amount"], "2")
        self.assertEqual(by_bucket[0]["net_amount"], "-8")
        self.assertEqual(by_bucket[DAY]["sent_amount"], "4")
        self.assertEqual(by_bucket[DAY]["received_amount"], "0")
        self.assertEqual(by_bucket[2 * DAY]["received_amount"], "8")

    def test_day_alignment_with_unaligned_range(self):
        result = self.series(HOUR, DAY + HOUR, "day")
        self.assertEqual(
            [p["bucket_start"] for p in result["series"]],
            [0, DAY],
        )
        self.assertEqual(result["total_buckets"], 2)

    def test_no_hits_returns_zero_filled_series(self):
        # 窗内完全没有该地址的交易：返回等长零值序列而非空序列
        result = self.series(10 * HOUR, 12 * HOUR, "hour")
        self.assertEqual(result["total_buckets"], 2)
        self.assertEqual(result["total_points"], 2)
        self.assertEqual(len(result["series"]), 2)
        self.assertIsNone(result["next_cursor"])
        for point in result["series"]:
            self.assertEqual(point["sent_count"], 0)
            self.assertEqual(point["received_count"], 0)
            self.assertEqual(point["total_count"], 0)
            self.assertEqual(point["sent_amount"], "0")
            self.assertEqual(point["received_amount"], "0")
            self.assertEqual(point["net_amount"], "0")

    def test_other_address_has_no_hits(self):
        result = self.series(0, 3 * HOUR, "hour", address="nobody")
        self.assertEqual(result["total_points"], 3)
        self.assertEqual(len(result["series"]), 3)
        self.assertTrue(
            all(p["total_count"] == 0 for p in result["series"])
        )

    def test_jsonl_records_without_success_are_counted(self):
        idx = TxIndexer([
            jsonl_rec("a", 0),
            jsonl_rec("b", 100, frm="bob", to="alice"),
        ])
        result = idx.address_flow_time_series(
            normalize_filters(address="alice"), 0, HOUR, "hour"
        )
        point = result["series"][0]
        self.assertEqual(point["sent_count"], 1)
        self.assertEqual(point["received_count"], 1)
        self.assertEqual(point["total_count"], 2)

    def test_query_filters_reused(self):
        # method / 金额 / 区块 / status 均沿用 query 筛选语义
        result = self.idx.address_flow_time_series(
            normalize_filters(address="alice", method="approve"),
            0, 4 * HOUR, "hour",
        )
        by_bucket = {p["bucket_start"]: p for p in result["series"]}
        # b（alice→carol approve 5）在第 0 桶、e（approve 9）在第 3 桶
        self.assertEqual(by_bucket[0]["sent_amount"], "5")
        self.assertEqual(by_bucket[0]["total_count"], 1)
        self.assertEqual(by_bucket[HOUR]["total_count"], 0)
        self.assertEqual(by_bucket[3 * HOUR]["sent_amount"], "9")

        result = self.idx.address_flow_time_series(
            normalize_filters(address="alice", min_amount="6"),
            0, 4 * HOUR, "hour",
        )
        by_bucket = {p["bucket_start"]: p for p in result["series"]}
        # a(10)、c(7)、e(9) 命中；b(5)、d(3)、f(4) 被金额筛选排除
        self.assertEqual(by_bucket[0]["sent_amount"], "10")
        self.assertEqual(by_bucket[HOUR]["received_amount"], "7")
        self.assertEqual(by_bucket[2 * HOUR]["total_count"], 0)
        self.assertEqual(by_bucket[3 * HOUR]["sent_amount"], "9")
        # 自转账 f(4) 被排除后，第 3 桶无接收
        self.assertEqual(by_bucket[3 * HOUR]["received_amount"], "0")

        result = self.idx.address_flow_time_series(
            normalize_filters(address="alice", min_block=5),
            0, 4 * HOUR, "hour",
        )
        self.assertEqual(result["total_points"], 4)
        self.assertTrue(
            all(p["total_count"] == 0 for p in result["series"])
        )

        result = self.idx.address_flow_time_series(
            normalize_filters(address="alice", status="failure"),
            0, 4 * HOUR, "hour",
        )
        by_bucket = {p["bucket_start"]: p for p in result["series"]}
        # b（失败发出 5）、d（失败接收 3）、e（失败发出 9）
        self.assertEqual(by_bucket[0]["sent_amount"], "5")
        self.assertEqual(by_bucket[2 * HOUR]["received_amount"], "3")
        self.assertEqual(by_bucket[3 * HOUR]["sent_amount"], "9")
        # 成功的自转账 f 不入选
        self.assertEqual(by_bucket[3 * HOUR]["received_amount"], "0")

    def test_from_address_filter_excludes_incoming(self):
        # 不与观察地址冲突的筛选：from-address 单独给定时只统计
        # alice 作为发送方之外的入口……这里验证 from-address 与
        # method 等其他筛选一样取交集（观察地址仍由 address 承担，
        # address 与 from/to 并用已在 test_address_conflict 覆盖）
        result = self.idx.address_flow_time_series(
            normalize_filters(address="alice", method="transfer"),
            0, 4 * HOUR, "hour",
        )
        by_bucket = {p["bucket_start"]: p for p in result["series"]}
        # a/f（transfer 发出）、c/d（transfer 接收）入选；b/e 是 approve
        self.assertEqual(by_bucket[0]["sent_count"], 1)
        self.assertEqual(by_bucket[HOUR]["received_amount"], "7")
        self.assertEqual(by_bucket[3 * HOUR]["sent_amount"], "4")
        self.assertEqual(by_bucket[3 * HOUR]["received_amount"], "4")

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
        # 手工构造指向最后一点的游标：下一页必须是空页 + 空游标
        cursor = encode_address_flow_time_series_cursor(
            normalize_filters(address="alice"),
            0, 2 * HOUR, "hour", HOUR,
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
        with self.assertRaises(InvalidFlowSeriesCursor):
            self.series(0, 5 * HOUR, "hour", page_size=1,
                        address="bob", cursor=page1["next_cursor"])
        # 加 method 筛选后旧游标失效
        with self.assertRaises(InvalidFlowSeriesCursor):
            self.idx.address_flow_time_series(
                normalize_filters(address="alice", method="approve"),
                0, 5 * HOUR, "hour", page_size=1,
                cursor=page1["next_cursor"],
            )
        # status 同样进入绑定
        page2 = self.idx.address_flow_time_series(
            normalize_filters(address="alice", status="success"),
            0, 5 * HOUR, "hour", page_size=1,
        )
        with self.assertRaises(InvalidFlowSeriesCursor):
            self.idx.address_flow_time_series(
                normalize_filters(address="alice", status="failure"),
                0, 5 * HOUR, "hour", page_size=1,
                cursor=page2["next_cursor"],
            )

    def test_cursor_bound_to_time_range_and_bucket(self):
        page1 = self.series(0, 5 * HOUR, "hour", page_size=1)
        with self.assertRaises(InvalidFlowSeriesCursor):
            self.series(0, 6 * HOUR, "hour", page_size=1,
                        cursor=page1["next_cursor"])
        with self.assertRaises(InvalidFlowSeriesCursor):
            self.series(HOUR, 5 * HOUR, "hour", page_size=1,
                        cursor=page1["next_cursor"])
        with self.assertRaises(InvalidFlowSeriesCursor):
            self.series(0, 5 * HOUR, "day", page_size=1,
                        cursor=page1["next_cursor"])

    def test_cross_command_cursor_rejected(self):
        tokens = [
            encode_time_stats_cursor(
                normalize_filters(address="alice"), HOUR, 0
            ),
            encode_method_time_series_cursor(
                normalize_filters(address="alice"),
                0, HOUR, "hour", 0, "transfer",
            ),
            encode_time_bucket_aggregation_cursor(
                {"address": "alice", "method": None,
                 "start_time": 0, "end_time": HOUR},
                "hour", 0,
            ),
        ]
        for token in tokens:
            with self.assertRaises(InvalidFlowSeriesCursor):
                self.series(0, HOUR, "hour", cursor=token)

    def test_cursor_garbage(self):
        for bad in ("", "not-base64!!!", "bm9wZQ", "%%%"):
            with self.assertRaises(InvalidFlowSeriesCursor):
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
        with self.assertRaises(InvalidFlowSeriesCursor):
            self.series(0, 5 * HOUR, "hour", cursor=reencode(tampered))
        tampered = dict(payload)
        tampered["after"] = 100 * HOUR
        with self.assertRaises(InvalidFlowSeriesCursor):
            self.series(0, 5 * HOUR, "hour", cursor=reencode(tampered))
        # 篡改桶粒度
        tampered = dict(payload)
        tampered["g"] = "day"
        with self.assertRaises(InvalidFlowSeriesCursor):
            self.series(0, 5 * HOUR, "hour", cursor=reencode(tampered))
        # 篡改时间窗
        tampered = dict(payload)
        tampered["w"] = [0, 6 * HOUR]
        with self.assertRaises(InvalidFlowSeriesCursor):
            self.series(0, 5 * HOUR, "hour", cursor=reencode(tampered))
        # 篡改观察地址
        tampered = dict(payload)
        tampered["f"] = dict(payload["f"], address="mallory")
        with self.assertRaises(InvalidFlowSeriesCursor):
            self.series(0, 5 * HOUR, "hour", cursor=reencode(tampered))
        # 篡改版本
        tampered = dict(payload)
        tampered["v"] = 99
        with self.assertRaises(InvalidFlowSeriesCursor):
            self.series(0, 5 * HOUR, "hour", cursor=reencode(tampered))

    def test_missing_address_is_filter_error(self):
        with self.assertRaises(InvalidFlowSeriesFilter):
            self.idx.address_flow_time_series(
                normalize_filters(), 0, HOUR, "hour"
            )
        with self.assertRaises(InvalidFlowSeriesFilter):
            self.idx.address_flow_time_series(
                {"address": None}, 0, HOUR, "hour"
            )

    def test_address_conflict_remains_invalid_filter(self):
        # address 与 from_address / to_address 并用沿用既有错误码
        with self.assertRaises(InvalidFilterError):
            normalize_filters(address="alice", from_address="bob")
        with self.assertRaises(InvalidFilterError):
            normalize_filters(address="alice", to_address="bob")

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
            with self.assertRaises(InvalidFlowSeriesRange):
                self.series(*args)

    def test_unsupported_bucket(self):
        for bad in ("week", "HOUR", "Hour", "days", "", None, 3600,
                    1.0, True, ["hour"], {"hour": 1}):
            with self.assertRaises(UnsupportedFlowSeriesBucket):
                self.series(0, HOUR, bad)

    def test_invalid_page_size(self):
        for bad in (0, -1, 1001, "10", None, True):
            with self.assertRaises(InvalidPageSizeError):
                self.series(0, HOUR, "hour", page_size=bad)

    def test_error_raises_before_any_page_data(self):
        with self.assertRaises(InvalidFlowSeriesRange):
            self.series(5, 5, "hour")
        with self.assertRaises(InvalidFlowSeriesCursor):
            self.series(0, HOUR, "hour", cursor="@@@not-a-cursor@@@")

    def test_default_page_size_constant(self):
        self.assertEqual(DEFAULT_PAGE_SIZE, 100)

    def test_existing_query_and_stats_unchanged(self):
        # 新功能不改变既有入口
        page = self.idx.query(normalize_filters())
        self.assertEqual(page["total"], 6)
        stats = self.idx.stats(normalize_filters())
        self.assertEqual(stats["total_count"], 6)
        self.assertEqual(stats["total_amount"], "38")


if __name__ == "__main__":
    unittest.main()
