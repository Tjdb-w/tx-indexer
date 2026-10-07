"""address-flow-time-series 命令的 CLI 端到端测试。"""

import io
import json
import os
import sys
import tempfile
import unittest

from tx_indexer.cli import main

HOUR = 3600
DAY = 86400

DATA_LINES = [
    {"tx_hash": "h1", "block_number": 1, "timestamp": 0,
     "from_address": "alice", "to_address": "bob",
     "method": "transfer", "amount": "10"},
    {"tx_hash": "h2", "block_number": 1, "timestamp": HOUR + 5,
     "from_address": "bob", "to_address": "alice",
     "method": "approve", "amount": "21", "success": False},
    {"tx_hash": "h3", "block_number": 2, "timestamp": 3 * HOUR,
     "from_address": "alice", "to_address": "alice",
     "method": "transfer", "amount": "5"},
]


class AddressFlowTimeSeriesCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "data.jsonl")
        with open(self.path, "w", encoding="utf-8") as fh:
            for obj in DATA_LINES:
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
            "address-flow-time-series", self.path,
            "--address", "alice",
            "--start-time", "0", "--end-time", str(4 * HOUR),
            "--bucket", "hour",
        ] + list(extra)

    def test_hour_series_continuous_zero_filled(self):
        code, result, err = self._run(self._base())
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(result["total_points"], 4)
        self.assertEqual(result["total_buckets"], 4)
        self.assertIsNone(result["next_cursor"])
        by_bucket = {p["bucket_start"]: p for p in result["series"]}
        # h1：alice 发出 10
        self.assertEqual(
            by_bucket[0],
            {"bucket_start": 0, "sent_count": 1, "received_count": 0,
             "total_count": 1, "sent_amount": "10", "received_amount": "0",
             "net_amount": "-10"},
        )
        # h2：alice 收到 21
        self.assertEqual(
            by_bucket[HOUR],
            {"bucket_start": HOUR, "sent_count": 0, "received_count": 1,
             "total_count": 1, "sent_amount": "0", "received_amount": "21",
             "net_amount": "21"},
        )
        # 空桶零填充
        self.assertEqual(
            by_bucket[2 * HOUR],
            {"bucket_start": 2 * HOUR, "sent_count": 0, "received_count": 0,
             "total_count": 0, "sent_amount": "0", "received_amount": "0",
             "net_amount": "0"},
        )
        # h3 自转账：两侧各累计一次、total_count 一次、net 为 0
        self.assertEqual(
            by_bucket[3 * HOUR],
            {"bucket_start": 3 * HOUR, "sent_count": 1, "received_count": 1,
             "total_count": 1, "sent_amount": "5", "received_amount": "5",
             "net_amount": "0"},
        )

    def test_day_series(self):
        code, result, _ = self._run([
            "address-flow-time-series", self.path,
            "--address", "alice",
            "--start-time", "0", "--end-time", str(DAY),
            "--bucket", "day",
        ])
        self.assertEqual(code, 0)
        self.assertEqual(result["total_buckets"], 1)
        point = result["series"][0]
        self.assertEqual(point["bucket_start"], 0)
        self.assertEqual(point["total_count"], 3)
        self.assertEqual(point["sent_amount"], "15")
        self.assertEqual(point["received_amount"], "26")
        self.assertEqual(point["net_amount"], "11")

    def test_no_hit_returns_zero_series(self):
        code, result, _ = self._run([
            "address-flow-time-series", self.path,
            "--address", "nobody",
            "--start-time", "0", "--end-time", str(2 * HOUR),
            "--bucket", "hour",
        ])
        self.assertEqual(code, 0)
        self.assertEqual(result["total_points"], 2)
        self.assertEqual(
            [p["total_count"] for p in result["series"]], [0, 0]
        )

    def test_pagination_no_skip_no_dup(self):
        code, page1, _ = self._run(self._base("--page-size", "3"))
        self.assertEqual(code, 0)
        self.assertEqual(len(page1["series"]), 3)
        self.assertIsNotNone(page1["next_cursor"])

        collected = list(page1["series"])
        cursor = page1["next_cursor"]
        while cursor is not None:
            code, page, _ = self._run(self._base(
                "--page-size", "3", "--cursor", cursor))
            self.assertEqual(code, 0)
            collected.extend(page["series"])
            cursor = page["next_cursor"]
        self.assertEqual(len(collected), 4)
        self.assertEqual(
            [p["bucket_start"] for p in collected],
            list(range(0, 4 * HOUR, HOUR)),
        )

    def test_page_size_change_does_not_affect_cursor(self):
        code, page1, _ = self._run(self._base("--page-size", "1"))
        self.assertEqual(code, 0)
        code, page2, _ = self._run(self._base(
            "--page-size", "100", "--cursor", page1["next_cursor"]))
        self.assertEqual(code, 0)
        self.assertEqual(len(page2["series"]), 3)
        self.assertIsNone(page2["next_cursor"])

    def test_filters_reused(self):
        code, result, _ = self._run(self._base("--method", "transfer"))
        self.assertEqual(code, 0)
        self.assertEqual(
            [p["total_count"] for p in result["series"]], [1, 0, 0, 1]
        )

        code, result, _ = self._run(self._base("--status", "failure"))
        self.assertEqual(code, 0)
        self.assertEqual(
            [p["received_count"] for p in result["series"]], [0, 1, 0, 0]
        )

    def _assert_error(self, argv, expected_error):
        code, out, err = self._run(argv)
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        self.assertEqual(err.strip().count("\n"), 0)
        payload = json.loads(err)
        self.assertEqual(payload["error"], expected_error)
        self.assertIsNone(payload["input_line"])

    def test_missing_address_is_filter_error_before_read(self):
        base = [
            "address-flow-time-series", self.missing,
            "--start-time", "0", "--end-time", "10", "--bucket", "hour",
        ]
        # 文件不存在：错误必须先于文件读取确定
        self._assert_error(base, "invalid_flow_series_filter")
        self._assert_error(
            base + ["--address", "   "], "invalid_flow_series_filter"
        )

    def test_missing_start_or_end_is_range_error_before_read(self):
        for args in (
            ["--end-time", "10", "--bucket", "hour"],
            ["--start-time", "0", "--bucket", "hour"],
            ["--bucket", "hour"],
        ):
            # 文件不存在：错误必须先于文件读取确定
            self._assert_error(
                ["address-flow-time-series", self.missing,
                 "--address", "alice"] + args,
                "invalid_flow_series_range",
            )

    def test_bad_time_values_are_range_error(self):
        base = [
            "address-flow-time-series", self.missing,
            "--address", "alice", "--bucket", "hour",
        ]
        for args in (
            ["--start-time", "abc", "--end-time", "10"],
            ["--start-time", "-1", "--end-time", "10"],
            ["--start-time", "10", "--end-time", "10"],
            ["--start-time", "100", "--end-time", "10"],
        ):
            self._assert_error(base + args, "invalid_flow_series_range")

    def test_missing_or_bad_bucket_is_unsupported_error(self):
        base = [
            "address-flow-time-series", self.missing,
            "--address", "alice",
            "--start-time", "0", "--end-time", "10",
        ]
        self._assert_error(base, "unsupported_flow_series_bucket")
        for bad in ("week", "HOUR", "hours", "Day", ""):
            self._assert_error(
                base + ["--bucket", bad],
                "unsupported_flow_series_bucket",
            )

    def test_address_conflicts_with_from_to(self):
        self._assert_error(
            self._base("--from-address", "bob"), "invalid_filter"
        )
        self._assert_error(
            self._base("--to-address", "bob"), "invalid_filter"
        )

    def test_generic_filter_errors_unchanged(self):
        base = [
            "address-flow-time-series", self.missing,
            "--address", "alice",
            "--start-time", "0", "--end-time", "10", "--bucket", "hour",
        ]
        self._assert_error(
            base + ["--status", "Success"], "invalid_status_filter"
        )
        self._assert_error(
            base + ["--min-amount", "-1"], "invalid_amount_filter"
        )
        self._assert_error(
            base + ["--min-block", "x"], "invalid_block_filter"
        )

    def test_invalid_page_size(self):
        self._assert_error(
            self._base("--page-size", "0"), "invalid_page_size"
        )
        self._assert_error(
            self._base("--page-size", "1001"), "invalid_page_size"
        )

    def test_garbage_cursor_is_flow_series_cursor_error(self):
        self._assert_error(
            self._base("--cursor", "not-a-cursor!!!"),
            "invalid_flow_series_cursor",
        )

    def test_cursor_bound_to_conditions(self):
        code, page1, _ = self._run(self._base("--page-size", "1"))
        self.assertEqual(code, 0)
        cursor = page1["next_cursor"]
        # 改观察地址
        self._assert_error([
            "address-flow-time-series", self.path,
            "--address", "bob",
            "--start-time", "0", "--end-time", str(4 * HOUR),
            "--bucket", "hour", "--page-size", "1", "--cursor", cursor,
        ], "invalid_flow_series_cursor")
        # 改桶粒度
        self._assert_error([
            "address-flow-time-series", self.path,
            "--address", "alice",
            "--start-time", "0", "--end-time", str(4 * HOUR),
            "--bucket", "day", "--page-size", "1", "--cursor", cursor,
        ], "invalid_flow_series_cursor")
        # 改时间窗
        self._assert_error([
            "address-flow-time-series", self.path,
            "--address", "alice",
            "--start-time", "0", "--end-time", str(5 * HOUR),
            "--bucket", "hour", "--page-size", "1", "--cursor", cursor,
        ], "invalid_flow_series_cursor")
        # 加筛选
        self._assert_error(
            self._base("--method", "transfer", "--page-size", "1",
                       "--cursor", cursor),
            "invalid_flow_series_cursor",
        )
        # 跨命令复用（method-time-series 游标）
        code, series, _ = self._run([
            "method-time-series", self.path,
            "--start-time", "0", "--end-time", str(4 * HOUR),
            "--bucket", "hour", "--page-size", "1",
        ])
        self.assertEqual(code, 0)
        self._assert_error(
            self._base("--cursor", series["next_cursor"]),
            "invalid_flow_series_cursor",
        )

    def test_page_beyond_last_is_empty_with_null_cursor(self):
        code, page1, _ = self._run(self._base())
        self.assertIsNone(page1["next_cursor"])
        # 翻到末页之后：空页 + null 游标
        from tx_indexer.cursor import (
            encode_address_flow_time_series_cursor,
        )
        from tx_indexer.engine import normalize_filters
        cursor = encode_address_flow_time_series_cursor(
            normalize_filters(address="alice"), 0, 4 * HOUR, "hour",
            3 * HOUR,
        )
        code, page, _ = self._run(self._base("--cursor", cursor))
        self.assertEqual(code, 0)
        self.assertEqual(page["series"], [])
        self.assertEqual(page["total_points"], 4)
        self.assertIsNone(page["next_cursor"])

    def test_existing_commands_still_work(self):
        # 新命令不影响既有命令的解析与输出
        code, page, _ = self._run(["query", self.path])
        self.assertEqual(code, 0)
        self.assertEqual(
            [t["tx_hash"] for t in page["transactions"]],
            ["h1", "h2", "h3"],
        )
        code, stats, _ = self._run(["stats", self.path])
        self.assertEqual(code, 0)
        self.assertEqual(stats["total_count"], 3)
        code, series, _ = self._run([
            "method-time-series", self.path,
            "--start-time", "0", "--end-time", str(4 * HOUR),
            "--bucket", "hour",
        ])
        self.assertEqual(code, 0)
        self.assertEqual(series["total_buckets"], 4)


if __name__ == "__main__":
    unittest.main()
