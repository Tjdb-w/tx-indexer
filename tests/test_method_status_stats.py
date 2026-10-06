"""method-status-stats（按 method 拆分成功/失败计数与金额的分页统计）测试。"""

import base64
import io
import json
import os
import sys
import tempfile
import unittest

from tx_indexer.cli import main as cli_main
from tx_indexer.cursor import (
    decode_method_status_stats_cursor,
    encode_method_stats_cursor,
    encode_method_status_stats_cursor,
)
from tx_indexer.engine import TxIndexer, normalize_filters
from tx_indexer.errors import (
    InvalidCursorError,
    InvalidPageSizeError,
    InvalidStatusFilterError,
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
    if success != "__absent__":
        record["success"] = success
    return record


RECORDS = [
    rec("h1", 1, 10, "alice", "bob", "transfer", "10"),              # 成功（缺省）
    rec("h2", 2, 20, "bob", "alice", "approve", "21", True),         # 成功
    rec("h3", 2, 30, "alice", "carol", "transfer", "5", False),      # 失败
    rec("h4", 3, 40, "bob", "carol", "transfer", "7", False),        # 失败
]


class MethodStatusStatsTest(unittest.TestCase):
    def setUp(self):
        self.idx = TxIndexer([dict(r) for r in RECORDS])

    def test_grouping_and_success_failure_split(self):
        result = self.idx.method_status_stats(normalize_filters())
        groups = result["groups"]
        # transfer：total 22 / 3 笔，成功 10 / 1 笔，失败 12 / 2 笔
        # approve：total 21 / 1 笔，全成功
        self.assertEqual(
            [
                (
                    g["method"],
                    g["total_count"],
                    g["total_amount"],
                    g["avg_amount"],
                    g["success_count"],
                    g["failure_count"],
                    g["success_amount"],
                    g["failure_amount"],
                )
                for g in groups
            ],
            [
                ("transfer", 3, "22", "7", 1, 2, "10", "12"),
                ("approve", 1, "21", "21", 1, 0, "21", "0"),
            ],
        )
        self.assertEqual(result["total_groups"], 2)
        self.assertIsNone(result["next_cursor"])

    def test_group_field_order_and_types(self):
        group = self.idx.method_status_stats(
            normalize_filters()
        )["groups"][0]
        self.assertEqual(
            list(group.keys()),
            [
                "method",
                "total_count",
                "total_amount",
                "avg_amount",
                "success_count",
                "failure_count",
                "success_amount",
                "failure_amount",
            ],
        )
        for name in ("total_amount", "avg_amount", "success_amount",
                     "failure_amount"):
            self.assertIsInstance(group[name], str)
            # 无前导零
            self.assertEqual(group[name], str(int(group[name])))
        for name in ("total_count", "success_count", "failure_count"):
            self.assertIsInstance(group[name], int)
            self.assertNotIsInstance(group[name], bool)

    def test_no_leading_zero_amounts(self):
        idx = TxIndexer([
            rec("x", 1, 1, "a", "b", "m", "0"),
            rec("y", 2, 2, "a", "b", "m", "0", False),
        ])
        group = idx.method_status_stats(normalize_filters())["groups"][0]
        self.assertEqual(group["total_amount"], "0")
        self.assertEqual(group["avg_amount"], "0")
        self.assertEqual(group["success_amount"], "0")
        self.assertEqual(group["failure_amount"], "0")

    def test_invariants_per_group(self):
        for g in self.idx.method_status_stats(normalize_filters())["groups"]:
            self.assertEqual(
                g["total_count"], g["success_count"] + g["failure_count"]
            )
            self.assertEqual(
                int(g["total_amount"]),
                int(g["success_amount"]) + int(g["failure_amount"]),
            )
            self.assertEqual(
                g["avg_amount"],
                str(int(g["total_amount"]) // g["total_count"]),
            )

    def test_sort_amount_count_success_failure_method(self):
        records = [
            # beta / gamma：total=10、count=2、success=1、failure=1 全等，
            # 按 method 码点
            rec("b1", 2, 2, "a", "b", "beta", "5", True),
            rec("b2", 3, 3, "a", "b", "beta", "5", False),
            rec("g1", 4, 4, "a", "b", "gamma", "6", True),
            rec("g2", 5, 5, "a", "b", "gamma", "4", False),
            # zeta：total=10、count=1、success=1；alpha：total=10、count=1、
            # success=0 → count 相同后 success_count 高的 zeta 在前
            rec("z1", 1, 1, "a", "b", "zeta", "10", True),
            rec("a1", 6, 6, "a", "b", "alpha", "10", False),
        ]
        result = TxIndexer(records).method_status_stats(normalize_filters())
        self.assertEqual(
            [(g["method"], g["success_count"], g["failure_count"])
             for g in result["groups"]],
            [
                ("beta", 1, 1),
                ("gamma", 1, 1),
                ("zeta", 1, 0),
                ("alpha", 0, 1),
            ],
        )

    def test_status_success_zeros_failure_side(self):
        result = self.idx.method_status_stats(
            normalize_filters(status="success")
        )
        groups = {g["method"]: g for g in result["groups"]}
        self.assertEqual(set(groups), {"approve", "transfer"})
        transfer = groups["transfer"]
        self.assertEqual(transfer["total_count"], 1)
        self.assertEqual(transfer["total_amount"], "10")
        self.assertEqual(transfer["success_count"], 1)
        self.assertEqual(transfer["failure_count"], 0)
        self.assertEqual(transfer["success_amount"], "10")
        self.assertEqual(transfer["failure_amount"], "0")

    def test_status_failure_zeros_success_side(self):
        result = self.idx.method_status_stats(
            normalize_filters(status="failure")
        )
        groups = {g["method"]: g for g in result["groups"]}
        self.assertEqual(set(groups), {"transfer"})
        transfer = groups["transfer"]
        self.assertEqual(transfer["total_count"], 2)
        self.assertEqual(transfer["total_amount"], "12")
        self.assertEqual(transfer["success_count"], 0)
        self.assertEqual(transfer["failure_count"], 2)
        self.assertEqual(transfer["success_amount"], "0")
        self.assertEqual(transfer["failure_amount"], "12")

    def test_no_match(self):
        result = self.idx.method_status_stats(
            normalize_filters(method="nope")
        )
        self.assertEqual(result, {
            "groups": [],
            "total_groups": 0,
            "next_cursor": None,
        })

    def test_filters_intersect_before_grouping(self):
        result = self.idx.method_status_stats(
            normalize_filters(from_address=["bob"], method=["transfer"])
        )
        self.assertEqual(
            [
                (g["method"], g["total_count"], g["failure_amount"])
                for g in result["groups"]
            ],
            [("transfer", 1, "7")],
        )

    def test_pagination_no_skip_no_dup_no_reorder(self):
        filters = normalize_filters()
        # 构造足够多的分组以产生多页
        records = []
        for index, method in enumerate("abcdefgh"):
            records.append(
                rec("ok-%s" % method, index * 2, index, "a", "b",
                    method, str(100 + index), True)
            )
            records.append(
                rec("bad-%s" % method, index * 2 + 1, index, "a", "b",
                    method, str(index), False)
            )
        idx = TxIndexer(records)
        full = [
            g["method"]
            for g in idx.method_status_stats(filters, page_size=1000)["groups"]
        ]

        collected = []
        cursor = None
        pages = 0
        while True:
            page = idx.method_status_stats(
                filters, page_size=3, cursor=cursor
            )
            pages += 1
            self.assertEqual(page["total_groups"], 8)
            collected.extend(g["method"] for g in page["groups"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
            self.assertLessEqual(pages, 8)
        self.assertEqual(collected, full)
        self.assertEqual(pages, 3)  # 8 组、page_size=3：3 页

    def test_page_size_bounds(self):
        with self.assertRaises(InvalidPageSizeError):
            self.idx.method_status_stats(normalize_filters(), page_size=0)
        with self.assertRaises(InvalidPageSizeError):
            self.idx.method_status_stats(
                normalize_filters(), page_size=1001
            )
        with self.assertRaises(InvalidPageSizeError):
            self.idx.method_status_stats(
                normalize_filters(), page_size="10"
            )
        with self.assertRaises(InvalidPageSizeError):
            self.idx.method_status_stats(
                normalize_filters(), page_size=True
            )

    def test_page_size_one_thousand_allowed(self):
        page = self.idx.method_status_stats(
            normalize_filters(), page_size=1000
        )
        self.assertEqual(page["total_groups"], 2)
        self.assertIsNone(page["next_cursor"])

    def test_cursor_not_bound_to_page_size(self):
        filters = normalize_filters()
        first = self.idx.method_status_stats(
            filters, page_size=1
        )
        self.assertIsNotNone(first["next_cursor"])
        # 用更大的 page_size 续翻：直接从第 2 组开始，不重不漏
        rest = self.idx.method_status_stats(
            filters, page_size=100, cursor=first["next_cursor"]
        )
        self.assertEqual(
            [g["method"] for g in rest["groups"]], ["approve"]
        )
        self.assertIsNone(rest["next_cursor"])

    def test_cursor_filter_mismatch(self):
        cursor = self.idx.method_status_stats(
            normalize_filters(), page_size=1
        )["next_cursor"]
        with self.assertRaises(InvalidCursorError):
            self.idx.method_status_stats(
                normalize_filters(method="transfer"),
                page_size=1,
                cursor=cursor,
            )

    def test_cursor_bound_to_status(self):
        cursor = self.idx.method_status_stats(
            normalize_filters(status="success"), page_size=1
        )["next_cursor"]
        # 去掉 status 续翻报错
        with self.assertRaises(InvalidCursorError):
            self.idx.method_status_stats(
                normalize_filters(), page_size=1, cursor=cursor
            )
        # 改成 failure 续翻报错
        with self.assertRaises(InvalidCursorError):
            self.idx.method_status_stats(
                normalize_filters(status="failure"),
                page_size=1,
                cursor=cursor,
            )

    @staticmethod
    def _strip_status_field(cursor):
        padding = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(cursor + padding)
        payload = json.loads(raw.decode("utf-8"))
        del payload["f"]["status"]
        encoded = json.dumps(
            payload, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        return base64.urlsafe_b64encode(encoded).rstrip(b"=").decode(
            "ascii"
        )

    def test_legacy_cursor_without_status_field(self):
        cursor = self.idx.method_status_stats(
            normalize_filters(), page_size=1
        )["next_cursor"]
        legacy = self._strip_status_field(cursor)
        # 未指定状态：旧游标可续翻
        page = self.idx.method_status_stats(
            normalize_filters(), page_size=1, cursor=legacy
        )
        self.assertEqual(
            [g["method"] for g in page["groups"]], ["approve"]
        )
        # 指定任一状态：旧游标失效
        for status in ("success", "failure"):
            with self.assertRaises(InvalidCursorError):
                self.idx.method_status_stats(
                    normalize_filters(status=status),
                    page_size=1,
                    cursor=legacy,
                )

    def test_cursor_amount_equivalence_leading_zeros(self):
        cursor = self.idx.method_status_stats(
            normalize_filters(min_amount="5"), page_size=1
        )["next_cursor"]
        # 仅前导零不同：数值等价，可续翻
        page = self.idx.method_status_stats(
            normalize_filters(min_amount="005"),
            page_size=1,
            cursor=cursor,
        )
        self.assertEqual(page["total_groups"], 2)
        # 改变金额边界：游标失效
        with self.assertRaises(InvalidCursorError):
            self.idx.method_status_stats(
                normalize_filters(min_amount="6"),
                page_size=1,
                cursor=cursor,
            )

    def test_cursor_cross_command_rejected(self):
        filters = normalize_filters()
        method_cursor = encode_method_stats_cursor(filters, 10, 1, "m")
        with self.assertRaises(InvalidCursorError):
            self.idx.method_status_stats(
                filters, cursor=method_cursor
            )
        own_cursor = encode_method_status_stats_cursor(
            filters, 10, 1, 1, 0, "m"
        )
        with self.assertRaises(InvalidCursorError):
            self.idx.method_stats(filters, cursor=own_cursor)

    def test_cursor_garbage_and_tamper(self):
        for bad in ("", "not-base64!!!", "bm9wZQ", "%%%"):
            with self.assertRaises(InvalidCursorError):
                self.idx.method_status_stats(
                    normalize_filters(), cursor=bad
                )
        cursor = self.idx.method_status_stats(
            normalize_filters(), page_size=1
        )["next_cursor"]
        # 篡改最后一个字符
        tampered = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
        if tampered != cursor:
            with self.assertRaises(InvalidCursorError):
                self.idx.method_status_stats(
                    normalize_filters(), cursor=tampered
                )

    def test_decode_rejects_bad_marker(self):
        filters = normalize_filters()
        # failure_count 为负
        bad = encode_method_status_stats_cursor(
            filters, 10, 1, 2, -1, "m"
        )
        with self.assertRaises(InvalidCursorError):
            decode_method_status_stats_cursor(bad, filters)
        # method 为空
        bad = encode_method_status_stats_cursor(
            filters, 10, 1, 1, 0, ""
        )
        with self.assertRaises(InvalidCursorError):
            decode_method_status_stats_cursor(bad, filters)

    def test_invalid_status_filter(self):
        for bad in ("Success", "FAILURE", "ok", "", "  ", True, None):
            if bad is None:
                continue
            with self.assertRaises(InvalidStatusFilterError):
                self.idx.method_status_stats(
                    normalize_filters(status=bad)
                )

    def test_big_amounts_exact_decimal(self):
        big = "123456789012345678901234567890"
        idx = TxIndexer([
            rec("x", 1, 1, "a", "b", "m", big),
            rec("y", 2, 2, "a", "b", "m", "5", False),
        ])
        group = idx.method_status_stats(
            normalize_filters()
        )["groups"][0]
        self.assertEqual(group["total_amount"], str(int(big) + 5))
        self.assertEqual(group["success_amount"], big)
        self.assertEqual(group["failure_amount"], "5")
        self.assertEqual(
            group["avg_amount"], str((int(big) + 5) // 2)
        )


class MethodStatusStatsCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "data.jsonl")
        with open(self.path, "w", encoding="utf-8") as fh:
            for record in RECORDS:
                fh.write(json.dumps(record) + "\n")
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
        code = cli_main(argv)
        out = sys.stdout.getvalue()
        err = sys.stderr.getvalue()
        return (
            code,
            json.loads(out) if out.strip() else None,
            json.loads(err.strip().splitlines()[-1]) if err.strip() else None,
        )

    def test_cli_basic(self):
        code, result, err = self._run(
            ["method-status-stats", self.path]
        )
        self.assertEqual(code, 0)
        self.assertIsNone(err)
        self.assertEqual(result["total_groups"], 2)
        self.assertEqual(
            [g["method"] for g in result["groups"]],
            ["transfer", "approve"],
        )
        transfer = result["groups"][0]
        self.assertEqual(transfer["failure_amount"], "12")
        self.assertEqual(transfer["success_amount"], "10")

    def test_cli_status_filter(self):
        code, result, err = self._run(
            ["method-status-stats", self.path, "--status", "failure"]
        )
        self.assertEqual(code, 0)
        self.assertIsNone(err)
        self.assertEqual(
            [g["method"] for g in result["groups"]], ["transfer"]
        )
        group = result["groups"][0]
        self.assertEqual(group["success_count"], 0)
        self.assertEqual(group["success_amount"], "0")

    def test_cli_pagination(self):
        code, page1, err = self._run(
            ["method-status-stats", self.path, "--page-size", "1"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            [g["method"] for g in page1["groups"]], ["transfer"]
        )
        self.assertIsNotNone(page1["next_cursor"])

        code, page2, err = self._run(
            ["method-status-stats", self.path, "--page-size", "1",
             "--cursor", page1["next_cursor"]]
        )
        self.assertEqual(code, 0)
        self.assertIsNone(err)
        self.assertEqual(
            [g["method"] for g in page2["groups"]], ["approve"]
        )
        self.assertIsNone(page2["next_cursor"])

    def test_cli_invalid_cursor(self):
        code, result, err = self._run(
            ["method-status-stats", self.path, "--cursor", "garbage!!"]
        )
        self.assertEqual(code, 2)
        self.assertIsNone(result)
        self.assertEqual(err["error"], "invalid_cursor")
        self.assertIsNone(err["input_line"])

    def test_cli_invalid_status_before_data(self):
        code, result, err = self._run(
            ["method-status-stats",
             os.path.join(self.tmp.name, "missing.jsonl"),
             "--status", "Success"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(err["error"], "invalid_status_filter")

    def test_cli_invalid_page_size(self):
        code, result, err = self._run(
            ["method-status-stats", self.path, "--page-size", "0"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(err["error"], "invalid_page_size")


if __name__ == "__main__":
    unittest.main()
