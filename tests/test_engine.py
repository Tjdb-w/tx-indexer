"""筛选、排序、分页与聚合测试。"""

import unittest

from tx_indexer.engine import TxIndexer, normalize_filters
from tx_indexer.errors import (
    InvalidBucketSizeError,
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


class AddressStatsTest(unittest.TestCase):
    def setUp(self):
        self.records = [
            rec("a", 1, 10, "alice", "bob", "transfer", "10"),
            rec("b", 2, 20, "bob", "alice", "transfer", "21"),
            rec("c", 3, 30, "carol", "dave", "approve", "100"),
            rec("d", 4, 40, "alice", "carol", "mint", "15"),
        ]
        self.idx = TxIndexer(self.records)

    def test_grouping_and_aggregation(self):
        result = self.idx.address_stats(normalize_filters())
        groups = result["groups"]
        self.assertEqual(
            [(g["address"], g["send_count"], g["receive_count"],
              g["total_count"], g["total_amount"], g["avg_amount"])
             for g in groups],
            [
                ("carol", 1, 1, 2, "115", "57"),
                ("dave", 0, 1, 1, "100", "100"),
                ("alice", 2, 1, 3, "46", "15"),  # 46 // 3
                ("bob", 1, 1, 2, "31", "15"),
            ],
        )
        self.assertEqual(result["total_groups"], 4)
        self.assertIsNone(result["next_cursor"])

    def test_group_field_order(self):
        group = self.idx.address_stats(normalize_filters())["groups"][0]
        self.assertEqual(
            list(group.keys()),
            ["address", "send_count", "receive_count", "total_count",
             "total_amount", "avg_amount"],
        )
        self.assertIsInstance(group["total_amount"], str)
        self.assertIsInstance(group["avg_amount"], str)

    def test_self_transfer_counted_once_amount_once_both_roles(self):
        records = [
            rec("s1", 1, 1, "eva", "eva", "m", "100"),
            rec("s2", 2, 2, "eva", "eva", "m", "50"),
            rec("o1", 3, 3, "fin", "gus", "m", "7"),
        ]
        result = TxIndexer(records).address_stats(normalize_filters())
        groups = {g["address"]: g for g in result["groups"]}
        eva = groups["eva"]
        self.assertEqual(eva["send_count"], 2)
        self.assertEqual(eva["receive_count"], 2)
        self.assertEqual(eva["total_count"], 2)  # 两笔不同交易
        self.assertEqual(eva["total_amount"], "150")  # 不重复累计
        self.assertEqual(eva["avg_amount"], "75")
        fin = groups["fin"]
        self.assertEqual((fin["send_count"], fin["receive_count"],
                          fin["total_count"]), (1, 0, 1))
        gus = groups["gus"]
        self.assertEqual((gus["send_count"], gus["receive_count"],
                          gus["total_count"]), (0, 1, 1))

    def test_self_transfer_mixed_with_normal(self):
        records = [
            rec("s1", 1, 1, "a", "a", "m", "10"),
            rec("o1", 2, 2, "a", "b", "m", "5"),
            rec("i1", 3, 3, "b", "a", "m", "7"),
        ]
        groups = {
            g["address"]: g
            for g in TxIndexer(records).address_stats(
                normalize_filters()
            )["groups"]
        }
        a = groups["a"]
        # a：自转账一笔（两身份）、发给 b 一笔、收 b 一笔
        self.assertEqual((a["send_count"], a["receive_count"],
                          a["total_count"]), (2, 2, 3))
        self.assertEqual(a["total_amount"], "22")
        self.assertEqual(a["avg_amount"], "7")  # 22 // 3

    def test_sort_amount_then_count_then_send_then_receive_then_address(self):
        records = [
            # x、y 总额相同(10)、count 不同 → count 高的在前
            rec("h1", 1, 1, "a", "x", "m", "5"),
            rec("h2", 2, 2, "a", "x", "m", "5"),
            rec("h3", 3, 3, "a", "y", "m", "10"),
            # z、w 总额 8、count 1、send 相同(0) → 按地址码点
            rec("h4", 4, 4, "a", "w", "m", "8"),
            rec("h5", 5, 5, "a", "z", "m", "8"),
        ]
        result = TxIndexer(records).address_stats(normalize_filters())
        # a 发送全部 5 笔、总额 36 居首；其后 10 档 count 高的 x 在前
        self.assertEqual(
            [g["address"] for g in result["groups"]],
            ["a", "x", "y", "w", "z"],
        )

    def test_sort_receive_count_tiebreak(self):
        records = [
            rec("h1", 1, 1, "x", "x", "m", "10"),  # send1 recv1 count1
            rec("h2", 2, 2, "y", "z", "m", "10"),  # y: send1 recv0 count1
        ]
        result = TxIndexer(records).address_stats(normalize_filters())
        # x 与 y 总额/count/send 相同，receive_count 高的 x 在前
        self.assertEqual(
            [g["address"] for g in result["groups"]][:2], ["x", "y"]
        )

    def test_pagination_no_skip_no_dup_no_reorder(self):
        filters = normalize_filters()
        all_addresses = [
            g["address"]
            for g in self.idx.address_stats(filters, page_size=100)["groups"]
        ]
        collected = []
        cursor = None
        pages = 0
        while True:
            page = self.idx.address_stats(
                filters, page_size=2, cursor=cursor
            )
            pages += 1
            collected.extend(g["address"] for g in page["groups"])
            self.assertEqual(page["total_groups"], 4)
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(pages, 2)  # 4 组、page_size=2：恰好 2 页
        self.assertEqual(collected, all_addresses)

    def test_last_page_partial_returns_null_cursor(self):
        page = self.idx.address_stats(normalize_filters(), page_size=3)
        self.assertIsNotNone(page["next_cursor"])
        last = self.idx.address_stats(
            normalize_filters(), page_size=3, cursor=page["next_cursor"]
        )
        self.assertEqual(len(last["groups"]), 1)
        self.assertIsNone(last["next_cursor"])

    def test_no_match(self):
        result = self.idx.address_stats(normalize_filters(method="nope"))
        self.assertEqual(result, {
            "groups": [],
            "total_groups": 0,
            "next_cursor": None,
        })

    def test_filters_intersect_before_grouping(self):
        result = self.idx.address_stats(
            normalize_filters(from_address=["alice"], method=["mint"])
        )
        # 只有 d：alice→carol 15
        groups = {g["address"]: g for g in result["groups"]}
        self.assertEqual(set(groups), {"alice", "carol"})
        self.assertEqual(groups["alice"]["total_amount"], "15")
        self.assertEqual(groups["carol"]["total_amount"], "15")
        self.assertEqual(result["total_groups"], 2)

    def test_page_size_bounds(self):
        with self.assertRaises(InvalidPageSizeError):
            self.idx.address_stats(normalize_filters(), page_size=0)
        with self.assertRaises(InvalidPageSizeError):
            self.idx.address_stats(normalize_filters(), page_size=1001)
        with self.assertRaises(InvalidPageSizeError):
            self.idx.address_stats(normalize_filters(), page_size="10")

    def test_cursor_filter_mismatch(self):
        page1 = self.idx.address_stats(
            normalize_filters(), page_size=1
        )
        cursor = page1["next_cursor"]
        with self.assertRaises(InvalidCursorError):
            self.idx.address_stats(
                normalize_filters(method="mint"), page_size=1, cursor=cursor
            )

    def test_cursor_cross_command_rejected(self):
        from tx_indexer.cursor import (
            decode_address_stats_cursor,
            decode_cursor,
            decode_method_stats_cursor,
            encode_address_stats_cursor,
            encode_cursor,
            encode_method_stats_cursor,
        )

        filters = normalize_filters()
        address_cursor = encode_address_stats_cursor(
            filters, 10, 1, 1, 0, "a"
        )
        query_cursor = encode_cursor(filters, 1, "h1")
        method_cursor = encode_method_stats_cursor(filters, 10, 1, "m")
        with self.assertRaises(InvalidCursorError):
            decode_cursor(address_cursor, filters)
        with self.assertRaises(InvalidCursorError):
            decode_method_stats_cursor(address_cursor, filters)
        with self.assertRaises(InvalidCursorError):
            decode_address_stats_cursor(query_cursor, filters)
        with self.assertRaises(InvalidCursorError):
            decode_address_stats_cursor(method_cursor, filters)

    def test_cursor_garbage(self):
        for bad in ("", "not-base64!!!", "bm9wZQ", "%%%"):
            with self.assertRaises(InvalidCursorError):
                self.idx.address_stats(normalize_filters(), cursor=bad)

    def test_big_amounts_exact_decimal(self):
        big = "123456789012345678901234567890"
        idx = TxIndexer([rec("x", 1, 1, "a", "b", "m", big)])
        groups = {g["address"]: g
                  for g in idx.address_stats(normalize_filters())["groups"]}
        self.assertEqual(groups["a"]["total_amount"], big)
        self.assertEqual(groups["a"]["avg_amount"], big)
        self.assertEqual(groups["b"]["total_amount"], big)
        self.assertEqual(groups["b"]["avg_amount"], big)


class CounterpartyStatsTest(unittest.TestCase):
    def setUp(self):
        self.records = [
            rec("h1", 1, 10, "alice", "bob", "transfer", "10"),
            rec("h2", 2, 20, "bob", "alice", "approve", "21"),
            rec("h3", 2, 30, "alice", "carol", "transfer", "5"),
            rec("h4", 3, 40, "alice", "alice", "mint", "7"),
            rec("h5", 4, 50, "carol", "dave", "transfer", "100"),
        ]
        self.idx = TxIndexer(self.records)
        self.filters = normalize_filters(address="alice")

    def test_requires_address_filter(self):
        with self.assertRaises(InvalidFilterError):
            self.idx.counterparty_stats(normalize_filters())

    def test_grouping_and_aggregation(self):
        result = self.idx.counterparty_stats(self.filters)
        self.assertEqual(result["address"], "alice")
        groups = result["groups"]
        self.assertEqual(
            [(g["counterparty"], g["send_count"], g["receive_count"],
              g["total_count"], g["total_amount"], g["avg_amount"])
             for g in groups],
            [
                ("bob", 1, 1, 2, "31", "15"),   # 31 // 2
                ("alice", 1, 1, 1, "7", "7"),   # 自转账：对手即自身
                ("carol", 0, 1, 1, "5", "5"),
            ],
        )
        self.assertEqual(result["total_groups"], 3)
        self.assertIsNone(result["next_cursor"])

    def test_result_field_order(self):
        result = self.idx.counterparty_stats(self.filters)
        self.assertEqual(
            list(result.keys()),
            ["address", "groups", "total_groups", "next_cursor"],
        )
        group = result["groups"][0]
        self.assertEqual(
            list(group.keys()),
            ["counterparty", "send_count", "receive_count", "total_count",
             "total_amount", "avg_amount"],
        )
        self.assertIsInstance(group["total_amount"], str)
        self.assertIsInstance(group["avg_amount"], str)

    def test_each_transaction_attributes_single_counterparty(self):
        # 观察地址为 carol：h3 归 alice（发送方视角），h5 归 dave
        result = self.idx.counterparty_stats(
            normalize_filters(address="carol")
        )
        groups = {g["counterparty"]: g for g in result["groups"]}
        self.assertEqual(set(groups), {"alice", "dave"})
        self.assertEqual(
            (groups["alice"]["send_count"], groups["alice"]["receive_count"],
             groups["alice"]["total_count"], groups["alice"]["total_amount"]),
            (1, 0, 1, "5"),
        )
        self.assertEqual(
            (groups["dave"]["send_count"], groups["dave"]["receive_count"],
             groups["dave"]["total_count"], groups["dave"]["total_amount"]),
            (0, 1, 1, "100"),
        )
        self.assertEqual(result["total_groups"], 2)

    def test_self_transfer_counted_once_amount_once_both_roles(self):
        records = [
            rec("s1", 1, 1, "eva", "eva", "m", "100"),
            rec("s2", 2, 2, "eva", "eva", "m", "50"),
            rec("o1", 3, 3, "fin", "gus", "m", "7"),
        ]
        result = TxIndexer(records).counterparty_stats(
            normalize_filters(address="eva")
        )
        self.assertEqual(result["total_groups"], 1)
        eva = result["groups"][0]
        self.assertEqual(eva["counterparty"], "eva")
        self.assertEqual(eva["send_count"], 2)
        self.assertEqual(eva["receive_count"], 2)
        self.assertEqual(eva["total_count"], 2)  # 两笔不同交易
        self.assertEqual(eva["total_amount"], "150")  # 不重复累计
        self.assertEqual(eva["avg_amount"], "75")

    def test_sort_amount_then_count_then_counterparty(self):
        records = [
            # x、y 总额相同(10)、count 不同 → count 高的在前
            rec("h1", 1, 1, "a", "x", "m", "5"),
            rec("h2", 2, 2, "a", "x", "m", "5"),
            rec("h3", 3, 3, "a", "y", "m", "10"),
            # z、w 总额 8、count/send/receive 相同 → 按对手码点升序
            rec("h4", 4, 4, "a", "w", "m", "8"),
            rec("h5", 5, 5, "a", "z", "m", "8"),
        ]
        result = TxIndexer(records).counterparty_stats(
            normalize_filters(address="a")
        )
        self.assertEqual(
            [g["counterparty"] for g in result["groups"]],
            ["x", "y", "w", "z"],
        )

    def test_sort_send_then_receive_tiebreak(self):
        records = [
            rec("h1", 1, 1, "x", "x", "m", "10"),  # x: send1 recv1 count1
            rec("h2", 2, 2, "y", "x", "m", "10"),  # y: send1 recv0 count1
        ]
        result = TxIndexer(records).counterparty_stats(
            normalize_filters(address="x")
        )
        # x 与 y 总额/count/send 相同，receive_count 高的 x 在前
        self.assertEqual(
            [g["counterparty"] for g in result["groups"]], ["x", "y"]
        )

    def test_pagination_no_skip_no_dup_no_reorder(self):
        all_counterparties = [
            g["counterparty"]
            for g in self.idx.counterparty_stats(
                self.filters, page_size=100
            )["groups"]
        ]
        collected = []
        cursor = None
        pages = 0
        while True:
            page = self.idx.counterparty_stats(
                self.filters, page_size=2, cursor=cursor
            )
            pages += 1
            collected.extend(g["counterparty"] for g in page["groups"])
            self.assertEqual(page["total_groups"], 3)
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(pages, 2)  # 3 组、page_size=2：恰好 2 页
        self.assertEqual(collected, all_counterparties)

    def test_cursor_not_bound_to_page_size(self):
        page1 = self.idx.counterparty_stats(self.filters, page_size=2)
        rest = self.idx.counterparty_stats(
            self.filters, page_size=100, cursor=page1["next_cursor"]
        )
        self.assertEqual(
            [g["counterparty"] for g in rest["groups"]], ["carol"]
        )
        self.assertIsNone(rest["next_cursor"])

    def test_last_page_partial_returns_null_cursor(self):
        page = self.idx.counterparty_stats(self.filters, page_size=2)
        self.assertIsNotNone(page["next_cursor"])
        last = self.idx.counterparty_stats(
            self.filters, page_size=2, cursor=page["next_cursor"]
        )
        self.assertEqual(len(last["groups"]), 1)
        self.assertIsNone(last["next_cursor"])

    def test_no_match_keeps_address(self):
        result = self.idx.counterparty_stats(
            normalize_filters(address="nobody")
        )
        self.assertEqual(result, {
            "address": "nobody",
            "groups": [],
            "total_groups": 0,
            "next_cursor": None,
        })

    def test_filters_intersect_before_grouping(self):
        result = self.idx.counterparty_stats(
            normalize_filters(address="alice", method=["transfer"])
        )
        # 只剩 h1 alice→bob 10、h3 alice→carol 5
        groups = {g["counterparty"]: g for g in result["groups"]}
        self.assertEqual(set(groups), {"bob", "carol"})
        self.assertEqual(groups["bob"]["total_amount"], "10")
        self.assertEqual(groups["carol"]["total_amount"], "5")
        self.assertEqual(result["total_groups"], 2)

    def test_page_size_bounds(self):
        with self.assertRaises(InvalidPageSizeError):
            self.idx.counterparty_stats(self.filters, page_size=0)
        with self.assertRaises(InvalidPageSizeError):
            self.idx.counterparty_stats(self.filters, page_size=1001)
        with self.assertRaises(InvalidPageSizeError):
            self.idx.counterparty_stats(self.filters, page_size="10")

    def test_cursor_filter_mismatch(self):
        page1 = self.idx.counterparty_stats(self.filters, page_size=1)
        cursor = page1["next_cursor"]
        # 改变观察地址复用旧游标 → invalid_cursor
        with self.assertRaises(InvalidCursorError):
            self.idx.counterparty_stats(
                normalize_filters(address="bob"), page_size=1, cursor=cursor
            )
        # 改变其他筛选同样拒绝
        with self.assertRaises(InvalidCursorError):
            self.idx.counterparty_stats(
                normalize_filters(address="alice", method=["mint"]),
                page_size=1,
                cursor=cursor,
            )

    def test_cursor_cross_command_rejected(self):
        from tx_indexer.cursor import (
            decode_address_stats_cursor,
            decode_counterparty_stats_cursor,
            decode_cursor,
            decode_method_stats_cursor,
            encode_address_stats_cursor,
            encode_counterparty_stats_cursor,
            encode_cursor,
            encode_method_stats_cursor,
        )

        filters = normalize_filters(address="alice")
        counterparty_cursor = encode_counterparty_stats_cursor(
            filters, 10, 1, 1, 0, "a"
        )
        query_cursor = encode_cursor(filters, 1, "h1")
        method_cursor = encode_method_stats_cursor(filters, 10, 1, "m")
        address_cursor = encode_address_stats_cursor(filters, 10, 1, 1, 0, "a")
        with self.assertRaises(InvalidCursorError):
            decode_cursor(counterparty_cursor, filters)
        with self.assertRaises(InvalidCursorError):
            decode_method_stats_cursor(counterparty_cursor, filters)
        with self.assertRaises(InvalidCursorError):
            decode_address_stats_cursor(counterparty_cursor, filters)
        with self.assertRaises(InvalidCursorError):
            decode_counterparty_stats_cursor(query_cursor, filters)
        with self.assertRaises(InvalidCursorError):
            decode_counterparty_stats_cursor(method_cursor, filters)
        with self.assertRaises(InvalidCursorError):
            decode_counterparty_stats_cursor(address_cursor, filters)

    def test_cursor_garbage(self):
        for bad in ("", "not-base64!!!", "bm9wZQ", "%%%"):
            with self.assertRaises(InvalidCursorError):
                self.idx.counterparty_stats(self.filters, cursor=bad)

    def test_big_amounts_exact_decimal(self):
        big = "123456789012345678901234567890"
        idx = TxIndexer([rec("x", 1, 1, "a", "b", "m", big)])
        result = idx.counterparty_stats(normalize_filters(address="a"))
        group = result["groups"][0]
        self.assertEqual(group["counterparty"], "b")
        self.assertEqual(group["total_amount"], big)
        self.assertEqual(group["avg_amount"], big)


class TimeStatsTest(unittest.TestCase):
    def setUp(self):
        self.records = [
            rec("h1", 1, 10, "alice", "bob", "transfer", "10"),
            rec("h2", 2, 20, "bob", "alice", "approve", "21"),
            rec("h3", 2, 30, "alice", "carol", "transfer", "5"),
            rec("h4", 3, 300, "bob", "dave", "transfer", "40"),
            rec("h5", 4, 50, "carol", "dave", "transfer", "100"),
        ]
        self.idx = TxIndexer(self.records)
        self.filters = normalize_filters()

    def test_bucket_alignment_epoch_left_closed_right_open(self):
        # bucket_size=60：区间 [0,60)、[60,120)、[120,180)…
        records = [
            rec("b0", 1, 0, "a", "b", "m", "1"),
            rec("b59", 2, 59, "a", "b", "m", "2"),
            rec("b60", 3, 60, "a", "b", "m", "4"),
            rec("b119", 4, 119, "a", "b", "m", "8"),
            rec("b120", 5, 120, "a", "b", "m", "16"),
        ]
        result = TxIndexer(records).time_stats(self.filters, 60)
        self.assertEqual(
            [(g["bucket_start"], g["bucket_end_exclusive"],
              g["total_count"], g["total_amount"])
             for g in result["groups"]],
            [(0, 60, 2, "3"), (60, 120, 2, "12"), (120, 180, 1, "16")],
        )
        self.assertEqual(result["total_groups"], 3)

    def test_grouping_aggregation_and_avg_floor(self):
        result = self.idx.time_stats(self.filters, 60)
        groups = result["groups"]
        self.assertEqual(
            [(g["bucket_start"], g["bucket_end_exclusive"], g["total_count"],
              g["total_amount"], g["avg_amount"]) for g in groups],
            [
                (0, 60, 4, "136", "34"),    # 136 // 4
                (300, 360, 1, "40", "40"),
            ],
        )
        self.assertEqual(result["total_groups"], 2)
        self.assertIsNone(result["next_cursor"])

    def test_avg_amount_floors(self):
        records = [
            rec("a", 1, 1, "x", "y", "m", "10"),
            rec("b", 2, 2, "x", "y", "m", "10"),
            rec("c", 3, 3, "x", "y", "m", "10"),
        ]
        result = TxIndexer(records).time_stats(self.filters, 60)
        group = result["groups"][0]
        self.assertEqual(group["total_amount"], "30")
        self.assertEqual(group["avg_amount"], "10")
        records[2]["amount"] = "11"
        result = TxIndexer(records).time_stats(self.filters, 60)
        self.assertEqual(result["groups"][0]["avg_amount"], "10")  # 31 // 3

    def test_only_nonempty_buckets_returned(self):
        # ts 10 与 ts 1000 之间大量空区间不得出现
        records = [
            rec("a", 1, 10, "x", "y", "m", "1"),
            rec("b", 2, 1000, "x", "y", "m", "2"),
        ]
        result = TxIndexer(records).time_stats(self.filters, 60)
        self.assertEqual(
            [g["bucket_start"] for g in result["groups"]], [0, 960]
        )
        self.assertEqual(result["total_groups"], 2)

    def test_sorted_by_bucket_start_ascending(self):
        result = self.idx.time_stats(self.filters, 10)
        starts = [g["bucket_start"] for g in result["groups"]]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(starts, [10, 20, 30, 50, 300])

    def test_result_field_order(self):
        result = self.idx.time_stats(self.filters, 60)
        self.assertEqual(
            list(result.keys()), ["groups", "total_groups", "next_cursor"]
        )
        group = result["groups"][0]
        self.assertEqual(
            list(group.keys()),
            ["bucket_start", "bucket_end_exclusive", "total_count",
             "total_amount", "avg_amount"],
        )
        self.assertIsInstance(group["bucket_start"], int)
        self.assertIsInstance(group["bucket_end_exclusive"], int)
        self.assertIsInstance(group["total_amount"], str)
        self.assertIsInstance(group["avg_amount"], str)

    def test_no_match(self):
        result = self.idx.time_stats(
            normalize_filters(method="nonexistent"), 60
        )
        self.assertEqual(
            result, {"groups": [], "total_groups": 0, "next_cursor": None}
        )

    def test_filters_applied(self):
        result = self.idx.time_stats(
            normalize_filters(method="transfer"), 60
        )
        self.assertEqual(
            [(g["bucket_start"], g["total_count"], g["total_amount"])
             for g in result["groups"]],
            [(0, 3, "115"), (300, 1, "40")],
        )

    def test_time_window_inclusive_both_ends(self):
        # 时间窗左闭右闭：端点 10 与 50 都计入
        result = self.idx.time_stats(
            normalize_filters(start_time=10, end_time=50), 60
        )
        self.assertEqual(result["total_groups"], 1)
        group = result["groups"][0]
        self.assertEqual(group["bucket_start"], 0)
        self.assertEqual(group["total_count"], 4)

    def test_pagination_no_skip_no_dup_no_reorder(self):
        collected = []
        cursor = None
        pages = 0
        while True:
            page = self.idx.time_stats(
                self.filters, 10, page_size=2, cursor=cursor
            )
            pages += 1
            collected.extend(g["bucket_start"] for g in page["groups"])
            self.assertEqual(page["total_groups"], 5)
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(pages, 3)  # 5 个区间、page_size=2：3 页
        self.assertEqual(collected, [10, 20, 30, 50, 300])

    def test_cursor_not_bound_to_page_size(self):
        page1 = self.idx.time_stats(self.filters, 10, page_size=1)
        self.assertEqual(
            [g["bucket_start"] for g in page1["groups"]], [10]
        )
        page2 = self.idx.time_stats(
            self.filters, 10, page_size=100, cursor=page1["next_cursor"]
        )
        self.assertEqual(
            [g["bucket_start"] for g in page2["groups"]], [20, 30, 50, 300]
        )
        self.assertIsNone(page2["next_cursor"])

    def test_cursor_bound_to_bucket_size(self):
        page1 = self.idx.time_stats(self.filters, 60, page_size=1)
        with self.assertRaises(InvalidCursorError):
            self.idx.time_stats(
                self.filters, 30, page_size=1, cursor=page1["next_cursor"]
            )

    def test_cursor_bound_to_filters(self):
        page1 = self.idx.time_stats(self.filters, 10, page_size=1)
        with self.assertRaises(InvalidCursorError):
            self.idx.time_stats(
                normalize_filters(method="transfer"),
                10,
                page_size=1,
                cursor=page1["next_cursor"],
            )

    def test_cross_command_cursor_rejected(self):
        from tx_indexer.cursor import (
            decode_method_stats_cursor,
            decode_time_stats_cursor,
            encode_method_stats_cursor,
            encode_time_stats_cursor,
        )

        filters = normalize_filters()
        time_cursor = encode_time_stats_cursor(filters, 60, 0)
        method_cursor = encode_method_stats_cursor(filters, 10, 1, "m")
        with self.assertRaises(InvalidCursorError):
            decode_time_stats_cursor(method_cursor, filters, 60)
        with self.assertRaises(InvalidCursorError):
            decode_method_stats_cursor(time_cursor, filters)

    def test_cursor_garbage(self):
        for bad in ("", "not-base64!!!", "bm9wZQ", "%%%"):
            with self.assertRaises(InvalidCursorError):
                self.idx.time_stats(self.filters, 60, cursor=bad)

    def test_cursor_tampered(self):
        import base64
        import json

        page1 = self.idx.time_stats(self.filters, 10, page_size=1)
        token = page1["next_cursor"]
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        payload = json.loads(raw.decode("utf-8"))
        payload["after"] = "not-an-int"  # 篡改位置信息
        tampered = base64.urlsafe_b64encode(
            json.dumps(payload).encode("utf-8")
        ).rstrip(b"=").decode("ascii")
        with self.assertRaises(InvalidCursorError):
            self.idx.time_stats(self.filters, 10, cursor=tampered)

    def test_invalid_bucket_size(self):
        for bad in (0, -1, -60, "60", 1.5, True, None):
            with self.assertRaises(InvalidBucketSizeError):
                self.idx.time_stats(self.filters, bad)

    def test_invalid_page_size(self):
        for bad in (0, -1, 1001, "10", None):
            with self.assertRaises(InvalidPageSizeError):
                self.idx.time_stats(self.filters, 60, page_size=bad)

    def test_big_amounts_exact_decimal(self):
        big = "123456789012345678901234567890"
        idx = TxIndexer([
            rec("x", 1, 1, "a", "b", "m", big),
            rec("y", 2, 2, "a", "b", "m", "10"),
        ])
        result = idx.time_stats(normalize_filters(), 60)
        group = result["groups"][0]
        self.assertEqual(group["total_amount"], str(int(big) + 10))
        self.assertEqual(group["avg_amount"], str((int(big) + 10) // 2))


class PairStatsTest(unittest.TestCase):
    def setUp(self):
        self.records = [
            rec("h1", 1, 10, "alice", "bob", "transfer", "10"),
            rec("h2", 2, 20, "bob", "alice", "approve", "21"),
            rec("h3", 3, 30, "alice", "carol", "transfer", "30"),
            rec("h4", 4, 40, "alice", "bob", "mint", "5"),
        ]
        self.idx = TxIndexer(self.records)
        self.filters = normalize_filters()

    def test_directed_grouping_and_aggregation(self):
        result = self.idx.pair_stats(self.filters)
        groups = result["groups"]
        # (alice,bob) 与 (bob,alice) 是两个不同的有向组合
        self.assertEqual(
            [(g["from_address"], g["to_address"], g["total_count"],
              g["total_amount"], g["avg_amount"]) for g in groups],
            [
                ("alice", "carol", 1, "30", "30"),
                ("bob", "alice", 1, "21", "21"),
                ("alice", "bob", 2, "15", "7"),  # 15 // 2
            ],
        )
        self.assertEqual(result["total_groups"], 3)
        self.assertIsNone(result["next_cursor"])

    def test_group_field_order(self):
        result = self.idx.pair_stats(self.filters)
        self.assertEqual(
            list(result.keys()), ["groups", "total_groups", "next_cursor"]
        )
        group = result["groups"][0]
        self.assertEqual(
            list(group.keys()),
            ["from_address", "to_address", "total_count",
             "total_amount", "avg_amount"],
        )
        self.assertIsInstance(group["total_amount"], str)
        self.assertIsInstance(group["avg_amount"], str)

    def test_self_transfer_is_one_directed_pair_counted_once(self):
        records = [
            rec("s1", 1, 1, "eva", "eva", "m", "100"),
            rec("s2", 2, 2, "eva", "eva", "m", "50"),
            rec("o1", 3, 3, "fin", "gus", "m", "7"),
        ]
        result = TxIndexer(records).pair_stats(normalize_filters())
        groups = {
            (g["from_address"], g["to_address"]): g
            for g in result["groups"]
        }
        self.assertEqual(set(groups), {("eva", "eva"), ("fin", "gus")})
        eva = groups[("eva", "eva")]
        self.assertEqual(eva["total_count"], 2)
        self.assertEqual(eva["total_amount"], "150")
        self.assertEqual(eva["avg_amount"], "75")

    def test_sort_amount_then_count_then_from_then_to(self):
        records = [
            # (a,x) 总额 10、count 2
            rec("h1", 1, 1, "a", "x", "m", "5"),
            rec("h2", 2, 2, "a", "x", "m", "5"),
            # (a,y) 总额 10、count 1：count 低排在 (a,x) 之后
            rec("h3", 3, 3, "a", "y", "m", "10"),
            # (b,w)/(b,z) 总额 8、count 1：from 相同按 to 码点
            rec("h4", 4, 4, "b", "w", "m", "8"),
            rec("h5", 5, 5, "b", "z", "m", "8"),
        ]
        result = TxIndexer(records).pair_stats(normalize_filters())
        self.assertEqual(
            [(g["from_address"], g["to_address"])
             for g in result["groups"]],
            [("a", "x"), ("a", "y"), ("b", "w"), ("b", "z")],
        )

    def test_sort_from_address_tiebreak(self):
        records = [
            rec("h1", 1, 1, "x", "b", "m", "10"),
            rec("h2", 2, 2, "y", "b", "m", "10"),
        ]
        result = TxIndexer(records).pair_stats(normalize_filters())
        # 总额/count/to 相同：from_address 码点升序
        self.assertEqual(
            [(g["from_address"], g["to_address"])
             for g in result["groups"]],
            [("x", "b"), ("y", "b")],
        )

    def test_pagination_no_skip_no_dup_no_reorder(self):
        all_pairs = [
            (g["from_address"], g["to_address"])
            for g in self.idx.pair_stats(self.filters, page_size=100)["groups"]
        ]
        collected = []
        cursor = None
        pages = 0
        while True:
            page = self.idx.pair_stats(
                self.filters, page_size=2, cursor=cursor
            )
            pages += 1
            collected.extend(
                (g["from_address"], g["to_address"]) for g in page["groups"]
            )
            self.assertEqual(page["total_groups"], 3)
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(pages, 2)  # 3 组、page_size=2：恰好 2 页
        self.assertEqual(collected, all_pairs)

    def test_cursor_not_bound_to_page_size(self):
        page1 = self.idx.pair_stats(self.filters, page_size=1)
        self.assertEqual(
            [(g["from_address"], g["to_address"]) for g in page1["groups"]],
            [("alice", "carol")],
        )
        rest = self.idx.pair_stats(
            self.filters, page_size=100, cursor=page1["next_cursor"]
        )
        self.assertEqual(
            [(g["from_address"], g["to_address"]) for g in rest["groups"]],
            [("bob", "alice"), ("alice", "bob")],
        )
        self.assertIsNone(rest["next_cursor"])

    def test_last_page_partial_returns_null_cursor(self):
        page = self.idx.pair_stats(self.filters, page_size=2)
        self.assertIsNotNone(page["next_cursor"])
        last = self.idx.pair_stats(
            self.filters, page_size=2, cursor=page["next_cursor"]
        )
        self.assertEqual(len(last["groups"]), 1)
        self.assertIsNone(last["next_cursor"])

    def test_no_match(self):
        result = self.idx.pair_stats(normalize_filters(method="nope"))
        self.assertEqual(result, {
            "groups": [],
            "total_groups": 0,
            "next_cursor": None,
        })

    def test_filters_intersect_before_grouping(self):
        result = self.idx.pair_stats(
            normalize_filters(from_address=["alice"], method=["mint"])
        )
        # 只有 h4：alice→bob 5
        self.assertEqual(
            [(g["from_address"], g["to_address"], g["total_amount"])
             for g in result["groups"]],
            [("alice", "bob", "5")],
        )
        self.assertEqual(result["total_groups"], 1)

    def test_page_size_bounds(self):
        with self.assertRaises(InvalidPageSizeError):
            self.idx.pair_stats(self.filters, page_size=0)
        with self.assertRaises(InvalidPageSizeError):
            self.idx.pair_stats(self.filters, page_size=1001)
        with self.assertRaises(InvalidPageSizeError):
            self.idx.pair_stats(self.filters, page_size="10")

    def test_cursor_filter_mismatch(self):
        page1 = self.idx.pair_stats(self.filters, page_size=1)
        cursor = page1["next_cursor"]
        with self.assertRaises(InvalidCursorError):
            self.idx.pair_stats(
                normalize_filters(method="mint"), page_size=1, cursor=cursor
            )

    def test_cursor_cross_command_rejected(self):
        from tx_indexer.cursor import (
            decode_address_stats_cursor,
            decode_cursor,
            decode_method_stats_cursor,
            decode_pair_stats_cursor,
            decode_time_stats_cursor,
            encode_address_stats_cursor,
            encode_cursor,
            encode_method_stats_cursor,
            encode_pair_stats_cursor,
            encode_time_stats_cursor,
        )

        filters = normalize_filters()
        pair_cursor = encode_pair_stats_cursor(filters, 10, 1, "a", "b")
        query_cursor = encode_cursor(filters, 1, "h1")
        method_cursor = encode_method_stats_cursor(filters, 10, 1, "m")
        address_cursor = encode_address_stats_cursor(
            filters, 10, 1, 1, 0, "a"
        )
        time_cursor = encode_time_stats_cursor(filters, 60, 0)
        for decoder in (
            lambda t: decode_cursor(t, filters),
            lambda t: decode_method_stats_cursor(t, filters),
            lambda t: decode_address_stats_cursor(t, filters),
            lambda t: decode_time_stats_cursor(t, filters, 60),
        ):
            with self.assertRaises(InvalidCursorError):
                decoder(pair_cursor)
        for other in (query_cursor, method_cursor, address_cursor,
                      time_cursor):
            with self.assertRaises(InvalidCursorError):
                decode_pair_stats_cursor(other, filters)

    def test_cursor_garbage(self):
        for bad in ("", "not-base64!!!", "bm9wZQ", "%%%"):
            with self.assertRaises(InvalidCursorError):
                self.idx.pair_stats(self.filters, cursor=bad)

    def test_cursor_tampered(self):
        import base64
        import json

        page1 = self.idx.pair_stats(self.filters, page_size=1)
        token = page1["next_cursor"]
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        payload = json.loads(raw.decode("utf-8"))
        payload["after"] = ["bad", 1, "a", "b"]  # 篡改位置信息
        tampered = base64.urlsafe_b64encode(
            json.dumps(payload).encode("utf-8")
        ).rstrip(b"=").decode("ascii")
        with self.assertRaises(InvalidCursorError):
            self.idx.pair_stats(self.filters, cursor=tampered)

    def test_big_amounts_exact_decimal(self):
        big = "123456789012345678901234567890"
        idx = TxIndexer([
            rec("x", 1, 1, "a", "b", "m", big),
            rec("y", 2, 2, "a", "b", "m", "10"),
        ])
        group = idx.pair_stats(normalize_filters())["groups"][0]
        self.assertEqual(group["total_amount"], str(int(big) + 10))
        self.assertEqual(group["avg_amount"], str((int(big) + 10) // 2))


if __name__ == "__main__":
    unittest.main()
