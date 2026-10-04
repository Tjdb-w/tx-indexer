"""MultiChainReplayManager 的按链隔离测试。"""

import threading
import unittest

from tx_indexer import MultiChainReplayManager
from tx_indexer.cursor import (
    SCOPE_IMPORT,
    _encode_payload,
    encode_cursor,
)
from tx_indexer.engine import normalize_filters
from tx_indexer.errors import (
    InvalidCursorError,
    InvalidFilterError,
    InvalidPageSizeError,
    InvalidTimeRangeError,
    SourceUnavailableError,
    TransactionConflictError,
)
from tx_indexer.replay import MultiChainReplayManager as MCRM


def tx(tx_hash, block_number, timestamp=100, frm="alice", to="bob",
       method="transfer", amount="10", **extra):
    raw = {
        "tx_hash": tx_hash,
        "block_number": block_number,
        "timestamp": timestamp,
        "from_address": frm,
        "to_address": to,
        "method": method,
        "amount": amount,
    }
    raw.update(extra)
    return raw


def block(height, transactions=None):
    return {"block_number": height, "transactions": transactions or []}


def tx_for(height, index=0, **overrides):
    """每区块每笔交易的确定性样例：哈希、时间、双方、方法各不相同。"""
    data = tx(
        "h-%d-%d" % (height, index),
        height,
        timestamp=1000 + height,
        frm="from-%d" % height,
        to="to-%d" % height,
        method="m%d" % (height % 3),
        amount=str(10 + height),
    )
    data.update(overrides)
    return data


def make_fetcher(txs_by_height=None, fail_heights=None, log=None):
    txs_by_height = txs_by_height or {}
    fail_heights = fail_heights or set()

    def fetch(start, end):
        if log is not None:
            log.append((start, end))
        if any(h in fail_heights for h in range(start, end + 1)):
            raise SourceUnavailableError("区块暂时缺失", None)
        return [
            block(h, txs_by_height.get(h, [tx_for(h)]))
            for h in range(start, end + 1)
        ]

    return fetch


def query_heights(manager, chain_id, **filter_kwargs):
    page = manager.query(
        chain_id, normalize_filters(**filter_kwargs), page_size=1000
    )
    return [t["block_number"] for t in page["transactions"]]


class ConstructionAndValidationTest(unittest.TestCase):
    def test_exported_from_package(self):
        self.assertIs(MCRM, MultiChainReplayManager)

    def test_non_callable_fetch_blocks_rejected(self):
        with self.assertRaises(ValueError):
            MultiChainReplayManager("not-callable")

    def test_missing_fetcher_raises_value_error(self):
        with self.assertRaises(ValueError):
            MultiChainReplayManager().submit("c", 0, 1, 10)

    def test_submit_fetch_blocks_overrides_constructor(self):
        manager = MultiChainReplayManager()
        result = manager.submit(
            "c", 0, 2, 2, fetch_blocks=make_fetcher()
        )
        self.assertEqual(result["committed_end_block"], 2)
        self.assertEqual(result["committed_count"], 3)

    def test_constructor_fetcher_used_by_submit(self):
        manager = MultiChainReplayManager(make_fetcher())
        result = manager.submit("c", 0, 2, 2)
        self.assertEqual(result["status"], "ok")

    def test_blank_chain_id_rejected(self):
        manager = MultiChainReplayManager(make_fetcher())
        for bad in ("", "   ", None, 7):
            with self.assertRaises(ValueError):
                manager.submit(bad, 0, 1, 10)
            with self.assertRaises(ValueError):
                manager.status(bad)
            with self.assertRaises(ValueError):
                manager.query(bad, normalize_filters())
            with self.assertRaises(ValueError):
                manager.stats(bad, normalize_filters())

    def test_range_and_batch_size_validation_matches_replay(self):
        manager = MultiChainReplayManager(make_fetcher())
        with self.assertRaises(ValueError):
            manager.submit("c", 5, 4, 10)
        with self.assertRaises(ValueError):
            manager.submit("c", -1, 4, 10)
        for bad in (0, -1, 1001, 1.5, True, "10"):
            with self.assertRaises(ValueError):
                manager.submit("c", 0, 4, bad)

    def test_status_negative_start_block_rejected(self):
        manager = MultiChainReplayManager(make_fetcher())
        with self.assertRaises(ValueError):
            manager.status("c", start_block=-1)


