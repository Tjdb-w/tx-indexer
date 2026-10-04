"""增量导入与断点续传测试。"""

import unittest

from tx_indexer.engine import TxIndexer, normalize_filters
from tx_indexer.errors import InvalidCursorError
from tx_indexer.importer import (
    BLOCK_CONFLICT,
    IMPORT_CURSOR_MISMATCH,
    INVALID_IMPORT_BATCH,
    TX_CONFLICT,
    IncrementalImporter,
)


def tx(tx_hash, block_number, block_hash, timestamp, frm, to, method,
       amount, fee="1", success=True):
    return {
        "tx_hash": tx_hash,
        "block_number": block_number,
        "block_hash": block_hash,
        "timestamp": timestamp,
        "from_address": frm,
        "to_address": to,
        "method": method,
        "amount": amount,
        "fee": fee,
        "success": success,
    }


def batch(chain_id, block_number, block_hash, transactions):
    return {
        "chain_id": chain_id,
        "block_number": block_number,
        "block_hash": block_hash,
        "transactions": transactions,
    }


def sample_batches(chain="chain-a"):
    """三个连续批次，共 6 笔交易。"""
    return [
        batch(chain, 1, "bh1", [
            tx("h1", 1, "bh1", 10, "alice", "bob", "transfer", "10"),
            tx("h2", 1, "bh1", 20, "bob", "alice", "approve", "20"),
        ]),
        batch(chain, 2, "bh2", [
            tx("h3", 2, "bh2", 100, "alice", "carol", "transfer", "30"),
            tx("h4", 2, "bh2", 120, "carol", "carol", "transfer", "5"),
        ]),
        batch(chain, 3, "bh3", [
            tx("h5", 3, "bh3", 300, "bob", "dave", "transfer", "40"),
            tx("h6", 3, "bh3", 300, "dave", "alice", "approve", "7"),
        ]),
    ]


def hashes(page):
    return [t["tx_hash"] for t in page["transactions"]]


class ImportResultTest(unittest.TestCase):
    def test_import_success_result_fields(self):
        imp = IncrementalImporter()
        result = imp.import_batch(sample_batches()[0])
        self.assertEqual(result["status"], "imported")
        self.assertEqual(result["chain_id"], "chain-a")
        self.assertEqual(result["imported_count"], 2)
        self.assertEqual(result["skipped_count"], 0)
        self.assertEqual(result["confirmed_block_number"], 1)
        self.assertEqual(result["confirmed_block_hash"], "bh1")
        self.assertIsInstance(result["next_import_cursor"], str)

    def test_import_advances_confirmed_height(self):
        imp = IncrementalImporter()
        for i, b in enumerate(sample_batches()):
            result = imp.import_batch(b)
            self.assertEqual(result["status"], "imported")
            self.assertEqual(result["confirmed_block_number"], i + 1)
            self.assertEqual(result["confirmed_block_hash"], "bh%d" % (i + 1))

    def test_imported_transactions_immediately_visible(self):
        imp = IncrementalImporter()
        imp.import_batch(sample_batches()[0])
        result = imp.indexer.query(normalize_filters())
        self.assertEqual(hashes(result), ["h1", "h2"])
        imp.import_batch(sample_batches()[1])
        result = imp.indexer.query(normalize_filters())
        self.assertEqual(hashes(result), ["h1", "h2", "h3", "h4"])

    def test_empty_block_allowed(self):
        imp = IncrementalImporter()
        result = imp.import_batch(batch("chain-a", 7, "bh7", []))
        self.assertEqual(result["status"], "imported")
        self.assertEqual(result["imported_count"], 0)
        self.assertEqual(result["confirmed_block_number"], 7)

    def test_first_batch_may_start_at_any_height(self):
        imp = IncrementalImporter()
        result = imp.import_batch(batch("chain-a", 100, "bh100", [
            tx("h1", 100, "bh100", 10, "alice", "bob", "transfer", "10"),
        ]))
        self.assertEqual(result["status"], "imported")
        self.assertEqual(result["confirmed_block_number"], 100)


