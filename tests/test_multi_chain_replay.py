"""MultiChainReplayManager 按链隔离的提交、水位、查询与统计测试。"""

import threading
import unittest

from tx_indexer import MultiChainReplayManager
from tx_indexer.engine import normalize_filters
from tx_indexer.errors import (
    InvalidCursorError,
    InvalidFilterError,
    InvalidPageSizeError,
    InvalidTimeRangeError,
    SourceUnavailableError,
    TransactionConflictError,
)

try:  # 以 tests 包方式运行
    from .test_replay import block, make_fetcher, tx, tx_for
except ImportError:  # unittest discover -s tests 时按顶层模块加载
    from test_replay import block, make_fetcher, tx, tx_for


def hashes(result):
    return [t["tx_hash"] for t in result["transactions"]]


def heights(result):
    return [t["block_number"] for t in result["transactions"]]


class ConstructionAndSubmitValidationTest(unittest.TestCase):
    def test_fetch_blocks_must_be_callable(self):
        with self.assertRaises(ValueError):
            MultiChainReplayManager(42)

    def test_no_fetcher_anywhere_raises_value_error(self):
        manager = MultiChainReplayManager()
        with self.assertRaises(ValueError):
            manager.submit("c", 0, 1, 10)

    def test_constructor_fetcher_used(self):
        manager = MultiChainReplayManager(make_fetcher())
        result = manager.submit("c", 0, 2, 10)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["committed_end_block"], 2)

    def test_submit_fetch_blocks_overrides_constructor(self):
        manager = MultiChainReplayManager()
        result = manager.submit("c", 0, 1, 10, fetch_blocks=make_fetcher())
        self.assertEqual(result["committed_count"], 2)

    def test_submit_override_does_not_replace_default_for_other_chains(self):
        manager = MultiChainReplayManager(make_fetcher())
        # chain-b 覆盖为另一份数据源；chain-a 仍走构造函数
        special = {
            h: [tx("x-%d" % h, h, timestamp=1000 + h,
                   frm="f", to="t", method="m", amount="1")]
            for h in range(0, 2)
        }
        manager.submit("chain-a", 0, 2, 10)
        manager.submit("chain-b", 0, 1, 10,
                       fetch_blocks=make_fetcher(special))
        manager.submit("chain-c", 0, 1, 10)
        self.assertEqual(
            hashes(manager.query("chain-a", normalize_filters())),
            ["h-0-0", "h-1-0", "h-2-0"],
        )
        self.assertEqual(
            hashes(manager.query("chain-b", normalize_filters())),
            ["x-0", "x-1"],
        )
        self.assertEqual(
            hashes(manager.query("chain-c", normalize_filters())),
            ["h-0-0", "h-1-0"],
        )

    def test_blank_chain_id_raises_value_error(self):
        manager = MultiChainReplayManager(make_fetcher())
        for bad in ("", "   ", None, 7):
            with self.assertRaises(ValueError):
                manager.submit(bad, 0, 1, 10)

    def test_invalid_range_and_batch_size_raise_value_error(self):
        manager = MultiChainReplayManager(make_fetcher())
        with self.assertRaises(ValueError):
            manager.submit("c", 5, 4, 10)
        with self.assertRaises(ValueError):
            manager.submit("c", -1, 4, 10)
        for bad in (0, -1, 1001, 1.5, True, "10"):
            with self.assertRaises(ValueError):
                manager.submit("c", 0, 4, bad)

    def test_validation_failure_does_not_create_chain(self):
        manager = MultiChainReplayManager(make_fetcher())
        with self.assertRaises(ValueError):
            manager.submit("c", 9, 4, 10)
        with self.assertRaises(ValueError):
            manager.submit("   ", 0, 4, 10)
        self.assertIsNone(manager.status("c")["committed_end_block"])

    def test_non_callable_submit_fetcher_raises_value_error(self):
        manager = MultiChainReplayManager(make_fetcher())
        with self.assertRaises(ValueError):
            manager.submit("c", 0, 1, 10, fetch_blocks="not-callable")


