"""可重放的分批增量导入与链分叉修正测试。"""

import unittest

from tx_indexer.engine import normalize_filters
from tx_indexer.errors import (
    BlockLinkMismatchError,
    CursorOutOfRangeError,
    InvalidCursorError,
    InvalidImportBatchError,
)
from tx_indexer.fork import ForkAwareImporter


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


def batch(seq, height, block_hash, parent_hash, transactions,
          chain_id="chain-a"):
    return {
        "source_chain_id": chain_id,
        "batch_seq": seq,
        "parent_block_hash": parent_hash,
        "block_hash": block_hash,
        "block_height": height,
        "transactions": transactions,
    }


def old_branch():
    """旧分支：b1(1) <- b2(2) <- b3(3)。"""
    return [
        batch(1, 1, "0xb1", "0xgenesis", [
            tx("h1", 1, 10, "alice", "bob", "transfer", "10"),
            tx("h2", 1, 20, "bob", "alice", "approve", "20", success=False),
        ]),
        batch(2, 2, "0xb2", "0xb1", [
            tx("h3", 2, 100, "alice", "carol", "transfer", "30"),
        ]),
        batch(3, 3, "0xb3", "0xb2", [
            tx("h4", 3, 300, "bob", "dave", "transfer", "40"),
        ]),
    ]


def import_all(importer, batches):
    result = None
    for b in batches:
        result = importer.import_batch(b)
        assert result["status"] == "ok", result
    return result


def hashes(page):
    return [t["tx_hash"] for t in page["transactions"]]


