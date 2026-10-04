"""增量交易导入与断点续传测试。"""

import json
import unittest

from tx_indexer.cursor import encode_cursor, encode_import_cursor
from tx_indexer.engine import TxIndexer, normalize_filters
from tx_indexer.errors import InvalidCursorError
from tx_indexer.importer import (
    BLOCK_CONFLICT,
    IMPORT_CURSOR_MISMATCH,
    INVALID_IMPORT_BATCH,
    INVALID_REPLACEMENT_BATCH,
    TX_CONFLICT,
    IncrementalImporter,
)
from tx_indexer.loader import load_lines


def tx(tx_hash, block_number, block_hash, timestamp, frm, to, method,
       amount, fee, success):
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


def batch(chain_id, start_block, block_hash, transactions):
    return {
        "chain_id": chain_id,
        "start_block": start_block,
        "block_hash": block_hash,
        "transactions": transactions,
    }


def sample_batches():
    """三个连续区块的样例批次。"""
    return [
        batch("chain-a", 1, "0xb1", [
            tx("h1", 1, "0xb1", 10, "alice", "bob", "transfer", "10", "1",
               True),
            tx("h2", 1, "0xb1", 20, "bob", "alice", "approve", "20", "2",
               True),
        ]),
        batch("chain-a", 2, "0xb2", [
            tx("h3", 2, "0xb2", 100, "alice", "carol", "transfer", "30",
               "3", False),
        ]),
        batch("chain-a", 3, "0xb3", [
            tx("h4", 3, "0xb3", 300, "bob", "dave", "transfer", "40", "4",
               True),
        ]),
    ]


def import_all(importer, batches):
    """顺序导入并串起导入游标，返回最后一个结果。"""
    cursor = None
    result = None
    for b in batches:
        result = importer.import_batch(b, cursor=cursor)
        assert result["status"] == "ok", result
        cursor = result["next_import_cursor"]
    return result


def hashes(page):
    return [t["tx_hash"] for t in page["transactions"]]


class ImportResultTest(unittest.TestCase):
    def test_first_batch_result_fields(self):
        importer = IncrementalImporter()
        result = importer.import_batch(sample_batches()[0])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["imported_count"], 2)
        self.assertEqual(result["skipped_count"], 0)
        self.assertEqual(result["confirmed_block_height"], 1)
        self.assertEqual(result["confirmed_block_hash"], "0xb1")
        self.assertIsInstance(result["next_import_cursor"], str)

    def test_sequential_import_advances_confirmed(self):
        importer = IncrementalImporter()
        result = import_all(importer, sample_batches())
        self.assertEqual(result["confirmed_block_height"], 3)
        self.assertEqual(result["confirmed_block_hash"], "0xb3")
        self.assertEqual(importer.chain_id, "chain-a")
        self.assertEqual(importer.confirmed_block_height, 3)
        self.assertEqual(importer.confirmed_block_hash, "0xb3")

    def test_query_observes_batch_immediately(self):
        importer = IncrementalImporter()
        importer.import_batch(sample_batches()[0])
        page = importer.indexer.query(normalize_filters())
        self.assertEqual(hashes(page), ["h1", "h2"])
        stats = importer.indexer.stats(normalize_filters())
        self.assertEqual(stats["total_count"], 2)
        self.assertEqual(stats["total_amount"], "30")

    def test_query_output_keeps_original_fields(self):
        importer = IncrementalImporter()
        importer.import_batch(sample_batches()[0])
        page = importer.indexer.query(normalize_filters())
        self.assertEqual(
            set(page["transactions"][0].keys()),
            {"tx_hash", "block_number", "timestamp", "from_address",
             "to_address", "method", "amount"},
        )

    def test_empty_block_advances_confirmed(self):
        importer = IncrementalImporter()
        result = importer.import_batch(batch("chain-a", 7, "0xb7", []))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["imported_count"], 0)
        self.assertEqual(result["confirmed_block_height"], 7)
        self.assertEqual(result["confirmed_block_hash"], "0xb7")