class SubmitIsolationTest(unittest.TestCase):
    def setUp(self):
        self.manager = MultiChainReplayManager(make_fetcher())

    def test_result_fields_match_replay_manager(self):
        keys = {
            "status", "chain_id", "range_start", "range_end",
            "committed_start_block", "committed_end_block", "next_block",
            "processed_batch_count", "committed_count", "skipped_count",
            "last_batch_committed_at",
        }
        result = self.manager.submit("chain-a", 0, 5, 2)
        self.assertEqual(set(result.keys()), keys)
        self.assertEqual(result["range_start"], 0)
        self.assertEqual(result["range_end"], 5)
        self.assertEqual(result["committed_start_block"], 0)
        self.assertEqual(result["committed_end_block"], 5)
        self.assertEqual(result["next_block"], 6)
        self.assertEqual(result["processed_batch_count"], 3)
        self.assertEqual(result["committed_count"], 6)
        self.assertEqual(result["skipped_count"], 0)
        self.assertIsInstance(result["last_batch_committed_at"], float)

    def test_independent_watermarks(self):
        self.manager.submit("chain-a", 0, 4, 2)
        self.manager.submit("chain-b", 10, 11, 2)
        a = self.manager.status("chain-a")
        b = self.manager.status("chain-b")
        self.assertEqual(a["committed_start_block"], 0)
        self.assertEqual(a["committed_end_block"], 4)
        self.assertEqual(a["next_block"], 5)
        self.assertEqual(b["committed_start_block"], 10)
        self.assertEqual(b["committed_end_block"], 11)
        self.assertEqual(b["next_block"], 12)
        # A 续提交必须连续；B 的高起点不影响 A 的缺口校验
        with self.assertRaises(ValueError):
            self.manager.submit("chain-a", 9, 9, 1)

    def test_same_hash_different_chains_are_distinct_transactions(self):
        # 两条链各自的区块 0 都含完全相同的 tx_hash 与内容
        shared = tx_for(0)
        manager = MultiChainReplayManager(make_fetcher({0: [shared]}))
        r1 = manager.submit("chain-a", 0, 0, 1)
        r2 = manager.submit("chain-b", 0, 0, 1)
        self.assertEqual(r1["committed_count"], 1)
        self.assertEqual(r1["skipped_count"], 0)
        # 跨链同哈希既不判重也不冲突：各自新增一笔
        self.assertEqual(r2["committed_count"], 1)
        self.assertEqual(r2["skipped_count"], 0)
        self.assertEqual(
            manager.query("chain-a", normalize_filters())["total"], 1
        )
        self.assertEqual(
            manager.query("chain-b", normalize_filters())["total"], 1
        )

    def test_same_hash_different_content_across_chains_is_not_conflict(self):
        # 同哈希在 B 链声明不同区块/时间/地址/方法：跨链不构成冲突
        on_a = tx("dup", 0, timestamp=100, frm="alice", to="bob",
                  method="m0", amount="1")
        on_b = tx("dup", 5, timestamp=999, frm="carol", to="dave",
                  method="m9", amount="2")
        manager = MultiChainReplayManager(
            make_fetcher({0: [on_a], 5: [on_b]})
        )
        manager.submit("chain-a", 0, 0, 1)
        manager.submit("chain-b", 5, 5, 1)
        rec_a = manager.query("chain-a", normalize_filters())["transactions"][0]
        rec_b = manager.query("chain-b", normalize_filters())["transactions"][0]
        self.assertEqual(rec_a["block_number"], 0)
        self.assertEqual(rec_a["amount"], "1")
        self.assertEqual(rec_b["block_number"], 5)
        self.assertEqual(rec_b["from_address"], "carol")
        self.assertEqual(rec_b["amount"], "2")

    def test_same_chain_first_write_wins_and_conflict_still_raises(self):
        first = tx_for(0)
        changed_amount = dict(first, amount="999")
        manager = MultiChainReplayManager(
            make_fetcher({0: [first, changed_amount]})
        )
        result = manager.submit("c", 0, 0, 1)
        self.assertEqual(result["committed_count"], 1)
        self.assertEqual(result["skipped_count"], 1)
        record = manager.query("c", normalize_filters())["transactions"][0]
        self.assertEqual(record["amount"], "10")

        # 同链同哈希不同身份仍抛 TransactionConflictError
        conflicting = dict(tx_for(0), block_number=1)
        with self.assertRaises(TransactionConflictError):
            manager.submit(
                "c", 1, 1, 1,
                fetch_blocks=make_fetcher({1: [conflicting]}),
            )
        # 但同样的冲突内容在另一条链上是全新交易
        other = MultiChainReplayManager(
            make_fetcher({1: [conflicting]})
        )
        other.submit("other", 1, 1, 1)
        self.assertEqual(
            other.query("other", normalize_filters())["total"], 1
        )

    def test_chain_block_content_independent(self):
        # 相同高度区间在两条链上的区块内容完全不同
        a_data = {h: [tx("a-%d" % h, h, timestamp=h, frm="af", to="at",
                         method="am", amount="1")] for h in range(3)}
        b_data = {h: [tx("b-%d" % h, h, timestamp=100 + h, frm="bf",
                         to="bt", method="bm", amount="2")]
                  for h in range(3)}
        manager = MultiChainReplayManager(make_fetcher(a_data))
        manager.submit("chain-a", 0, 2, 10)
        manager.submit("chain-b", 0, 2, 10,
                       fetch_blocks=make_fetcher(b_data))
        self.assertEqual(
            hashes(manager.query("chain-a", normalize_filters())),
            ["a-0", "a-1", "a-2"],
        )
        self.assertEqual(
            hashes(manager.query("chain-b", normalize_filters())),
            ["b-0", "b-1", "b-2"],
        )

    def test_replay_skip_is_per_chain(self):
        shared = tx_for(0)
        manager = MultiChainReplayManager(make_fetcher({0: [shared]}))
        manager.submit("chain-a", 0, 0, 1)
        # 同链重放：幂等，不重新计数
        again = manager.submit("chain-a", 0, 0, 1)
        self.assertEqual(again["processed_batch_count"], 0)
        self.assertEqual(again["committed_count"], 0)
        self.assertEqual(again["skipped_count"], 0)
        # 另一链首次提交同范围同哈希：按新交易处理
        first_b = manager.submit("chain-b", 0, 0, 1)
        self.assertEqual(first_b["committed_count"], 1)

    def test_source_unavailable_continuation_is_per_chain(self):
        manager = MultiChainReplayManager(make_fetcher(fail_heights={6}))
        with self.assertRaises(SourceUnavailableError):
            manager.submit("chain-a", 0, 9, 3)
        # A 水位停在 5；B 不受影响，独立完整提交
        manager.submit("chain-b", 0, 9, 3, fetch_blocks=make_fetcher())
        self.assertEqual(
            manager.status("chain-a")["committed_end_block"], 5
        )
        self.assertEqual(
            manager.status("chain-b")["committed_end_block"], 9
        )
        # A 原样续传未完成范围
        result = manager.submit(
            "chain-a", 6, 9, 3, fetch_blocks=make_fetcher()
        )
        self.assertEqual(result["committed_end_block"], 9)
        self.assertEqual(result["committed_count"], 4)

    def test_batching_ascending(self):
        log = []
        manager = MultiChainReplayManager(make_fetcher(log=log))
        result = manager.submit("c", 0, 9, 3)
        self.assertEqual(log, [(0, 2), (3, 5), (6, 8), (9, 9)])
        self.assertEqual(result["processed_batch_count"], 4)