class InvalidBatchTest(unittest.TestCase):
    def setUp(self):
        self.imp = IncrementalImporter()

    def assert_invalid(self, b):
        result = self.imp.import_batch(b)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error_code"], INVALID_IMPORT_BATCH)
        self.assertIn("message", result)

    def test_batch_not_dict(self):
        self.assert_invalid(None)
        self.assert_invalid([1, 2])

    def test_missing_chain_id(self):
        self.assert_invalid({"block_number": 1, "block_hash": "bh1",
                             "transactions": []})

    def test_blank_chain_id(self):
        self.assert_invalid(batch("  ", 1, "bh1", []))

    def test_missing_block_number(self):
        self.assert_invalid({"chain_id": "c", "block_hash": "bh1",
                             "transactions": []})

    def test_negative_block_number(self):
        self.assert_invalid(batch("c", -1, "bh1", []))

    def test_bool_block_number(self):
        self.assert_invalid(batch("c", True, "bh1", []))

    def test_missing_block_hash(self):
        self.assert_invalid({"chain_id": "c", "block_number": 1,
                             "transactions": []})

    def test_transactions_not_list(self):
        self.assert_invalid(batch("c", 1, "bh1", None))
        self.assert_invalid(batch("c", 1, "bh1", {}))

    def test_tx_missing_required_field(self):
        bad = tx("h1", 1, "bh1", 10, "alice", "bob", "transfer", "10")
        del bad["fee"]
        self.assert_invalid(batch("c", 1, "bh1", [bad]))

    def test_tx_block_mismatch(self):
        bad = tx("h1", 2, "bh1", 10, "alice", "bob", "transfer", "10")
        self.assert_invalid(batch("c", 1, "bh1", [bad]))
        bad = tx("h1", 1, "other", 10, "alice", "bob", "transfer", "10")
        self.assert_invalid(batch("c", 1, "bh1", [bad]))

    def test_tx_bad_fee_and_success(self):
        bad = tx("h1", 1, "bh1", 10, "alice", "bob", "transfer", "10",
                 fee="-1")
        self.assert_invalid(batch("c", 1, "bh1", [bad]))
        bad = tx("h1", 1, "bh1", 10, "alice", "bob", "transfer", "10",
                 fee="1.5")
        self.assert_invalid(batch("c", 1, "bh1", [bad]))
        bad = tx("h1", 1, "bh1", 10, "alice", "bob", "transfer", "10",
                 success="yes")
        self.assert_invalid(batch("c", 1, "bh1", [bad]))

    def test_rejected_batch_leaves_no_state(self):
        bad = tx("h1", 1, "bh1", 10, "alice", "bob", "transfer", "10",
                 fee="x")
        self.imp.import_batch(batch("c", 1, "bh1", [bad]))
        self.assertIsNone(self.imp.confirmed_block_number)
        self.assertEqual(self.imp.indexer.query(normalize_filters())["total"], 0)


class CursorMismatchTest(unittest.TestCase):
    def test_height_gap_rejected(self):
        imp = IncrementalImporter()
        imp.import_batch(sample_batches()[0])
        result = imp.import_batch(sample_batches()[2])  # 高度 3，期望 2
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error_code"], IMPORT_CURSOR_MISMATCH)

    def test_chain_mismatch_rejected(self):
        imp = IncrementalImporter()
        imp.import_batch(sample_batches()[0])
        result = imp.import_batch(batch("chain-b", 2, "bh2", []))
        self.assertEqual(result["error_code"], IMPORT_CURSOR_MISMATCH)

    def test_height_before_import_start_rejected(self):
        imp = IncrementalImporter()
        imp.import_batch(batch("c", 10, "bh10", []))
        result = imp.import_batch(batch("c", 9, "bh9", []))
        self.assertEqual(result["error_code"], IMPORT_CURSOR_MISMATCH)

    def test_stale_cursor_rejected(self):
        imp = IncrementalImporter()
        cursor1 = imp.import_batch(sample_batches()[0])["next_import_cursor"]
        imp.import_batch(sample_batches()[1])
        # 用上一批的旧游标导入第三批：游标与当前状态不一致
        result = imp.import_batch(sample_batches()[2], cursor=cursor1)
        self.assertEqual(result["error_code"], IMPORT_CURSOR_MISMATCH)

    def test_fresh_cursor_accepted(self):
        imp = IncrementalImporter()
        cursor = None
        for b in sample_batches():
            result = imp.import_batch(b, cursor=cursor)
            self.assertEqual(result["status"], "imported")
            cursor = result["next_import_cursor"]

    def test_garbage_cursor_rejected(self):
        imp = IncrementalImporter()
        imp.import_batch(sample_batches()[0])
        for bad in ("", "not-a-cursor", "eyJ2IjoxfQ"):
            result = imp.import_batch(sample_batches()[1], cursor=bad)
            self.assertEqual(result["error_code"], IMPORT_CURSOR_MISMATCH)

    def test_cursor_on_empty_state_rejected(self):
        imp = IncrementalImporter()
        cursor = IncrementalImporter().import_batch(
            sample_batches()[0])["next_import_cursor"]
        result = imp.import_batch(sample_batches()[0], cursor=cursor)
        self.assertEqual(result["error_code"], IMPORT_CURSOR_MISMATCH)


