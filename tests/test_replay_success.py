"""重放标准化保留 success 后的状态筛选、聚合与冲突测试。"""

import unittest

from tx_indexer import MultiChainReplayManager
from tx_indexer.engine import normalize_filters
from tx_indexer.errors import TransactionConflictError
from tx_indexer.replay import ReplayManager

try:  # 以 tests 包方式运行
    from .test_replay import block, make_fetcher, tx, tx_for
except ImportError:  # unittest discover -s tests 时按顶层模块加载
    from test_replay import block, make_fetcher, tx, tx_for


def heights(indexer):
    page = indexer.query(normalize_filters(), page_size=1000)
    return [t["block_number"] for t in page["transactions"]]


class ReplaySuccessFilterTest(unittest.TestCase):
    def setUp(self):
        # 区块 0：一笔显式失败 + 一笔缺省 success（计成功）
        # 区块 1：一笔显式成功
        txs = {
            0: [
                tx("fail-0", 0, timestamp=1000, frm="a", to="b",
                   method="transfer", amount="7", success=False),
                tx("ok-default", 0, timestamp=1001, frm="a", to="c",
                   method="transfer", amount="5"),
            ],
            1: [
                tx("ok-explicit", 1, timestamp=1002, frm="a", to="d",
                   method="approve", amount="11", success=True),
            ],
        }
        self.manager = ReplayManager(make_fetcher(txs))
        self.manager.submit("c", 0, 1, 5)
        self.indexer = self.manager.indexer

    def test_public_output_still_excludes_success(self):
        page = self.indexer.query(normalize_filters(), page_size=10)
        for record in page["transactions"]:
            self.assertEqual(
                set(record.keys()),
                {"tx_hash", "block_number", "timestamp", "from_address",
                 "to_address", "method", "amount"},
            )

    def test_status_success_matches_true_and_default(self):
        page = self.indexer.query(normalize_filters(status="success"))
        self.assertEqual(page["total"], 2)
        self.assertEqual(
            sorted(t["tx_hash"] for t in page["transactions"]),
            ["ok-default", "ok-explicit"],
        )

    def test_status_failure_matches_only_false(self):
        page = self.indexer.query(normalize_filters(status="failure"))
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["transactions"][0]["tx_hash"], "fail-0")

    def test_no_status_filter_matches_all(self):
        page = self.indexer.query(normalize_filters())
        self.assertEqual(page["total"], 3)

    def test_status_stats_splits_counts_and_amounts(self):
        stats = self.indexer.status_stats(normalize_filters())
        self.assertEqual(stats["total_count"], 3)
        self.assertEqual(stats["success_count"], 2)
        self.assertEqual(stats["failure_count"], 1)
        self.assertEqual(stats["success_amount"], "16")  # 5 + 11
        self.assertEqual(stats["failure_amount"], "7")

    def test_status_stats_respects_status_filter(self):
        failed = self.indexer.status_stats(normalize_filters(status="failure"))
        self.assertEqual(failed["total_count"], 1)
        self.assertEqual(failed["success_count"], 0)
        self.assertEqual(failed["success_amount"], "0")
        self.assertEqual(failed["failure_count"], 1)
        self.assertEqual(failed["failure_amount"], "7")

    def test_method_status_stats_splits_by_method(self):
        groups = self.indexer.method_status_stats(normalize_filters())
        by_method = {g["method"]: g for g in groups["groups"]}
        transfer = by_method["transfer"]
        self.assertEqual(transfer["total_count"], 2)
        self.assertEqual(transfer["success_count"], 1)
        self.assertEqual(transfer["failure_count"], 1)
        self.assertEqual(transfer["success_amount"], "5")
        self.assertEqual(transfer["failure_amount"], "7")
        approve = by_method["approve"]
        self.assertEqual(approve["success_count"], 1)
        self.assertEqual(approve["failure_count"], 0)
        self.assertEqual(approve["failure_amount"], "0")