class ImportBatchTest(unittest.TestCase):
    def test_sequential_import_builds_index(self):
        importer = ForkAwareImporter()
        result = import_all(importer, old_branch())
        self.assertEqual(result["tip_block_height"], 3)
        self.assertEqual(result["tip_block_hash"], "0xb3")
        self.assertEqual(importer.chain_id, "chain-a")
        self.assertEqual(importer.tip_block_height, 3)
        self.assertEqual(importer.tip_block_hash, "0xb3")

        page = importer.query(normalize_filters())
        self.assertEqual(hashes(page), ["h1", "h2", "h3", "h4"])
        self.assertEqual(page["total"], 4)
        stats = importer.stats(normalize_filters())
        self.assertEqual(stats["total_count"], 4)
        self.assertEqual(stats["total_amount"], "100")

    def test_import_result_counts(self):
        importer = ForkAwareImporter()
        result = importer.import_batch(old_branch()[0])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["imported_count"], 2)
        self.assertEqual(result["skipped_count"], 0)
        self.assertEqual(result["tip_block_height"], 1)
        self.assertEqual(result["tip_block_hash"], "0xb1")

    def test_genesis_block_can_be_empty(self):
        importer = ForkAwareImporter()
        result = importer.import_batch(batch(1, 0, "0xb0", "0xzero", []))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["imported_count"], 0)
        self.assertEqual(importer.tip_block_hash, "0xb0")

    def test_filters_and_time_window_follow_existing_semantics(self):
        importer = ForkAwareImporter()
        import_all(importer, old_branch())
        filters = normalize_filters(address="alice", method="transfer")
        page = importer.query(filters)
        self.assertEqual(hashes(page), ["h1", "h3"])
        filters = normalize_filters(start_time=20, end_time=100)
        page = importer.query(filters)
        self.assertEqual(hashes(page), ["h2", "h3"])

    def test_repeated_batch_returns_already_applied(self):
        importer = ForkAwareImporter()
        importer.import_batch(old_branch()[0])
        result = importer.import_batch(old_branch()[0])
        self.assertEqual(result["status"], "already_applied")
        self.assertEqual(result["imported_count"], 0)
        self.assertEqual(result["tip_block_hash"], "0xb1")
        # 不重复写入、不重复累计
        page = importer.query(normalize_filters())
        self.assertEqual(hashes(page), ["h1", "h2"])
        stats = importer.stats(normalize_filters())
        self.assertEqual(stats["total_count"], 2)
        self.assertEqual(stats["total_amount"], "30")

    def test_repeated_batch_after_more_imports(self):
        importer = ForkAwareImporter()
        import_all(importer, old_branch())
        result = importer.import_batch(old_branch()[1])
        self.assertEqual(result["status"], "already_applied")
        self.assertEqual(result["tip_block_hash"], "0xb3")
        self.assertEqual(importer.stats(normalize_filters())["total_count"], 4)

    def test_parent_mismatch_raises_and_preserves_state(self):
        importer = ForkAwareImporter()
        import_all(importer, old_branch())
        before_page = importer.query(normalize_filters())
        before_stats = importer.stats(normalize_filters())

        bad = batch(4, 4, "0xb4", "0xwrong", [
            tx("h5", 4, 400, "alice", "bob", "transfer", "50"),
        ])
        with self.assertRaises(BlockLinkMismatchError):
            importer.import_batch(bad)

        self.assertEqual(importer.query(normalize_filters()), before_page)
        self.assertEqual(importer.stats(normalize_filters()), before_stats)
        self.assertEqual(importer.tip_block_hash, "0xb3")

    def test_height_gap_raises_block_link_mismatch(self):
        importer = ForkAwareImporter()
        importer.import_batch(old_branch()[0])
        bad = batch(2, 5, "0xb5", "0xb1", [])
        with self.assertRaises(BlockLinkMismatchError):
            importer.import_batch(bad)

    def test_chain_id_mismatch_raises_block_link_mismatch(self):
        importer = ForkAwareImporter()
        importer.import_batch(old_branch()[0])
        bad = batch(2, 2, "0xb2", "0xb1", [], chain_id="chain-b")
        with self.assertRaises(BlockLinkMismatchError):
            importer.import_batch(bad)

    def test_invalid_batches_raise_and_preserve_state(self):
        importer = ForkAwareImporter()
        importer.import_batch(old_branch()[0])
        before = importer.query(normalize_filters())

        cases = [
            # 来源链标识为空
            batch(2, 2, "0xb2", "0xb1", [], chain_id=""),
            batch(2, 2, "0xb2", "0xb1", [], chain_id="   "),
            # 区块高度小于零
            batch(2, -1, "0xb2", "0xb1", []),
            # 批次序号非正数
            batch(0, 2, "0xb2", "0xb1", []),
            batch(-3, 2, "0xb2", "0xb1", []),
        ]
        for bad in cases:
            with self.assertRaises(InvalidImportBatchError):
                importer.import_batch(bad)
        self.assertEqual(importer.query(normalize_filters()), before)
        self.assertEqual(importer.tip_block_hash, "0xb1")

    def test_invalid_transaction_raises_invalid_import_batch(self):
        importer = ForkAwareImporter()
        bad = batch(1, 1, "0xb1", "0xgenesis", [
            tx("h1", 2, 10, "alice", "bob", "transfer", "10"),
        ])
        with self.assertRaises(InvalidImportBatchError):
            importer.import_batch(bad)
        bad_amount = batch(1, 1, "0xb1", "0xgenesis", [
            tx("h1", 1, 10, "alice", "bob", "transfer", "-10"),
        ])
        with self.assertRaises(InvalidImportBatchError):
            importer.import_batch(bad_amount)
        self.assertIsNone(importer.tip_block_hash)

    def test_field_aliases_accepted(self):
        importer = ForkAwareImporter()
        result = importer.import_batch({
            "chain_id": "chain-a",
            "batch_sequence": 1,
            "parent_hash": "0xgenesis",
            "block_hash": "0xb1",
            "block_number": 1,
            "transactions": [
                tx("h1", 1, 10, "alice", "bob", "transfer", "10"),
            ],
        })
        self.assertEqual(result["status"], "ok")
        self.assertEqual(importer.tip_block_height, 1)

    def test_same_block_hash_different_seq_rejected(self):
        importer = ForkAwareImporter()
        importer.import_batch(old_branch()[0])
        dup = batch(99, 2, "0xb1", "0xb1x", [])
        with self.assertRaises(InvalidImportBatchError):
            importer.import_batch(dup)