class ChainIsolationTest(unittest.TestCase):
    def test_independent_watermarks(self):
        manager = MultiChainReplayManager(make_fetcher())
        manager.submit("chain-a", 0, 4, 2)
        manager.submit("chain-b", 10, 11, 2)
        self.assertEqual(
            manager.status("chain-a")["committed_end_block"], 4
        )
        self.assertEqual(
            manager.status("chain-b")["committed_start_block"], 10
        )
        self.assertEqual(manager.status("chain-b")["next_block"], 12)
        self.assertIsNone(manager.status("chain-c")["committed_end_block"])

    def test_same_hash_same_content_is_distinct_per_chain(self):
        shared = tx_for(0)
        manager = MultiChainReplayManager(make_fetcher({0: [shared]}))
        r1 = manager.submit("chain-a", 0, 0, 1)
        r2 = manager.submit("chain-b", 0, 0, 1)
        # 跨链不视为重复：两条链各新增一笔
        self.assertEqual(r1["committed_count"], 1)
        self.assertEqual(r1["skipped_count"], 0)
        self.assertEqual(r2["committed_count"], 1)
        self.assertEqual(r2["skipped_count"], 0)
        # 各链查询只观察到本链的一笔
        for chain_id in ("chain-a", "chain-b"):
            page = manager.query(chain_id, normalize_filters())
            self.assertEqual(page["total"], 1)
            self.assertEqual(
                page["transactions"][0]["tx_hash"], "h-0-0"
            )

    def test_same_hash_different_content_across_chains_no_conflict(self):
        version_a = tx(
            "same", 0, timestamp=100, frm="alice", to="bob",
            method="transfer", amount="10",
        )
        version_b = tx(
            "same", 0, timestamp=200, frm="carol", to="dave",
            method="approve", amount="99",
        )
        manager = MultiChainReplayManager()
        manager.submit(
            "chain-a", 0, 0, 1,
            fetch_blocks=make_fetcher({0: [version_a]}),
        )
        # 异链同哈希但标准化内容不同：不是冲突，正常提交
        manager.submit(
            "chain-b", 0, 0, 1,
            fetch_blocks=make_fetcher({0: [version_b]}),
        )
        got_a = manager.query("chain-a", normalize_filters())
        got_b = manager.query("chain-b", normalize_filters())
        self.assertEqual(got_a["transactions"][0]["timestamp"], 100)
        self.assertEqual(got_a["transactions"][0]["from_address"], "alice")
        self.assertEqual(got_b["transactions"][0]["timestamp"], 200)
        self.assertEqual(got_b["transactions"][0]["from_address"], "carol")
        # 按链筛选：A 链查 carol 无结果，B 链查 alice 无结果
        self.assertEqual(
            manager.query(
                "chain-a", normalize_filters(address="carol")
            )["total"],
            0,
        )
        self.assertEqual(
            manager.query(
                "chain-b", normalize_filters(address="alice")
            )["total"],
            0,
        )

    def test_block_contents_and_query_results_independent(self):
        manager = MultiChainReplayManager(make_fetcher())
        manager.submit("chain-a", 0, 4, 10)
        manager.submit("chain-b", 20, 23, 10)
        self.assertEqual(query_heights(manager, "chain-a"), [0, 1, 2, 3, 4])
        self.assertEqual(
            query_heights(manager, "chain-b"), [20, 21, 22, 23]
        )
        # 确定性样例在两链都有 h-2-0，但 A 链按区块 2 查、B 链按 22 查，
        # 各自只返回本链数据
        page_a = manager.query(
            "chain-a",
            normalize_filters(start_time=1002, end_time=1002),
        )
        page_b = manager.query(
            "chain-b",
            normalize_filters(start_time=1022, end_time=1022),
        )
        self.assertEqual(
            [t["tx_hash"] for t in page_a["transactions"]], ["h-2-0"]
        )
        self.assertEqual(
            [t["tx_hash"] for t in page_b["transactions"]], ["h-22-0"]
        )

    def test_same_chain_conflict_still_raises(self):
        conflicting = dict(tx_for(0), block_number=1)
        manager = MultiChainReplayManager(make_fetcher({0: [tx_for(0)]}))
        manager.submit("chain-a", 0, 0, 1)
        with self.assertRaises(TransactionConflictError):
            manager.submit(
                "chain-a", 1, 1, 1,
                fetch_blocks=make_fetcher({1: [conflicting]}),
            )
        # 冲突批次不提交，水位停在 0
        self.assertEqual(
            manager.status("chain-a")["committed_end_block"], 0
        )
        # 异链同哈希不同内容不受影响
        manager.submit(
            "chain-b", 1, 1, 1,
            fetch_blocks=make_fetcher({1: [conflicting]}),
        )
        self.assertEqual(
            manager.status("chain-b")["committed_end_block"], 1
        )

    def test_same_chain_first_write_not_overwritten(self):
        first = tx_for(2, amount="100")
        changed_amount = tx_for(2, amount="999")
        manager = MultiChainReplayManager(
            make_fetcher({2: [first, changed_amount]})
        )
        result = manager.submit("chain-a", 0, 2, 5)
        self.assertEqual(result["committed_count"], 3)
        self.assertEqual(result["skipped_count"], 1)
        page = manager.query("chain-a", normalize_filters())
        record = next(
            t for t in page["transactions"] if t["tx_hash"] == "h-2-0"
        )
        self.assertEqual(record["amount"], "100")

    def test_same_chain_replay_skips_after_other_chain_committed(self):
        manager = MultiChainReplayManager(make_fetcher())
        manager.submit("chain-a", 0, 2, 5)
        manager.submit("chain-b", 0, 2, 5)  # 同哈希，异链，独立写入
        result = manager.submit("chain-a", 0, 2, 5)
        self.assertEqual(result["processed_batch_count"], 0)
        self.assertEqual(result["committed_count"], 0)
        self.assertEqual(result["skipped_count"], 0)
        self.assertEqual(
            query_heights(manager, "chain-a"), [0, 1, 2]
        )

    def test_submit_result_fields_match_replay_manager(self):
        manager = MultiChainReplayManager(make_fetcher())
        result = manager.submit("chain-a", 0, 9, 3)
        self.assertEqual(
            set(result.keys()),
            {
                "status", "chain_id", "range_start", "range_end",
                "committed_start_block", "committed_end_block",
                "next_block", "processed_batch_count", "committed_count",
                "skipped_count", "last_batch_committed_at",
            },
        )
        self.assertEqual(result["processed_batch_count"], 4)
        self.assertEqual(result["next_block"], 10)

    def test_source_unavailable_resumes_per_chain(self):
        manager = MultiChainReplayManager(make_fetcher())
        manager.submit("chain-a", 0, 4, 2)
        with self.assertRaises(SourceUnavailableError):
            manager.submit(
                "chain-b", 0, 4, 2,
                fetch_blocks=make_fetcher(fail_heights={2}),
            )
        # B 链首批 [0,1] 已提交，[2,3] 未提交；A 链水位不受影响
        self.assertEqual(manager.status("chain-b")["committed_end_block"], 1)
        self.assertEqual(manager.status("chain-a")["committed_end_block"], 4)
        result = manager.submit(
            "chain-b", 0, 4, 2, fetch_blocks=make_fetcher()
        )
        self.assertEqual(result["committed_end_block"], 4)
        self.assertEqual(result["committed_count"], 3)

    def test_gap_rule_is_per_chain(self):
        manager = MultiChainReplayManager(make_fetcher())
        manager.submit("chain-a", 0, 2, 10)
        # B 链未提交过低区块，可从任意起点开始（不受 A 链水位约束）
        result = manager.submit("chain-b", 50, 51, 10)
        self.assertEqual(result["committed_start_block"], 50)
        # 但 B 链自身仍不允许越过缺口
        with self.assertRaises(ValueError):
            manager.submit("chain-b", 60, 61, 10)