class StatusTest(unittest.TestCase):
    def setUp(self):
        self.manager = MultiChainReplayManager(make_fetcher())

    def test_unknown_chain_not_started(self):
        status = self.manager.status("never")
        self.assertEqual(status, {
            "chain_id": "never",
            "committed_start_block": None,
            "committed_end_block": None,
            "next_block": None,
            "last_batch_committed_at": None,
        })

    def test_unknown_chain_uses_passed_start_block(self):
        status = self.manager.status("never", start_block=42)
        self.assertIsNone(status["committed_start_block"])
        self.assertIsNone(status["committed_end_block"])
        self.assertEqual(status["next_block"], 42)

    def test_status_is_read_only(self):
        self.manager.status("never", start_block=3)
        self.manager.status("never", start_block=3)
        self.assertIsNone(self.manager.status("never")["committed_end_block"])
        result = self.manager.submit("never", 3, 3, 1)
        self.assertEqual(result["committed_start_block"], 3)

    def test_blank_chain_id_raises_value_error(self):
        for bad in ("", "  \t", None, 1):
            with self.assertRaises(ValueError):
                self.manager.status(bad)

    def test_negative_start_block_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.manager.status("c", start_block=-1)

    def test_started_chain_returns_watermark(self):
        self.manager.submit("c", 7, 9, 2)
        status = self.manager.status("c")
        self.assertEqual(status["committed_start_block"], 7)
        self.assertEqual(status["committed_end_block"], 9)
        self.assertEqual(status["next_block"], 10)
        self.assertIsInstance(status["last_batch_committed_at"], float)


