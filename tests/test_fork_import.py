"""可重放的分批增量导入与链分叉修正测试。"""

import unittest

from tx_indexer.engine import normalize_filters
from tx_indexer.errors import (
    BlockLinkMismatchError,
    CursorOutOfRangeError,
    InvalidImportBatchError,
)
from tx_indexer.fork_import import ForkAwareImporter


def tx(tx_hash, block_number, timestamp, frm, to, method, amount,
       success=None):
    record = {
        "tx_hash": tx_hash,
        "block_number": block_number,
        "timestamp": timestamp,
        "from_address": frm,
        "to_address": to,
        "method": method,
        "amount": amount,
    }
    if success is not None:
        record["success"] = success
    return record


def batch(chain_id, batch_seq, parent_hash, block_hash, block_height,
          transactions):
    return {
        "chain_id": chain_id,
        "batch_seq": batch_seq,
        "parent_hash": parent_hash,
        "block_hash": block_hash,
        "block_height": block_height,
        "transactions": transactions,
    }


def hashes(page):
    return [t["tx_hash"] for t in page["transactions"]]


def sample_chain():
    """三个连续区块：A(0) -> B(1) -> C(2)。"""
    return [
        batch("chain-a", 1, "0xgenesis", "0xa", 0, [
            tx("a1", 0, 10, "alice", "bob", "transfer", "10"),
        ]),
        batch("chain-a", 2, "0xa", "0xb", 1, [
            tx("b1", 1, 20, "bob", "carol", "transfer", "20"),
            tx("b2", 1, 30, "carol", "dave", "approve", "5",
               success=False),
        ]),
        batch("chain-a", 3, "0xb", "0xc", 2, [
            tx("c1", 2, 40, "dave", "alice", "transfer", "7"),
        ]),
    ]


class ImportBatchesTest(unittest.TestCase):
    def setUp(self):
        self.importer = ForkAwareImporter()

    def test_genesis_and_consecutive_batches(self):
        result = self.importer.import_batches(sample_chain())
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["chain_id"], "chain-a")
        self.assertEqual(result["rolled_back_block_count"], 0)
        self.assertEqual(result["applied_block_count"], 3)
        self.assertEqual(result["tip_block_height"], 2)
        self.assertEqual(result["tip_block_hash"], "0xc")

        page = self.importer.indexer.query(normalize_filters())
        self.assertEqual(hashes(page), ["a1", "b1", "b2", "c1"])
        self.assertEqual(page["total"], 4)

    def test_single_batch_entry(self):
        result = self.importer.import_batch(sample_chain()[0])
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["applied_block_count"], 1)
        self.assertEqual(result["tip_block_hash"], "0xa")

    def test_batches_applied_in_block_height_order(self):
        batches = sample_chain()
        result = self.importer.import_batches(
            [batches[2], batches[0], batches[1]]
        )
        self.assertEqual(result["applied_block_count"], 3)
        self.assertEqual(
            hashes(self.importer.indexer.query(normalize_filters())),
            ["a1", "b1", "b2", "c1"],
        )

    def test_aggregates_reflect_imported_transactions(self):
        self.importer.import_batches(sample_chain())
        stats = self.importer.indexer.stats(normalize_filters())
        self.assertEqual(stats["total_count"], 4)
        self.assertEqual(stats["total_amount"], "42")
        method_stats = self.importer.indexer.method_stats(
            normalize_filters()
        )
        groups = {g["method"]: g for g in method_stats["groups"]}
        self.assertEqual(groups["transfer"]["total_count"], 3)
        self.assertEqual(groups["transfer"]["total_amount"], "37")
        self.assertEqual(groups["approve"]["total_count"], 1)

    def test_parent_mismatch_raises_and_keeps_state(self):
        self.importer.import_batches(sample_chain())
        before = self.importer.indexer.query(normalize_filters())
        with self.assertRaises(BlockLinkMismatchError):
            self.importer.import_batch(
                batch("chain-a", 4, "0xwrong", "0xd", 3, [])
            )
        after = self.importer.indexer.query(normalize_filters())
        self.assertEqual(before, after)
        self.assertEqual(
            self.importer.indexer.stats(normalize_filters())["total_count"],
            4,
        )

    def test_unknown_parent_raises(self):
        self.importer.import_batches(sample_chain())
        with self.assertRaises(BlockLinkMismatchError):
            self.importer.import_batch(
                batch("chain-a", 4, "0xunknown", "0xd", 3, [])
            )

    def test_internal_link_mismatch_raises(self):
        batches = sample_chain()
        batches[1]["parent_hash"] = "0xtampered"
        with self.assertRaises(BlockLinkMismatchError):
            self.importer.import_batches(batches)
        self.assertEqual(
            self.importer.indexer.query(normalize_filters())["total"], 0
        )

    def test_duplicate_batch_returns_already_applied(self):
        self.importer.import_batches(sample_chain())
        result = self.importer.import_batch(sample_chain()[2])
        self.assertEqual(result["status"], "already_applied")
        self.assertEqual(result["rolled_back_block_count"], 0)
        self.assertEqual(result["applied_block_count"], 0)
        self.assertEqual(result["tip_block_hash"], "0xc")
        # 不重复写入、不重复累计
        self.assertEqual(
            self.importer.indexer.stats(normalize_filters())["total_count"],
            4,
        )

    def test_duplicate_whole_submission_returns_already_applied(self):
        batches = sample_chain()
        self.importer.import_batches(batches)
        result = self.importer.import_batches(batches)
        self.assertEqual(result["status"], "already_applied")
        self.assertEqual(
            self.importer.indexer.stats(normalize_filters())["total_count"],
            4,
        )

    def test_partial_overlap_skips_applied_and_appends(self):
        batches = sample_chain()
        self.importer.import_batches(batches[:2])
        result = self.importer.import_batches(batches[1:])
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["applied_block_count"], 1)
        self.assertEqual(result["tip_block_hash"], "0xc")
        self.assertEqual(
            self.importer.indexer.query(normalize_filters())["total"], 4
        )