class QueryAndStatsTest(unittest.TestCase):
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
        # B 链只有一笔时间/哈希都不同的交易
        self.manager.submit(
            "chain-b", 0, 0, 5,
            fetch_blocks=make_fetcher({
                0: [tx("b1", 0, timestamp=15, frm="zoe", to="alice",
                       method="transfer", amount="1000")],
            }),
        )

    def test_filters_time_window_and_ordering_per_chain(self):
        page = self.manager.query("chain-a", normalize_filters())
        self.assertEqual(
            [(t["block_number"], t["tx_hash"])
             for t in page["transactions"]],
            [(0, "a1"), (0, "a2"), (1, "a3")],
        )
        self.assertEqual(page["total"], 3)
        self.assertIsNone(page["next_cursor"])

        alice = self.manager.query(
            "chain-a", normalize_filters(address="alice")
        )
        self.assertEqual(
            [t["tx_hash"] for t in alice["transactions"]],
            ["a1", "a2", "a3"],
        )
        window = self.manager.query(
            "chain-a", normalize_filters(start_time=15, end_time=30)
        )
        self.assertEqual(
            [t["tx_hash"] for t in window["transactions"]], ["a2", "a3"]
        )
        # 左闭右闭：端点都命中
        edges = self.manager.query(
            "chain-a", normalize_filters(start_time=10, end_time=20)
        )
        self.assertEqual(
            [t["tx_hash"] for t in edges["transactions"]], ["a1", "a2"]
        )
        # B 链的 alice 交易与 A 链筛选结果相互独立
        b_alice = self.manager.query(
            "chain-b", normalize_filters(address="alice")
        )
        self.assertEqual(
            [t["tx_hash"] for t in b_alice["transactions"]], ["b1"]
        )

    def test_stats_per_chain(self):
        stats_a = self.manager.stats("chain-a", normalize_filters())
        self.assertEqual(stats_a["total_count"], 3)
        self.assertEqual(stats_a["total_amount"], "112")
        self.assertEqual(stats_a["min_amount"], "5")
        self.assertEqual(stats_a["max_amount"], "100")
        self.assertEqual(stats_a["avg_amount"], "37")
        stats_b = self.manager.stats("chain-b", normalize_filters())
        self.assertEqual(stats_b["total_count"], 1)
        self.assertEqual(stats_b["total_amount"], "1000")

    def test_stats_respect_filters_per_chain(self):
        stats = self.manager.stats(
            "chain-a", normalize_filters(method="transfer")
        )
        self.assertEqual(stats["total_count"], 2)
        self.assertEqual(stats["total_amount"], "105")

    def test_unknown_chain_query_is_empty_and_read_only(self):
        page = self.manager.query("never", normalize_filters())
        self.assertEqual(page["transactions"], [])
        self.assertEqual(page["total"], 0)
        self.assertIsNone(page["next_cursor"])
        # 不创建链：status 仍为未知
        self.assertIsNone(
            self.manager.status("never")["committed_end_block"]
        )

    def test_unknown_chain_stats_is_no_match(self):
        stats = self.manager.stats("never", normalize_filters())
        self.assertEqual(
            stats,
            {
                "total_count": 0,
                "total_amount": "0",
                "min_amount": None,
                "max_amount": None,
                "avg_amount": None,
            },
        )

    def test_unknown_chain_with_cursor_still_validates_cursor(self):
        good_fetcher = make_fetcher()
        other = MultiChainReplayManager(good_fetcher)
        other.submit("chain-a", 0, 4, 10)
        first = other.query("chain-a", normalize_filters(), page_size=2)
        cursor = first["next_cursor"]
        # 游标绑定的链尚不存在：不是「空结果」，而是游标链不匹配
        with self.assertRaises(InvalidCursorError):
            other.query("chain-b", normalize_filters(), cursor=cursor)

    def test_invalid_page_size_on_known_and_unknown_chain(self):
        for bad in (0, -1, 1001, 1.5, True, "10"):
            with self.assertRaises(InvalidPageSizeError):
                self.manager.query(
                    "chain-a", normalize_filters(), page_size=bad
                )
            with self.assertRaises(InvalidPageSizeError):
                self.manager.query(
                    "never", normalize_filters(), page_size=bad
                )

    def test_invalid_filters_and_time_range(self):
        with self.assertRaises(InvalidFilterError):
            self.manager.query(
                "chain-a", normalize_filters(address=" ")
            )
        with self.assertRaises(InvalidFilterError):
            self.manager.stats(
                "chain-a",
                normalize_filters(address="alice", from_address="bob"),
            )
        with self.assertRaises(InvalidTimeRangeError):
            self.manager.query(
                "chain-a",
                normalize_filters(start_time=30, end_time=10),
            )


