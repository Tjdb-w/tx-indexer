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
        self.assertEqual(result["total_buckets"], 4)
        self.assertEqual(result["total_points"], 4)
        self.assertIsNone(result["next_cursor"])
        self.assertEqual(
            [p["bucket_start"] for p in result["series"]],
            list(range(0, 4 * HOUR, HOUR)),
        )
        by_bucket = {p["bucket_start"]: p for p in result["series"]}
        # h1：alice 发出 10
        self.assertEqual(by_bucket[0]["sent_amount"], "10")
        self.assertEqual(by_bucket[0]["received_amount"], "0")
        self.assertEqual(by_bucket[0]["net_amount"], "-10")
        self.assertEqual(by_bucket[0]["total_count"], 1)
        # h2：alice 接收（失败交易也计入资金流向）
        self.assertEqual(by_bucket[HOUR]["received_amount"], "21")
        self.assertEqual(by_bucket[HOUR]["net_amount"], "21")
        # h3：自转账两侧累计、total_count 一次
        self.assertEqual(by_bucket[3 * HOUR]["sent_count"], 1)
        self.assertEqual(by_bucket[3 * HOUR]["received_count"], 1)
        self.assertEqual(by_bucket[3 * HOUR]["total_count"], 1)
        self.assertEqual(by_bucket[3 * HOUR]["sent_amount"], "5")
        self.assertEqual(by_bucket[3 * HOUR]["received_amount"], "5")
        self.assertEqual(by_bucket[3 * HOUR]["net_amount"], "0")
        # 空桶零值
        self.assertEqual(
            by_bucket[2 * HOUR],
            {"bucket_start": 2 * HOUR, "sent_count": 0,
             "received_count": 0, "total_count": 0,
             "sent_amount": "0", "received_amount": "0",
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
        self.assertEqual(result["total_points"], 1)
        point = result["series"][0]
        # 三笔都在第 0 日桶：h1（发出 10）、h2（接收 21）、h3（自转 5）
        self.assertEqual(point["sent_count"], 2)
        self.assertEqual(point["received_count"], 2)
        self.assertEqual(point["total_count"], 3)
        self.assertEqual(point["sent_amount"], "15")
        self.assertEqual(point["received_amount"], "26")
        self.assertEqual(point["net_amount"], "11")

    def test_no_hits_returns_zero_filled_series(self):
        code, result, _ = self._run([
            "address-flow-time-series", self.path,
            "--address", "zzz",
            "--start-time", "0", "--end-time", str(2 * HOUR),
            "--bucket", "hour",
        ])
        self.assertEqual(code, 0)
        self.assertEqual(result["total_points"], 2)
        self.assertEqual(len(result["series"]), 2)
        self.assertTrue(
            all(p["total_count"] == 0 for p in result["series"])
        )

    def test_pagination_no_skip_no_dup(self):
        code, page1, _ = self._run(self._base("--page-size", "2"))
        self.assertEqual(code, 0)
        self.assertEqual(len(page1["series"]), 2)
        self.assertIsNotNone(page1["next_cursor"])

        collected = list(page1["series"])
        cursor = page1["next_cursor"]
        while cursor is not None:
            code, page, _ = self._run(self._base(
                "--page-size", "2", "--cursor", cursor))
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
        # h2 是 approve 被排除：第 1 桶零值
        by_bucket = {p["bucket_start"]: p for p in result["series"]}
        self.assertEqual(by_bucket[0]["sent_amount"], "10")
        self.assertEqual(by_bucket[HOUR]["total_count"], 0)
        self.assertEqual(by_bucket[3 * HOUR]["sent_amount"], "5")

        code, result, _ = self._run(self._base(
            "--min-amount", "15"))
        self.assertEqual(code, 0)
        # 只保留 h2（21）：h1(10)、h3(5) 被金额筛选排除
        by_bucket = {p["bucket_start"]: p for p in result["series"]}
        self.assertEqual(by_bucket[HOUR]["received_amount"], "21")
        self.assertEqual(by_bucket[0]["total_count"], 0)
        self.assertEqual(by_bucket[3 * HOUR]["total_count"], 0)

        code, result, _ = self._run(self._base("--status", "failure"))
        self.assertEqual(code, 0)
        by_bucket = {p["bucket_start"]: p for p in result["series"]}
        self.assertEqual(by_bucket[HOUR]["received_amount"], "21")
        self.assertEqual(by_bucket[0]["total_count"], 0)

    def _assert_error(self, argv, expected_error):
        code, out, err = self._run(argv)
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        self.assertEqual(err.strip().count("\n"), 0)
        payload = json.loads(err)
        self.assertEqual(payload["error"], expected_error)
        self.assertIsNone(payload["input_line"])

    def test_missing_address_is_flow_filter_error_before_read(self):
        # 文件不存在：错误必须先于文件读取确定
        self._assert_error([
            "address-flow-time-series", self.missing,
            "--start-time", "0", "--end-time", "10", "--bucket", "hour",
        ], "invalid_flow_series_filter")

    def test_address_with_from_or_to_remains_invalid_filter(self):
        base = [
            "address-flow-time-series", self.missing,
            "--start-time", "0", "--end-time", "10", "--bucket", "hour",
        ]
        self._assert_error(
            base + ["--address", "alice", "--from-address", "bob"],
            "invalid_filter",
        )
        self._assert_error(
            base + ["--address", "alice", "--to-address", "bob"],
            "invalid_filter",
        )

    def test_blank_address_remains_invalid_filter(self):
        self._assert_error([
            "address-flow-time-series", self.missing,
            "--address", "   ",
            "--start-time", "0", "--end-time", "10", "--bucket", "hour",
        ], "invalid_filter")

    def test_missing_start_or_end_is_range_error_before_read(self):
        for args in (
            ["--address", "alice", "--end-time", "10", "--bucket", "hour"],
            ["--address", "alice", "--start-time", "0", "--bucket", "hour"],
            ["--address", "alice", "--bucket", "hour"],
        ):
            self._assert_error(
                ["address-flow-time-series", self.missing] + args,
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
            self._assert_error(
                base + args, "invalid_flow_series_range"
            )

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

    def test_garbage_cursor_is_flow_cursor_error(self):
        self._assert_error(
            self._base("--cursor", "not-a-cursor!!!"),
            "invalid_flow_series_cursor",
        )

    def test_cursor_bound_to_conditions(self):
        code, page1, _ = self._run(self._base("--page-size", "1"))
        self.assertEqual(code, 0)
        cursor = page1["next_cursor"]
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
        # 改观察地址
        self._assert_error([
            "address-flow-time-series", self.path,
            "--address", "bob",
            "--start-time", "0", "--end-time", str(4 * HOUR),
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
            normalize_filters(address="alice"),
            0, 4 * HOUR, "hour", 3 * HOUR,
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
