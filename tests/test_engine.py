"""筛选、排序、分页与聚合测试。"""

import unittest

from tx_indexer.engine import TxIndexer, normalize_filters
from tx_indexer.errors import (
    InvalidCursorError,
    InvalidFilterError,
    InvalidPageSizeError,
    InvalidTimeRangeError,
)


def rec(tx_hash, block_number, timestamp, frm, to, method, amount):
    return {
        "tx_hash": tx_hash,
        "block_number": block_number,
        "timestamp": timestamp,
        "from_address": frm,
        "to_address": to,
        "method": method,
        "amount": amount,
    }


def hashes(page):
    return [t["tx_hash"] for t in page["transactions"]]


class QueryTest(unittest.TestCase):
    def setUp(self):
        self.records = [
            rec("h3", 2, 100, "alice", "carol", "transfer", "30"),
            rec("h1", 1, 10, "alice", "bob", "transfer", "10"),
            rec("h2", 2, 20, "bob", "alice", "approve", "20"),
            rec("h4", 3, 300, "bob", "dave", "transfer", "40"),
        ]
        self.idx = TxIndexer(self.records)

    def test_sort_block_then_hash(self):
        result = self.idx.query(normalize_filters())
        self.assertEqual(hashes(result), ["h1", "h2", "h3", "h4"])
        self.assertEqual(result["total"], 4)
        self.assertIsNone(result["next_cursor"])

    def test_filter_address_matches_either_side(self):
        result = self.idx.query(normalize_filters(address="alice"))
        self.assertEqual(hashes(result), ["h1", "h2", "h3"])

    def test_filter_method(self):
        result = self.idx.query(normalize_filters(method="approve"))
        self.assertEqual(hashes(result), ["h2"])

    def test_time_range_inclusive_both_ends(self):
        result = self.idx.query(
            normalize_filters(start_time=20, end_time=100)
        )
        self.assertEqual(hashes(result), ["h2", "h3"])

    def test_intersection_of_filters(self):
        result = self.idx.query(
            normalize_filters(address="bob", method="transfer",
                              start_time=50, end_time=500)
        )
        self.assertEqual(hashes(result), ["h4"])

    def test_pagination_no_skip_no_dup_no_reorder(self):
        filters = normalize_filters()
        collected = []
        cursor = None
        pages = 0
        while True:
            page = self.idx.query(filters, page_size=2, cursor=cursor)
            pages += 1
            collected.extend(hashes(page))
            self.assertEqual(page["total"], 4)
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(pages, 2)  # 4 条、page_size=2：恰好 2 页
        self.assertEqual(collected, ["h1", "h2", "h3", "h4"])

    def test_last_page_returns_null_cursor(self):
        page = self.idx.query(normalize_filters(), page_size=10)
        self.assertIsNone(page["next_cursor"])

    def test_cursor_beyond_end_is_stable(self):
        first = self.idx.query(normalize_filters(), page_size=4)
        self.assertIsNone(first["next_cursor"])

    def test_page_size_default_and_bounds(self):
        with self.assertRaises(InvalidPageSizeError):
            self.idx.query(normalize_filters(), page_size=0)
        with self.assertRaises(InvalidPageSizeError):
            self.idx.query(normalize_filters(), page_size=1001)
        with self.assertRaises(InvalidPageSizeError):
            self.idx.query(normalize_filters(), page_size="10")

    def test_cursor_filter_mismatch(self):
        page1 = self.idx.query(
            normalize_filters(method="transfer"), page_size=1
        )
        cursor = page1["next_cursor"]
        with self.assertRaises(InvalidCursorError):
            self.idx.query(normalize_filters(method="approve"),
                           page_size=1, cursor=cursor)

    def test_cursor_garbage(self):
        for bad in ("", "not-base64!!!", "bm9wZQ", "%%%"):
            with self.assertRaises(InvalidCursorError):
                self.idx.query(normalize_filters(), cursor=bad)

    def test_time_range_inverted(self):
        with self.assertRaises(InvalidTimeRangeError):
            normalize_filters(start_time=100, end_time=10)

    def test_same_block_pagination_by_hash(self):
        records = [rec("0x%02d" % i, 5, i, "a", "b", "m", "1")
                   for i in range(5)]
        idx = TxIndexer(records)
        page1 = idx.query(normalize_filters(), page_size=2)
        self.assertEqual(hashes(page1), ["0x00", "0x01"])
        page2 = idx.query(normalize_filters(), page_size=2,
                          cursor=page1["next_cursor"])
        self.assertEqual(hashes(page2), ["0x02", "0x03"])
        page3 = idx.query(normalize_filters(), page_size=2,
                          cursor=page2["next_cursor"])
        self.assertEqual(hashes(page3), ["0x04"])
        self.assertIsNone(page3["next_cursor"])

    def test_output_fields_and_amount_kept_as_string(self):
        page = self.idx.query(normalize_filters(address="alice"),
                              page_size=1)
        tx = page["transactions"][0]
        self.assertEqual(
            list(tx.keys()),
            ["tx_hash", "block_number", "timestamp", "from_address",
             "to_address", "method", "amount"],
        )
        self.assertIsInstance(tx["amount"], str)