class ReplaySuccessValidationTest(unittest.TestCase):
    def _manager_with_prior_batch(self, bad_tx):
        fetcher = make_fetcher({1: [tx_for(1), bad_tx]})
        manager = ReplayManager(fetcher)
        manager.submit("c", 0, 0, 1)  # 前序批次完整提交
        return manager

    def test_invalid_success_values_rejected(self):
        for bad in (0, 1, None, "true", "false", "True", 1.0, []):
            with self.subTest(bad=bad):
                bad_tx = tx("bad", 1, success=bad)
                manager = self._manager_with_prior_batch(bad_tx)
                with self.assertRaises(ValueError):
                    manager.submit("c", 1, 1, 1)
                # 当前批次不部分提交：水位停在前序批次，合法的同批交易也不可见
                self.assertEqual(
                    manager.status("c")["committed_end_block"], 0
                )
                self.assertEqual(heights(manager.indexer), [0])

    def test_invalid_success_in_first_batch_leaves_empty_index(self):
        for bad in (0, None, "false"):
            bad_tx = tx("bad", 0, success=bad)
            manager = ReplayManager(make_fetcher({0: [bad_tx]}))
            with self.assertRaises(ValueError):
                manager.submit("c", 0, 0, 1)
            self.assertIsNone(manager.status("c")["committed_end_block"])
            self.assertEqual(heights(manager.indexer), [])


class ReplaySuccessIdentityTest(unittest.TestCase):
    def test_explicit_true_equivalent_to_default(self):
        first = tx_for(0)  # 缺省 success
        again = dict(tx_for(0), success=True)  # 显式 true
        manager = ReplayManager(make_fetcher({0: [first, again]}))
        result = manager.submit("c", 0, 0, 1)
        self.assertEqual(result["committed_count"], 1)
        self.assertEqual(result["skipped_count"], 1)

    def test_explicit_true_then_default_across_batches_is_skip(self):
        # ReplayManager 的 tx_hash 台账跨链全局：链 A 以显式 true 写入后，
        # 链 B 在不同批次提交同哈希但缺省 success（同状态）应判重跳过，
        # 真正走到跨批次的 _same_identity 判定
        data = {0: [dict(tx_for(0), success=True)]}
        manager = ReplayManager(make_fetcher(data))
        manager.submit("chain-a", 0, 0, 1)
        result = manager.submit(
            "chain-b", 0, 0, 1,
            fetch_blocks=make_fetcher({0: [tx_for(0)]}),
        )
        self.assertEqual(result["committed_count"], 0)
        self.assertEqual(result["skipped_count"], 1)
        # 首次写入保留显式 true；缺省等价为成功，记录仍是成功
        page = manager.indexer.query(
            normalize_filters(status="success"), page_size=10
        )
        self.assertEqual(page["total"], 1)

    def test_default_then_false_conflicts(self):
        manager = ReplayManager(make_fetcher({0: [tx_for(0)]}))
        manager.submit("c", 0, 0, 1)
        flipped = dict(tx_for(0), block_number=1, success=False)
        with self.assertRaises(TransactionConflictError):
            manager.submit(
                "c", 1, 1, 1,
                fetch_blocks=make_fetcher({1: [flipped]}),
            )
        self.assertEqual(manager.status("c")["committed_end_block"], 0)
        self.assertEqual(heights(manager.indexer), [0])

    def test_false_then_default_true_conflicts(self):
        manager = ReplayManager(
            make_fetcher({0: [dict(tx_for(0), success=False)]})
        )
        manager.submit("c", 0, 0, 1)
        flipped = dict(tx_for(0), block_number=1)  # 缺省 => true
        with self.assertRaises(TransactionConflictError):
            manager.submit(
                "c", 1, 1, 1,
                fetch_blocks=make_fetcher({1: [flipped]}),
            )

    def test_same_false_status_replayed_is_skip(self):
        record = dict(tx_for(0), success=False)
        manager = ReplayManager(make_fetcher({0: [record]}))
        manager.submit("c", 0, 0, 1)
        # 整段已被水位覆盖：直接幂等返回，不重新拉取
        result = manager.submit("c", 0, 0, 1)
        self.assertEqual(result["processed_batch_count"], 0)
        page = manager.indexer.query(normalize_filters(status="failure"))
        self.assertEqual(page["total"], 1)

    def test_status_conflict_in_later_batch_keeps_earlier(self):
        # 前两批正常提交，第三批出现成功状态翻转：仅第三批拒绝
        flipped = dict(tx_for(1), block_number=6, success=False)
        manager = ReplayManager(
            make_fetcher({6: [tx_for(6), flipped]})
        )
        with self.assertRaises(TransactionConflictError):
            manager.submit("c", 0, 9, 3)
        self.assertEqual(manager.status("c")["committed_end_block"], 5)
        self.assertEqual(heights(manager.indexer), list(range(6)))

    def test_failure_records_counted_in_aggregation(self):
        txs = {
            0: [
                tx("f1", 0, timestamp=100, frm="a", to="b", method="m",
                   amount="4", success=False),
                tx("f2", 0, timestamp=101, frm="a", to="b", method="m",
                   amount="6", success=False),
                tx("s1", 0, timestamp=102, frm="a", to="b", method="m",
                   amount="10"),
            ],
        }
        manager = ReplayManager(make_fetcher(txs))
        manager.submit("c", 0, 0, 1)
        stats = manager.indexer.status_stats(normalize_filters())
        self.assertEqual(stats["success_count"], 1)
        self.assertEqual(stats["failure_count"], 2)
        self.assertEqual(stats["success_amount"], "10")
        self.assertEqual(stats["failure_amount"], "10")