class BlockConflictTest(unittest.TestCase):
    def test_same_height_different_hash(self):
        imp = IncrementalImporter()
        imp.import_batch(sample_batches()[0])
        result = imp.import_batch(batch("chain-a", 1, "other-hash", [
            tx("h1", 1, "other-hash", 10, "alice", "bob", "transfer", "10"),
            tx("h2", 1, "other-hash", 20, "bob", "alice", "approve", "20"),
        ]))
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error_code"], BLOCK_CONFLICT)

    def test_same_hash_different_tx_set(self):
        imp = IncrementalImporter()
        imp.import_batch(sample_batches()[0])
        # 同高度同哈希，但交易集合变化（多一笔）
        result = imp.import_batch(batch("chain-a", 1, "bh1", [
            tx("h1", 1, "bh1", 10, "alice", "bob", "transfer", "10"),
            tx("h2", 1, "bh1", 20, "bob", "alice", "approve", "20"),
            tx("h9", 1, "bh1", 30, "alice", "bob", "transfer", "1"),
        ]))
        self.assertEqual(result["error_code"], BLOCK_CONFLICT)
        # 子集也算变化
        result = imp.import_batch(batch("chain-a", 1, "bh1", [
            tx("h1", 1, "bh1", 10, "alice", "bob", "transfer", "10"),
        ]))
        self.assertEqual(result["error_code"], BLOCK_CONFLICT)

    def test_same_hash_different_height(self):
        imp = IncrementalImporter()
        imp.import_batch(sample_batches()[0])
        result = imp.import_batch(batch("chain-a", 2, "bh1", []))
        self.assertEqual(result["error_code"], BLOCK_CONFLICT)

    def test_block_conflict_leaves_no_state(self):
        imp = IncrementalImporter()
        imp.import_batch(sample_batches()[0])
        imp.import_batch(batch("chain-a", 1, "bh1", [
            tx("h1", 1, "bh1", 10, "alice", "bob", "transfer", "10"),
        ]))
        self.assertEqual(imp.confirmed_block_number, 1)
        self.assertEqual(imp.confirmed_block_hash, "bh1")
        self.assertEqual(
            imp.indexer.query(normalize_filters())["total"], 2
        )