class QueryTest(unittest.TestCase):
    def setUp(self):
        txs = {
            0: [
                tx("a1", 0, timestamp=10, frm="alice", to="bob",
                   method="transfer", amount="5"),
                tx("a2", 0, timestamp=20, frm="bob", to="alice",
                   method="approve", amount="7"),
            ],
            1: [
                tx("a3", 1, timestamp=30, frm="alice", to="carol",
                   method="transfer", amount="100"),
            ],
        }
        self.manager = MultiChainReplayManager(make_fetcher(txs))
        self.manager.submit("chain-a", 0, 1, 5)
        # chain-b 提交同样的哈希，但金额不同以验证按链独立
        b_txs = {
            0: [
                tx("a1", 0, timestamp=10, frm="alice", to="bob",
                   method="transfer", amount="500"),
            ],
        }
        self.manager.submit("chain-b", 0, 0, 5,
                            fetch_blocks=make_fetcher(b_txs))

    def test_unknown_chain_returns_empty_page(self):
        result = self.manager.query("ghost", normalize_filters())
        self.assertEqual(result, {
            "transactions": [],
            "total": 0,
            "next_cursor": None,
        })

    def test_blank_chain_id_raises_value_error(self):
        for bad in ("", "  ", None, 5):
            with self.assertRaises(ValueError):
                self.manager.query(bad, normalize_filters())

    def test_query_is_scoped_per_chain(self):
        result = self.manager.query("chain-a", normalize_filters())
        self.assertEqual(hashes(result), ["a1", "a2", "a3"])
        self.assertEqual(result["total"], 3)
        b = self.manager.query("chain-b", normalize_filters())
        self.assertEqual(hashes(b), ["a1"])
        # chain-b 的金额是它自己首次写入的内容
        self.assertEqual(b["transactions"][0]["amount"], "500")

    def test_filters_and_closed_time_window(self):
        alice = self.manager.query(
            "chain-a", normalize_filters(address="alice")
        )
        self.assertEqual(hashes(alice), ["a1", "a2", "a3"])
        transfer = self.manager.query(
            "chain-a", normalize_filters(method="transfer")
        )
        self.assertEqual(hashes(transfer), ["a1", "a3"])
        # 左闭右闭：15 <= ts <= 30
        window = self.manager.query(
            "chain-a", normalize_filters(start_time=15, end_time=30)
        )
        self.assertEqual(hashes(window), ["a2", "a3"])
        boundary = self.manager.query(
            "chain-a", normalize_filters(start_time=10, end_time=10)
        )
        self.assertEqual(hashes(boundary), ["a1"])

    def test_invalid_filter_raises(self):
        with self.assertRaises(InvalidFilterError):
            self.manager.query("chain-a", normalize_filters(address=" "))
        with self.assertRaises(InvalidFilterError):
            self.manager.query(
                "chain-a",
                normalize_filters(address="alice", from_address="bob"),
            )

    def test_invalid_time_range_raises(self):
        with self.assertRaises(InvalidTimeRangeError):
            self.manager.query(
                "chain-a", normalize_filters(start_time=30, end_time=10)
            )

    def test_block_filter(self):
        result = self.manager.query(
            "chain-a", normalize_filters(min_block=1)
        )
        self.assertEqual(hashes(result), ["a3"])
        self.assertEqual(result["total"], 1)
        result = self.manager.query(
            "chain-a", normalize_filters(max_block=0)
        )
        self.assertEqual(hashes(result), ["a1", "a2"])
        result = self.manager.query(
            "chain-a", normalize_filters(min_block=0, max_block=1)
        )
        self.assertEqual(hashes(result), ["a1", "a2", "a3"])
        # stats 同样按区块交集计算
        stats = self.manager.stats(
            "chain-a", normalize_filters(min_block=1)
        )
        self.assertEqual(stats["total_count"], 1)
        self.assertEqual(stats["total_amount"], "100")

    def test_block_cursor_bound_to_bounds(self):
        page = self.manager.query(
            "chain-a", normalize_filters(min_block=0), page_size=1
        )
        cursor = page["next_cursor"]
        self.assertIsNotNone(cursor)
        # 相同边界可续页
        again = self.manager.query(
            "chain-a", normalize_filters(min_block=0), page_size=1,
            cursor=cursor,
        )
        self.assertEqual(hashes(again), ["a2"])
        # 改变/删除边界 → invalid_cursor
        for f in (normalize_filters(min_block=1), normalize_filters()):
            with self.assertRaises(InvalidCursorError):
                self.manager.query(
                    "chain-a", f, page_size=1, cursor=cursor
                )

    def test_invalid_page_size_raises(self):
        for bad in (0, -1, 1001, 1.5, True, "10"):
            with self.assertRaises(InvalidPageSizeError):
                self.manager.query(
                    "chain-a", normalize_filters(), page_size=bad
                )

    def test_unknown_chain_still_validates_page_size(self):
        with self.assertRaises(InvalidPageSizeError):
            self.manager.query("ghost", normalize_filters(), page_size=0)

    def test_paging_within_chain(self):
        manager = MultiChainReplayManager(make_fetcher())
        manager.submit("c", 0, 4, 10)
        first = manager.query("c", normalize_filters(), page_size=2)
        self.assertEqual(len(first["transactions"]), 2)
        self.assertEqual(first["total"], 5)
        second = manager.query(
            "c", normalize_filters(), page_size=2,
            cursor=first["next_cursor"],
        )
        self.assertEqual(heights(second), [2, 3])
        third = manager.query(
            "c", normalize_filters(), page_size=2,
            cursor=second["next_cursor"],
        )
        self.assertEqual(heights(third), [4])
        self.assertIsNone(third["next_cursor"])

    def test_cursor_rejected_across_chains(self):
        first_a = self.manager.query(
            "chain-a", normalize_filters(), page_size=1
        )
        cursor = first_a["next_cursor"]
        with self.assertRaises(InvalidCursorError):
            self.manager.query(
                "chain-b", normalize_filters(), cursor=cursor
            )
        with self.assertRaises(InvalidCursorError):
            self.manager.query(
                "ghost", normalize_filters(), cursor=cursor
            )

    def test_cursor_rejected_when_filters_change(self):
        first = self.manager.query(
            "chain-a", normalize_filters(), page_size=1
        )
        with self.assertRaises(InvalidCursorError):
            self.manager.query(
                "chain-a", normalize_filters(method="transfer"),
                cursor=first["next_cursor"],
            )

    def test_cursor_rejected_when_equivalent_filters_then_change(self):
        # 等价筛选（集合写法不同）可续页
        first = self.manager.query(
            "chain-a", normalize_filters(method=("transfer", "approve")),
            page_size=2,
        )
        cursor = first["next_cursor"]
        second = self.manager.query(
            "chain-a",
            normalize_filters(method=["approve", "transfer", "transfer"]),
            page_size=2, cursor=cursor,
        )
        self.assertEqual(hashes(second), ["a3"])
        # 不等价则拒绝
        with self.assertRaises(InvalidCursorError):
            self.manager.query(
                "chain-a", normalize_filters(method="approve"),
                cursor=cursor,
            )

    def test_plain_query_cursor_rejected_in_multichain(self):
        # 单索引 TxIndexer 签发的 query 游标不能在多链入口续用
        from tx_indexer.cursor import encode_cursor

        token = encode_cursor(normalize_filters(), 0, "a1")
        with self.assertRaises(InvalidCursorError):
            self.manager.query(
                "chain-a", normalize_filters(), cursor=token
            )

    def test_multichain_cursor_rejected_by_plain_indexer(self):
        # 反之亦然：作用域不匹配
        from tx_indexer.cursor import encode_multichain_query_cursor

        token = encode_multichain_query_cursor(
            "chain-a", normalize_filters(), 0, "a1"
        )
        indexer = self.manager._get_manager("chain-a").indexer
        with self.assertRaises(InvalidCursorError):
            indexer.query(normalize_filters(), cursor=token)

    def test_malformed_cursor_rejected(self):
        for bad in ("", "not-base64!!!", "aaaa", None):
            if bad is None:
                continue
            with self.assertRaises(InvalidCursorError):
                self.manager.query(
                    "chain-a", normalize_filters(), cursor=bad
                )

    def test_query_after_further_submit_keeps_keyset_sane(self):
        # 游标 marker 位于现存记录之间时，新提交后续页不跳过、不重复
        manager = MultiChainReplayManager(make_fetcher())
        manager.submit("c", 0, 3, 10)
        first = manager.query("c", normalize_filters(), page_size=2)
        manager.submit("c", 4, 5, 10)
        second = manager.query(
            "c", normalize_filters(), page_size=10,
            cursor=first["next_cursor"],
        )
        self.assertEqual(heights(second), [2, 3, 4, 5])
        self.assertEqual(second["total"], 6)

    def test_amount_filter_scoped_per_chain(self):
        # chain-a: 5/7/100，chain-b: 500
        result = self.manager.query(
            "chain-a", normalize_filters(min_amount="7", max_amount="100")
        )
        self.assertEqual(hashes(result), ["a2", "a3"])
        only_high = self.manager.query(
            "chain-a", normalize_filters(min_amount="100")
        )
        self.assertEqual(hashes(only_high), ["a3"])
        # 金额筛选同样按链隔离：chain-b 的 500 不受影响
        b = self.manager.query(
            "chain-b", normalize_filters(min_amount="500")
        )
        self.assertEqual(hashes(b), ["a1"])
        b_none = self.manager.query(
            "chain-b", normalize_filters(max_amount="499")
        )
        self.assertEqual(b_none["total"], 0)
        self.assertIsNone(b_none["next_cursor"])

    def test_amount_filter_unknown_chain_empty(self):
        result = self.manager.query(
            "ghost", normalize_filters(min_amount="1")
        )
        self.assertEqual(result, {
            "transactions": [],
            "total": 0,
            "next_cursor": None,
        })

    def test_invalid_amount_filter_raises(self):
        from tx_indexer.errors import (
            InvalidAmountFilterError,
            InvalidAmountRangeError,
        )
        for bad in ("", "-1", "1.0", 5, True):
            with self.assertRaises(InvalidAmountFilterError):
                self.manager.query(
                    "chain-a", normalize_filters(min_amount=bad)
                )
        with self.assertRaises(InvalidAmountRangeError):
            self.manager.query(
                "chain-a",
                normalize_filters(min_amount="8", max_amount="7"),
            )

    def test_cursor_bound_to_amount_bounds(self):
        first = self.manager.query(
            "chain-a", normalize_filters(min_amount="7"), page_size=1
        )
        self.assertIsNotNone(first["next_cursor"])
        # 前导零等价可续页
        second = self.manager.query(
            "chain-a", normalize_filters(min_amount="007"), page_size=1,
            cursor=first["next_cursor"],
        )
        self.assertEqual(hashes(second), ["a3"])
        # 改变/删除边界 → 拒绝
        for changed in (
            normalize_filters(min_amount="5"),
            normalize_filters(min_amount="7", max_amount="100"),
            normalize_filters(),
        ):
            with self.assertRaises(InvalidCursorError):
                self.manager.query(
                    "chain-a", changed, page_size=1,
                    cursor=first["next_cursor"],
                )

    def test_amount_cursor_bound_to_chain(self):
        # 金额边界与 chain_id 共同绑定多链游标
        first = self.manager.query(
            "chain-a", normalize_filters(min_amount="5"), page_size=1
        )
        with self.assertRaises(InvalidCursorError):
            self.manager.query(
                "chain-b", normalize_filters(min_amount="5"),
                cursor=first["next_cursor"],
            )
        # 多链金额游标仍不能与单索引 query 游标互用
        from tx_indexer.cursor import encode_cursor

        token = encode_cursor(normalize_filters(min_amount="5"), 0, "a1")
        with self.assertRaises(InvalidCursorError):
            self.manager.query(
                "chain-a", normalize_filters(min_amount="5"),
                cursor=token,
            )


