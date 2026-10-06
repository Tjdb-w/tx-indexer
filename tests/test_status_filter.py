"""状态筛选与 status-stats 的引擎级测试。"""

import base64
import json
import unittest

from tx_indexer.engine import TxIndexer, normalize_filters
from tx_indexer.errors import (
    InvalidCursorError,
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


def hashes(page):
    return [t["tx_hash"] for t in page["transactions"]]


class StatusFilterNormalizationTest(unittest.TestCase):
    def test_none_means_no_filter(self):
        self.assertIsNone(normalize_filters()["status"])
        self.assertEqual(
            normalize_filters(status="success")["status"], "success"
        )
        self.assertEqual(
            normalize_filters(status="failure")["status"], "failure"
        )

    def test_invalid_values(self):
        for bad in ("", "  ", "Success", "FAILURE", "ok", " success",
                    True, False, 0, 1, 1.0, (), [], object()):
            with self.assertRaises(InvalidStatusFilterError):
                normalize_filters(status=bad)

    def test_none_is_allowed_explicitly(self):
        self.assertIsNone(normalize_filters(status=None)["status"])


class StatusFilterQueryTest(unittest.TestCase):
    def setUp(self):
        self.records = [
            rec("h1", 1, 10, "alice", "bob", "transfer", "10"),
            rec("h2", 2, 20, "bob", "alice", "approve", "21",
                success=True),
            rec("h3", 2, 30, "alice", "carol", "transfer", "5",
                success=False),
            rec("h4", 3, 40, "bob", "carol", "transfer", "7",
                success=False),
        ]
        self.indexer = TxIndexer(self.records)

    def test_no_status_returns_all(self):
        page = self.indexer.query(normalize_filters())
        self.assertEqual(hashes(page), ["h1", "h2", "h3", "h4"])
        self.assertEqual(page["total"], 4)

    def test_success_matches_true_and_absent(self):
        page = self.indexer.query(normalize_filters(status="success"))
        self.assertEqual(hashes(page), ["h1", "h2"])
        self.assertEqual(page["total"], 2)

    def test_failure_matches_false_only(self):
        page = self.indexer.query(normalize_filters(status="failure"))
        self.assertEqual(hashes(page), ["h3", "h4"])
        self.assertEqual(page["total"], 2)

    def test_status_intersects_other_filters(self):
        page = self.indexer.query(
            normalize_filters(method="transfer", status="success")
        )
        self.assertEqual(hashes(page), ["h1"])
        self.assertEqual(page["total"], 1)

    def test_status_applies_to_all_stats_commands(self):
        f = normalize_filters(status="failure")
        self.assertEqual(
            self.indexer.stats(f),
            {
                "total_count": 2,
                "total_amount": "12",
                "min_amount": "5",
                "max_amount": "7",
                "avg_amount": "6",
            },
        )
        method_groups = self.indexer.method_stats(f)["groups"]
        self.assertEqual(
            [(g["method"], g["total_count"]) for g in method_groups],
            [("transfer", 2)],
        )
        pairs = self.indexer.pair_stats(f)["groups"]
        self.assertEqual(
            sorted((p["from_address"], p["to_address"], p["total_count"])
                   for p in pairs),
            [("alice", "carol", 1), ("bob", "carol", 1)],
        )


class StatusStatsTest(unittest.TestCase):
    def setUp(self):
        self.records = [
            rec("h1", 1, 10, "alice", "bob", "transfer", "10"),
            rec("h2", 2, 20, "bob", "alice", "approve", "21",
                success=True),
            rec("h3", 2, 30, "alice", "carol", "transfer", "5",
                success=False),
            rec("h4", 3, 40, "bob", "carol", "transfer", "7",
                success=False),
        ]
        self.indexer = TxIndexer(self.records)

    def test_all_records(self):
        self.assertEqual(
            self.indexer.status_stats(normalize_filters()),
            {
                "total_count": 4,
                "success_count": 2,
                "failure_count": 2,
                "success_amount": "31",
                "failure_amount": "12",
            },
        )

    def test_empty_result_is_all_zero(self):
        result = self.indexer.status_stats(
            normalize_filters(method="nonexistent")
        )
        self.assertEqual(
            result,
            {
                "total_count": 0,
                "success_count": 0,
                "failure_count": 0,
                "success_amount": "0",
                "failure_amount": "0",
            },
        )

    def test_filtered_success_zeros_failure_side(self):
        result = self.indexer.status_stats(
            normalize_filters(status="success")
        )
        self.assertEqual(result["total_count"], 2)
        self.assertEqual(result["success_count"], 2)
        self.assertEqual(result["failure_count"], 0)
        self.assertEqual(result["success_amount"], "31")
        self.assertEqual(result["failure_amount"], "0")

    def test_filtered_failure_zeros_success_side(self):
        result = self.indexer.status_stats(
            normalize_filters(status="failure")
        )
        self.assertEqual(result["total_count"], 2)
        self.assertEqual(result["success_count"], 0)
        self.assertEqual(result["failure_count"], 2)
        self.assertEqual(result["success_amount"], "0")
        self.assertEqual(result["failure_amount"], "12")


class StatusCursorBindingTest(unittest.TestCase):
    def setUp(self):
        self.records = [
            rec("h1", 1, 10, "alice", "bob", "transfer", "10"),
            rec("h2", 2, 20, "bob", "alice", "approve", "21",
                success=True),
            rec("h3", 2, 30, "alice", "carol", "transfer", "5",
                success=False),
            rec("h4", 3, 40, "bob", "carol", "transfer", "7",
                success=False),
        ]
        self.indexer = TxIndexer(self.records)

    def test_pagination_under_same_status(self):
        f = normalize_filters(status="success")
        page1 = self.indexer.query(f, page_size=1)
        self.assertEqual(hashes(page1), ["h1"])
        page2 = self.indexer.query(
            f, page_size=1, cursor=page1["next_cursor"]
        )
        self.assertEqual(hashes(page2), ["h2"])
        self.assertIsNone(page2["next_cursor"])

    def test_changing_status_invalidates_cursor(self):
        page1 = self.indexer.query(
            normalize_filters(), page_size=2
        )
        cursor = page1["next_cursor"]
        with self.assertRaises(InvalidCursorError):
            self.indexer.query(
                normalize_filters(status="success"),
                page_size=2,
                cursor=cursor,
            )
        with self.assertRaises(InvalidCursorError):
            self.indexer.query(
                normalize_filters(status="failure"),
                page_size=2,
                cursor=cursor,
            )
        success_page = self.indexer.query(
            normalize_filters(status="success"), page_size=1
        )
        # success 游标不能切到 failure
        with self.assertRaises(InvalidCursorError):
            self.indexer.query(
                normalize_filters(status="failure"),
                page_size=1,
                cursor=success_page["next_cursor"],
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
        page1 = self.indexer.query(normalize_filters(), page_size=2)
        legacy = self._strip_status_field(page1["next_cursor"])

        # 未指定状态：旧游标可续翻，不跳过、不重复
        page2 = self.indexer.query(
            normalize_filters(), page_size=2, cursor=legacy
        )
        self.assertEqual(hashes(page2), ["h3", "h4"])

        # 指定任一状态：旧游标失效
        for status in ("success", "failure"):
            with self.assertRaises(InvalidCursorError):
                self.indexer.query(
                    normalize_filters(status=status),
                    page_size=2,
                    cursor=legacy,
                )

    def test_status_bound_on_group_stats_cursors(self):
        page1 = self.indexer.address_stats(
            normalize_filters(), page_size=1
        )
        with self.assertRaises(InvalidCursorError):
            self.indexer.address_stats(
                normalize_filters(status="success"),
                page_size=1,
                cursor=page1["next_cursor"],
            )


if __name__ == "__main__":
    unittest.main()