class TxConflictTest(unittest.TestCase):
    def test_conflicting_field_rejects_whole_batch(self):
        imp = IncrementalImporter()
        imp.import_batch(sample_batches()[0])
        # 新高度，但 h3 与已存在的 h1 同哈希不同字段
        result = imp.import_batch(batch("chain-a", 2, "bh2", [
            tx("h1", 2, "bh2", 10, "alice", "bob", "transfer", "10"),
        ]))
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error_code"], TX_CONFLICT)
        # 整批拒绝：索引没有任何变化
        self.assertEqual(imp.confirmed_block_number, 1)
        self.assertEqual(
            imp.indexer.query(normalize_filters())["total"], 2
        )

    def test_each_query_relevant_field_participates(self):
        base = tx("h1", 1, "bh1", 10, "alice", "bob", "transfer", "10")
        variants = [
            dict(base, timestamp=11),
            dict(base, from_address="mallory"),
            dict(base, to_address="mallory"),
            dict(base, method="approve"),
            dict(base, amount="11"),
            dict(base, fee="2"),
            dict(base, success=False),
        ]
        for variant in variants:
            imp = IncrementalImporter()
            imp.import_batch(batch("c", 1, "bh1", [base]))
            moved = dict(variant, block_number=2, block_hash="bh2")
            result = imp.import_batch(batch("c", 2, "bh2", [moved]))
            self.assertEqual(result["error_code"], TX_CONFLICT,
                             "variant %r 应触发 TX_CONFLICT" % variant)

    def test_identical_tx_in_new_block_rejected_as_batch_invalid(self):
        # 交易的区块字段必须与批次一致；与已导入完全相同的交易只可能
        # 通过重放原批次再次出现（幂等重试路径），不能混入新区块
        imp = IncrementalImporter()
        imp.import_batch(sample_batches()[0])
        result = imp.import_batch(batch("chain-a", 2, "bh2", [
            tx("h1", 1, "bh1", 10, "alice", "bob", "transfer", "10"),
        ]))
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error_code"], INVALID_IMPORT_BATCH)

    def test_duplicate_within_batch_identical(self):
        imp = IncrementalImporter()
        t = tx("h1", 1, "bh1", 10, "alice", "bob", "transfer", "10")
        result = imp.import_batch(batch("c", 1, "bh1", [dict(t), dict(t)]))
        self.assertEqual(result["status"], "imported")
        self.assertEqual(result["imported_count"], 1)
        self.assertEqual(result["skipped_count"], 1)

    def test_duplicate_within_batch_conflicting(self):
        imp = IncrementalImporter()
        t1 = tx("h1", 1, "bh1", 10, "alice", "bob", "transfer", "10")
        t2 = tx("h1", 1, "bh1", 10, "alice", "bob", "transfer", "11")
        result = imp.import_batch(batch("c", 1, "bh1", [t1, t2]))
        self.assertEqual(result["error_code"], TX_CONFLICT)
        self.assertEqual(
            imp.indexer.query(normalize_filters())["total"], 0
        )

    def test_existing_data_not_overwritten(self):
        imp = IncrementalImporter()
        imp.import_batch(sample_batches()[0])
        # 重放同一批次（幂等重试）后，已存在数据保持原值
        imp.import_batch(sample_batches()[0])
        imp.import_batch(sample_batches()[1])
        result = imp.indexer.query(normalize_filters())
        h1 = [t for t in result["transactions"] if t["tx_hash"] == "h1"]
        self.assertEqual(len(h1), 1)
        self.assertEqual(h1[0]["block_number"], 1)
        self.assertEqual(h1[0]["amount"], "10")


class IdempotentRetryTest(unittest.TestCase):
    def test_same_batch_retry_is_idempotent(self):
        imp = IncrementalImporter()
        first = imp.import_batch(sample_batches()[0])
        retry = imp.import_batch(sample_batches()[0])
        self.assertEqual(retry["status"], "imported")
        self.assertEqual(retry["imported_count"], 0)
        self.assertEqual(retry["skipped_count"], 2)
        # 游标与确认高度不因重试改变
        self.assertEqual(retry["next_import_cursor"],
                         first["next_import_cursor"])
        self.assertEqual(retry["confirmed_block_number"], 1)
        self.assertEqual(
            imp.indexer.query(normalize_filters())["total"], 2
        )

    def test_retry_after_further_imports(self):
        imp = IncrementalImporter()
        for b in sample_batches():
            imp.import_batch(b)
        # 继续导入后重放第一批：仍然幂等，确认高度不回退
        result = imp.import_batch(sample_batches()[0])
        self.assertEqual(result["status"], "imported")
        self.assertEqual(result["imported_count"], 0)
        self.assertEqual(result["skipped_count"], 2)
        self.assertEqual(result["confirmed_block_number"], 3)
        self.assertEqual(result["confirmed_block_hash"], "bh3")
        self.assertEqual(
            imp.indexer.query(normalize_filters())["total"], 6
        )

    def test_restart_reimport_from_scratch_is_reproducible(self):
        """同一输入序列在全新导入器上重放，结果与游标完全一致。"""
        imp1 = IncrementalImporter()
        results1 = [imp1.import_batch(b) for b in sample_batches()]
        imp2 = IncrementalImporter()
        results2 = [imp2.import_batch(b) for b in sample_batches()]
        self.assertEqual(results1, results2)
        q1 = imp1.indexer.query(normalize_filters())
        q2 = imp2.indexer.query(normalize_filters())
        self.assertEqual(q1, q2)


