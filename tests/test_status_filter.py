"""--status 成功/失败筛选与 status-stats 汇总测试。"""

import unittest

from tx_indexer.engine import TxIndexer, normalize_filters
from tx_indexer.errors import (
    InvalidCursorError,
    InvalidStatusFilterError,
)
from tx_indexer.loader import load_lines


def line(tx_hash, amount, success=None, method="transfer", ts=10,
         frm="alice", to="bob", block=1):
    import json

    obj = {
        "tx_hash": tx_hash, "block_number": block, "timestamp": ts,
        "from_address": frm, "to_address": to, "method": method,
        "amount": amount,
    }
    if success is not None:
        obj["success"] = success
    return json.dumps(obj)


def build(lines):
    return TxIndexer(load_lines(lines))


class StatusFilterValidationTest(unittest.TestCase):
    def test_none_success_failure_are_valid(self):
        self.assertIsNone(normalize_filters()["status"])
        self.assertEqual(normalize_filters(status="success")["status"],
                         "success")
        self.assertEqual(normalize_filters(status="failure")["status"],
                         "failure")

    def test_invalid_values(self):
        for bad in ("", "   ", "Success", "FAILURE", "succeed",
                    0, 1, True, False, None, object()):
            if bad is None:
                # None 是合法的「不筛选」，跳过
                continue
            with self.assertRaises(InvalidStatusFilterError):
                normalize_filters(status=bad)

    def test_none_explicitly_means_no_filter(self):
        # status 缺省 / None：不过滤，键值为 None
        self.assertIn("status", normalize_filters())


class StatusQueryTest(unittest.TestCase):
    def setUp(self):
        self.idx = build([
            line("s1", "100", True, ts=10),
            line("f1", "50", False, ts=20),
            line("s2", "5", None, ts=30),      # 缺省 -> 成功
        ])

    def test_filter_success(self):
        result = self.idx.query(normalize_filters(status="success"))
        self.assertEqual([t["tx_hash"] for t in result["transactions"]],
                         ["s1", "s2"])
        self.assertEqual(result["total"], 2)

    def test_filter_failure(self):
        result = self.idx.query(normalize_filters(status="failure"))
        self.assertEqual([t["tx_hash"] for t in result["transactions"]],
                         ["f1"])
        self.assertEqual(result["total"], 1)

    def test_no_filter_matches_all(self):
        result = self.idx.query(normalize_filters())
        self.assertEqual(result["total"], 3)

    def test_intersects_with_other_filters(self):
        # method 全部为 transfer，加金额下界只保留 >=50
        result = self.idx.query(
            normalize_filters(status="success", min_amount="50")
        )
        self.assertEqual([t["tx_hash"] for t in result["transactions"]],
                         ["s1"])

    def test_query_transactions_omit_success_field(self):
        result = self.idx.query(normalize_filters(status="success"))
        tx = result["transactions"][0]
        self.assertNotIn("success", tx)
        self.assertEqual(
            set(tx),
            {"tx_hash", "block_number", "timestamp", "from_address",
             "to_address", "method", "amount"},
        )

    def test_status_applies_to_grouped_stats(self):
        stats = self.idx.stats(normalize_filters(status="failure"))
        self.assertEqual(stats["total_count"], 1)
        self.assertEqual(stats["total_amount"], "50")

        method = self.idx.method_stats(normalize_filters(status="success"))
        self.assertEqual(method["total_groups"], 1)
        self.assertEqual(method["groups"][0]["total_count"], 2)

        buckets = self.idx.time_stats(
            normalize_filters(status="failure"), bucket_size=60
        )
        self.assertEqual(buckets["total_groups"], 1)
        self.assertEqual(buckets["groups"][0]["total_count"], 1)