class CursorBindingTest(unittest.TestCase):
    def setUp(self):
        self.manager = MultiChainReplayManager(make_fetcher())
        self.manager.submit("chain-a", 0, 5, 10)
        self.manager.submit("chain-b", 0, 5, 10)

    def test_paging_within_same_chain(self):
        first = self.manager.query(
            "chain-a", normalize_filters(), page_size=2
        )
        self.assertEqual(len(first["transactions"]), 2)
        self.assertEqual(first["total"], 6)
        second = self.manager.query(
            "chain-a", normalize_filters(), page_size=2,
            cursor=first["next_cursor"],
        )
        self.assertEqual(
            [t["block_number"] for t in second["transactions"]], [2, 3]
        )
        third = self.manager.query(
            "chain-a", normalize_filters(), page_size=2,
            cursor=second["next_cursor"],
        )
        self.assertEqual(
            [t["block_number"] for t in third["transactions"]], [4, 5]
        )
        self.assertIsNone(third["next_cursor"])

    def test_cursor_rejected_on_other_chain(self):
        cursor = self.manager.query(
            "chain-a", normalize_filters(), page_size=2
        )["next_cursor"]
        with self.assertRaises(InvalidCursorError):
            self.manager.query(
                "chain-b", normalize_filters(), page_size=2, cursor=cursor
            )

    def test_cursor_rejected_when_filters_change(self):
        cursor = self.manager.query(
            "chain-a", normalize_filters(), page_size=2
        )["next_cursor"]
        with self.assertRaises(InvalidCursorError):
            self.manager.query(
                "chain-a", normalize_filters(method="m0"),
                page_size=2, cursor=cursor,
            )
        with self.assertRaises(InvalidCursorError):
            self.manager.query(
                "chain-a", normalize_filters(start_time=0),
                page_size=2, cursor=cursor,
            )

    def test_cursor_accepted_with_equivalent_filters(self):
        first = self.manager.query(
            "chain-a",
            normalize_filters(method=("m0", "m1")),
            page_size=1,
        )
        cursor = first["next_cursor"]
        # 集合顺序不同但筛选等价：可续页
        second = self.manager.query(
            "chain-a",
            normalize_filters(method=["m1", "m0"]),
            page_size=1,
            cursor=cursor,
        )
        self.assertEqual(len(second["transactions"]), 1)

    def test_plain_indexer_cursor_rejected(self):
        # 普通 TxIndexer query 游标没有多链包装层
        plain = encode_cursor(normalize_filters(), 0, "h-0-0")
        with self.assertRaises(InvalidCursorError):
            self.manager.query(
                "chain-a", normalize_filters(), cursor=plain
            )

    def test_import_cursor_rejected(self):
        import_cursor = _encode_payload({
            "v": 1,
            "c": SCOPE_IMPORT,
            "chain": "chain-a",
            "height": 0,
            "hash": "0xabc",
        })
        with self.assertRaises(InvalidCursorError):
            self.manager.query(
                "chain-a", normalize_filters(), cursor=import_cursor
            )

    def test_garbled_cursor_rejected(self):
        cursor = self.manager.query(
            "chain-a", normalize_filters(), page_size=2
        )["next_cursor"]
        for bad in ("", "mcq1.!!!", "mcq1." + cursor[6:], cursor[:-1]):
            with self.assertRaises(InvalidCursorError):
                self.manager.query(
                    "chain-a", normalize_filters(), cursor=bad
                )

    def test_cursor_does_not_leak_across_manager_instances(self):
        other = MultiChainReplayManager(make_fetcher())
        other.submit("chain-a", 0, 5, 10)
        cursor = self.manager.query(
            "chain-a", normalize_filters(), page_size=2
        )["next_cursor"]
        # 同链同筛选但来自另一管理器：游标自包含，仍可解码续页
        page = other.query(
            "chain-a", normalize_filters(), page_size=2, cursor=cursor
        )
        self.assertEqual(
            [t["block_number"] for t in page["transactions"]], [2, 3]
        )