class StatsTest(unittest.TestCase):
    def setUp(self):
        txs = {
            0: [
                tx("a1", 0, timestamp=10, frm="alice", to="bob",
                   method="transfer", amount="5"),
                tx("a2", 0, timestamp=20, frm="bob", to="alice",
                   method="approve", amount="7"),
            ],
            1: [
                tx("a3", 1, timestamp=30, frm="alice", to="carol",
                   method="transfer", amount="100"),
            ],
        }
        self.manager = MultiChainReplayManager(make_fetcher(txs))
        self.manager.submit("chain-a", 0, 1, 5)

    def test_unknown_chain_returns_no_matches(self):
        self.assertEqual(
            self.manager.stats("ghost", normalize_filters()),
            {
                "total_count": 0,
                "total_amount": "0",
                "min_amount": None,
                "max_amount": None,
                "avg_amount": None,
            },
        )

    def test_blank_chain_id_raises_value_error(self):
        for bad in ("", "  ", None, 5):
            with self.assertRaises(ValueError):
                self.manager.stats(bad, normalize_filters())

    def test_stats_scoped_per_chain(self):
        stats = self.manager.stats("chain-a", normalize_filters())
        self.assertEqual(stats["total_count"], 3)
        self.assertEqual(stats["total_amount"], "112")
        self.assertEqual(stats["min_amount"], "5")
        self.assertEqual(stats["max_amount"], "100")
        self.assertEqual(stats["avg_amount"], "37")
        # 未知链与已提交链互不影响
        ghost = self.manager.stats("ghost", normalize_filters())
        self.assertEqual(ghost["total_count"], 0)

    def test_stats_filters_and_closed_window(self):
        transfer = self.manager.stats(
            "chain-a", normalize_filters(method="transfer")
        )
        self.assertEqual(transfer["total_count"], 2)
        self.assertEqual(transfer["total_amount"], "105")
        window = self.manager.stats(
            "chain-a", normalize_filters(start_time=15, end_time=30)
        )
        self.assertEqual(window["total_count"], 2)
        self.assertEqual(window["total_amount"], "107")

    def test_stats_invalid_filters_raise(self):
        with self.assertRaises(InvalidFilterError):
            self.manager.stats("chain-a", normalize_filters(method=""))
        with self.assertRaises(InvalidTimeRangeError):
            self.manager.stats(
                "chain-a", normalize_filters(start_time=9, end_time=1)
            )

    def test_stats_amount_filter(self):
        # chain-a: 5/7/100，闭区间 [7, 100]
        stats = self.manager.stats(
            "chain-a", normalize_filters(min_amount="7", max_amount="100")
        )
        self.assertEqual(stats["total_count"], 2)
        self.assertEqual(stats["total_amount"], "107")
        self.assertEqual(stats["min_amount"], "7")
        self.assertEqual(stats["max_amount"], "100")
        self.assertEqual(stats["avg_amount"], "53")
        # 未开始的链继续返回无匹配统计
        ghost = self.manager.stats(
            "ghost", normalize_filters(min_amount="1")
        )
        self.assertEqual(ghost, {
            "total_count": 0,
            "total_amount": "0",
            "min_amount": None,
            "max_amount": None,
            "avg_amount": None,
        })

    def test_stats_invalid_amount_raises(self):
        from tx_indexer.errors import (
            InvalidAmountFilterError,
            InvalidAmountRangeError,
        )
        with self.assertRaises(InvalidAmountFilterError):
            self.manager.stats(
                "chain-a", normalize_filters(min_amount="1.0")
            )
        with self.assertRaises(InvalidAmountRangeError):
            self.manager.stats(
                "chain-a",
                normalize_filters(min_amount="101", max_amount="100"),
            )