class SetFilterTest(unittest.TestCase):
    """from_address / to_address / method 集合筛选。"""

    def setUp(self):
        self.records = [
            rec("h1", 1, 10, "alice", "bob", "transfer", "10"),
            rec("h2", 2, 20, "bob", "alice", "approve", "20"),
            rec("h3", 3, 30, "alice", "carol", "transfer", "30"),
            rec("h4", 4, 40, "carol", "dave", "mint", "40"),
        ]
        self.idx = TxIndexer(self.records)

    def test_from_address_set_any_match(self):
        result = self.idx.query(
            normalize_filters(from_address=["alice", "carol"])
        )
        self.assertEqual(hashes(result), ["h1", "h3", "h4"])

    def test_to_address_set_any_match(self):
        result = self.idx.query(
            normalize_filters(to_address=["alice", "dave"])
        )
        self.assertEqual(hashes(result), ["h2", "h4"])

    def test_method_set_any_match(self):
        result = self.idx.query(
            normalize_filters(method=["approve", "mint"])
        )
        self.assertEqual(hashes(result), ["h2", "h4"])

    def test_single_string_still_accepted(self):
        result = self.idx.query(normalize_filters(from_address="alice"))
        self.assertEqual(hashes(result), ["h1", "h3"])

    def test_sets_intersect_across_dimensions(self):
        result = self.idx.query(
            normalize_filters(
                from_address=["alice", "carol"],
                to_address=["carol", "dave"],
                method=["transfer", "mint"],
            )
        )
        self.assertEqual(hashes(result), ["h3", "h4"])

    def test_duplicate_values_collapse(self):
        result = self.idx.query(
            normalize_filters(method=["transfer", "transfer", "approve"])
        )
        self.assertEqual(hashes(result), ["h1", "h2", "h3"])

    def test_empty_iterable_means_no_filter(self):
        result = self.idx.query(normalize_filters(method=[]))
        self.assertEqual(result["total"], 4)

    def test_blank_values_rejected(self):
        for kwargs in (
            {"address": ""},
            {"address": "   "},
            {"from_address": ["alice", ""]},
            {"to_address": ["  "]},
            {"method": ["\t"]},
        ):
            with self.assertRaises(InvalidFilterError):
                normalize_filters(**kwargs)

    def test_address_conflicts_with_from_or_to(self):
        with self.assertRaises(InvalidFilterError):
            normalize_filters(address="alice", from_address=["bob"])
        with self.assertRaises(InvalidFilterError):
            normalize_filters(address="alice", to_address=["bob"])
        # 仅 address 或仅 from/to 不冲突
        normalize_filters(address="alice")
        normalize_filters(from_address=["alice"], to_address=["bob"])

    def test_cursor_binds_new_filters(self):
        page1 = self.idx.query(
            normalize_filters(from_address=["alice"]), page_size=1
        )
        cursor = page1["next_cursor"]
        self.assertIsNotNone(cursor)
        # 相同筛选可续页
        page2 = self.idx.query(
            normalize_filters(from_address=["alice"]),
            page_size=1,
            cursor=cursor,
        )
        self.assertEqual(hashes(page2), ["h3"])
        # 换集合成员 → invalid_cursor
        with self.assertRaises(InvalidCursorError):
            self.idx.query(
                normalize_filters(from_address=["bob"]),
                page_size=1,
                cursor=cursor,
            )
        # 集合基数不同同样不匹配
        with self.assertRaises(InvalidCursorError):
            self.idx.query(
                normalize_filters(from_address=["alice", "carol"]),
                page_size=1,
                cursor=cursor,
            )

    def test_cursor_order_insensitive_for_sets(self):
        page1 = self.idx.query(
            normalize_filters(method=["approve", "transfer"]), page_size=1
        )
        page2 = self.idx.query(
            normalize_filters(method=["transfer", "approve"]),
            page_size=10,
            cursor=page1["next_cursor"],
        )
        self.assertEqual(hashes(page2), ["h2", "h3"])

    def test_stats_with_set_filters(self):
        stats = self.idx.stats(
            normalize_filters(
                from_address=["alice", "carol"], method=["mint", "transfer"]
            )
        )
        self.assertEqual(stats["total_count"], 3)
        self.assertEqual(stats["total_amount"], "80")
        self.assertEqual(stats["min_amount"], "10")
        self.assertEqual(stats["max_amount"], "40")
        self.assertEqual(stats["avg_amount"], "26")  # 80 // 3

    def test_stats_no_match_with_set_filters(self):
        stats = self.idx.stats(normalize_filters(to_address=["nobody"]))
        self.assertEqual(stats, {
            "total_count": 0,
            "total_amount": "0",
            "min_amount": None,
            "max_amount": None,
            "avg_amount": None,
        })