class ForkCorrectionTest(unittest.TestCase):
    def new_branch(self):
        """新分支：从 b1 分叉，b2'(2) <- b3'(3) <- b4'(4)。"""
        return [
            batch(10, 2, "0xc2", "0xb1", [
                tx("x1", 2, 110, "carol", "alice", "transfer", "7"),
            ]),
            batch(11, 3, "0xc3", "0xc2", [
                tx("x2", 3, 200, "alice", "dave", "approve", "8"),
            ]),
            batch(12, 4, "0xc4", "0xc3", [
                tx("x3", 4, 400, "dave", "bob", "transfer", "9"),
            ]),
        ]

    def test_fork_correction_result_and_queries(self):
        importer = ForkAwareImporter()
        import_all(importer, old_branch())

        result = importer.apply_branch(self.new_branch())
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["rolled_back_block_count"], 2)
        self.assertEqual(result["applied_block_count"], 3)
        self.assertEqual(result["tip_block_hash"], "0xc4")
        self.assertEqual(result["current_tip_block_hash"], "0xc4")
        self.assertEqual(result["tip_block_height"], 4)
        self.assertEqual(importer.tip_block_hash, "0xc4")

        # 所有查询只反映当前有效分支
        page = importer.query(normalize_filters())
        self.assertEqual(hashes(page), ["h1", "h2", "x1", "x2", "x3"])
        self.assertEqual(page["total"], 5)

        # 聚合数值与明细过滤结果保持一致
        stats = importer.stats(normalize_filters())
        self.assertEqual(stats["total_count"], 5)
        self.assertEqual(stats["total_amount"], "54")
        filtered = importer.query(normalize_filters(method="transfer"))
        filtered_stats = importer.stats(normalize_filters(method="transfer"))
        self.assertEqual(filtered["total"], 3)
        self.assertEqual(filtered_stats["total_count"], 3)
        total = sum(int(t["amount"]) for t in filtered["transactions"])
        self.assertEqual(filtered_stats["total_amount"], str(total))

        # 地址 / 方法 / 时间窗索引同样只反映新分支
        addr = importer.query(normalize_filters(address="carol"))
        self.assertEqual(hashes(addr), ["x1"])
        window = importer.query(normalize_filters(start_time=0, end_time=150))
        self.assertEqual(hashes(window), ["h1", "h2", "x1"])

    def test_rolled_back_batch_can_be_resubmitted_as_new(self):
        importer = ForkAwareImporter()
        import_all(importer, old_branch())
        importer.apply_branch(self.new_branch())
        # 旧分支批次已回滚，不再是「已应用」；其父哈希不在当前分支上
        with self.assertRaises(BlockLinkMismatchError):
            importer.import_batch(old_branch()[1])

    def test_pure_append_branch_rolls_back_nothing(self):
        importer = ForkAwareImporter()
        import_all(importer, old_branch())
        result = importer.apply_branch([
            batch(4, 4, "0xb4", "0xb3", [
                tx("h5", 4, 400, "alice", "bob", "transfer", "50"),
            ]),
        ])
        self.assertEqual(result["rolled_back_block_count"], 0)
        self.assertEqual(result["applied_block_count"], 1)
        self.assertEqual(result["tip_block_hash"], "0xb4")
        self.assertEqual(importer.query(normalize_filters())["total"], 5)

    def test_fork_requires_higher_tip(self):
        importer = ForkAwareImporter()
        import_all(importer, old_branch())
        before = importer.query(normalize_filters())
        # 新分支末端高度 3 不高于当前末端 3
        not_higher = [
            batch(10, 2, "0xc2", "0xb1", []),
            batch(11, 3, "0xc3", "0xc2", []),
        ]
        with self.assertRaises(BlockLinkMismatchError):
            importer.apply_branch(not_higher)
        self.assertEqual(importer.query(normalize_filters()), before)
        self.assertEqual(importer.tip_block_hash, "0xb3")

    def test_unknown_fork_point_raises(self):
        importer = ForkAwareImporter()
        import_all(importer, old_branch())
        before = importer.query(normalize_filters())
        orphan = [batch(10, 9, "0xc9", "0xunknown", [])]
        with self.assertRaises(BlockLinkMismatchError):
            importer.apply_branch(orphan)
        self.assertEqual(importer.query(normalize_filters()), before)

    def test_duplicate_block_hash_in_branch_rejected(self):
        importer = ForkAwareImporter()
        import_all(importer, old_branch())
        before = importer.query(normalize_filters())
        dup = [
            batch(10, 2, "0xc2", "0xb1", []),
            batch(11, 3, "0xc2", "0xc2", []),
            batch(12, 4, "0xc4", "0xc2", []),
        ]
        with self.assertRaises(InvalidImportBatchError):
            importer.apply_branch(dup)
        self.assertEqual(importer.query(normalize_filters()), before)

    def test_invalid_branch_structure_rejected(self):
        importer = ForkAwareImporter()
        with self.assertRaises(InvalidImportBatchError):
            importer.apply_branch([])
        with self.assertRaises(InvalidImportBatchError):
            importer.apply_branch("not-a-list")
        with self.assertRaises(InvalidImportBatchError):
            importer.apply_branch([batch(0, 1, "0xb1", "0xg", [])])

    def test_broken_internal_link_raises(self):
        importer = ForkAwareImporter()
        import_all(importer, old_branch())
        broken = [
            batch(10, 2, "0xc2", "0xb1", []),
            batch(11, 3, "0xc3", "0xwrong", []),
        ]
        with self.assertRaises(BlockLinkMismatchError):
            importer.apply_branch(broken)
        self.assertEqual(importer.tip_block_hash, "0xb3")

    def test_rollback_dedups_within_current_branch(self):
        importer = ForkAwareImporter()
        import_all(importer, old_branch())
        # 新分支包含与保留前缀相同哈希的交易（h1）：只保留一份
        branch = [
            batch(10, 2, "0xc2", "0xb1", [
                tx("h1", 2, 110, "alice", "bob", "transfer", "10"),
                tx("x1", 2, 120, "carol", "alice", "transfer", "7"),
                tx("x1", 2, 120, "carol", "alice", "transfer", "7"),
            ]),
            batch(11, 3, "0xc3", "0xc2", []),
            batch(12, 4, "0xc4", "0xc3", []),
        ]
        result = importer.apply_branch(branch)
        self.assertEqual(result["rolled_back_block_count"], 2)
        self.assertEqual(result["applied_block_count"], 3)
        page = importer.query(normalize_filters())
        # h1 仍只有一份（保留前缀中的原记录），x1 只写入一次
        self.assertEqual(hashes(page), ["h1", "h2", "x1"])
        stats = importer.stats(normalize_filters())
        self.assertEqual(stats["total_count"], 3)
        self.assertEqual(stats["total_amount"], "37")

    def test_rolled_back_transaction_hash_can_reappear(self):
        importer = ForkAwareImporter()
        import_all(importer, old_branch())
        # 旧分支 h3 被回滚后，新分支可以再次使用同一哈希
        branch = [
            batch(10, 2, "0xc2", "0xb1", [
                tx("h3", 2, 110, "alice", "carol", "transfer", "30"),
            ]),
            batch(11, 3, "0xc3", "0xc2", []),
            batch(12, 4, "0xc4", "0xc3", []),
        ]
        result = importer.apply_branch(branch)
        self.assertEqual(result["rolled_back_transaction_count"], 2)
        page = importer.query(normalize_filters())
        self.assertEqual(sorted(hashes(page)), ["h1", "h2", "h3"])
        h3 = [t for t in page["transactions"] if t["tx_hash"] == "h3"][0]
        self.assertEqual(h3["block_number"], 2)
        self.assertEqual(h3["timestamp"], 110)

    def test_fork_on_empty_engine_applies_branch(self):
        importer = ForkAwareImporter()
        result = importer.apply_branch([
            batch(1, 1, "0xb1", "0xgenesis", [
                tx("h1", 1, 10, "alice", "bob", "transfer", "10"),
            ]),
            batch(2, 2, "0xb2", "0xb1", []),
        ])
        self.assertEqual(result["rolled_back_block_count"], 0)
        self.assertEqual(result["applied_block_count"], 2)
        self.assertEqual(result["tip_block_hash"], "0xb2")