class InvalidBatchTest(unittest.TestCase):
    def setUp(self):
        self.importer = ForkAwareImporter()
        self.importer.import_batches(sample_chain())

    def assert_invalid(self, batches):
        before = self.importer.indexer.query(normalize_filters())
        with self.assertRaises(InvalidImportBatchError):
            self.importer.import_batches(batches)
        self.assertEqual(
            before, self.importer.indexer.query(normalize_filters())
        )

    def test_empty_chain_id(self):
        self.assert_invalid([
            batch("", 4, "0xc", "0xd", 3, []),
        ])
        self.assert_invalid([
            batch("   ", 4, "0xc", "0xd", 3, []),
        ])

    def test_negative_block_height(self):
        self.assert_invalid([
            batch("chain-a", 4, "0xc", "0xd", -1, []),
        ])

    def test_non_positive_batch_seq(self):
        self.assert_invalid([batch("chain-a", 0, "0xc", "0xd", 3, [])])
        self.assert_invalid([batch("chain-a", -2, "0xc", "0xd", 3, [])])
        self.assert_invalid([batch("chain-a", True, "0xc", "0xd", 3, [])])

    def test_duplicate_block_hash_within_submission(self):
        self.assert_invalid([
            batch("chain-a", 4, "0xc", "0xd", 3, []),
            batch("chain-a", 5, "0xd", "0xd", 4, []),
        ])

    def test_duplicate_block_hash_already_on_chain(self):
        # 与当前分支已有区块哈希相同但批次序号不同：同样是重复区块哈希
        self.assert_invalid([
            batch("chain-a", 99, "0xb", "0xc", 2, []),
        ])

    def test_empty_submission(self):
        self.assert_invalid([])

    def test_mixed_chain_ids(self):
        self.assert_invalid([
            batch("chain-a", 4, "0xc", "0xd", 3, []),
            batch("chain-b", 5, "0xd", "0xe", 4, []),
        ])

    def test_missing_batch_field(self):
        bad = batch("chain-a", 4, "0xc", "0xd", 3, [])
        del bad["block_hash"]
        self.assert_invalid([bad])

    def test_tx_block_number_mismatch(self):
        self.assert_invalid([
            batch("chain-a", 4, "0xc", "0xd", 3, [
                tx("d1", 2, 50, "alice", "bob", "transfer", "1"),
            ]),
        ])

    def test_tx_field_semantics(self):
        # amount 不符公开记录格式
        self.assert_invalid([
            batch("chain-a", 4, "0xc", "0xd", 3, [
                tx("d1", 3, 50, "alice", "bob", "transfer", "-1"),
            ]),
        ])
        # success 非布尔值
        self.assert_invalid([
            batch("chain-a", 4, "0xc", "0xd", 3, [
                tx("d1", 3, 50, "alice", "bob", "transfer", "1",
                   success=1),
            ]),
        ])
        # 未定义的交易字段
        self.assert_invalid([
            batch("chain-a", 4, "0xc", "0xd", 3, [
                dict(tx("d1", 3, 50, "alice", "bob", "transfer", "1"),
                     fee="1"),
            ]),
        ])