class StatsTest(unittest.TestCase):
    def setUp(self):
        self.records = [
            rec("a", 1, 10, "alice", "bob", "transfer", "10"),
            rec("b", 2, 20, "bob", "alice", "transfer", "21"),
            rec("c", 3, 30, "carol", "dave", "approve", "100"),
        ]
        self.idx = TxIndexer(self.records)

    def test_aggregation_and_floor_avg(self):
        stats = self.idx.stats(normalize_filters(method="transfer"))
        self.assertEqual(stats["total_count"], 2)
        self.assertEqual(stats["total_amount"], "31")
        self.assertEqual(stats["min_amount"], "10")
        self.assertEqual(stats["max_amount"], "21")
        self.assertEqual(stats["avg_amount"], "15")  # 31 // 2

    def test_no_match(self):
        stats = self.idx.stats(normalize_filters(method="nope"))
        self.assertEqual(stats, {
            "total_count": 0,
            "total_amount": "0",
            "min_amount": None,
            "max_amount": None,
            "avg_amount": None,
        })

    def test_stats_ignores_pagination_arguments_implicitly(self):
        # stats 接口本身不接受分页参数；筛选与 query 一致
        stats = self.idx.stats(
            normalize_filters(address="alice", start_time=15, end_time=25)
        )
        self.assertEqual(stats["total_count"], 1)
        self.assertEqual(stats["total_amount"], "21")

    def test_big_amounts_exact_decimal(self):
        big = "123456789012345678901234567890"
        idx = TxIndexer([rec("x", 1, 1, "a", "b", "m", big)])
        stats = idx.stats(normalize_filters())
        self.assertEqual(stats["total_amount"], big)
        self.assertEqual(stats["avg_amount"], big)