class RetryTest(unittest.TestCase):
    def test_retry_same_batch_skips_all(self):
        importer = IncrementalImporter()
        batches = sample_batches()
        cursor = import_all(importer, batches[:1])["next_import_cursor"]
        retry = importer.import_batch(batches[0], cursor=cursor)
        self.assertEqual(retry["status"], "ok")
        self.assertEqual(retry["imported_count"], 0)
        self.assertEqual(retry["skipped_count"], 2)
        self.assertEqual(retry["confirmed_block_height"], 1)
        # 聚合不重复累计
        stats = importer.indexer.stats(normalize_filters())
        self.assertEqual(stats["total_count"], 2)
        self.assertEqual(stats["total_amount"], "30")

    def test_retry_older_block_after_advancing(self):
        importer = IncrementalImporter()
        batches = sample_batches()
        cursor = import_all(importer, batches)["next_import_cursor"]
        retry = importer.import_batch(batches[1], cursor=cursor)
        self.assertEqual(retry["status"], "ok")
        self.assertEqual(retry["imported_count"], 0)
        self.assertEqual(retry["skipped_count"], 1)
        # 已确认高度保持在链尖
        self.assertEqual(retry["confirmed_block_height"], 3)
        self.assertEqual(retry["confirmed_block_hash"], "0xb3")
        stats = importer.indexer.stats(normalize_filters())
        self.assertEqual(stats["total_count"], 4)

    def test_duplicate_does_not_overwrite_existing(self):
        importer = IncrementalImporter()
        batches = sample_batches()
        cursor = import_all(importer, batches[:1])["next_import_cursor"]
        importer.import_batch(batches[0], cursor=cursor)
        page = importer.indexer.query(normalize_filters())
        self.assertEqual(page["transactions"][0]["amount"], "10")

    def test_replay_from_empty_gives_same_results(self):
        batches = sample_batches()
        first = IncrementalImporter()
        import_all(first, batches)
        # 模拟进程重启：从空索引重放同一批序列
        second = IncrementalImporter()
        import_all(second, batches)

        for filters in (
            normalize_filters(),
            normalize_filters(address="alice"),
            normalize_filters(method="transfer"),
            normalize_filters(start_time=50, end_time=500),
        ):
            self.assertEqual(
                first.indexer.query(filters), second.indexer.query(filters)
            )
            self.assertEqual(
                first.indexer.stats(filters), second.indexer.stats(filters)
            )
            self.assertEqual(
                first.indexer.method_stats(filters),
                second.indexer.method_stats(filters),
            )

    def test_incremental_import_matches_bulk_load(self):
        batches = sample_batches()
        importer = IncrementalImporter()
        import_all(importer, batches)

        lines = [
            json.dumps({
                "tx_hash": t["tx_hash"],
                "block_number": t["block_number"],
                "timestamp": t["timestamp"],
                "from_address": t["from_address"],
                "to_address": t["to_address"],
                "method": t["method"],
                "amount": t["amount"],
            })
            for b in batches
            for t in b["transactions"]
        ]
        bulk = TxIndexer(load_lines(lines))

        filters = normalize_filters()
        self.assertEqual(
            importer.indexer.query(filters), bulk.query(filters)
        )
        self.assertEqual(importer.indexer.stats(filters), bulk.stats(filters))
        self.assertEqual(
            importer.indexer.address_stats(filters),
            bulk.address_stats(filters),
        )
        self.assertEqual(
            importer.indexer.time_stats(filters, 60),
            bulk.time_stats(filters, 60),
        )


class InvalidBatchTest(unittest.TestCase):
    def setUp(self):
        self.importer = IncrementalImporter()

    def assert_invalid(self, b):
        result = self.importer.import_batch(b)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error_code"], INVALID_IMPORT_BATCH)
        self.assertIn("message", result)

    def test_missing_batch_fields(self):
        good = sample_batches()[0]
        for field in ("chain_id", "start_block", "block_hash",
                      "transactions"):
            bad = {k: v for k, v in good.items() if k != field}
            self.assert_invalid(bad)

    def test_not_a_dict(self):
        self.assert_invalid([1, 2, 3])

    def test_blank_chain_id(self):
        self.assert_invalid(batch("  ", 1, "0xb1", []))

    def test_negative_start_block(self):
        self.assert_invalid(batch("chain-a", -1, "0xb1", []))

    def test_bool_start_block(self):
        self.assert_invalid(batch("chain-a", True, "0xb1", []))

    def test_transactions_not_a_list(self):
        self.assert_invalid(batch("chain-a", 1, "0xb1", {}))

    def test_missing_tx_fields(self):
        good = tx("h1", 1, "0xb1", 10, "alice", "bob", "transfer", "10",
                  "1", True)
        for field in (
            "tx_hash", "block_number", "block_hash", "timestamp",
            "from_address", "to_address", "method", "amount", "fee",
            "success",
        ):
            bad = {k: v for k, v in good.items() if k != field}
            self.assert_invalid(batch("chain-a", 1, "0xb1", [bad]))

    def test_tx_block_mismatch(self):
        bad_height = tx("h1", 2, "0xb1", 10, "alice", "bob", "transfer",
                        "10", "1", True)
        self.assert_invalid(batch("chain-a", 1, "0xb1", [bad_height]))
        bad_hash = tx("h1", 1, "0xb2", 10, "alice", "bob", "transfer",
                      "10", "1", True)
        self.assert_invalid(batch("chain-a", 1, "0xb1", [bad_hash]))

    def test_bad_tx_field_types(self):
        base = tx("h1", 1, "0xb1", 10, "alice", "bob", "transfer", "10",
                  "1", True)
        for field, value in (
            ("amount", 10), ("amount", "-1"), ("amount", "1.5"),
            ("fee", "1.5"), ("fee", 0),
            ("success", 1), ("success", "true"),
            ("timestamp", -1), ("from_address", "  "),
        ):
            bad = dict(base, **{field: value})
            self.assert_invalid(batch("chain-a", 1, "0xb1", [bad]))

    def test_invalid_batch_leaves_no_state(self):
        self.assert_invalid(batch("chain-a", 1, "0xb1", [{}]))
        result = self.importer.import_batch(sample_batches()[0])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["imported_count"], 2)