class CrossChainConcurrencyTest(unittest.TestCase):
    def test_different_chains_run_independently(self):
        gate_a = threading.Event()

        def fetch_a(start, end):
            gate_a.wait(5)
            return [block(h, [tx_for(h)]) for h in range(start, end + 1)]

        manager = MultiChainReplayManager(fetch_a)
        done_b = threading.Event()

        def run_a():
            manager.submit("chain-a", 0, 1, 2)

        def run_b():
            manager.submit("chain-b", 10, 11, 2,
                           fetch_blocks=make_fetcher())
            done_b.set()

        t_a = threading.Thread(target=run_a)
        t_b = threading.Thread(target=run_b)
        t_a.start()
        threading.Event().wait(0.1)
        t_b.start()
        # 异链独立：B 不被挂起的 A 阻塞
        self.assertTrue(done_b.wait(2))
        self.assertIsNone(
            manager.status("chain-a")["committed_end_block"]
        )
        self.assertEqual(
            manager.status("chain-b")["committed_end_block"], 11
        )
        gate_a.set()
        t_a.join(5)
        t_b.join(5)
        self.assertEqual(
            manager.status("chain-a")["committed_end_block"], 1
        )

    def test_same_chain_concurrent_submits_serialize(self):
        gate = threading.Event()
        calls = []

        def fetch(start, end):
            calls.append((start, end))
            if start == 0:
                gate.wait(5)
            return [block(h, [tx_for(h)]) for h in range(start, end + 1)]

        manager = MultiChainReplayManager(fetch)
        errors = []

        def low():
            try:
                manager.submit("c", 0, 1, 1)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        def high():
            try:
                manager.submit("c", 2, 3, 1)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        t_low = threading.Thread(target=low)
        t_high = threading.Thread(target=high)
        t_low.start()
        while not calls:
            threading.Event().wait(0.001)
        t_high.start()
        threading.Event().wait(0.1)
        # 同链高范围等待：链内串行化沿用 ReplayManager
        self.assertIsNone(manager.status("c")["committed_end_block"])
        gate.set()
        t_low.join(5)
        t_high.join(5)
        self.assertFalse(errors)
        self.assertEqual(manager.status("c")["committed_end_block"], 3)
        self.assertEqual(
            heights(manager.query("c", normalize_filters(), page_size=10)),
            [0, 1, 2, 3],
        )

    def test_same_hash_submitted_concurrently_on_two_chains_both_commit(self):
        # 两个线程同时向不同链提交同一笔哈希：都应成功新增
        gate = threading.Event()
        shared = tx_for(0)

        def make_blocking_fetch():
            def fetch(start, end):
                gate.wait(5)
                return [block(0, [dict(shared)])]
            return fetch

        manager = MultiChainReplayManager(make_blocking_fetch())
        results = {}

        def run(chain):
            results[chain] = manager.submit(
                chain, 0, 0, 1, fetch_blocks=make_blocking_fetch()
            )

        threads = [
            threading.Thread(target=run, args=("chain-a",)),
            threading.Thread(target=run, args=("chain-b",)),
        ]
        for t in threads:
            t.start()
        threading.Event().wait(0.2)
        gate.set()
        for t in threads:
            t.join(5)
        self.assertEqual(results["chain-a"]["committed_count"], 1)
        self.assertEqual(results["chain-b"]["committed_count"], 1)
        self.assertEqual(
            manager.query("chain-a", normalize_filters())["total"], 1
        )
        self.assertEqual(
            manager.query("chain-b", normalize_filters())["total"], 1
        )


if __name__ == "__main__":
    unittest.main()
