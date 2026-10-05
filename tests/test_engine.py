"""筛选、排序、分页与聚合测试。"""

import unittest

from tx_indexer.engine import TxIndexer, normalize_filters
from tx_indexer.errors import (
    InvalidAmountFilterError,
    InvalidAmountRangeError,
    InvalidBlockFilterError,
    InvalidBlockRangeError,
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
            rec("h2", 2, 20, "bob", "alice", "transfer", "21"),
            rec("h3", 2, 30, "alice", "carol", "transfer", "5"),
            rec("h4", 3, 300, "alice", "bob", "approve", "5"),
            rec("h5", 4, 40, "alice", "alice", "transfer", "100"),
        ]
        self.idx = TxIndexer(self.records)
        self.filters = normalize_filters()

    def test_directed_grouping_and_aggregation(self):
        result = self.idx.pair_stats(self.filters)
        groups = result["groups"]
        self.assertEqual(
            [(g["from_address"], g["to_address"], g["total_count"],
              g["total_amount"], g["avg_amount"]) for g in groups],
            [
                ("alice", "alice", 1, "100", "100"),
                ("bob", "alice", 1, "21", "21"),
                ("alice", "bob", 2, "15", "7"),   # 15 // 2
                ("alice", "carol", 1, "5", "5"),
            ],
        )
        self.assertEqual(result["total_groups"], 4)
        self.assertIsNone(result["next_cursor"])

    def test_direction_makes_distinct_groups(self):
        # alice→bob 与 bob→alice 必须是两个不同的有向分组
        result = self.idx.pair_stats(self.filters)
        keys = [(g["from_address"], g["to_address"]) for g in result["groups"]]
        self.assertIn(("alice", "bob"), keys)
        self.assertIn(("bob", "alice"), keys)

    def test_self_transfer_counted_once(self):
        records = [
            rec("s1", 1, 1, "alice", "alice", "m", "10"),
            rec("s2", 2, 2, "alice", "alice", "m", "11"),
        ]
        result = TxIndexer(records).pair_stats(self.filters)
        self.assertEqual(result["total_groups"], 1)
        group = result["groups"][0]
        self.assertEqual(
            (group["from_address"], group["to_address"],
             group["total_count"], group["total_amount"],
             group["avg_amount"]),
            ("alice", "alice", 2, "21", "10"),  # 21 // 2
        )

    def test_result_field_order(self):
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

    def test_sort_amount_then_count_then_from_then_to(self):
        records = [
            # a→x 与 b→x total 均为 10、count 均为 1：按 from 码点
            rec("h1", 1, 1, "a", "x", "m", "10"),
            rec("h2", 2, 2, "b", "x", "m", "10"),
            # a→y count=2 total=10：count 高于上面两组，应在它们之前
            rec("h3", 3, 3, "a", "y", "m", "4"),
            rec("h4", 4, 4, "a", "y", "m", "6"),
            # a→z total=11：total 最高，应最前
            rec("h5", 5, 5, "a", "z", "m", "11"),
        ]
        result = TxIndexer(records).pair_stats(self.filters)
        self.assertEqual(
            [(g["from_address"], g["to_address"])
             for g in result["groups"]],
            [("a", "z"), ("a", "y"), ("a", "x"), ("b", "x")],
        )

    def test_no_match(self):
        result = self.idx.pair_stats(normalize_filters(method="nope"))
        self.assertEqual(
            result, {"groups": [], "total_groups": 0, "next_cursor": None}
        )

    def test_filters_intersect_before_grouping(self):
        result = self.idx.pair_stats(
            normalize_filters(
                from_address=["alice"], method=["transfer"],
                start_time=10, end_time=30,
            )
        )
        # h1(alice→bob,10) 与 h3(alice→carol,5) 命中；
        # h4 是 approve、h5 ts=40 超出时间窗
        self.assertEqual(
            [(g["from_address"], g["to_address"], g["total_amount"])
             for g in result["groups"]],
            [("alice", "bob", "10"), ("alice", "carol", "5")],
        )
        self.assertEqual(result["total_groups"], 2)

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
            self.assertEqual(page["total_groups"], 4)
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(pages, 2)  # 4 组、page_size=2：恰好 2 页
        self.assertEqual(collected, all_pairs)

    def test_cursor_not_bound_to_page_size(self):
        page1 = self.idx.pair_stats(self.filters, page_size=1)
        self.assertEqual(
            [(g["from_address"], g["to_address"]) for g in page1["groups"]],
            [("alice", "alice")],
        )
        page2 = self.idx.pair_stats(
            self.filters, page_size=100, cursor=page1["next_cursor"]
        )
        self.assertEqual(
            [(g["from_address"], g["to_address"]) for g in page2["groups"]],
            [("bob", "alice"), ("alice", "bob"), ("alice", "carol")],
        )
        self.assertIsNone(page2["next_cursor"])

    def test_cursor_bound_to_filters(self):
        page1 = self.idx.pair_stats(self.filters, page_size=1)
        with self.assertRaises(InvalidCursorError):
            self.idx.pair_stats(
                normalize_filters(method="transfer"),
                page_size=1,
                cursor=page1["next_cursor"],
            )

    def test_cross_command_cursor_rejected_both_ways(self):
        from tx_indexer.cursor import (
            decode_method_stats_cursor,
            decode_pair_stats_cursor,
            encode_method_stats_cursor,
            encode_pair_stats_cursor,
        )

        filters = self.filters
        pair_cursor = encode_pair_stats_cursor(filters, 10, 1, "a", "b")
        method_cursor = encode_method_stats_cursor(filters, 10, 1, "m")
        with self.assertRaises(InvalidCursorError):
            decode_pair_stats_cursor(method_cursor, filters)
        with self.assertRaises(InvalidCursorError):
            decode_method_stats_cursor(pair_cursor, filters)

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
        payload["after"] = ["not", "an", "int", "marker"]
        tampered = base64.urlsafe_b64encode(
            json.dumps(payload).encode("utf-8")
        ).rstrip(b"=").decode("ascii")
        with self.assertRaises(InvalidCursorError):
            self.idx.pair_stats(self.filters, cursor=tampered)

    def test_invalid_page_size(self):
        for bad in (0, -1, 1001, "10", None):
            with self.assertRaises(InvalidPageSizeError):
                self.idx.pair_stats(self.filters, page_size=bad)

    def test_big_amounts_exact_decimal(self):
        big = "123456789012345678901234567890"
        idx = TxIndexer([
            rec("x", 1, 1, "a", "b", "m", big),
            rec("y", 2, 2, "a", "b", "m", "10"),
        ])
        group = idx.pair_stats(self.filters)["groups"][0]
        self.assertEqual(group["total_amount"], str(int(big) + 10))
        self.assertEqual(group["avg_amount"], str((int(big) + 10) // 2))


class AddressTimeStatsTest(unittest.TestCase):
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

    def test_grouping_and_aggregation(self):
        result = self.idx.address_time_stats(self.filters, 60)
        groups = result["groups"]
        self.assertEqual(
            [(g["address"], g["bucket_start"], g["bucket_end_exclusive"],
              g["send_count"], g["receive_count"], g["total_count"],
              g["total_amount"], g["avg_amount"]) for g in groups],
            [
                ("carol", 0, 60, 1, 1, 2, "105", "52"),  # 105 // 2
                ("dave", 0, 60, 0, 1, 1, "100", "100"),
                ("alice", 0, 60, 2, 1, 3, "36", "12"),   # 36 // 3
                ("bob", 0, 60, 1, 1, 2, "31", "15"),     # 31 // 2
                ("bob", 300, 360, 1, 0, 1, "40", "40"),
                ("dave", 300, 360, 0, 1, 1, "40", "40"),
            ],
        )
        self.assertEqual(result["total_groups"], 6)
        self.assertIsNone(result["next_cursor"])

    def test_group_field_order(self):
        result = self.idx.address_time_stats(self.filters, 60)
        self.assertEqual(
            list(result.keys()), ["groups", "total_groups", "next_cursor"]
        )
        group = result["groups"][0]
        self.assertEqual(
            list(group.keys()),
            ["address", "bucket_start", "bucket_end_exclusive",
             "send_count", "receive_count", "total_count",
             "total_amount", "avg_amount"],
        )
        self.assertIsInstance(group["bucket_start"], int)
        self.assertIsInstance(group["bucket_end_exclusive"], int)
        self.assertIsInstance(group["total_amount"], str)
        self.assertIsInstance(group["avg_amount"], str)

    def test_bucket_alignment_epoch_left_closed_right_open(self):
        records = [
            rec("b0", 1, 0, "a", "b", "m", "1"),
            rec("b59", 2, 59, "a", "b", "m", "2"),
            rec("b60", 3, 60, "a", "b", "m", "4"),
            rec("b119", 4, 119, "a", "b", "m", "8"),
            rec("b120", 5, 120, "a", "b", "m", "16"),
        ]
        result = TxIndexer(records).address_time_stats(self.filters, 60)
        # 每个非空区间有 a、b 两组；总额并列时 count/send 高的发送方在前，
        # bucket 0 各项并列按地址码点
        self.assertEqual(
            [(g["bucket_start"], g["address"], g["total_amount"])
             for g in result["groups"]],
            [
                (0, "a", "3"), (0, "b", "3"),
                (60, "a", "12"), (60, "b", "12"),
                (120, "a", "16"), (120, "b", "16"),
            ],
        )
        self.assertEqual(result["total_groups"], 6)

    def test_self_transfer_single_group_all_three_counts_amount_once(self):
        records = [
            rec("s1", 1, 1, "eva", "eva", "m", "100"),
            rec("s2", 2, 2, "eva", "eva", "m", "50"),
            rec("o1", 3, 3, "fin", "gus", "m", "7"),
        ]
        result = TxIndexer(records).address_time_stats(
            normalize_filters(), 60
        )
        groups = {(g["bucket_start"], g["address"]): g
                  for g in result["groups"]}
        self.assertEqual(set(groups), {
            (0, "eva"), (0, "fin"), (0, "gus"),
        })
        eva = groups[(0, "eva")]
        self.assertEqual(
            (eva["send_count"], eva["receive_count"], eva["total_count"]),
            (2, 2, 2),
        )
        self.assertEqual(eva["total_amount"], "150")  # 金额只累计一次/笔
        self.assertEqual(eva["avg_amount"], "75")

    def test_sort_bucket_start_primary_then_amount_count_send_receive_addr(self):
        records = [
            # bucket 0：
            rec("h1", 1, 1, "a", "x", "m", "5"),
            rec("h2", 2, 2, "a", "x", "m", "5"),    # x: recv2 total10
            rec("h3", 3, 3, "a", "y", "m", "10"),   # y: recv1 total10
            # z、w 总额 8、count 1、send 0 相同 → receive 相同按地址码点
            rec("h4", 4, 4, "a", "w", "m", "8"),
            rec("h5", 5, 5, "a", "z", "m", "8"),
            # bucket 60：receive 相同、count 相同，send 高的排前
            rec("h6", 6, 60, "p", "q", "m", "10"),  # q: recv1 send0
            rec("h7", 7, 61, "q", "q", "m", "10"),  # q 自转账: send1 recv1
        ]
        result = TxIndexer(records).address_time_stats(
            normalize_filters(), 60
        )
        self.assertEqual(
            [(g["bucket_start"], g["address"]) for g in result["groups"]],
            [
                # a: send4 total36 居首；x/y total10：count2 的 x 在前
                (0, "a"), (0, "x"), (0, "y"), (0, "w"), (0, "z"),
                # bucket 60：q(total20, send1) 先于 p(10, send1)
                (60, "q"), (60, "p"),
            ],
        )

    def test_sort_receive_count_tiebreak(self):
        records = [
            rec("h1", 1, 1, "x", "x", "m", "10"),  # x: send1 recv1 count1
            rec("h2", 2, 2, "y", "z", "m", "10"),  # y: send1 recv0 count1
        ]
        result = TxIndexer(records).address_time_stats(
            normalize_filters(), 60
        )
        # x 与 y 总额/count/send 相同，receive_count 高的 x 在前；
        # z receive-only 与 y 总额相同但 count 相同 send 低，故在后
        self.assertEqual(
            [g["address"] for g in result["groups"]][:3],
            ["x", "y", "z"],
        )

    def test_only_nonempty_bucket_address_groups_returned(self):
        records = [
            rec("a", 1, 10, "x", "y", "m", "1"),
            rec("b", 2, 1000, "x", "y", "m", "2"),
        ]
        result = TxIndexer(records).address_time_stats(self.filters, 60)
        self.assertEqual(
            [(g["bucket_start"], g["address"]) for g in result["groups"]],
            [(0, "x"), (0, "y"), (960, "x"), (960, "y")],
        )

    def test_filters_applied(self):
        result = self.idx.address_time_stats(
            normalize_filters(method="transfer"), 60
        )
        # h1/h3/h5 在 bucket 0，h4 在 bucket 300；
        # carol 收 5 又发 100，bucket 0 内总额 105 居首
        self.assertEqual(
            [(g["bucket_start"], g["address"], g["total_amount"])
             for g in result["groups"]],
            [
                (0, "carol", "105"),
                (0, "dave", "100"),
                (0, "alice", "15"),
                (0, "bob", "10"),
                (300, "bob", "40"),
                (300, "dave", "40"),
            ],
        )

    def test_time_window_inclusive_both_ends(self):
        result = self.idx.address_time_stats(
            normalize_filters(start_time=10, end_time=20), 60
        )
        # h1、h2 命中：alice/bob 各一组
        self.assertEqual(result["total_groups"], 2)
        self.assertEqual(
            [(g["address"], g["total_count"]) for g in result["groups"]],
            [("alice", 2), ("bob", 2)],
        )

    def test_no_match(self):
        result = self.idx.address_time_stats(
            normalize_filters(method="nonexistent"), 60
        )
        self.assertEqual(
            result, {"groups": [], "total_groups": 0, "next_cursor": None}
        )

    def test_pagination_no_skip_no_dup_no_reorder(self):
        all_keys = [
            (g["bucket_start"], g["address"])
            for g in self.idx.address_time_stats(
                self.filters, 60, page_size=100
            )["groups"]
        ]
        collected = []
        cursor = None
        pages = 0
        while True:
            page = self.idx.address_time_stats(
                self.filters, 60, page_size=2, cursor=cursor
            )
            pages += 1
            collected.extend(
                (g["bucket_start"], g["address"]) for g in page["groups"]
            )
            self.assertEqual(page["total_groups"], 6)
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(pages, 3)  # 6 组、page_size=2：3 页
        self.assertEqual(collected, all_keys)

    def test_cursor_not_bound_to_page_size(self):
        page1 = self.idx.address_time_stats(self.filters, 60, page_size=2)
        page2 = self.idx.address_time_stats(
            self.filters, 60, page_size=100, cursor=page1["next_cursor"]
        )
        self.assertEqual(
            [(g["bucket_start"], g["address"]) for g in page2["groups"]],
            [(0, "alice"), (0, "bob"), (300, "bob"), (300, "dave")],
        )
        self.assertIsNone(page2["next_cursor"])

    def test_cursor_bound_to_bucket_size(self):
        page1 = self.idx.address_time_stats(self.filters, 60, page_size=1)
        with self.assertRaises(InvalidCursorError):
            self.idx.address_time_stats(
                self.filters, 30, page_size=1, cursor=page1["next_cursor"]
            )

    def test_cursor_bound_to_filters(self):
        page1 = self.idx.address_time_stats(self.filters, 60, page_size=1)
        with self.assertRaises(InvalidCursorError):
            self.idx.address_time_stats(
                normalize_filters(method="transfer"),
                60, page_size=1, cursor=page1["next_cursor"],
            )

    def test_cross_command_cursor_rejected(self):
        from tx_indexer.cursor import (
            decode_address_stats_cursor,
            decode_address_time_stats_cursor,
            decode_time_stats_cursor,
            encode_address_stats_cursor,
            encode_address_time_stats_cursor,
            encode_time_stats_cursor,
        )

        filters = normalize_filters()
        ats_cursor = encode_address_time_stats_cursor(
            filters, 60, 0, 10, 1, 1, 0, "a"
        )
        time_cursor = encode_time_stats_cursor(filters, 60, 0)
        address_cursor = encode_address_stats_cursor(
            filters, 10, 1, 1, 0, "a"
        )
        with self.assertRaises(InvalidCursorError):
            decode_address_time_stats_cursor(time_cursor, filters, 60)
        with self.assertRaises(InvalidCursorError):
            decode_address_time_stats_cursor(address_cursor, filters, 60)
        with self.assertRaises(InvalidCursorError):
            decode_time_stats_cursor(ats_cursor, filters, 60)
        with self.assertRaises(InvalidCursorError):
            decode_address_stats_cursor(ats_cursor, filters)

    def test_cursor_garbage(self):
        for bad in ("", "not-base64!!!", "bm9wZQ", "%%%"):
            with self.assertRaises(InvalidCursorError):
                self.idx.address_time_stats(self.filters, 60, cursor=bad)

    def test_cursor_tampered(self):
        import base64
        import json

        page1 = self.idx.address_time_stats(self.filters, 60, page_size=1)
        token = page1["next_cursor"]
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        payload = json.loads(raw.decode("utf-8"))
        payload["b"] = 30  # 篡改 bucket_size
        tampered = base64.urlsafe_b64encode(
            json.dumps(payload).encode("utf-8")
        ).rstrip(b"=").decode("ascii")
        with self.assertRaises(InvalidCursorError):
            self.idx.address_time_stats(self.filters, 60, cursor=tampered)

    def test_invalid_bucket_size(self):
        for bad in (0, -1, -60, "60", 1.5, True, None):
            with self.assertRaises(InvalidBucketSizeError):
                self.idx.address_time_stats(self.filters, bad)

    def test_invalid_page_size(self):
        for bad in (0, -1, 1001, "10", None):
            with self.assertRaises(InvalidPageSizeError):
                self.idx.address_time_stats(self.filters, 60, page_size=bad)

    def test_big_amounts_exact_decimal(self):
        big = "123456789012345678901234567890"
        idx = TxIndexer([
            rec("x", 1, 1, "a", "b", "m", big),
            rec("y", 2, 2, "a", "b", "m", "10"),
        ])
        result = idx.address_time_stats(normalize_filters(), 60)
        groups = {(g["address"]): g for g in result["groups"]}
        total = str(int(big) + 10)
        self.assertEqual(groups["a"]["total_amount"], total)
        self.assertEqual(groups["a"]["avg_amount"], str(int(total) // 2))
        self.assertEqual(groups["b"]["total_amount"], total)
        self.assertEqual(groups["b"]["avg_amount"], str(int(total) // 2))


class AmountFilterTest(unittest.TestCase):
    def setUp(self):
        self.records = [
            rec("h0", 1, 0, "alice", "bob", "transfer", "0"),
            rec("h1", 1, 10, "alice", "bob", "transfer", "10"),
            rec("h2", 2, 20, "bob", "alice", "approve", "20"),
            rec("h3", 2, 30, "alice", "carol", "transfer", "30"),
            rec("h4", 3, 40, "bob", "dave", "transfer", "100"),
        ]
        self.idx = TxIndexer(self.records)

    def test_query_closed_interval_inclusive_both_ends(self):
        result = self.idx.query(
            normalize_filters(min_amount="10", max_amount="30")
        )
        self.assertEqual(hashes(result), ["h1", "h2", "h3"])
        self.assertEqual(result["total"], 3)
        self.assertIsNone(result["next_cursor"])

    def test_query_single_bound_unbounded_other_side(self):
        self.assertEqual(
            hashes(self.idx.query(normalize_filters(min_amount="20"))),
            ["h2", "h3", "h4"],
        )
        self.assertEqual(
            hashes(self.idx.query(normalize_filters(max_amount="20"))),
            ["h0", "h1", "h2"],
        )

    def test_query_zero_is_valid_bound(self):
        result = self.idx.query(
            normalize_filters(min_amount="0", max_amount="0")
        )
        self.assertEqual(hashes(result), ["h0"])

    def test_query_leading_zeros_numeric_equivalence(self):
        result = self.idx.query(
            normalize_filters(min_amount="0010", max_amount="030")
        )
        self.assertEqual(hashes(result), ["h1", "h2", "h3"])

    def test_query_intersects_with_other_filters(self):
        result = self.idx.query(
            normalize_filters(
                address="alice", method="transfer",
                start_time=0, end_time=100,
                min_amount="10", max_amount="10",
            )
        )
        self.assertEqual(hashes(result), ["h1"])

    def test_query_empty_result_shape_unchanged(self):
        result = self.idx.query(
            normalize_filters(min_amount="1000"), page_size=2
        )
        self.assertEqual(result["transactions"], [])
        self.assertEqual(result["total"], 0)
        self.assertIsNone(result["next_cursor"])

    def test_stats_recomputed_on_matched_only(self):
        stats = self.idx.stats(
            normalize_filters(min_amount="20", max_amount="100")
        )
        self.assertEqual(stats, {
            "total_count": 3,
            "total_amount": "150",
            "min_amount": "20",
            "max_amount": "100",
            "avg_amount": "50",
        })

    def test_stats_empty_shape_unchanged(self):
        stats = self.idx.stats(normalize_filters(max_amount="0"))
        self.assertEqual(stats["total_count"], 1)
        self.assertEqual(stats["total_amount"], "0")
        self.assertEqual(stats["min_amount"], "0")
        self.assertEqual(stats["max_amount"], "0")
        self.assertEqual(stats["avg_amount"], "0")
        none_match = self.idx.stats(normalize_filters(min_amount="999"))
        self.assertEqual(none_match, {
            "total_count": 0,
            "total_amount": "0",
            "min_amount": None,
            "max_amount": None,
            "avg_amount": None,
        })

    def test_grouped_stats_all_scope_amount_filter(self):
        f = normalize_filters(min_amount="20")
        method = self.idx.method_stats(f)
        self.assertEqual(
            [g["method"] for g in method["groups"]],
            ["transfer", "approve"],
        )
        self.assertEqual(method["total_groups"], 2)
        by_method = {g["method"]: g for g in method["groups"]}
        self.assertEqual(by_method["approve"]["total_amount"], "20")
        self.assertEqual(by_method["transfer"]["total_amount"], "130")

        addresses = self.idx.address_stats(f)
        self.assertEqual(addresses["total_groups"], 4)
        amounts = {g["address"]: g["total_amount"]
                   for g in addresses["groups"]}
        self.assertEqual(amounts,
                         {"alice": "50", "bob": "120",
                          "carol": "30", "dave": "100"})

        counterparties = self.idx.counterparty_stats(
            normalize_filters(address="alice", min_amount="20")
        )
        self.assertEqual(
            [g["counterparty"] for g in counterparties["groups"]],
            ["carol", "bob"],
        )
        cp_amounts = {g["counterparty"]: g["total_amount"]
                      for g in counterparties["groups"]}
        self.assertEqual(cp_amounts, {"carol": "30", "bob": "20"})

        buckets = self.idx.time_stats(f, bucket_size=100)
        self.assertEqual(buckets["total_groups"], 1)
        self.assertEqual(buckets["groups"][0]["total_amount"], "150")

        pairs = self.idx.pair_stats(f)
        self.assertEqual(pairs["total_groups"], 3)
        pair_amounts = {
            (g["from_address"], g["to_address"]): g["total_amount"]
            for g in pairs["groups"]
        }
        self.assertEqual(pair_amounts, {
            ("bob", "dave"): "100",
            ("alice", "carol"): "30",
            ("bob", "alice"): "20",
        })

        ats = self.idx.address_time_stats(f, bucket_size=100)
        self.assertEqual(ats["total_groups"], 4)

    def test_grouped_stats_empty_shape_unchanged(self):
        f = normalize_filters(min_amount="999")
        self.assertEqual(self.idx.method_stats(f)["groups"], [])
        self.assertEqual(self.idx.method_stats(f)["total_groups"], 0)
        self.assertIsNone(self.idx.method_stats(f)["next_cursor"])
        self.assertEqual(self.idx.address_stats(f)["groups"], [])
        cp = self.idx.counterparty_stats(
            normalize_filters(address="alice", min_amount="999"))
        self.assertEqual(cp["groups"], [])
        self.assertEqual(cp["address"], "alice")
        self.assertEqual(self.idx.time_stats(f, 60)["groups"], [])
        self.assertEqual(self.idx.pair_stats(f)["groups"], [])
        self.assertEqual(self.idx.address_time_stats(f, 60)["groups"], [])

    def test_invalid_amount_filter_values(self):
        bad_values = (
            "", " ", "-1", "+1", "1.0", ".5", "1e3", "0x10",
            " 10", "10 ", "１０", "1_000",
        )
        # None 表示未给定，不是非法值
        self.assertIsNone(normalize_filters(min_amount=None)["min_amount"])
        for bad in bad_values:
            with self.assertRaises(InvalidAmountFilterError) as ctx_min:
                normalize_filters(min_amount=bad)
            self.assertNotIsInstance(
                ctx_min.exception, ValueError,
                "金额非法不能回退为 ValueError：%r" % bad,
            )
            with self.assertRaises(InvalidAmountFilterError):
                normalize_filters(max_amount=bad)

    def test_invalid_amount_filter_non_string(self):
        for bad in (10, 10.0, True, b"10", ["10"], {"v": 1}, object()):
            with self.assertRaises(InvalidAmountFilterError) as ctx:
                normalize_filters(min_amount=bad)
            self.assertNotIsInstance(ctx.exception, ValueError)
            with self.assertRaises(InvalidAmountFilterError):
                normalize_filters(max_amount=bad)

    def test_amount_range_inverted(self):
        with self.assertRaises(InvalidAmountRangeError) as ctx:
            normalize_filters(min_amount="11", max_amount="10")
        self.assertNotIsInstance(ctx.exception, ValueError)
        self.assertEqual(ctx.exception.error, "invalid_amount_range")
        # 相等是合法闭区间
        filters = normalize_filters(min_amount="10", max_amount="10")
        self.assertEqual(filters["min_amount"], "10")
        self.assertEqual(filters["max_amount"], "10")

    def test_range_check_uses_numeric_value_with_leading_zeros(self):
        # 数值等价：0011 == 11 > 10，倒置；0010 == 10，不倒置
        with self.assertRaises(InvalidAmountRangeError):
            normalize_filters(min_amount="0011", max_amount="10")
        filters = normalize_filters(min_amount="0010", max_amount="010")
        self.assertEqual(filters["min_amount"], "10")
        self.assertEqual(filters["max_amount"], "10")

    def test_pagination_bound_to_amount(self):
        filters = normalize_filters(min_amount="20")
        collected = []
        cursor = None
        while True:
            page = self.idx.query(filters, page_size=1, cursor=cursor)
            self.assertEqual(page["total"], 3)
            collected.extend(hashes(page))
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(collected, ["h2", "h3", "h4"])

    def test_cursor_rejects_changed_amount_bounds(self):
        page = self.idx.query(
            normalize_filters(min_amount="20"), page_size=1
        )
        cursor = page["next_cursor"]
        self.assertIsNotNone(cursor)
        # 改下界
        with self.assertRaises(InvalidCursorError):
            self.idx.query(
                normalize_filters(min_amount="10"), page_size=1,
                cursor=cursor,
            )
        # 增上界
        with self.assertRaises(InvalidCursorError):
            self.idx.query(
                normalize_filters(min_amount="20", max_amount="100"),
                page_size=1, cursor=cursor,
            )
        # 删除边界
        with self.assertRaises(InvalidCursorError):
            self.idx.query(
                normalize_filters(), page_size=1, cursor=cursor
            )
        # 无金额游标不能在有金额筛选下续用
        no_amount = self.idx.query(normalize_filters(), page_size=1)
        with self.assertRaises(InvalidCursorError):
            self.idx.query(
                normalize_filters(max_amount="30"), page_size=1,
                cursor=no_amount["next_cursor"],
            )

    def test_cursor_allows_leading_zero_only_change(self):
        page1 = self.idx.query(
            normalize_filters(min_amount="20"), page_size=1
        )
        cursor = page1["next_cursor"]
        # 只调整前导零：数值等价，可继续翻页
        page2 = self.idx.query(
            normalize_filters(min_amount="020"), page_size=1,
            cursor=cursor,
        )
        self.assertEqual(hashes(page2), ["h3"])

    def test_grouped_stats_cursor_bound_to_amount(self):
        page = self.idx.method_stats(
            normalize_filters(min_amount="20"), page_size=1
        )
        cursor = page["next_cursor"]
        with self.assertRaises(InvalidCursorError):
            self.idx.method_stats(
                normalize_filters(min_amount="10"), page_size=1,
                cursor=cursor,
            )
        # 前导零等价可续页
        again = self.idx.method_stats(
            normalize_filters(min_amount="020"), page_size=1,
            cursor=cursor,
        )
        self.assertEqual([g["method"] for g in again["groups"]], ["approve"])

    def test_big_amounts_exact_decimal_comparison(self):
        big = "123456789012345678901234567890"
        bigger = "123456789012345678901234567891"
        idx = TxIndexer([
            rec("x", 1, 1, "a", "b", "m", big),
            rec("y", 2, 2, "a", "b", "m", bigger),
        ])
        result = idx.query(normalize_filters(min_amount=bigger))
        self.assertEqual(hashes(result), ["y"])
        stats = idx.stats(normalize_filters(min_amount=big, max_amount=big))
        self.assertEqual(stats["total_amount"], big)


class BlockFilterTest(unittest.TestCase):
    def setUp(self):
        self.records = [
            rec("h0", 0, 0, "alice", "bob", "transfer", "0"),
            rec("h1", 1, 10, "alice", "bob", "transfer", "10"),
            rec("h2", 2, 20, "bob", "alice", "approve", "20"),
            rec("h3", 2, 30, "alice", "carol", "transfer", "30"),
            rec("h4", 3, 40, "bob", "dave", "transfer", "100"),
        ]
        self.idx = TxIndexer(self.records)

    def test_query_closed_interval_inclusive_both_ends(self):
        result = self.idx.query(
            normalize_filters(min_block=1, max_block=2)
        )
        self.assertEqual(hashes(result), ["h1", "h2", "h3"])
        self.assertEqual(result["total"], 3)
        self.assertIsNone(result["next_cursor"])

    def test_query_single_bound_unbounded_other_side(self):
        self.assertEqual(
            hashes(self.idx.query(normalize_filters(min_block=2))),
            ["h2", "h3", "h4"],
        )
        self.assertEqual(
            hashes(self.idx.query(normalize_filters(max_block=1))),
            ["h0", "h1"],
        )

    def test_query_zero_is_valid_bound(self):
        result = self.idx.query(
            normalize_filters(min_block=0, max_block=0)
        )
        self.assertEqual(hashes(result), ["h0"])

    def test_equal_bounds_single_block(self):
        filters = normalize_filters(min_block=2, max_block=2)
        self.assertEqual(filters["min_block"], 2)
        self.assertEqual(filters["max_block"], 2)
        self.assertEqual(
            hashes(self.idx.query(filters)), ["h2", "h3"]
        )

    def test_query_intersects_with_other_filters(self):
        result = self.idx.query(
            normalize_filters(
                address="alice", method="transfer",
                start_time=0, end_time=100,
                min_amount="10", max_amount="30",
                min_block=1, max_block=2,
            )
        )
        self.assertEqual(hashes(result), ["h1", "h3"])
        # 与金额区间求交集：alice 的 transfer 中金额只有 h1/h3 落在
        # 10..30，但区块下界 2 排除 h1
        result2 = self.idx.query(
            normalize_filters(
                address="alice", method="transfer",
                min_amount="10", max_amount="30",
                min_block=2,
            )
        )
        self.assertEqual(hashes(result2), ["h3"])

    def test_query_empty_result_shape_unchanged(self):
        result = self.idx.query(
            normalize_filters(min_block=999), page_size=2
        )
        self.assertEqual(result["transactions"], [])
        self.assertEqual(result["total"], 0)
        self.assertIsNone(result["next_cursor"])

    def test_stats_recomputed_on_matched_only(self):
        stats = self.idx.stats(normalize_filters(min_block=2))
        self.assertEqual(stats, {
            "total_count": 3,
            "total_amount": "150",
            "min_amount": "20",
            "max_amount": "100",
            "avg_amount": "50",
        })

    def test_stats_empty_shape_unchanged(self):
        stats = self.idx.stats(normalize_filters(max_block=0))
        self.assertEqual(stats["total_count"], 1)
        self.assertEqual(stats["total_amount"], "0")
        none_match = self.idx.stats(normalize_filters(min_block=999))
        self.assertEqual(none_match, {
            "total_count": 0,
            "total_amount": "0",
            "min_amount": None,
            "max_amount": None,
            "avg_amount": None,
        })

    def test_grouped_stats_all_scope_block_filter(self):
        f = normalize_filters(min_block=2)
        method = self.idx.method_stats(f)
        self.assertEqual(method["total_groups"], 2)
        by_method = {g["method"]: g for g in method["groups"]}
        self.assertEqual(by_method["transfer"]["total_count"], 2)
        self.assertEqual(by_method["transfer"]["total_amount"], "130")
        self.assertEqual(by_method["approve"]["total_amount"], "20")

        addresses = self.idx.address_stats(f)
        self.assertEqual(addresses["total_groups"], 4)
        amounts = {g["address"]: g["total_amount"]
                   for g in addresses["groups"]}
        self.assertEqual(amounts,
                         {"alice": "50", "bob": "120",
                          "carol": "30", "dave": "100"})

        counterparties = self.idx.counterparty_stats(
            normalize_filters(address="alice", min_block=2)
        )
        self.assertEqual(counterparties["address"], "alice")
        self.assertEqual(counterparties["total_groups"], 2)
        cp_amounts = {g["counterparty"]: g["total_amount"]
                      for g in counterparties["groups"]}
        self.assertEqual(cp_amounts, {"carol": "30", "bob": "20"})

        buckets = self.idx.time_stats(f, bucket_size=100)
        self.assertEqual(buckets["total_groups"], 1)
        self.assertEqual(buckets["groups"][0]["total_amount"], "150")

        pairs = self.idx.pair_stats(f)
        self.assertEqual(pairs["total_groups"], 3)

        ats = self.idx.address_time_stats(f, bucket_size=100)
        self.assertEqual(ats["total_groups"], 4)

    def test_grouped_stats_empty_shape_unchanged(self):
        f = normalize_filters(min_block=999)
        self.assertEqual(self.idx.method_stats(f)["groups"], [])
        self.assertEqual(self.idx.method_stats(f)["total_groups"], 0)
        self.assertIsNone(self.idx.method_stats(f)["next_cursor"])
        self.assertEqual(self.idx.address_stats(f)["groups"], [])
        cp = self.idx.counterparty_stats(
            normalize_filters(address="alice", min_block=999))
        self.assertEqual(cp["groups"], [])
        self.assertEqual(cp["address"], "alice")
        self.assertEqual(self.idx.time_stats(f, 60)["groups"], [])
        self.assertEqual(self.idx.time_stats(f, 60)["total_groups"], 0)
        self.assertEqual(self.idx.pair_stats(f)["groups"], [])
        self.assertEqual(self.idx.address_time_stats(f, 60)["groups"], [])

    def test_invalid_block_filter_non_int(self):
        self.assertIsNone(normalize_filters(min_block=None)["min_block"])
        for bad in ("10", "", " ", "-1", "+1", "1.0", 10.0, True, False,
                    b"10", [1], {"v": 1}, object(), -1):
            with self.assertRaises(InvalidBlockFilterError) as ctx_min:
                normalize_filters(min_block=bad)
            self.assertNotIsInstance(
                ctx_min.exception, ValueError,
                "区块边界非法不能回退为 ValueError：%r" % bad,
            )
            with self.assertRaises(InvalidBlockFilterError):
                normalize_filters(max_block=bad)

    def test_block_range_inverted(self):
        with self.assertRaises(InvalidBlockRangeError) as ctx:
            normalize_filters(min_block=11, max_block=10)
        self.assertNotIsInstance(ctx.exception, ValueError)
        self.assertEqual(ctx.exception.error, "invalid_block_range")

    def test_pagination_bound_to_block(self):
        filters = normalize_filters(min_block=2)
        collected = []
        cursor = None
        while True:
            page = self.idx.query(filters, page_size=1, cursor=cursor)
            self.assertEqual(page["total"], 3)
            collected.extend(hashes(page))
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(collected, ["h2", "h3", "h4"])

    def test_cursor_rejects_changed_block_bounds(self):
        page = self.idx.query(
            normalize_filters(min_block=2), page_size=1
        )
        cursor = page["next_cursor"]
        self.assertIsNotNone(cursor)
        # 改下界
        with self.assertRaises(InvalidCursorError):
            self.idx.query(
                normalize_filters(min_block=1), page_size=1,
                cursor=cursor,
            )
        # 增上界
        with self.assertRaises(InvalidCursorError):
            self.idx.query(
                normalize_filters(min_block=2, max_block=100),
                page_size=1, cursor=cursor,
            )
        # 删除边界
        with self.assertRaises(InvalidCursorError):
            self.idx.query(
                normalize_filters(), page_size=1, cursor=cursor
            )
        # 无区块边界游标不能在有区块筛选下续用
        no_block = self.idx.query(normalize_filters(), page_size=1)
        with self.assertRaises(InvalidCursorError):
            self.idx.query(
                normalize_filters(max_block=30), page_size=1,
                cursor=no_block["next_cursor"],
            )

    def test_cursor_same_bounds_different_request_continues(self):
        page1 = self.idx.query(
            normalize_filters(min_block=2), page_size=1
        )
        cursor = page1["next_cursor"]
        # 相同边界（数值相同即可，与金额不同，整数无前导零概念）续页
        page2 = self.idx.query(
            normalize_filters(min_block=2), page_size=1, cursor=cursor
        )
        self.assertEqual(hashes(page2), ["h3"])

    def test_grouped_stats_cursor_bound_to_block(self):
        page = self.idx.method_stats(
            normalize_filters(min_block=2), page_size=1
        )
        cursor = page["next_cursor"]
        with self.assertRaises(InvalidCursorError):
            self.idx.method_stats(
                normalize_filters(min_block=1), page_size=1,
                cursor=cursor,
            )
        again = self.idx.method_stats(
            normalize_filters(min_block=2), page_size=1, cursor=cursor
        )
        # min_block=2 下 transfer 总额 130 排首位，次页为 approve
        self.assertEqual([g["method"] for g in again["groups"]], ["approve"])

    def test_time_stats_cursor_bound_to_block(self):
        page = self.idx.time_stats(
            normalize_filters(max_block=2), bucket_size=15, page_size=1
        )
        self.assertIsNotNone(page["next_cursor"])
        cursor = page["next_cursor"]
        with self.assertRaises(InvalidCursorError):
            self.idx.time_stats(
                normalize_filters(), bucket_size=15, page_size=1,
                cursor=cursor,
            )

    def test_legacy_cursor_without_block_fields(self):
        """旧游标（筛选快照缺少 min_block/max_block）只在未指定区块
        边界时可续翻；指定任一边界即报 invalid_cursor。"""
        from tx_indexer.cursor import SCOPE_QUERY, _encode_payload

        legacy_filters = {
            "address": None,
            "from_address": None,
            "to_address": None,
            "method": None,
            "start_time": None,
            "end_time": None,
            "min_amount": None,
            "max_amount": None,
        }
        token = _encode_payload({
            "v": 1,
            "c": SCOPE_QUERY,
            "f": legacy_filters,
            "after": [1, "h1"],
        })
        # 未指定区块边界：旧游标可续页（从 h1 之后开始）
        page = self.idx.query(
            normalize_filters(), page_size=10, cursor=token
        )
        self.assertEqual(hashes(page), ["h2", "h3", "h4"])
        # 指定任一边界：旧游标失效
        with self.assertRaises(InvalidCursorError):
            self.idx.query(
                normalize_filters(min_block=0), page_size=10, cursor=token
            )
        with self.assertRaises(InvalidCursorError):
            self.idx.query(
                normalize_filters(max_block=3), page_size=10, cursor=token
            )


if __name__ == "__main__":
    unittest.main()