class CursorMismatchTest(unittest.TestCase):
    def test_second_batch_without_cursor(self):
        importer = IncrementalImporter()
        batches = sample_batches()
        importer.import_batch(batches[0])
        result = importer.import_batch(batches[1])
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error_code"], IMPORT_CURSOR_MISMATCH)

    def test_cursor_on_fresh_importer(self):
        importer = IncrementalImporter()
        cursor = importer.import_batch(
            sample_batches()[0])["next_import_cursor"]
        fresh = IncrementalImporter()
        result = fresh.import_batch(sample_batches()[1], cursor=cursor)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error_code"], IMPORT_CURSOR_MISMATCH)

    def test_stale_cursor_rejected(self):
        importer = IncrementalImporter()
        batches = sample_batches()
        cursor1 = importer.import_batch(batches[0])["next_import_cursor"]
        cursor = cursor1
        for b in batches[1:]:
            result = importer.import_batch(b, cursor=cursor)
            assert result["status"] == "ok", result
            cursor = result["next_import_cursor"]
        # 用旧游标续接下一区块
        result = importer.import_batch(
            batch("chain-a", 4, "0xb4", []), cursor=cursor1
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error_code"], IMPORT_CURSOR_MISMATCH)

    def test_height_gap_rejected(self):
        importer = IncrementalImporter()
        cursor = importer.import_batch(
            sample_batches()[0])["next_import_cursor"]
        result = importer.import_batch(
            batch("chain-a", 5, "0xb5", []), cursor=cursor
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error_code"], IMPORT_CURSOR_MISMATCH)

    def test_unknown_old_height_rejected(self):
        importer = IncrementalImporter()
        batches = sample_batches()
        cursor = import_all(importer, batches)["next_import_cursor"]
        # 高度 9 从未导入过，且不在链尖之后
        result = importer.import_batch(
            batch("chain-a", 9, "0xb9", []), cursor=cursor
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error_code"], IMPORT_CURSOR_MISMATCH)

    def test_different_chain_rejected(self):
        importer = IncrementalImporter()
        cursor = importer.import_batch(
            sample_batches()[0])["next_import_cursor"]
        result = importer.import_batch(
            batch("chain-b", 2, "0xb2", []), cursor=cursor
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error_code"], IMPORT_CURSOR_MISMATCH)

    def test_malformed_cursor_rejected(self):
        importer = IncrementalImporter()
        importer.import_batch(sample_batches()[0])
        for bad_cursor in ("", "not-base64!!!", "eyJ2IjoxfQ"):
            result = importer.import_batch(
                sample_batches()[1], cursor=bad_cursor
            )
            self.assertEqual(result["status"], "rejected")
            self.assertEqual(result["error_code"], IMPORT_CURSOR_MISMATCH)

    def test_pagination_cursor_not_accepted_as_import_cursor(self):
        importer = IncrementalImporter()
        importer.import_batch(sample_batches()[0])
        page_cursor = encode_cursor(normalize_filters(), 1, "h2")
        result = importer.import_batch(
            sample_batches()[1], cursor=page_cursor
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error_code"], IMPORT_CURSOR_MISMATCH)

    def test_import_cursor_not_accepted_as_pagination_cursor(self):
        importer = IncrementalImporter()
        cursor = importer.import_batch(
            sample_batches()[0])["next_import_cursor"]
        with self.assertRaises(InvalidCursorError):
            importer.indexer.query(normalize_filters(), cursor=cursor)

    def test_mismatch_leaves_state_unchanged(self):
        importer = IncrementalImporter()
        batches = sample_batches()
        cursor = importer.import_batch(batches[0])["next_import_cursor"]
        importer.import_batch(batch("chain-a", 5, "0xb5", []), cursor=cursor)
        # 拒绝后仍可用原游标无缝续接
        result = importer.import_batch(batches[1], cursor=cursor)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["confirmed_block_height"], 2)


class TxConflictTest(unittest.TestCase):
    def test_conflicting_field_rejected(self):
        importer = IncrementalImporter()
        batches = sample_batches()
        cursor = import_all(importer, batches[:1])["next_import_cursor"]
        conflicting = tx("h1", 2, "0xb2", 10, "alice", "bob", "transfer",
                         "10", "1", True)
        result = importer.import_batch(
            batch("chain-a", 2, "0xb2", [conflicting]), cursor=cursor
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error_code"], TX_CONFLICT)

    def test_each_deciding_field_triggers_conflict(self):
        base = tx("h1", 1, "0xb1", 10, "alice", "bob", "transfer", "10",
                  "1", True)
        cases = [
            # (改动字段, 改动值, 期望错误码)
            ("block_number", 2, TX_CONFLICT),
            ("block_hash", "0xb2", BLOCK_CONFLICT),
            ("timestamp", 11, TX_CONFLICT),
            ("from_address", "carol", TX_CONFLICT),
            ("to_address", "dave", TX_CONFLICT),
            ("method", "approve", TX_CONFLICT),
            ("amount", "11", TX_CONFLICT),
            ("fee", "2", TX_CONFLICT),
            ("success", False, TX_CONFLICT),
        ]
        for field, value, expected in cases:
            importer = IncrementalImporter()
            cursor = importer.import_batch(
                batch("chain-a", 1, "0xb1", [base])
            )["next_import_cursor"]
            changed = dict(base, **{field: value})
            # 批次声明与交易保持一致，以暴露交易级/区块级冲突
            b = batch("chain-a", changed["block_number"],
                      changed["block_hash"], [changed])
            result = importer.import_batch(b, cursor=cursor)
            self.assertEqual(result["status"], "rejected", field)
            self.assertEqual(result["error_code"], expected, field)

    def test_conflict_rejects_whole_batch_atomically(self):
        importer = IncrementalImporter()
        batches = sample_batches()
        cursor = import_all(importer, batches[:1])["next_import_cursor"]
        good = tx("h9", 2, "0xb2", 100, "alice", "carol", "transfer", "30",
                  "3", True)
        conflicting = tx("h1", 2, "0xb2", 10, "alice", "bob", "transfer",
                         "10", "1", True)
        result = importer.import_batch(
            batch("chain-a", 2, "0xb2", [good, conflicting]), cursor=cursor
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error_code"], TX_CONFLICT)
        # 整批拒绝：h9 也没有写入，已确认高度不变
        self.assertEqual(importer.confirmed_block_height, 1)
        page = importer.indexer.query(normalize_filters())
        self.assertEqual(hashes(page), ["h1", "h2"])
        # 同一位置用合法批次重试仍可继续
        retry = importer.import_batch(batches[1], cursor=cursor)
        self.assertEqual(retry["status"], "ok")
        self.assertEqual(retry["imported_count"], 1)

    def test_conflicting_duplicate_within_batch(self):
        importer = IncrementalImporter()
        first = tx("h1", 1, "0xb1", 10, "alice", "bob", "transfer", "10",
                   "1", True)
        second = dict(first, amount="11")
        result = importer.import_batch(
            batch("chain-a", 1, "0xb1", [first, second]))
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error_code"], TX_CONFLICT)
        self.assertIsNone(importer.confirmed_block_height)

    def test_identical_duplicate_within_batch_skipped(self):
        importer = IncrementalImporter()
        first = tx("h1", 1, "0xb1", 10, "alice", "bob", "transfer", "10",
                   "1", True)
        result = importer.import_batch(
            batch("chain-a", 1, "0xb1", [first, dict(first)]))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["imported_count"], 1)
        self.assertEqual(result["skipped_count"], 1)
        stats = importer.indexer.stats(normalize_filters())
        self.assertEqual(stats["total_count"], 1)


class BlockConflictTest(unittest.TestCase):
    def test_same_height_different_hash(self):
        importer = IncrementalImporter()
        batches = sample_batches()
        cursor = importer.import_batch(batches[0])["next_import_cursor"]
        cursor = importer.import_batch(
            batches[1], cursor=cursor)["next_import_cursor"]
        replay = batch("chain-a", 1, "0xother", [
            tx("h1", 1, "0xother", 10, "alice", "bob", "transfer", "10",
               "1", True),
            tx("h2", 1, "0xother", 20, "bob", "alice", "approve", "20",
               "2", True),
        ])
        result = importer.import_batch(replay, cursor=cursor)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error_code"], BLOCK_CONFLICT)

    def test_same_hash_changed_tx_set(self):
        importer = IncrementalImporter()
        batches = sample_batches()
        cursor = import_all(importer, batches[:1])["next_import_cursor"]
        changed = batch("chain-a", 1, "0xb1", [
            tx("h1", 1, "0xb1", 10, "alice", "bob", "transfer", "10", "1",
               True),
            tx("hx", 1, "0xb1", 20, "bob", "alice", "approve", "20", "2",
               True),
        ])
        result = importer.import_batch(changed, cursor=cursor)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error_code"], BLOCK_CONFLICT)

    def test_block_conflict_leaves_state_unchanged(self):
        importer = IncrementalImporter()
        batches = sample_batches()
        cursor = import_all(importer, batches[:1])["next_import_cursor"]
        changed = batch("chain-a", 1, "0xb1", [])
        result = importer.import_batch(changed, cursor=cursor)
        self.assertEqual(result["error_code"], BLOCK_CONFLICT)
        # 拒绝后原批次重试仍然成功，状态不变
        retry = importer.import_batch(batches[0], cursor=cursor)
        self.assertEqual(retry["status"], "ok")
        self.assertEqual(retry["skipped_count"], 2)
        self.assertEqual(importer.confirmed_block_height, 1)


def replace_all_on(importer, start_block, blocks, cursor):
    """执行链重组替换，断言成功并返回结果。"""
    result = importer.replace_from(start_block, blocks, cursor)
    assert result["status"] == "ok", result
    return result


class ReplaceFromResultTest(unittest.TestCase):
    def setUp(self):
        self.importer = IncrementalImporter()
        self.batches = sample_batches()
        self.cursor = import_all(self.importer, self.batches)[
            "next_import_cursor"]

    def reorg_blocks(self):
        """从高度 2 重组：h3 区块哈希变化，h5 成为新区块 3。"""
        return [
            batch("chain-a", 2, "0xb2'", [
                tx("h3", 2, "0xb2'", 100, "alice", "carol", "transfer",
                   "30", "3", False),
            ]),
            batch("chain-a", 3, "0xb5", [
                tx("h5", 3, "0xb5", 310, "carol", "dave", "transfer", "50",
                   "5", True),
            ]),
        ]

    def test_result_counts_and_confirmation(self):
        result = replace_all_on(
            self.importer, 2, self.reorg_blocks(), self.cursor
        )
        self.assertEqual(result["removed_block_count"], 2)
        self.assertEqual(result["removed_transaction_count"], 2)
        self.assertEqual(result["imported_block_count"], 2)
        self.assertEqual(result["imported_count"], 2)
        self.assertEqual(result["skipped_count"], 0)
        self.assertEqual(result["confirmed_block_height"], 3)
        self.assertEqual(result["confirmed_block_hash"], "0xb5")
        self.assertEqual(self.importer.confirmed_block_height, 3)
        self.assertEqual(self.importer.confirmed_block_hash, "0xb5")

    def test_query_and_stats_observe_only_new_suffix(self):
        replace_all_on(self.importer, 2, self.reorg_blocks(), self.cursor)
        page = self.importer.indexer.query(normalize_filters())
        # h1/h2 前缀保留，h4 随旧后缀消失，h3 沿用哈希但内容替换，h5 新增
        self.assertEqual(hashes(page), ["h1", "h2", "h3", "h5"])
        self.assertEqual(self.importer._by_hash["h3"]["block_hash"], "0xb2'")
        stats = self.importer.indexer.stats(normalize_filters())
        self.assertEqual(stats["total_count"], 4)
        self.assertEqual(stats["total_amount"], "110")

    def test_prefix_is_preserved(self):
        result = replace_all_on(
            self.importer, 3,
            [batch("chain-a", 3, "0xb6", [
                tx("h6", 3, "0xb6", 400, "dave", "erin", "transfer", "60",
                   "6", True),
            ])],
            self.cursor,
        )
        self.assertEqual(result["removed_block_count"], 1)
        self.assertEqual(result["removed_transaction_count"], 1)
        page = self.importer.indexer.query(normalize_filters())
        self.assertEqual(hashes(page), ["h1", "h2", "h3", "h6"])

    def test_next_cursor_continues_to_import_batch(self):
        replaced = replace_all_on(
            self.importer, 2, self.reorg_blocks(), self.cursor
        )
        result = self.importer.import_batch(
            batch("chain-a", 4, "0xb7", []),
            cursor=replaced["next_import_cursor"],
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["confirmed_block_height"], 4)

    def test_next_cursor_continues_to_another_replace(self):
        replaced = replace_all_on(
            self.importer, 2, self.reorg_blocks(), self.cursor
        )
        again = replace_all_on(
            self.importer, 3,
            [batch("chain-a", 3, "0xb8", [
                tx("h8", 3, "0xb8", 500, "erin", "frank", "transfer", "80",
                   "8", True),
            ])],
            replaced["next_import_cursor"],
        )
        self.assertEqual(again["confirmed_block_hash"], "0xb8")
        page = self.importer.indexer.query(normalize_filters())
        self.assertEqual(hashes(page), ["h1", "h2", "h3", "h8"])

    def test_empty_new_tip_block_allowed(self):
        result = replace_all_on(
            self.importer, 3,
            [batch("chain-a", 3, "0xb9", [])],
            self.cursor,
        )
        self.assertEqual(result["removed_block_count"], 1)
        self.assertEqual(result["removed_transaction_count"], 1)
        self.assertEqual(result["imported_count"], 0)
        self.assertEqual(result["confirmed_block_height"], 3)
        self.assertEqual(result["confirmed_block_hash"], "0xb9")

    def test_new_suffix_longer_than_old(self):
        blocks = self.reorg_blocks() + [
            batch("chain-a", 4, "0xb7", [
                tx("h7", 4, "0xb7", 600, "frank", "grace", "transfer", "70",
                   "7", True),
            ]),
        ]
        result = replace_all_on(self.importer, 2, blocks, self.cursor)
        self.assertEqual(result["removed_block_count"], 2)
        self.assertEqual(result["imported_block_count"], 3)
        self.assertEqual(result["confirmed_block_height"], 4)
        page = self.importer.indexer.query(normalize_filters())
        self.assertEqual(hashes(page), ["h1", "h2", "h3", "h5", "h7"])


class ReplaceFromSkipTest(unittest.TestCase):
    def test_identical_old_suffix_transaction_skipped(self):
        importer = IncrementalImporter()
        batches = sample_batches()
        cursor = import_all(importer, batches)["next_import_cursor"]
        # 高度 3 的内容与旧后缀完全一致（重排前先含 h3/h4），同时替换高度 2、3
        blocks = [
            batches[1],
            batches[2],
        ]
        result = replace_all_on(importer, 2, blocks, cursor)
        self.assertEqual(result["removed_block_count"], 2)
        self.assertEqual(result["removed_transaction_count"], 0)
        self.assertEqual(result["imported_count"], 0)
        self.assertEqual(result["skipped_count"], 2)
        stats = importer.indexer.stats(normalize_filters())
        self.assertEqual(stats["total_count"], 4)

    def test_same_hash_moved_to_new_block_is_replaced_not_skipped(self):
        importer = IncrementalImporter()
        batches = sample_batches()
        cursor = import_all(importer, batches)["next_import_cursor"]
        # h4 从高度 3 移动到新高度 2：旧后缀同名但字段变化，旧记录移除、
        # 新记录导入，不视为冲突
        blocks = [
            batch("chain-a", 2, "0xc2", [
                tx("h4", 2, "0xc2", 300, "bob", "dave", "transfer", "40",
                   "4", True),
            ]),
        ]
        result = replace_all_on(importer, 2, blocks, cursor)
        self.assertEqual(result["removed_transaction_count"], 2)
        self.assertEqual(result["imported_count"], 1)
        self.assertEqual(result["skipped_count"], 0)
        page = importer.indexer.query(normalize_filters())
        self.assertEqual(hashes(page), ["h1", "h2", "h4"])
        self.assertEqual(
            [t["block_number"] for t in page["transactions"]
             if t["tx_hash"] == "h4"],
            [2],
        )

    def test_identical_duplicate_within_new_suffix_skipped(self):
        importer = IncrementalImporter()
        cursor = import_all(importer, sample_batches())["next_import_cursor"]
        duplicate = tx("h9", 2, "0xd2", 100, "alice", "carol", "transfer",
                       "9", "9", True)
        blocks = [
            batch("chain-a", 2, "0xd2", [duplicate, dict(duplicate)]),
        ]
        result = replace_all_on(importer, 2, blocks, cursor)
        self.assertEqual(result["imported_count"], 1)
        self.assertEqual(result["skipped_count"], 1)
        stats = importer.indexer.stats(normalize_filters())
        self.assertEqual(stats["total_count"], 3)


class ReplaceFromInvalidBatchTest(unittest.TestCase):
    def setUp(self):
        self.importer = IncrementalImporter()
        self.cursor = import_all(self.importer, sample_batches())[
            "next_import_cursor"]

    def assert_invalid(self, start_block, blocks, cursor=None):
        result = self.importer.replace_from(
            start_block, blocks, self.cursor if cursor is None else cursor
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error_code"], INVALID_REPLACEMENT_BATCH)
        self.assertIn("message", result)

    def test_empty_blocks(self):
        self.assert_invalid(2, [])

    def test_blocks_not_a_list(self):
        self.assert_invalid(2, {"chain_id": "chain-a"})

    def test_start_block_not_int(self):
        self.assert_invalid("2", [sample_batches()[1]])
        self.assert_invalid(True, [sample_batches()[1]])

    def test_first_height_must_equal_start_block(self):
        self.assert_invalid(2, [sample_batches()[2]])

    def test_non_contiguous_heights(self):
        self.assert_invalid(
            2, [sample_batches()[1], sample_batches()[2],
                batch("chain-a", 5, "0xb5", [])]
        )

    def test_inconsistent_chain_id(self):
        blocks = [
            sample_batches()[1],
            batch("chain-b", 3, "0xb3", [
                tx("h4", 3, "0xb3", 300, "bob", "dave", "transfer", "40",
                   "4", True),
            ]),
        ]
        self.assert_invalid(2, blocks)

    def test_bad_block_structure(self):
        self.assert_invalid(2, [{"chain_id": "chain-a"}])

    def test_bad_tx_block_relation(self):
        bad = batch("chain-a", 2, "0xb2", [
            tx("h3", 3, "0xb2", 100, "alice", "carol", "transfer", "30",
               "3", False),
        ])
        self.assert_invalid(2, [bad])

    def test_invalid_replace_leaves_state_unchanged(self):
        self.assert_invalid(2, [sample_batches()[2]])
        page = self.importer.indexer.query(normalize_filters())
        self.assertEqual(hashes(page), ["h1", "h2", "h3", "h4"])
        self.assertEqual(self.importer.confirmed_block_height, 3)
        # 当前游标仍可用于另一次合法替换
        result = replace_all_on(
            self.importer, 3,
            [batch("chain-a", 3, "0xbb", [
                tx("h9", 3, "0xbb", 900, "a", "b", "transfer", "9", "9",
                   True),
            ])],
            self.cursor,
        )
        self.assertEqual(result["status"], "ok")


class ReplaceFromCursorTest(unittest.TestCase):
    def setUp(self):
        self.importer = IncrementalImporter()
        self.cursor = import_all(self.importer, sample_batches())[
            "next_import_cursor"]

    def assert_mismatch(self, start_block, blocks, cursor):
        result = self.importer.replace_from(start_block, blocks, cursor)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error_code"], IMPORT_CURSOR_MISMATCH)

    def test_start_block_not_imported(self):
        self.assert_mismatch(4, [batch("chain-a", 4, "0xb4", [])],
                             self.cursor)
        self.assert_mismatch(0, [batch("chain-a", 0, "0xb0", [])],
                             self.cursor)

    def test_missing_cursor(self):
        self.assert_mismatch(2, [sample_batches()[1]], None)

    def test_on_fresh_importer(self):
        fresh = IncrementalImporter()
        result = fresh.replace_from(1, [sample_batches()[0]], self.cursor)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error_code"], IMPORT_CURSOR_MISMATCH)

    def test_stale_cursor_rejected(self):
        # 高度 1 的游标（当前链尖已到高度 3）
        stale = encode_import_cursor("chain-a", 1, "0xb1")
        self.assert_mismatch(2, [sample_batches()[1]], stale)

    def test_wrong_chain_rejected(self):
        self.assert_mismatch(
            2, [batch("chain-b", 2, "0xb2", [])], self.cursor
        )

    def test_malformed_cursor_rejected(self):
        for bad in ("", "not-base64!!!", "eyJ2IjoxfQ"):
            self.assert_mismatch(2, [sample_batches()[1]], bad)

    def test_pagination_cursor_rejected(self):
        page_cursor = encode_cursor(normalize_filters(), 1, "h2")
        self.assert_mismatch(2, [sample_batches()[1]], page_cursor)

    def test_mismatch_leaves_state_unchanged(self):
        self.assert_mismatch(4, [batch("chain-a", 4, "0xb4", [])],
                             self.cursor)
        page = self.importer.indexer.query(normalize_filters())
        self.assertEqual(hashes(page), ["h1", "h2", "h3", "h4"])
        # 原游标仍可用于合法替换
        result = replace_all_on(
            self.importer, 3,
            [batch("chain-a", 3, "0xbb", [
                tx("h9", 3, "0xbb", 900, "a", "b", "transfer", "9", "9",
                   True),
            ])],
            self.cursor,
        )
        self.assertEqual(result["status"], "ok")


class ReplaceFromTxConflictTest(unittest.TestCase):
    def test_conflict_with_prefix_rejected(self):
        importer = IncrementalImporter()
        cursor = import_all(importer, sample_batches())["next_import_cursor"]
        # h1 属于保留前缀（高度 1），新后缀同哈希但 amount 变化
        blocks = [
            batch("chain-a", 2, "0xe2", [
                tx("h1", 2, "0xe2", 10, "alice", "bob", "transfer", "99",
                   "1", True),
            ]),
        ]
        result = importer.replace_from(2, blocks, cursor)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error_code"], TX_CONFLICT)
        # 索引未改变
        page = importer.indexer.query(normalize_filters())
        self.assertEqual(hashes(page), ["h1", "h2", "h3", "h4"])
        self.assertEqual(page["transactions"][0]["amount"], "10")

    def test_conflicting_duplicate_within_new_suffix_rejected(self):
        importer = IncrementalImporter()
        cursor = import_all(importer, sample_batches())["next_import_cursor"]
        first = tx("h9", 2, "0xf2", 100, "alice", "carol", "transfer", "9",
                   "9", True)
        second = dict(first, amount="99")
        result = importer.replace_from(
            2, [batch("chain-a", 2, "0xf2", [first, second])], cursor
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error_code"], TX_CONFLICT)

    def test_old_suffix_same_hash_change_is_not_conflict(self):
        importer = IncrementalImporter()
        cursor = import_all(importer, sample_batches())["next_import_cursor"]
        # h3 在旧后缀中，新后缀同哈希但内容变化：允许替换
        blocks = [
            batch("chain-a", 2, "0xg2", [
                tx("h3", 2, "0xg2", 100, "alice", "zoe", "transfer", "300",
                   "3", True),
            ]),
        ]
        result = replace_all_on(importer, 2, blocks, cursor)
        page = importer.indexer.query(normalize_filters())
        h3 = [t for t in page["transactions"] if t["tx_hash"] == "h3"][0]
        self.assertEqual(h3["to_address"], "zoe")
        self.assertEqual(h3["amount"], "300")

    def test_conflict_is_atomic(self):
        importer = IncrementalImporter()
        cursor = import_all(importer, sample_batches())["next_import_cursor"]
        good = tx("h9", 2, "0xh2", 100, "a", "b", "transfer", "9", "9",
                  True)
        conflicting = tx("h1", 2, "0xh2", 10, "alice", "bob", "transfer",
                         "99", "1", True)
        result = importer.replace_from(
            2, [batch("chain-a", 2, "0xh2", [good, conflicting])], cursor
        )
        self.assertEqual(result["error_code"], TX_CONFLICT)
        page = importer.indexer.query(normalize_filters())
        self.assertEqual(hashes(page), ["h1", "h2", "h3", "h4"])
        # 原游标仍可重试合法替换
        retry = replace_all_on(
            importer, 2,
            [batch("chain-a", 2, "0xh2", [good])], cursor
        )
        self.assertEqual(retry["imported_count"], 1)


class PaginationStabilityTest(unittest.TestCase):
    def test_pagination_order_unaffected_by_imports(self):
        importer = IncrementalImporter()
        batches = sample_batches()
        cursor = import_all(importer, batches[:1])["next_import_cursor"]

        filters = normalize_filters()
        first_page = importer.indexer.query(filters, page_size=1)
        self.assertEqual(hashes(first_page), ["h1"])

        # 在翻页中途继续导入新区块：已签发游标仍然可用且不重复
        cursor = importer.import_batch(batches[1], cursor=cursor)[
            "next_import_cursor"]
        importer.import_batch(batches[2], cursor=cursor)

        collected = list(hashes(first_page))
        page_cursor = first_page["next_cursor"]
        while page_cursor is not None:
            page = importer.indexer.query(filters, page_size=1,
                                          cursor=page_cursor)
            collected.extend(hashes(page))
            page_cursor = page["next_cursor"]
        self.assertEqual(collected, ["h1", "h2", "h3", "h4"])

    def test_stats_and_group_stats_after_import(self):
        importer = IncrementalImporter()
        import_all(importer, sample_batches())
        filters = normalize_filters()
        self.assertEqual(
            importer.indexer.method_stats(filters)["total_groups"], 2
        )
        self.assertEqual(
            importer.indexer.pair_stats(filters)["total_groups"], 4
        )
        counterparty = importer.indexer.counterparty_stats(
            normalize_filters(address="alice")
        )
        self.assertEqual(counterparty["address"], "alice")
        self.assertEqual(counterparty["total_groups"], 2)
        address_time = importer.indexer.address_time_stats(filters, 60)
        self.assertEqual(address_time["total_groups"], 6)


if __name__ == "__main__":
    unittest.main()