class StatusStatsTest(unittest.TestCase):
    def test_mixed_counts_and_amounts(self):
        idx = build([
            line("s1", "100", True),
            line("f1", "30", False),
            line("s2", "5", None),
            line("f2", "20", False),
        ])
        result = idx.status_stats(normalize_filters())
        self.assertEqual(result, {
            "total_count": 4,
            "success_count": 2,
            "failure_count": 2,
            "success_amount": "105",
            "failure_amount": "50",
        })

    def test_empty_result_is_all_zero(self):
        idx = build([line("s1", "100", True)])
        result = idx.status_stats(normalize_filters(method="nope"))
        self.assertEqual(result, {
            "total_count": 0,
            "success_count": 0,
            "failure_count": 0,
            "success_amount": "0",
            "failure_amount": "0",
        })

    def test_status_success_zeroes_failure(self):
        idx = build([line("s1", "100", True), line("f1", "30", False)])
        result = idx.status_stats(normalize_filters(status="success"))
        self.assertEqual(result, {
            "total_count": 1,
            "success_count": 1,
            "failure_count": 0,
            "success_amount": "100",
            "failure_amount": "0",
        })

    def test_status_failure_zeroes_success(self):
        idx = build([line("s1", "100", True), line("f1", "30", False)])
        result = idx.status_stats(normalize_filters(status="failure"))
        self.assertEqual(result, {
            "total_count": 1,
            "success_count": 0,
            "failure_count": 1,
            "success_amount": "0",
            "failure_amount": "30",
        })

    def test_respects_other_filters(self):
        idx = build([
            line("s1", "100", True, method="transfer"),
            line("f1", "30", False, method="approve"),
        ])
        result = idx.status_stats(normalize_filters(method="approve"))
        self.assertEqual(result["failure_count"], 1)
        self.assertEqual(result["success_count"], 0)
        self.assertEqual(result["failure_amount"], "30")


class StatusCursorTest(unittest.TestCase):
    def setUp(self):
        self.idx = build([
            line("s1", "1", True, block=1),
            line("f1", "2", False, block=2),
            line("s2", "3", True, block=3),
            line("f2", "4", False, block=4),
        ])

    def test_walk_pages_same_status(self):
        filters = normalize_filters(status="success")
        collected, cursor = [], None
        while True:
            page = self.idx.query(filters, page_size=1, cursor=cursor)
            collected.extend(t["tx_hash"] for t in page["transactions"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(collected, ["s1", "s2"])

    def test_change_status_invalidates_cursor(self):
        cursor = self.idx.query(
            normalize_filters(status="success"), page_size=1
        )["next_cursor"]
        with self.assertRaises(InvalidCursorError):
            self.idx.query(
                normalize_filters(status="failure"), page_size=1,
                cursor=cursor,
            )

    def test_status_cursor_not_usable_without_status(self):
        cursor = self.idx.query(
            normalize_filters(status="success"), page_size=1
        )["next_cursor"]
        with self.assertRaises(InvalidCursorError):
            self.idx.query(normalize_filters(), page_size=1, cursor=cursor)

    def test_legacy_cursor_without_status_field(self):
        import base64
        import json

        from tx_indexer.cursor import encode_cursor

        filters = normalize_filters()
        token = encode_cursor(filters, 1, "s1")
        raw = json.loads(
            base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        )
        del raw["f"]["status"]  # 模拟旧版无 status 字段的游标
        legacy = base64.urlsafe_b64encode(
            json.dumps(raw, separators=(",", ":"), sort_keys=True).encode()
        ).rstrip(b"=").decode()

        # 未指定状态：旧游标可续翻
        page = self.idx.query(normalize_filters(), page_size=1, cursor=legacy)
        self.assertEqual([t["tx_hash"] for t in page["transactions"]],
                         ["f1"])
        # 指定状态：旧游标失效
        with self.assertRaises(InvalidCursorError):
            self.idx.query(
                normalize_filters(status="success"), cursor=legacy
            )

    def test_group_stats_cursor_bound_to_status(self):
        idx = build([
            line("a", "100", True, method="m1"),
            line("c", "50", True, method="m3"),
            line("b", "200", False, method="m2"),
        ])
        cursor = idx.method_stats(
            normalize_filters(status="success"), page_size=1
        )["next_cursor"]
        self.assertIsNotNone(cursor)
        with self.assertRaises(InvalidCursorError):
            idx.method_stats(
                normalize_filters(status="failure"), page_size=1,
                cursor=cursor,
            )
        with self.assertRaises(InvalidCursorError):
            idx.method_stats(
                normalize_filters(), page_size=1, cursor=cursor
            )


if __name__ == "__main__":
    unittest.main()
