"""address-flow-stats（地址资金流向统计）测试。"""

import unittest

from tx_indexer.engine import TxIndexer, normalize_filters
from tx_indexer.errors import InvalidCursorError, InvalidPageSizeError


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


class AddressFlowStatsTest(unittest.TestCase):
    def setUp(self):
        self.records = [
            rec("a", 1, 10, "alice", "bob", "transfer", "100"),
            rec("b", 2, 20, "bob", "alice", "transfer", "30"),
            rec("c", 3, 30, "carol", "dave", "approve", "200"),
            rec("d", 4, 40, "alice", "carol", "mint", "15"),
        ]
        self.idx = TxIndexer(self.records)

    def test_split_sent_received_and_net(self):
        result = self.idx.address_flow_stats(normalize_filters())
        groups = result["groups"]
        # bob：收 100、发 30 → net 70；carol：收 15、发 200 → net -185；
        # dave：收 200 → net 200；alice：发 115、收 30 → net -85
        self.assertEqual(
            [(g["address"], g["sent_amount"], g["received_amount"],
              g["net_amount"], g["send_count"], g["receive_count"],
              g["total_count"])
             for g in groups],
            [
                ("dave", "0", "200", "200", 0, 1, 1),
                ("bob", "30", "100", "70", 1, 1, 2),
                ("alice", "115", "30", "-85", 2, 1, 3),
                ("carol", "200", "15", "-185", 1, 1, 2),
            ],
        )
        self.assertEqual(result["total_groups"], 4)
        self.assertIsNone(result["next_cursor"])

    def test_group_field_order_and_types(self):
        group = self.idx.address_flow_stats(normalize_filters())["groups"][0]
        self.assertEqual(
            list(group.keys()),
            ["address", "sent_amount", "received_amount", "net_amount",
             "send_count", "receive_count", "total_count"],
        )
        for name in ("sent_amount", "received_amount", "net_amount"):
            self.assertIsInstance(group[name], str)

    def test_zero_net_amount_is_zero_string(self):
        records = [
            rec("x", 1, 10, "a", "b", "m", "10"),
            rec("y", 2, 20, "b", "a", "m", "10"),
        ]
        groups = {
            g["address"]: g
            for g in TxIndexer(records).address_flow_stats(
                normalize_filters()
            )["groups"]
        }
        self.assertEqual(groups["a"]["net_amount"], "0")
        self.assertEqual(groups["b"]["net_amount"], "0")

    def test_self_transfer_counts_both_sides_total_once(self):
        records = [
            rec("s1", 1, 1, "eva", "eva", "m", "100"),
            rec("s2", 2, 2, "eva", "eva", "m", "50"),
            rec("o1", 3, 3, "fin", "gus", "m", "7"),
        ]
        groups = {
            g["address"]: g
            for g in TxIndexer(records).address_flow_stats(
                normalize_filters()
            )["groups"]
        }
        eva = groups["eva"]
        # 自转账两方各计：sent = received = 150，net = 0；
        # total_count 两笔只计两次，send/receive 各两次
        self.assertEqual(eva["sent_amount"], "150")
        self.assertEqual(eva["received_amount"], "150")
        self.assertEqual(eva["net_amount"], "0")
        self.assertEqual(eva["send_count"], 2)
        self.assertEqual(eva["receive_count"], 2)
        self.assertEqual(eva["total_count"], 2)
        self.assertEqual(groups["fin"]["sent_amount"], "7")
        self.assertEqual(groups["fin"]["received_amount"], "0")
        self.assertEqual(groups["fin"]["net_amount"], "-7")
        self.assertEqual(groups["gus"]["received_amount"], "7")
        self.assertEqual(groups["gus"]["sent_amount"], "0")
        self.assertEqual(groups["gus"]["net_amount"], "7")

    def test_self_transfer_mixed_with_normal(self):
        records = [
            rec("s1", 1, 1, "a", "a", "m", "10"),
            rec("o1", 2, 2, "a", "b", "m", "5"),
            rec("i1", 3, 3, "b", "a", "m", "7"),
        ]
        groups = {
            g["address"]: g
            for g in TxIndexer(records).address_flow_stats(
                normalize_filters()
            )["groups"]
        }
        a = groups["a"]
        # a：自转账 10 两方各计、发给 b 5、收 b 7
        # sent = 10+5 = 15，received = 10+7 = 17，net = 2
        self.assertEqual(a["sent_amount"], "15")
        self.assertEqual(a["received_amount"], "17")
        self.assertEqual(a["net_amount"], "2")
        self.assertEqual((a["send_count"], a["receive_count"],
                          a["total_count"]), (2, 2, 3))
        b = groups["b"]
        self.assertEqual(b["sent_amount"], "7")
        self.assertEqual(b["received_amount"], "5")
        self.assertEqual(b["net_amount"], "-2")

    def test_sort_net_then_sent_then_received_then_count_then_address(self):
        records = [
            # 两笔 a→b 各 5、一笔 b→a 5：a、b 的 net 都为 -5 / 5
            # 另构造 net 相同的对照：
            # x 收 10（净 10），y 收 10（净 10）→ net 相同，
            # sent/received/total_count 全相同，按地址码点 x、y
            rec("h1", 1, 1, "a", "x", "m", "10"),
            rec("h2", 2, 2, "a", "y", "m", "10"),
            # z 净 10 但 received 来自两笔（total_count 2），
            # 与只收一笔净 10 的地址对照 total_count 降序
            rec("h3", 3, 3, "a", "z", "m", "4"),
            rec("h4", 4, 4, "c", "z", "m", "6"),
        ]
        result = TxIndexer(records).address_flow_stats(normalize_filters())
        addresses = [g["address"] for g in result["groups"]]
        # x、y、z 净 +10：z total_count=2 在前，x/y total_count=1
        # 按码点；a 净 -20 最后，c 净 -6 在 a 前
        self.assertEqual(addresses, ["z", "x", "y", "c", "a"])

    def test_sort_sent_tiebreak(self):
        records = [
            # p、q net 相同(-10)、received 相同(0)、total_count 相同(1)：
            # sent 相同(10) → 按地址码点
            rec("h1", 1, 1, "p", "r", "m", "10"),
            rec("h2", 2, 2, "q", "r", "m", "10"),
        ]
        result = TxIndexer(records).address_flow_stats(normalize_filters())
        self.assertEqual(
            [g["address"] for g in result["groups"]], ["r", "p", "q"]
        )

    def test_mixed_net_ordering(self):
        records = [
            rec("h1", 1, 1, "v", "u", "m", "5"),
            rec("h2", 2, 2, "u", "v", "m", "10"),
            rec("h3", 3, 3, "w", "v", "m", "15"),
            rec("h4", 4, 4, "v", "w", "m", "20"),
        ]
        # v：sent 5+20=25，received 10+15=25 → net 0
        # u：sent 10，received 5 → net -5
        # w：sent 15，received 20 → net 5
        result = TxIndexer(records).address_flow_stats(normalize_filters())
        self.assertEqual(
            [g["address"] for g in result["groups"]], ["w", "v", "u"]
        )

    def test_pagination_no_skip_no_dup_no_reorder(self):
        filters = normalize_filters()
        all_addresses = [
            g["address"]
            for g in self.idx.address_flow_stats(
                filters, page_size=100
            )["groups"]
        ]
        collected = []
        cursor = None
        pages = 0
        while True:
            page = self.idx.address_flow_stats(
                filters, page_size=2, cursor=cursor
            )
            pages += 1
            collected.extend(g["address"] for g in page["groups"])
            self.assertEqual(page["total_groups"], 4)
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(pages, 2)
        self.assertEqual(collected, all_addresses)

    def test_page_size_not_bound_to_cursor(self):
        page1 = self.idx.address_flow_stats(
            normalize_filters(), page_size=1
        )
        # 游标在 page_size=3 的请求下续页同样有效
        page2 = self.idx.address_flow_stats(
            normalize_filters(), page_size=3, cursor=page1["next_cursor"]
        )
        self.assertEqual(
            [g["address"] for g in page2["groups"]],
            ["bob", "alice", "carol"],
        )
        self.assertIsNone(page2["next_cursor"])

    def test_last_page_partial_returns_null_cursor(self):
        page = self.idx.address_flow_stats(
            normalize_filters(), page_size=3
        )
        self.assertIsNotNone(page["next_cursor"])
        last = self.idx.address_flow_stats(
            normalize_filters(), page_size=3,
            cursor=page["next_cursor"],
        )
        self.assertEqual(len(last["groups"]), 1)
        self.assertIsNone(last["next_cursor"])

    def test_no_match(self):
        result = self.idx.address_flow_stats(normalize_filters(method="nope"))
        self.assertEqual(result, {
            "groups": [],
            "total_groups": 0,
            "next_cursor": None,
        })

    def test_filters_intersect_before_grouping(self):
        result = self.idx.address_flow_stats(
            normalize_filters(
                start_time=15, end_time=30, method=["transfer"]
            )
        )
        # 仅 b：bob→alice 30 命中（a 在窗外、c 方法不符、d 方法不符）
        groups = {g["address"]: g for g in result["groups"]}
        self.assertEqual(set(groups), {"alice", "bob"})
        self.assertEqual(groups["bob"]["sent_amount"], "30")
        self.assertEqual(groups["alice"]["received_amount"], "30")
        self.assertEqual(result["total_groups"], 2)

    def test_address_filter(self):
        result = self.idx.address_flow_stats(
            normalize_filters(address="alice")
        )
        # alice 参与：a(alice→bob 100)、b(bob→alice 30)、d(alice→carol 15)
        groups = {g["address"]: g for g in result["groups"]}
        self.assertEqual(set(groups), {"alice", "bob", "carol"})
        self.assertEqual(groups["alice"]["sent_amount"], "115")
        self.assertEqual(groups["alice"]["received_amount"], "30")
        self.assertEqual(groups["alice"]["net_amount"], "-85")

    def test_amount_and_block_filters(self):
        result = self.idx.address_flow_stats(
            normalize_filters(min_amount="100", min_block=3)
        )
        # 仅 c：carol→dave 200（a 区块 1、b/d 金额不足）
        groups = {g["address"]: g for g in result["groups"]}
        self.assertEqual(set(groups), {"carol", "dave"})
        self.assertEqual(groups["carol"]["sent_amount"], "200")
        self.assertEqual(groups["dave"]["received_amount"], "200")

    def test_time_window_inclusive_both_ends(self):
        records = [
            rec("t1", 1, 100, "a", "b", "m", "1"),
            rec("t2", 2, 200, "a", "b", "m", "1"),
            rec("t3", 3, 300, "a", "b", "m", "1"),
        ]
        idx = TxIndexer(records)
        result = idx.address_flow_stats(
            normalize_filters(start_time=100, end_time=300)
        )
        self.assertEqual(result["total_groups"], 2)
        edge = idx.address_flow_stats(
            normalize_filters(start_time=200, end_time=200)
        )
        groups = {g["address"]: g for g in edge["groups"]}
        self.assertEqual(groups["a"]["sent_amount"], "1")
        self.assertEqual(groups["b"]["received_amount"], "1")

    def test_page_size_bounds(self):
        with self.assertRaises(InvalidPageSizeError):
            self.idx.address_flow_stats(normalize_filters(), page_size=0)
        with self.assertRaises(InvalidPageSizeError):
            self.idx.address_flow_stats(
                normalize_filters(), page_size=1001
            )
        with self.assertRaises(InvalidPageSizeError):
            self.idx.address_flow_stats(
                normalize_filters(), page_size="10"
            )
        with self.assertRaises(InvalidPageSizeError):
            self.idx.address_flow_stats(
                normalize_filters(), page_size=True
            )

    def test_default_page_size_is_100(self):
        records = [
            rec("h%d" % i, i, i, "a%d" % i, "b%d" % i, "m", "1")
            for i in range(150)
        ]
        page = TxIndexer(records).address_flow_stats(normalize_filters())
        self.assertEqual(len(page["groups"]), 100)
        self.assertIsNotNone(page["next_cursor"])
        self.assertEqual(page["total_groups"], 300)

    def test_cursor_filter_mismatch(self):
        cursor = self.idx.address_flow_stats(
            normalize_filters(), page_size=1
        )["next_cursor"]
        with self.assertRaises(InvalidCursorError):
            self.idx.address_flow_stats(
                normalize_filters(method="mint"), page_size=1,
                cursor=cursor,
            )

    def test_cursor_leading_zero_equivalent_amount(self):
        page1 = self.idx.address_flow_stats(
            normalize_filters(min_amount="100"), page_size=1
        )
        cursor = page1["next_cursor"]
        page2 = self.idx.address_flow_stats(
            normalize_filters(min_amount="00100"), page_size=1,
            cursor=cursor,
        )
        # 金额边界按数值等价绑定：只改前导零可续页，
        # min_amount=100 命中 a(100)、c(200)，首页为 dave、续页为 bob
        self.assertEqual(
            [g["address"] for g in page2["groups"]], ["bob"]
        )

    def test_cursor_cross_command_rejected(self):
        from tx_indexer.cursor import (
            decode_address_flow_stats_cursor,
            decode_address_stats_cursor,
            encode_address_flow_stats_cursor,
            encode_address_stats_cursor,
        )

        filters = normalize_filters()
        flow_cursor = encode_address_flow_stats_cursor(
            filters, 10, 5, 15, 1, "a"
        )
        address_cursor = encode_address_stats_cursor(
            filters, 15, 1, 1, 0, "a"
        )
        with self.assertRaises(InvalidCursorError):
            decode_address_stats_cursor(flow_cursor, filters)
        with self.assertRaises(InvalidCursorError):
            decode_address_flow_stats_cursor(address_cursor, filters)

    def test_cursor_replay_returns_same_page(self):
        page1 = self.idx.address_flow_stats(
            normalize_filters(), page_size=2
        )
        again = self.idx.address_flow_stats(
            normalize_filters(), page_size=2,
            cursor=page1["next_cursor"],
        )
        # 重复使用游标从同一 marker 续页：结果一致
        once_more = self.idx.address_flow_stats(
            normalize_filters(), page_size=2,
            cursor=page1["next_cursor"],
        )
        self.assertEqual(
            [g["address"] for g in again["groups"]],
            [g["address"] for g in once_more["groups"]],
        )

    def test_cursor_garbage(self):
        for bad in ("", "not-base64!!!", "bm9wZQ", "%%%"):
            with self.assertRaises(InvalidCursorError):
                self.idx.address_flow_stats(
                    normalize_filters(), cursor=bad
                )

    def test_cursor_tampered_payload_rejected(self):
        import base64
        import json

        cursor = self.idx.address_flow_stats(
            normalize_filters(), page_size=1
        )["next_cursor"]
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode(cursor + padding).decode("utf-8")
        )
        payload["after"] = "not-an-int"  # 篡改位置信息为非法类型
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        tampered = base64.urlsafe_b64encode(raw.encode("utf-8")).rstrip(
            b"="
        ).decode("ascii")
        with self.assertRaises(InvalidCursorError):
            self.idx.address_flow_stats(
                normalize_filters(), page_size=1, cursor=tampered
            )

    def test_big_amounts_exact_decimal_no_float(self):
        big = "123456789012345678901234567890"
        bigger = "123456789012345678901234567891"
        idx = TxIndexer([
            rec("x", 1, 1, "a", "b", "m", big),
            rec("y", 2, 2, "b", "a", "m", bigger),
        ])
        groups = {
            g["address"]: g
            for g in idx.address_flow_stats(normalize_filters())["groups"]
        }
        # a：sent big、received bigger → net = 1
        self.assertEqual(groups["a"]["sent_amount"], big)
        self.assertEqual(groups["a"]["received_amount"], bigger)
        self.assertEqual(groups["a"]["net_amount"], "1")
        self.assertEqual(groups["b"]["net_amount"], "-1")


if __name__ == "__main__":
    unittest.main()
