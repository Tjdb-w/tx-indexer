"""method-time-series（method 时间序列）测试。

覆盖：连续 hour/day 桶 × method 零填充、纪元对齐（首桶可早于
start_time、末桶只计 end_time 之前）、bucket_start 升序 + method 码点
升序、金额为无前导零十进制字符串、未携带 success 计成功、沿用 query
通用筛选、total_points/total_methods/total_buckets、游标分页（不重复
不遗漏、改 page_size 不影响续页、绑定筛选/时间窗/桶粒度、篡改与跨命令
复用拒绝），以及三类独立序列异常与 CLI 端到端行为。
"""

import base64
import io
import json
import os
import sys
import tempfile
import unittest

from tx_indexer.cli import main
from tx_indexer.cursor import (
    encode_cursor,
    encode_time_bucket_aggregation_cursor,
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
            rec("a", 100, method="transfer", amount="10", success=True),
            rec("b", HOUR - 1, method="approve", amount="5", success=False),
            jsonl_rec("c", HOUR, method="transfer", amount="7"),
            rec("d", 2 * HOUR, method="transfer", amount="3",
                success=False),
            rec("e", 3 * HOUR + 10, method="approve", amount="9",
                success=False),
        ]
        self.idx = TxIndexer(self.records)
        self.filters = normalize_filters()

    def series(self, *args, **kwargs):
        return self.idx.method_time_series(self.filters, *args, **kwargs)

    def test_continuous_buckets_per_method_zero_filled(self):
        result = self.series(0, 4 * HOUR, "hour")
        self.assertEqual(result["total_buckets"], 4)
        self.assertEqual(result["total_methods"], 2)
        self.assertEqual(result["total_points"], 8)
        self.assertIsNone(result["next_cursor"])
        keys = [(p["bucket_start"], p["method"]) for p in result["series"]]
        self.assertEqual(
            keys,
            [
                (0, "approve"), (0, "transfer"),
                (HOUR, "approve"), (HOUR, "transfer"),
                (2 * HOUR, "approve"), (2 * HOUR, "transfer"),
                (3 * HOUR, "approve"), (3 * HOUR, "transfer"),
            ],
        )
        by_key = {(p["bucket_start"], p["method"]): p
                  for p in result["series"]}
        # 空点：计数为 0、金额为 "0"
        self.assertEqual(
            by_key[(HOUR, "approve")],
            {"method": "approve", "bucket_start": HOUR, "total_count": 0,
             "total_amount": "0", "success_count": 0, "failure_count": 0},
        )
        self.assertEqual(
            by_key[(0, "transfer")],
            {"method": "transfer", "bucket_start": 0, "total_count": 1,
             "total_amount": "10", "success_count": 1, "failure_count": 0},
        )
        self.assertEqual(
            by_key[(0, "approve")],
            {"method": "approve", "bucket_start": 0, "total_count": 1,
             "total_amount": "5", "success_count": 0, "failure_count": 1},
        )
        # 未携带 success 的 JSONL 记录计成功
        self.assertEqual(
            by_key[(HOUR, "transfer")],
            {"method": "transfer", "bucket_start": HOUR, "total_count": 1,
             "total_amount": "7", "success_count": 1, "failure_count": 0},
        )
        self.assertEqual(
            by_key[(3 * HOUR, "approve")],
            {"method": "approve", "bucket_start": 3 * HOUR,
             "total_count": 1, "total_amount": "9",
             "success_count": 0, "failure_count": 1},
        )

    def test_result_and_point_field_order(self):
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

    def test_first_bucket_aligned_before_start_time(self):
        # start_time 落在桶内：首桶按纪元整点对齐，可早于 start_time，
        # 且只计 start_time 之后的数据
        result = self.series(HOUR - 1, 2 * HOUR, "hour")
        self.assertEqual(result["total_buckets"], 2)
        starts = [p["bucket_start"] for p in result["series"]]
        self.assertEqual(sorted(set(starts)), [0, HOUR])
        by_key = {(p["bucket_start"], p["method"]): p
                  for p in result["series"]}
        # 首桶只含 timestamp=HOUR-1 的 approve（timestamp=100 被排除）
        self.assertEqual(by_key[(0, "approve")]["total_count"], 1)
        self.assertEqual(by_key[(0, "transfer")]["total_count"], 0)

    def test_end_time_exclusive(self):
        # 末桶只计 end_time 之前的数据：timestamp == end_time 不计入
        result = self.series(0, HOUR, "hour")
        self.assertEqual(result["total_buckets"], 1)
        by_method = {p["method"]: p for p in result["series"]}
        self.assertEqual(by_method["transfer"]["total_count"], 1)
        self.assertEqual(by_method["transfer"]["total_amount"], "10")
        self.assertEqual(by_method["approve"]["total_count"], 1)

    def test_day_buckets(self):
        result = self.series(0, 2 * DAY, "day")
        self.assertEqual(result["total_buckets"], 2)
        self.assertEqual(result["total_methods"], 2)
        self.assertEqual(result["total_points"], 4)
        by_key = {(p["bucket_start"], p["method"]): p
                  for p in result["series"]}
        self.assertEqual(by_key[(0, "transfer")]["total_count"], 3)
        self.assertEqual(by_key[(0, "transfer")]["total_amount"], "20")
        self.assertEqual(by_key[(0, "transfer")]["failure_count"], 1)
        self.assertEqual(by_key[(0, "approve")]["total_count"], 2)
        self.assertEqual(by_key[(DAY, "transfer")]["total_count"], 0)

    def test_method_codepoint_order_within_bucket(self):
        records = [
            rec("m1", 0, method="transfer"),
            rec("m2", 0, method="Approve"),
            rec("m3", 0, method="approve"),
            rec("m4", 0, method="转账"),
        ]
        idx = TxIndexer(records)
        result = idx.method_time_series(
            normalize_filters(), 0, HOUR, "hour"
        )
        methods = [p["method"] for p in result["series"]]
        self.assertEqual(methods, ["Approve", "approve", "transfer", "转账"])

    def test_no_transactions_empty_series(self):
        result = self.series(10 * HOUR, 12 * HOUR, "hour")
        self.assertEqual(result["series"], [])
        self.assertEqual(result["total_points"], 0)
        self.assertEqual(result["total_methods"], 0)
        self.assertEqual(result["total_buckets"], 2)
        self.assertIsNone(result["next_cursor"])

    def test_filters_applied(self):
        # method 筛选：只入选命中的 method
        filters = normalize_filters(method="transfer")
        result = self.idx.method_time_series(filters, 0, 4 * HOUR, "hour")
        self.assertEqual(result["total_methods"], 1)
        self.assertEqual(
            {p["method"] for p in result["series"]}, {"transfer"}
        )
        # status 筛选
        filters = normalize_filters(status="failure")
        result = self.idx.method_time_series(filters, 0, 4 * HOUR, "hour")
        by_key = {(p["bucket_start"], p["method"]): p
                  for p in result["series"]}
        self.assertEqual(by_key[(0, "transfer")]["total_count"], 0)
        self.assertEqual(by_key[(2 * HOUR, "transfer")]["total_count"], 1)
        self.assertEqual(by_key[(2 * HOUR, "transfer")]["failure_count"], 1)
        self.assertEqual(by_key[(HOUR, "transfer")]["total_count"], 0)
        # 金额与地址筛选
        filters = normalize_filters(min_amount="8", address="carol")
        result = self.idx.method_time_series(filters, 0, 4 * HOUR, "hour")
        self.assertEqual(result["total_methods"], 0)
        self.assertEqual(result["series"], [])
        filters = normalize_filters(min_amount="8")
        result = self.idx.method_time_series(filters, 0, 4 * HOUR, "hour")
        by_key = {(p["bucket_start"], p["method"]): p
                  for p in result["series"]}
        self.assertEqual(by_key[(0, "transfer")]["total_amount"], "10")
        self.assertEqual(by_key[(3 * HOUR, "approve")]["total_amount"], "9")

    def test_pagination_no_gap_no_dup(self):
        # 8 个点，按 3/3/2 翻页，合并后与单页一致
        full = self.series(0, 4 * HOUR, "hour")
        seen = []
        cursor = None
        pages = 0
        while True:
            result = self.series(0, 4 * HOUR, "hour", page_size=3,
                                 cursor=cursor)
            seen.extend(result["series"])
            pages += 1
            cursor = result["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(pages, 3)
        self.assertEqual(seen, full["series"])
        for result_page in (full,):
            self.assertEqual(result_page["total_points"], 8)

    def test_page_size_change_does_not_affect_continuation(self):
        first = self.series(0, 4 * HOUR, "hour", page_size=5)
        self.assertEqual(len(first["series"]), 5)
        # 续页使用不同的 page_size：仍按序取完剩余 3 点，不重不漏
        rest = self.series(0, 4 * HOUR, "hour", page_size=3,
                           cursor=first["next_cursor"])
        self.assertEqual(len(rest["series"]), 3)
        self.assertIsNone(rest["next_cursor"])
        full = self.series(0, 4 * HOUR, "hour")
        self.assertEqual(first["series"] + rest["series"], full["series"])

    def test_next_cursor_only_when_more_points(self):
        # 恰好一页：无 next_cursor
        result = self.series(0, 4 * HOUR, "hour", page_size=8)
        self.assertIsNone(result["next_cursor"])
        result = self.series(0, 4 * HOUR, "hour", page_size=7)
        self.assertIsNotNone(result["next_cursor"])

    def test_default_page_size(self):
        records = [
            rec("x%d" % i, i * 10, method="m%d" % i)
            for i in range(DEFAULT_PAGE_SIZE + 1)
        ]
        idx = TxIndexer(records)
        result = idx.method_time_series(
            normalize_filters(), 0, (DEFAULT_PAGE_SIZE + 1) * 10, "day"
        )
        self.assertEqual(len(result["series"]), DEFAULT_PAGE_SIZE)
        self.assertIsNotNone(result["next_cursor"])

    def test_cursor_binds_filters_window_and_granularity(self):
        first = self.series(0, 4 * HOUR, "hour", page_size=3)
        cursor = first["next_cursor"]
        # 改筛选
        with self.assertRaises(InvalidSeriesCursorError):
            self.idx.method_time_series(
                normalize_filters(method="transfer"),
                0, 4 * HOUR, "hour", cursor=cursor,
            )
        # 改时间窗
        with self.assertRaises(InvalidSeriesCursorError):
            self.series(0, 5 * HOUR, "hour", cursor=cursor)
        # 改桶粒度
        with self.assertRaises(InvalidSeriesCursorError):
            self.series(0, 4 * HOUR, "day", cursor=cursor)
        # 原条件可续页
        rest = self.series(0, 4 * HOUR, "hour", cursor=cursor)
        self.assertEqual(len(rest["series"]), 5)

    def test_cursor_tampered_or_undecodable(self):
        first = self.series(0, 4 * HOUR, "hour", page_size=3)
        cursor = first["next_cursor"]
        for bad in ("", "!!!", "a" * 10, "####"):
            with self.assertRaises(InvalidSeriesCursorError):
                self.series(0, 4 * HOUR, "hour", cursor=bad)
        # 篡改 payload 内 marker
        raw = json.loads(base64.urlsafe_b64decode(
            cursor + "=" * (-len(cursor) % 4)
        ).decode("utf-8"))
        raw["after"] = [123, "transfer"]  # 非桶边界
        tampered = base64.urlsafe_b64encode(
            json.dumps(raw).encode("utf-8")
        ).rstrip(b"=").decode("ascii")
        with self.assertRaises(InvalidSeriesCursorError):
            self.series(0, 4 * HOUR, "hour", cursor=tampered)

    def test_cursor_cross_command_rejected(self):
        other = encode_time_bucket_aggregation_cursor(
            {"address": None, "method": None,
             "start_time": 0, "end_time": 4 * HOUR},
            "hour", 0,
        )
        with self.assertRaises(InvalidSeriesCursorError):
            self.series(0, 4 * HOUR, "hour", cursor=other)
        query_cursor = encode_cursor(self.filters, 1, "a")
        with self.assertRaises(InvalidSeriesCursorError):
            self.series(0, 4 * HOUR, "hour", cursor=query_cursor)

    def test_range_errors(self):
        for start, end in (
            (None, HOUR), (HOUR, None), (None, None),
            ("0", HOUR), (0, "x"), (True, HOUR), (0, 1.5),
            (-1, HOUR), (0, -5), (HOUR, HOUR), (2 * HOUR, HOUR),
        ):
            with self.assertRaises(InvalidSeriesRangeError):
                self.series(start, end, "hour")

    def test_bucket_errors(self):
        for bucket in (None, "", "minute", "Hour", "HOUR", 3600, True):
            with self.assertRaises(UnsupportedSeriesBucketError):
                self.series(0, HOUR, bucket)

    def test_page_size_errors(self):
        for page_size in (0, -1, 1001, "10", 1.5, True):
            with self.assertRaises(InvalidPageSizeError):
                self.series(0, 4 * HOUR, "hour", page_size=page_size)

    def test_error_priority_range_before_bucket_before_page_size(self):
        with self.assertRaises(InvalidSeriesRangeError):
            self.series(None, None, "bad", page_size=0)
        with self.assertRaises(UnsupportedSeriesBucketError):
            self.series(0, HOUR, "bad", page_size=0)


class MethodTimeSeriesCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "data.jsonl")
        records = [
            {"tx_hash": "h1", "block_number": 1, "timestamp": 0,
             "from_address": "alice", "to_address": "bob",
             "method": "transfer", "amount": "10"},
            {"tx_hash": "h2", "block_number": 1, "timestamp": HOUR + 5,
             "from_address": "bob", "to_address": "alice",
             "method": "approve", "amount": "21", "success": False},
            {"tx_hash": "h3", "block_number": 2, "timestamp": 3 * HOUR,
             "from_address": "alice", "to_address": "carol",
             "method": "transfer", "amount": "5"},
        ]
        with open(self.path, "w", encoding="utf-8") as fh:
            for obj in records:
                fh.write(json.dumps(obj) + "\n")
        self.missing = os.path.join(self.tmp.name, "missing.jsonl")
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

    def _base(self, *extra):
        return [
            "method-time-series", self.path,
            "--start-time", "0", "--end-time", str(4 * HOUR + 1),
            "--bucket", "hour",
        ] + list(extra)

    def test_cli_series_output(self):
        code, result, err = self._run(self._base())
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(result["total_buckets"], 5)
        self.assertEqual(result["total_methods"], 2)
        self.assertEqual(result["total_points"], 10)
        self.assertIsNone(result["next_cursor"])
        keys = [(p["bucket_start"], p["method"]) for p in result["series"]]
        self.assertEqual(
            keys[:4],
            [(0, "approve"), (0, "transfer"),
             (HOUR, "approve"), (HOUR, "transfer")],
        )
        # 未携带 success 的 JSONL 记录计成功
        first = result["series"][1]
        self.assertEqual(first["success_count"], 1)
        self.assertEqual(first["failure_count"], 0)
        # 失败记录
        second = result["series"][2]
        self.assertEqual(
            second,
            {"method": "approve", "bucket_start": HOUR, "total_count": 1,
             "total_amount": "21", "success_count": 0, "failure_count": 1},
        )

    def test_cli_pagination(self):
        code, page1, _ = self._run(self._base("--page-size", "4"))
        self.assertEqual(code, 0)
        self.assertEqual(len(page1["series"]), 4)
        self.assertIsNotNone(page1["next_cursor"])
        code, page2, _ = self._run(
            self._base("--page-size", "100",
                       "--cursor", page1["next_cursor"])
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(page2["series"]), 6)
        self.assertIsNone(page2["next_cursor"])
        code, full, _ = self._run(self._base())
        self.assertEqual(page1["series"] + page2["series"], full["series"])

    def test_cli_filters(self):
        code, result, _ = self._run(self._base("--method", "transfer"))
        self.assertEqual(code, 0)
        self.assertEqual(result["total_methods"], 1)
        self.assertEqual(result["total_points"], 5)
        code, result, _ = self._run(
            self._base("--status", "failure")
        )
        self.assertEqual(code, 0)
        self.assertEqual(result["total_methods"], 1)
        self.assertEqual(result["series"][0]["method"], "approve")

    def test_cli_range_errors(self):
        cases = [
            # 缺失
            ["method-time-series", self.path,
             "--end-time", "10", "--bucket", "hour"],
            ["method-time-series", self.path,
             "--start-time", "0", "--bucket", "hour"],
            # 非整数
            ["method-time-series", self.path,
             "--start-time", "x", "--end-time", "10", "--bucket", "hour"],
            # 为负
            ["method-time-series", self.path,
             "--start-time", "-1", "--end-time", "10", "--bucket", "hour"],
            # end <= start
            ["method-time-series", self.path,
             "--start-time", "10", "--end-time", "10", "--bucket", "hour"],
            ["method-time-series", self.path,
             "--start-time", "20", "--end-time", "10", "--bucket", "hour"],
        ]
        for argv in cases:
            code, out, err = self._run(argv)
            self.assertEqual(code, 2, argv)
            self.assertIsNone(out)
            self.assertEqual(json.loads(err)["error"], "invalid_series_range",
                             argv)

    def test_cli_bucket_errors(self):
        for extra in ([], ["--bucket", "minute"], ["--bucket", ""]):
            argv = [
                "method-time-series", self.path,
                "--start-time", "0", "--end-time", "10",
            ] + extra
            code, out, err = self._run(argv)
            self.assertEqual(code, 2, argv)
            self.assertIsNone(out)
            self.assertEqual(
                json.loads(err)["error"], "unsupported_series_bucket", argv
            )

    def test_cli_validation_before_file_read(self):
        # 范围与桶粒度错误先于数据文件读取
        code, _, err = self._run([
            "method-time-series", self.missing,
            "--start-time", "5", "--end-time", "5", "--bucket", "hour",
        ])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error"], "invalid_series_range")
        code, _, err = self._run([
            "method-time-series", self.missing,
            "--start-time", "0", "--end-time", "5", "--bucket", "bad",
        ])
        self.assertEqual(code, 2)
        self.assertEqual(
            json.loads(err)["error"], "unsupported_series_bucket"
        )

    def test_cli_cursor_errors(self):
        code, page1, _ = self._run(self._base("--page-size", "2"))
        cursor = page1["next_cursor"]
        # 改时间窗
        code, out, err = self._run([
            "method-time-series", self.path,
            "--start-time", "0", "--end-time", str(8 * HOUR),
            "--bucket", "hour", "--cursor", cursor,
        ])
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        self.assertEqual(json.loads(err)["error"], "invalid_series_cursor")
        # 改桶粒度
        code, out, err = self._run([
            "method-time-series", self.path,
            "--start-time", "0", "--end-time", str(4 * HOUR + 1),
            "--bucket", "day", "--cursor", cursor,
        ])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error"], "invalid_series_cursor")
        # 改筛选
        code, out, err = self._run(
            self._base("--method", "transfer", "--cursor", cursor)
        )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error"], "invalid_series_cursor")
        # 无法解码
        code, out, err = self._run(self._base("--cursor", "garbage!!"))
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error"], "invalid_series_cursor")

    def test_cli_page_size_error(self):
        code, out, err = self._run(self._base("--page-size", "0"))
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        self.assertEqual(json.loads(err)["error"], "invalid_page_size")

    def test_cli_generic_filter_error_unchanged(self):
        code, out, err = self._run(self._base("--status", "Success"))
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error"], "invalid_status_filter")


if __name__ == "__main__":
    unittest.main()