class ConcurrencyTest(unittest.TestCase):
    def test_different_chains_do_not_block_each_other(self):
        gate_a = threading.Event()

        def fetch_a(start, end):
            gate_a.wait(5)
            return [block(h, [tx_for(h)]) for h in range(start, end + 1)]

        manager = MultiChainReplayManager()
        done_b = threading.Event()

        def run_a():
            manager.submit(
                "chain-a", 0, 1, 2, fetch_blocks=fetch_a
            )

        def run_b():
            manager.submit(
                "chain-b", 10, 11, 2, fetch_blocks=make_fetcher()
            )
            done_b.set()

        t_a = threading.Thread(target=run_a)
        t_b = threading.Thread(target=run_b)
        t_a.start()
        threading.Event().wait(0.1)
        t_b.start()
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
        threads = [
            threading.Thread(
                target=lambda: manager.submit("chain-a", 0, 1, 1)
            ),
            threading.Thread(
                target=lambda: manager.submit("chain-a", 2, 3, 1)
            ),
        ]
        threads[0].start()
        while not calls:
            threading.Event().wait(0.001)
        threads[1].start()
        threading.Event().wait(0.1)
        self.assertIsNone(
            manager.status("chain-a")["committed_end_block"]
        )
        gate.set()
        for t in threads:
            t.join(5)
        self.assertEqual(
            manager.status("chain-a")["committed_end_block"], 3
        )

    def test_same_hash_concurrent_across_chains_both_commit(self):
        gates = {"chain-a": threading.Event(), "chain-b": threading.Event()}
        entered = {"chain-a": threading.Event(),
                   "chain-b": threading.Event()}
        manager = MultiChainReplayManager()

        def make_fetcher_for(chain_id):
            def fetch(start, end):
                entered[chain_id].set()
                gates[chain_id].wait(5)
                return [block(0, [tx_for(0)])]
            return fetch

        results = {}

        def run(chain_id):
            results[chain_id] = manager.submit(
                chain_id, 0, 0, 1,
                fetch_blocks=make_fetcher_for(chain_id),
            )

        threads = [
            threading.Thread(target=run, args=("chain-a",)),
            threading.Thread(target=run, args=("chain-b",)),
        ]
        for t in threads:
            t.start()
        self.assertTrue(entered["chain-a"].wait(2))
        self.assertTrue(entered["chain-b"].wait(2))
        gates["chain-a"].set()
        gates["chain-b"].set()
        for t in threads:
            t.join(5)

        # 两条链都真正新增了同哈希交易，互不判重、互不冲突
        for chain_id in ("chain-a", "chain-b"):
            self.assertEqual(results[chain_id]["committed_count"], 1)
            page = manager.query(chain_id, normalize_filters())
            self.assertEqual(page["total"], 1)
            self.assertEqual(page["transactions"][0]["tx_hash"], "h-0-0")


if __name__ == "__main__":
    unittest.main()