class MultiChainReplaySuccessTest(unittest.TestCase):
    def test_status_filter_scoped_per_chain(self):
        on_a = tx("dup", 0, timestamp=1, frm="a", to="b", method="m",
                  amount="3", success=False)
        on_b = tx("dup", 0, timestamp=1, frm="a", to="b", method="m",
                  amount="3", success=True)
        manager = MultiChainReplayManager(
            make_fetcher({0: [on_a]})
        )
        manager.submit("chain-a", 0, 0, 1)
        manager.submit("chain-b", 0, 0, 1,
                       fetch_blocks=make_fetcher({0: [on_b]}))

        a_failed = manager.query(
            "chain-a", normalize_filters(status="failure")
        )
        self.assertEqual(a_failed["total"], 1)
        a_succeeded = manager.query(
            "chain-a", normalize_filters(status="success")
        )
        self.assertEqual(a_succeeded["total"], 0)
        b_failed = manager.query(
            "chain-b", normalize_filters(status="failure")
        )
        self.assertEqual(b_failed["total"], 0)
        b_succeeded = manager.query(
            "chain-b", normalize_filters(status="success")
        )
        self.assertEqual(b_succeeded["total"], 1)

    def test_status_stats_scoped_per_chain(self):
        on_a = tx("dup", 0, timestamp=1, frm="a", to="b", method="m",
                  amount="3", success=False)
        on_b = tx("dup", 0, timestamp=1, frm="a", to="b", method="m",
                  amount="3")  # 缺省 => 成功
        manager = MultiChainReplayManager(make_fetcher({0: [on_a]}))
        manager.submit("chain-a", 0, 0, 1)
        manager.submit("chain-b", 0, 0, 1,
                       fetch_blocks=make_fetcher({0: [on_b]}))
        # 按链隔离的索引各自暴露 status_stats
        a_stats = manager._get_manager("chain-a").indexer.status_stats(
            normalize_filters()
        )
        b_stats = manager._get_manager("chain-b").indexer.status_stats(
            normalize_filters()
        )
        self.assertEqual(a_stats["failure_count"], 1)
        self.assertEqual(a_stats["success_count"], 0)
        self.assertEqual(b_stats["failure_count"], 0)
        self.assertEqual(b_stats["success_count"], 1)

    def test_same_hash_different_success_across_chains_both_commit(self):
        # 跨链同哈希即便 success 不同也不构成冲突：各自写入
        on_a = tx("dup", 0, timestamp=1, frm="a", to="b", method="m",
                  amount="1", success=False)
        on_b = tx("dup", 0, timestamp=1, frm="a", to="b", method="m",
                  amount="1", success=True)
        manager = MultiChainReplayManager(make_fetcher({0: [on_a]}))
        r1 = manager.submit("chain-a", 0, 0, 1)
        r2 = manager.submit("chain-b", 0, 0, 1,
                            fetch_blocks=make_fetcher({0: [on_b]}))
        self.assertEqual(r1["committed_count"], 1)
        self.assertEqual(r2["committed_count"], 1)

    def test_same_chain_status_flip_conflicts(self):
        manager = MultiChainReplayManager(
            make_fetcher({0: [tx_for(0)]})
        )
        manager.submit("c", 0, 0, 1)
        flipped = dict(tx_for(0), block_number=1, success=False)
        with self.assertRaises(TransactionConflictError):
            manager.submit(
                "c", 1, 1, 1,
                fetch_blocks=make_fetcher({1: [flipped]}),
            )
        self.assertEqual(manager.status("c")["committed_end_block"], 0)

    def test_invalid_success_rejected_before_batch_commit(self):
        bad = tx("bad", 0, success=0)
        manager = MultiChainReplayManager(make_fetcher({0: [bad]}))
        with self.assertRaises(ValueError):
            manager.submit("c", 0, 0, 1)
        self.assertIsNone(manager.status("c")["committed_end_block"])
        self.assertEqual(
            manager.query("c", normalize_filters())["total"], 0
        )


if __name__ == "__main__":
    unittest.main()
