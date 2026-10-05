"""time-bucket-aggregation 命令的 CLI 端到端测试。"""

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
     "method": "approve", "amount": "21"},
    {"tx_hash": "h3", "block_number": 2, "timestamp": 3 * HOUR,
     "from_address": "alice", "to_address": "carol",
     "method": "transfer", "amount": "5"},
]


class TimeBucketCliTest(unittest.TestCase):
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
            "time-bucket-aggregation", self.path,
            "--start-time", "0", "--end-time", str(4 * HOUR),
            "--bucket", "hour",
        ] + list(extra)

    def test_hour_aggregation_continuous_zero_filled(self):
        code, result, err = self._run(self._base())
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(
            [b["bucket_start"] for b in result["buckets"]],
            [0, HOUR, 2 * HOUR, 3 * HOUR],
        )
        self.assertEqual(result["total_buckets"], 4)
        # JSONL 记录无 success：全部计入成功
        self.assertEqual(result["buckets"][0]["success_count"], 1)
        self.assertEqual(result["buckets"][0]["failure_count"], 0)
        # 空桶零填充
        self.assertEqual(
            result["buckets"][2],
            {"bucket_start": 2 * HOUR, "total_count": 0,
             "success_count": 0, "failure_count": 0},
        )

    def test_day_aggregation(self):
        code, result, _ = self._run([
            "time-bucket-aggregation", self.path,
            "--start-time", "0", "--end-time", str(DAY),
            "--bucket", "day",
        ])
        self.assertEqual(code, 0)
        self.assertEqual(
            [b["bucket_start"] for b in result["buckets"]], [0]
        )
        self.assertEqual(result["buckets"][0]["total_count"], 3)

    def test_pagination_no_skip_no_dup(self):
        code, page1, _ = self._run(self._base("--page-size", "2"))
        self.assertEqual(code, 0)
        self.assertEqual(
            [b["bucket_start"] for b in page1["buckets"]], [0, HOUR]
        )
        self.assertIsNotNone(page1["next_cursor"])

        code, page2, _ = self._run(self._base(
            "--page-size", "2", "--cursor", page1["next_cursor"]))
        self.assertEqual(code, 0)
        self.assertEqual(
            [b["bucket_start"] for b in page2["buckets"]],
            [2 * HOUR, 3 * HOUR],
        )
        self.assertIsNone(page2["next_cursor"])

    def test_filters(self):
        code, result, _ = self._run(self._base(
            "--address", "alice", "--method", "transfer"))
        self.assertEqual(code, 0)
        # h1(alice→bob transfer)、h3(alice→carol transfer)
        self.assertEqual(
            [b["total_count"] for b in result["buckets"]],
            [1, 0, 0, 1],
        )

    def _assert_error(self, argv, expected_error, missing_file=False):
        code, out, err = self._run(argv)
        self.assertEqual(code, 2)
        self.assertIsNone(out)
        self.assertEqual(err.strip().count("\n"), 0)
        payload = json.loads(err)
        self.assertEqual(payload["error"], expected_error)
        self.assertIsNone(payload["input_line"])

    def test_missing_start_or_end_is_range_error_before_read(self):
        for args in (
            ["--end-time", "10", "--bucket", "hour"],
            ["--start-time", "0", "--bucket", "hour"],
            ["--bucket", "hour"],
        ):
            # 文件不存在：错误必须先于文件读取确定
            self._assert_error(
                ["time-bucket-aggregation", self.missing] + args,
                "invalid_aggregation_range",
            )

    def test_bad_time_values_are_range_error(self):
        base = ["time-bucket-aggregation", self.missing, "--bucket", "hour"]
        for args in (
            ["--start-time", "abc", "--end-time", "10"],
            ["--start-time", "-1", "--end-time", "10"],
            ["--start-time", "10", "--end-time", "10"],
            ["--start-time", "100", "--end-time", "10"],
        ):
            self._assert_error(base + args, "invalid_aggregation_range")

    def test_missing_or_bad_bucket_is_unsupported_error(self):
        base = [
            "time-bucket-aggregation", self.missing,
            "--start-time", "0", "--end-time", "10",
        ]
        self._assert_error(base, "unsupported_aggregation_bucket")
        for bad in ("week", "HOUR", "hours", "Day", ""):
            self._assert_error(
                base + ["--bucket", bad],
                "unsupported_aggregation_bucket",
            )

    def test_blank_filter_is_aggregation_filter_error(self):
        base = [
            "time-bucket-aggregation", self.missing,
            "--start-time", "0", "--end-time", "10", "--bucket", "hour",
        ]
        self._assert_error(
            base + ["--address", "   "], "invalid_aggregation_filter"
        )
        self._assert_error(
            base + ["--method", "  "], "invalid_aggregation_filter"
        )

    def test_garbage_cursor_is_aggregation_cursor_error(self):
        self._assert_error(
            self._base("--cursor", "not-a-cursor!!!"),
            "invalid_aggregation_cursor",
        )

    def test_cursor_bound_to_query_conditions(self):
        code, page1, _ = self._run(self._base("--page-size", "1"))
        self.assertEqual(code, 0)
        # 改桶粒度
        self._assert_error([
            "time-bucket-aggregation", self.path,
            "--start-time", "0", "--end-time", str(4 * HOUR),
            "--bucket", "day", "--page-size", "1",
            "--cursor", page1["next_cursor"],
        ], "invalid_aggregation_cursor")
        # 改时间窗
        self._assert_error([
            "time-bucket-aggregation", self.path,
            "--start-time", "0", "--end-time", str(5 * HOUR),
            "--bucket", "hour", "--page-size", "1",
            "--cursor", page1["next_cursor"],
        ], "invalid_aggregation_cursor")
        # 加地址筛选
        self._assert_error(
            self._base("--address", "alice", "--page-size", "1",
                       "--cursor", page1["next_cursor"]),
            "invalid_aggregation_cursor",
        )

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

    def test_page_beyond_last_is_empty_with_null_cursor(self):
        code, page1, _ = self._run(self._base())
        self.assertIsNone(page1["next_cursor"])
        # 翻到末页之后：空页 + null 游标
        from tx_indexer.cursor import (
            encode_time_bucket_aggregation_cursor,
        )
        filters = {"address": None, "method": None,
                   "start_time": 0, "end_time": 4 * HOUR}
        cursor = encode_time_bucket_aggregation_cursor(
            filters, "hour", 3 * HOUR
        )
        code, page, _ = self._run(self._base("--cursor", cursor))
        self.assertEqual(code, 0)
        self.assertEqual(page["buckets"], [])
        self.assertEqual(page["total_buckets"], 4)
        self.assertIsNone(page["next_cursor"])


if __name__ == "__main__":
    unittest.main()