class ForkCursorTest(unittest.TestCase):
    def test_cursor_to_rolled_back_transaction_raises(self):
        importer = ForkAwareImporter()
        import_all(importer, old_branch())
        filters = normalize_filters()
        page1 = importer.query(filters, page_size=3)
        self.assertEqual(hashes(page1), ["h1", "h2", "h3"])
        cursor = page1["next_cursor"]
        self.assertIsNotNone(cursor)

        # 分叉修正回滚了 h2 所在区块（b2、b3 被取代）
        importer.apply_branch([
            batch(10, 2, "0xc2", "0xb1", [
                tx("x1", 2, 110, "carol", "alice", "transfer", "7"),
            ]),
            batch(11, 3, "0xc3", "0xc2", []),
            batch(12, 4, "0xc4", "0xc3", []),
        ])
        with self.assertRaises(CursorOutOfRangeError):
            importer.query(filters, page_size=2, cursor=cursor)

    def test_cursor_to_surviving_transaction_still_works(self):
        importer = ForkAwareImporter()
        import_all(importer, old_branch())
        filters = normalize_filters()
        page1 = importer.query(filters, page_size=1)
        self.assertEqual(hashes(page1), ["h1"])
        cursor = page1["next_cursor"]

        importer.apply_branch([
            batch(10, 2, "0xc2", "0xb1", [
                tx("x1", 2, 110, "carol", "alice", "transfer", "7"),
            ]),
            batch(11, 3, "0xc3", "0xc2", []),
            batch(12, 4, "0xc4", "0xc3", []),
        ])
        page2 = importer.query(filters, page_size=100, cursor=cursor)
        self.assertEqual(hashes(page2), ["h2", "x1"])
        self.assertIsNone(page2["next_cursor"])

    def test_cursor_filter_mismatch_still_invalid_cursor(self):
        importer = ForkAwareImporter()
        import_all(importer, old_branch())
        page1 = importer.query(normalize_filters(), page_size=1)
        cursor = page1["next_cursor"]
        with self.assertRaises(InvalidCursorError):
            importer.query(normalize_filters(method="transfer"), cursor=cursor)

    def test_pagination_stable_without_fork(self):
        importer = ForkAwareImporter()
        import_all(importer, old_branch())
        filters = normalize_filters()
        seen = []
        cursor = None
        while True:
            page = importer.query(filters, page_size=3, cursor=cursor)
            seen.extend(hashes(page))
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(seen, ["h1", "h2", "h3", "h4"])


class ExistingBehaviorTest(unittest.TestCase):
    def test_underlying_indexer_entries_unchanged(self):
        importer = ForkAwareImporter()
        import_all(importer, old_branch())
        indexer = importer.indexer
        filters = normalize_filters()
        self.assertEqual(indexer.stats(filters)["total_amount"], "100")
        method_groups = indexer.method_stats(filters)["groups"]
        self.assertEqual(
            {g["method"] for g in method_groups}, {"transfer", "approve"}
        )
        status = indexer.status_stats(filters)
        self.assertEqual(status["failure_count"], 1)
        self.assertEqual(status["failure_amount"], "20")


if __name__ == "__main__":
    unittest.main()