class ForkReplacementTest(unittest.TestCase):
    def setUp(self):
        self.importer = ForkAwareImporter()
        self.importer.import_batches(sample_chain())

    def fork_batches(self):
        """从 0xa 分叉的更高新分支：B'(1) -> C'(2) -> D'(3)。"""
        return [
            batch("chain-a", 11, "0xa", "0xb2", 1, [
                tx("x1", 1, 21, "alice", "carol", "transfer", "100"),
            ]),
            batch("chain-a", 12, "0xb2", "0xc2", 2, [
                tx("x2", 2, 41, "carol", "bob", "approve", "3"),
            ]),
            batch("chain-a", 13, "0xc2", "0xd2", 3, [
                tx("x3", 3, 61, "bob", "dave", "transfer", "9",
                   success=False),
            ]),
        ]

    def test_fork_replaces_old_branch(self):
        result = self.importer.import_batches(self.fork_batches())
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["rolled_back_block_count"], 2)
        self.assertEqual(result["applied_block_count"], 3)
        self.assertEqual(result["tip_block_height"], 3)
        self.assertEqual(result["tip_block_hash"], "0xd2")

        page = self.importer.indexer.query(normalize_filters())
        self.assertEqual(hashes(page), ["a1", "x1", "x2", "x3"])
        stats = self.importer.indexer.stats(normalize_filters())
        self.assertEqual(stats["total_count"], 4)
        self.assertEqual(stats["total_amount"], "122")

    def test_fork_updates_all_aggregations(self):
        self.importer.import_batches(self.fork_batches())
        filters = normalize_filters()
        method_stats = self.importer.indexer.method_stats(filters)
        groups = {g["method"]: g for g in method_stats["groups"]}
        self.assertEqual(groups["transfer"]["total_count"], 3)
        self.assertEqual(groups["transfer"]["total_amount"], "119")
        self.assertEqual(groups["approve"]["total_count"], 1)
        self.assertEqual(groups["approve"]["total_amount"], "3")

        status = self.importer.indexer.status_stats(filters)
        self.assertEqual(status["success_count"], 3)
        self.assertEqual(status["failure_count"], 1)
        self.assertEqual(status["failure_amount"], "9")

        time_stats = self.importer.indexer.time_stats(filters, 30)
        buckets = {
            g["bucket_start"]: g for g in time_stats["groups"]
        }
        # 旧分支的 timestamp（20/30/40）已撤销，只剩当前有效分支
        self.assertEqual(
            sorted(buckets), [0, 30, 60]
        )
        self.assertEqual(buckets[0]["total_count"], 2)
        self.assertEqual(buckets[30]["total_count"], 1)
        self.assertEqual(buckets[60]["total_count"], 1)

    def test_rollback_dedups_same_tx_hash_within_branch(self):
        # 新分支 B' 重新包含旧分支的交易 b1（同哈希同内容）：
        # 回滚后按当前分支内去重，只保留一份、只累计一次
        fork = self.fork_batches()
        fork[0]["transactions"].append(
            tx("b1", 1, 20, "bob", "carol", "transfer", "20")
        )
        result = self.importer.import_batches(fork)
        self.assertEqual(result["status"], "applied")
        page = self.importer.indexer.query(normalize_filters())
        self.assertEqual(hashes(page), ["a1", "b1", "x1", "x2", "x3"])
        stats = self.importer.indexer.stats(normalize_filters())
        self.assertEqual(stats["total_count"], 5)
        self.assertEqual(stats["total_amount"], "142")

    def test_duplicate_tx_within_new_branch_deduped(self):
        fork = self.fork_batches()
        fork[1]["transactions"].append(
            tx("x1", 2, 55, "alice", "carol", "transfer", "100")
        )
        self.importer.import_batches(fork)
        page = self.importer.indexer.query(normalize_filters())
        self.assertEqual(hashes(page), ["a1", "x1", "x2", "x3"])

    def test_not_higher_branch_rejected(self):
        # 新分支末端高度不超过当前末端：不接受，状态不变
        before = self.importer.indexer.query(normalize_filters())
        with self.assertRaises(BlockLinkMismatchError):
            self.importer.import_batches([
                batch("chain-a", 11, "0xa", "0xb2", 1, []),
                batch("chain-a", 12, "0xb2", "0xc2", 2, []),
            ])
        self.assertEqual(
            before, self.importer.indexer.query(normalize_filters())
        )

    def test_old_branch_batches_no_longer_idempotent_after_fork(self):
        # 旧分支批次被回滚后不再视为已应用，重新提交按链接规则处理
        self.importer.import_batches(self.fork_batches())
        with self.assertRaises(BlockLinkMismatchError):
            self.importer.import_batch(sample_chain()[2])

    def test_cursor_pointing_to_rolled_back_tx_raises(self):
        indexer = self.importer.indexer
        filters = normalize_filters()
        page1 = indexer.query(filters, page_size=2)
        cursor_at_b1 = page1["next_cursor"]  # 指向旧分支交易 b1
        page2 = indexer.query(filters, page_size=2, cursor=cursor_at_b1)
        self.assertEqual(hashes(page2), ["b2", "c1"])

        self.importer.import_batches(self.fork_batches())

        with self.assertRaises(CursorOutOfRangeError):
            indexer.query(filters, page_size=2, cursor=cursor_at_b1)

    def test_cursor_pointing_to_surviving_tx_still_works(self):
        indexer = self.importer.indexer
        filters = normalize_filters()
        page1 = indexer.query(filters, page_size=1)
        cursor_at_a1 = page1["next_cursor"]  # 指向保留前缀中的 a1

        self.importer.import_batches(self.fork_batches())

        page = indexer.query(filters, page_size=10, cursor=cursor_at_a1)
        self.assertEqual(hashes(page), ["x1", "x2", "x3"])

    def test_queries_only_reflect_current_branch(self):
        # 分叉前按地址筛选有命中；分叉后旧分支数据不再可见
        self.importer.import_batches(self.fork_batches())
        page = self.importer.indexer.query(
            normalize_filters(address="dave")
        )
        self.assertEqual(hashes(page), ["x3"])