class QueryEquivalenceTest(unittest.TestCase):
    """从空索引一次性导入与分批逐步导入，查询结果必须一致。"""

    def setUp(self):
        merged = []
        for b in sample_batches():
            merged.extend(b["transactions"])
        # 逐块导入
        self.stepwise = IncrementalImporter()
        for b in sample_batches():
            self.stepwise.import_batch(b)
        # 等价记录直接进引擎（模拟一次性加载）
        records = [
            {
                "tx_hash": t["tx_hash"],
                "block_number": t["block_number"],
                "timestamp": t["timestamp"],
                "from_address": t["from_address"],
                "to_address": t["to_address"],
                "method": t["method"],
                "amount": t["amount"],
            }
            for t in merged
        ]
        self.baseline = TxIndexer(records)

    def test_query_and_filters_match_baseline(self):
        filter_sets = [
            normalize_filters(),
            normalize_filters(address="alice"),
            normalize_filters(from_address=["bob", "carol"]),
            normalize_filters(to_address="alice", method="transfer"),
            normalize_filters(method=["approve", "transfer"]),
            normalize_filters(start_time=20, end_time=300),
            normalize_filters(address="carol", start_time=100),
        ]
        for filters in filter_sets:
            self.assertEqual(
                self.stepwise.indexer.query(filters),
                self.baseline.query(filters),
            )

    def test_pagination_order_matches_baseline(self):
        filters = normalize_filters()
        for page_size in (1, 2, 5):
            collected = {}
            for name, idx in (("stepwise", self.stepwise.indexer),
                              ("baseline", self.baseline)):
                out = []
                cursor = None
                while True:
                    page = idx.query(filters, page_size=page_size,
                                     cursor=cursor)
                    out.extend(hashes(page))
                    cursor = page["next_cursor"]
                    if cursor is None:
                        break
                collected[name] = out
            self.assertEqual(collected["stepwise"], collected["baseline"])

    def test_aggregates_match_baseline(self):
        filters = normalize_filters()
        self.assertEqual(
            self.stepwise.indexer.stats(filters), self.baseline.stats(filters)
        )
        self.assertEqual(
            self.stepwise.indexer.method_stats(filters),
            self.baseline.method_stats(filters),
        )
        self.assertEqual(
            self.stepwise.indexer.address_stats(filters),
            self.baseline.address_stats(filters),
        )
        self.assertEqual(
            self.stepwise.indexer.counterparty_stats(
                normalize_filters(address="alice")
            ),
            self.baseline.counterparty_stats(
                normalize_filters(address="alice")
            ),
        )
        self.assertEqual(
            self.stepwise.indexer.time_stats(filters, 60),
            self.baseline.time_stats(filters, 60),
        )
        self.assertEqual(
            self.stepwise.indexer.pair_stats(filters),
            self.baseline.pair_stats(filters),
        )
        self.assertEqual(
            self.stepwise.indexer.address_time_stats(filters, 60),
            self.baseline.address_time_stats(filters, 60),
        )

    def test_query_output_keeps_original_public_fields(self):
        result = self.stepwise.indexer.query(normalize_filters())
        for t in result["transactions"]:
            self.assertEqual(
                set(t.keys()),
                {"tx_hash", "block_number", "timestamp", "from_address",
                 "to_address", "method", "amount"},
            )


class CursorIndependenceTest(unittest.TestCase):
    def test_import_cursor_rejected_by_pagination(self):
        imp = IncrementalImporter()
        result = imp.import_batch(sample_batches()[0])
        with self.assertRaises(InvalidCursorError):
            imp.indexer.query(normalize_filters(),
                              cursor=result["next_import_cursor"])

    def test_pagination_cursor_rejected_by_import(self):
        imp = IncrementalImporter()
        imp.import_batch(sample_batches()[0])
        page = imp.indexer.query(normalize_filters(), page_size=1)
        self.assertIsNotNone(page["next_cursor"])
        result = imp.import_batch(sample_batches()[1],
                                  cursor=page["next_cursor"])
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error_code"], IMPORT_CURSOR_MISMATCH)

    def test_pagination_unaffected_by_imports(self):
        """导入新批次后，旧分页游标语义不变（keyset 续页不跳过不重复）。"""
        imp = IncrementalImporter()
        imp.import_batch(sample_batches()[0])
        filters = normalize_filters()
        page1 = imp.indexer.query(filters, page_size=1)
        imp.import_batch(sample_batches()[1])
        imp.import_batch(sample_batches()[2])
        # 用导入前签发的游标继续翻页：marker 仍按排序键定位
        page2 = imp.indexer.query(filters, page_size=100,
                                  cursor=page1["next_cursor"])
        self.assertEqual(hashes(page2), ["h2", "h3", "h4", "h5", "h6"])


if __name__ == "__main__":
    unittest.main()