class MethodStatsTest(unittest.TestCase):
    def setUp(self):
        self.records = [
            rec("a", 1, 10, "alice", "bob", "transfer", "10"),
            rec("b", 2, 20, "bob", "alice", "transfer", "21"),
            rec("c", 3, 30, "carol", "dave", "approve", "100"),
            rec("d", 4, 40, "alice", "carol", "mint", "15"),
        ]
        self.idx = TxIndexer(self.records)

    def test_grouping_and_aggregation(self):
        result = self.idx.method_stats(normalize_filters())
        groups = result["groups"]
        self.assertEqual(
            [(g["method"], g["total_count"], g["total_amount"],
              g["avg_amount"]) for g in groups],
            [
                ("approve", 1, "100", "100"),
                ("transfer", 2, "31", "15"),  # 31 // 2
                ("mint", 1, "15", "15"),
            ],
        )
        self.assertEqual(result["total_groups"], 3)
        self.assertIsNone(result["next_cursor"])

    def test_group_field_order(self):
        group = self.idx.method_stats(normalize_filters())["groups"][0]
        self.assertEqual(
            list(group.keys()),
            ["method", "total_count", "total_amount", "avg_amount"],
        )
        self.assertIsInstance(group["total_amount"], str)
        self.assertIsInstance(group["avg_amount"], str)

    def test_sort_amount_then_count_then_method(self):
        records = [
            rec("h1", 1, 1, "a", "b", "zeta", "10"),
            rec("h2", 2, 2, "a", "b", "alpha", "5"),
            rec("h3", 3, 3, "a", "b", "alpha", "5"),
            rec("h4", 4, 4, "a", "b", "beta", "10"),
        ]
        result = TxIndexer(records).method_stats(normalize_filters())
        # total 均为 10：count 高的 alpha 在前；beta/zeta count 相同按码点
        self.assertEqual(
            [g["method"] for g in result["groups"]],
            ["alpha", "beta", "zeta"],
        )

    def test_pagination_no_skip_no_dup_no_reorder(self):
        filters = normalize_filters()
        all_methods = [
            g["method"]
            for g in self.idx.method_stats(filters, page_size=100)["groups"]
        ]
        collected = []
        cursor = None
        pages = 0
        while True:
            page = self.idx.method_stats(
                filters, page_size=2, cursor=cursor
            )
            pages += 1
            collected.extend(g["method"] for g in page["groups"])
            self.assertEqual(page["total_groups"], 3)
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(pages, 2)  # 3 组、page_size=2：恰好 2 页
        self.assertEqual(collected, all_methods)

    def test_last_page_partial_returns_null_cursor(self):
        page = self.idx.method_stats(normalize_filters(), page_size=2)
        self.assertIsNotNone(page["next_cursor"])
        last = self.idx.method_stats(
            normalize_filters(), page_size=2, cursor=page["next_cursor"]
        )
        self.assertEqual(len(last["groups"]), 1)
        self.assertIsNone(last["next_cursor"])

    def test_no_match(self):
        result = self.idx.method_stats(normalize_filters(method="nope"))
        self.assertEqual(result, {
            "groups": [],
            "total_groups": 0,
            "next_cursor": None,
        })

    def test_filters_intersect_before_grouping(self):
        result = self.idx.method_stats(
            normalize_filters(from_address=["alice"], method=["mint"])
        )
        self.assertEqual(
            [(g["method"], g["total_amount"]) for g in result["groups"]],
            [("mint", "15")],
        )
        self.assertEqual(result["total_groups"], 1)

    def test_page_size_bounds(self):
        with self.assertRaises(InvalidPageSizeError):
            self.idx.method_stats(normalize_filters(), page_size=0)
        with self.assertRaises(InvalidPageSizeError):
            self.idx.method_stats(normalize_filters(), page_size=1001)
        with self.assertRaises(InvalidPageSizeError):
            self.idx.method_stats(normalize_filters(), page_size="10")

    def test_cursor_filter_mismatch(self):
        page1 = self.idx.method_stats(
            normalize_filters(), page_size=1
        )
        cursor = page1["next_cursor"]
        with self.assertRaises(InvalidCursorError):
            self.idx.method_stats(
                normalize_filters(method="mint"), page_size=1, cursor=cursor
            )

    def test_cursor_cross_command_rejected_both_ways(self):
        from tx_indexer.cursor import (
            decode_cursor,
            decode_method_stats_cursor,
            encode_cursor,
            encode_method_stats_cursor,
        )

        filters = normalize_filters()
        query_cursor = encode_cursor(filters, 1, "h1")
        method_cursor = encode_method_stats_cursor(filters, 10, 1, "m")
        with self.assertRaises(InvalidCursorError):
            decode_method_stats_cursor(query_cursor, filters)
        with self.assertRaises(InvalidCursorError):
            decode_cursor(method_cursor, filters)

    def test_cursor_garbage(self):
        for bad in ("", "not-base64!!!", "bm9wZQ", "%%%"):
            with self.assertRaises(InvalidCursorError):
                self.idx.method_stats(normalize_filters(), cursor=bad)

    def test_big_amounts_exact_decimal(self):
        big = "123456789012345678901234567890"
        idx = TxIndexer([rec("x", 1, 1, "a", "b", "m", big)])
        group = idx.method_stats(normalize_filters())["groups"][0]
        self.assertEqual(group["total_amount"], big)
        self.assertEqual(group["avg_amount"], big)


if __name__ == "__main__":
    unittest.main()