class MultiChainTest(unittest.TestCase):
    def test_chains_are_independent(self):
        importer = ForkAwareImporter()
        importer.import_batch(
            batch("chain-a", 1, "0xg", "0xa1", 0, [
                tx("t1", 0, 1, "alice", "bob", "transfer", "1"),
            ])
        )
        importer.import_batch(
            batch("chain-b", 1, "0xg", "0xb1", 0, [
                tx("t1", 0, 2, "carol", "dave", "approve", "2"),
            ])
        )
        # 跨链同哈希属于不同交易
        self.assertEqual(
            importer.indexer.query(normalize_filters())["total"], 2
        )

        # chain-a 分叉回滚不影响 chain-b 的同哈希记录
        result = importer.import_batches([
            batch("chain-a", 2, "0xg", "0xa2", 0, []),
            batch("chain-a", 3, "0xa2", "0xa3", 1, []),
        ])
        self.assertEqual(result["rolled_back_block_count"], 1)
        page = importer.indexer.query(normalize_filters())
        self.assertEqual(hashes(page), ["t1"])
        stats = importer.indexer.stats(normalize_filters())
        self.assertEqual(stats["total_count"], 1)
        self.assertEqual(stats["total_amount"], "2")


if __name__ == "__main__":
    unittest.main()
