"""增量交易导入与断点续传测试。"""

import json
import unittest

from tx_indexer.cursor import encode_cursor
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


class ReplaceFromTest(unittest.TestCase):
    """链重组 replace_from：原子覆盖 start_block 至链尖的旧后缀。"""

    def setUp(self):
        self.importer = IncrementalImporter()
        result = import_all(self.importer, sample_batches())
        # 样例：区块1(h1,h2)、区块2(h3)、区块3(h4)
        self.cursor = result["next_import_cursor"]

    def replace(self, start_block, blocks, cursor=None):
        return self.importer.replace_from(
            start_block, blocks, self.cursor if cursor is None else cursor
        )

    def test_basic_reorg_counts_and_tip(self):
        new_blocks = [
            batch("chain-a", 2, "0xc2", [
                tx("h3", 2, "0xc2", 100, "alice", "carol", "transfer",
                   "30", "3", True),
            ]),
            batch("chain-a", 3, "0xc3", [
                tx("h5", 3, "0xc3", 310, "eve", "dave", "transfer", "50",
                   "5", True),
            ]),
        ]
        result = self.replace(2, new_blocks)
        self.assertEqual(result["status"], "ok")
        # 旧后缀为区块 2、3：2 个区块；旧交易 h3、h4 被真正替换/移除
        self.assertEqual(result["removed_block_count"], 2)
        self.assertEqual(result["removed_transaction_count"], 2)
        self.assertEqual(result["imported_block_count"], 2)
        self.assertEqual(result["imported_count"], 2)
        self.assertEqual(result["skipped_count"], 0)
        self.assertEqual(result["confirmed_block_height"], 3)
        self.assertEqual(result["confirmed_block_hash"], "0xc3")
        self.assertIsInstance(result["next_import_cursor"], str)

    def test_query_and_stats_only_observe_new_suffix(self):
        new_blocks = [
            batch("chain-a", 2, "0xc2", [
                tx("h5", 2, "0xc2", 100, "alice", "carol", "swap", "99",
                   "3", True),
            ]),
            batch("chain-a", 3, "0xc3", [
                tx("h6", 3, "0xc3", 310, "bob", "dave", "transfer", "1",
                   "4", True),
            ]),
        ]
        self.replace(2, new_blocks)
        # 保留前缀 h1、h2；旧后缀 h3、h4 消失；新后缀 h5、h6 可见
        page = self.importer.indexer.query(normalize_filters())
        self.assertEqual(hashes(page), ["h1", "h2", "h5", "h6"])
        stats = self.importer.indexer.stats(normalize_filters())
        self.assertEqual(stats["total_count"], 4)
        # 10 + 20 + 99 + 1
        self.assertEqual(stats["total_amount"], "130")
        methods = self.importer.indexer.method_stats(normalize_filters())
        self.assertEqual(
            sorted(g["method"] for g in methods["groups"]),
            ["approve", "swap", "transfer"],
        )

    def test_lower_prefix_is_preserved(self):
        new_blocks = [
            batch("chain-a", 3, "0xc3", [
                tx("h5", 3, "0xc3", 310, "eve", "dave", "transfer", "50",
                   "5", True),
            ]),
        ]
        self.replace(3, new_blocks)
        page = self.importer.indexer.query(normalize_filters())
        # h1、h2（区块1）、h3（区块2）保留
        self.assertEqual(hashes(page), ["h1", "h2", "h3", "h5"])
        h1 = [t for t in page["transactions"] if t["tx_hash"] == "h1"][0]
        self.assertEqual(h1["amount"], "10")

    def test_identical_old_suffix_records_retained_and_skipped(self):
        # 用与已导入后缀完全相同的区块重新替换：记录保留、计入 skipped，
        # 不删除、不重新导入
        same = sample_batches()[1:]
        result = self.replace(2, same)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["removed_transaction_count"], 0)
        self.assertEqual(result["imported_count"], 0)
        self.assertEqual(result["skipped_count"], 2)
        self.assertEqual(result["removed_block_count"], 2)
        self.assertEqual(result["imported_block_count"], 2)
        page = self.importer.indexer.query(normalize_filters())
        self.assertEqual(hashes(page), ["h1", "h2", "h3", "h4"])

    def test_identical_duplicate_within_new_suffix_skipped(self):
        dup = tx("h5", 2, "0xc2", 100, "alice", "carol", "swap", "99",
                 "3", True)
        new_blocks = [
            batch("chain-a", 2, "0xc2", [dict(dup), dict(dup)]),
            batch("chain-a", 3, "0xc3", [
                tx("h6", 3, "0xc3", 310, "bob", "dave", "transfer", "1",
                   "4", True),
            ]),
        ]
        result = self.replace(2, new_blocks)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["imported_count"], 2)
        self.assertEqual(result["skipped_count"], 1)
        stats = self.importer.indexer.stats(normalize_filters())
        # h1+h2+h5+h6
        self.assertEqual(stats["total_count"], 4)

    def test_same_hash_changed_fields_is_removed_and_reimported(self):
        # h3 在旧后缀中已存在；新后缀同 hash 但字段不同（重组），
        # 旧记录删除、新记录写入，不计 skipped
        new_blocks = [
            batch("chain-a", 2, "0xc2", [
                tx("h3", 2, "0xc2", 123, "new", "carol", "swap", "77",
                   "3", False),
            ]),
        ]
        result = self.replace(2, new_blocks)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["removed_transaction_count"], 2)
        self.assertEqual(result["imported_count"], 1)
        self.assertEqual(result["skipped_count"], 0)
        page = self.importer.indexer.query(normalize_filters())
        # 旧 h4 随区块3 被截掉；h3 为新内容
        self.assertEqual(hashes(page), ["h1", "h2", "h3"])
        h3 = [t for t in page["transactions"] if t["tx_hash"] == "h3"][0]
        self.assertEqual(h3["method"], "swap")
        self.assertEqual(h3["amount"], "77")

    def test_new_suffix_can_shrink_chain_tip(self):
        # 从高度 2 替换为只含一个区块的更短后缀：链尖缩回到 2，区块3/h4 截掉
        new_blocks = [
            batch("chain-a", 2, "0xc2", [
                tx("h5", 2, "0xc2", 100, "alice", "carol", "swap", "99",
                   "3", True),
            ]),
        ]
        result = self.replace(2, new_blocks)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["confirmed_block_height"], 2)
        self.assertEqual(result["confirmed_block_hash"], "0xc2")
        self.assertEqual(result["removed_block_count"], 2)
        self.assertEqual(result["imported_block_count"], 1)
        page = self.importer.indexer.query(normalize_filters())
        self.assertEqual(hashes(page), ["h1", "h2", "h5"])

    def test_replace_from_height_one_replaces_whole_chain(self):
        new_blocks = [
            batch("chain-a", 1, "0xz1", [
                tx("h9", 1, "0xz1", 1, "z", "y", "mint", "1", "1", True),
            ]),
        ]
        result = self.replace(1, new_blocks)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["removed_block_count"], 3)
        self.assertEqual(result["removed_transaction_count"], 4)
        self.assertEqual(result["imported_count"], 1)
        self.assertEqual(result["confirmed_block_height"], 1)
        self.assertEqual(result["confirmed_block_hash"], "0xz1")
        page = self.importer.indexer.query(normalize_filters())
        self.assertEqual(hashes(page), ["h9"])

    def test_returned_cursor_continues_with_import_batch(self):
        new_blocks = [
            batch("chain-a", 2, "0xc2", [
                tx("h5", 2, "0xc2", 100, "alice", "carol", "swap", "99",
                   "3", True),
            ]),
        ]
        result = self.replace(2, new_blocks)
        # 新游标绑定新链尖（高度 2），可续用于 import_batch 高度 3
        cont = self.importer.import_batch(
            batch("chain-a", 3, "0xc3", [
                tx("h6", 3, "0xc3", 310, "bob", "dave", "transfer", "1",
                   "4", True),
            ]),
            cursor=result["next_import_cursor"],
        )
        self.assertEqual(cont["status"], "ok")
        self.assertEqual(cont["confirmed_block_height"], 3)

    def test_returned_cursor_continues_with_another_replace(self):
        new_blocks = [
            batch("chain-a", 2, "0xc2", [
                tx("h5", 2, "0xc2", 100, "alice", "carol", "swap", "99",
                   "3", True),
            ]),
        ]
        first = self.replace(2, new_blocks)
        second = self.importer.replace_from(
            2,
            [batch("chain-a", 2, "0xd2", [
                tx("h7", 2, "0xd2", 101, "x", "y", "mint", "2", "1", True),
            ])],
            first["next_import_cursor"],
        )
        self.assertEqual(second["status"], "ok")
        self.assertEqual(second["confirmed_block_hash"], "0xd2")


class ReplaceFromTxConflictTest(unittest.TestCase):
    def setUp(self):
        self.importer = IncrementalImporter()
        result = import_all(self.importer, sample_batches())
        self.cursor = result["next_import_cursor"]

    def test_conflict_with_retained_prefix_rejected(self):
        # h1 在保留前缀（区块1）中；新后缀从高度2开始，同 hash 但字段不同
        new_blocks = [
            batch("chain-a", 2, "0xc2", [
                tx("h1", 2, "0xc2", 10, "alice", "bob", "transfer", "99",
                   "1", True),
            ]),
        ]
        result = self.importer.replace_from(2, new_blocks, self.cursor)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error_code"], TX_CONFLICT)

    def test_conflict_within_new_suffix_rejected(self):
        new_blocks = [
            batch("chain-a", 2, "0xc2", [
                tx("h9", 2, "0xc2", 100, "a", "b", "m", "1", "1", True),
            ]),
            batch("chain-a", 3, "0xc3", [
                tx("h9", 3, "0xc3", 200, "a", "b", "m", "2", "1", True),
            ]),
        ]
        result = self.importer.replace_from(2, new_blocks, self.cursor)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error_code"], TX_CONFLICT)

    def test_old_suffix_same_name_never_conflicts(self):
        # h3、h4 都在被替换的旧后缀中；同名字段改变不构成 TX_CONFLICT
        new_blocks = [
            batch("chain-a", 2, "0xc2", [
                tx("h3", 2, "0xc2", 123, "new", "carol", "swap", "77",
                   "3", False),
            ]),
            batch("chain-a", 3, "0xc3", [
                tx("h4", 3, "0xc3", 321, "other", "dave", "mint", "88",
                   "4", False),
            ]),
        ]
        result = self.importer.replace_from(2, new_blocks, self.cursor)
        self.assertEqual(result["status"], "ok")

    def test_conflict_is_atomic_and_leaves_state_unchanged(self):
        new_blocks = [
            batch("chain-a", 2, "0xc2", [
                tx("h5", 2, "0xc2", 100, "a", "b", "good", "1", "1", True),
            ]),
            batch("chain-a", 3, "0xc3", [
                tx("h1", 3, "0xc3", 10, "alice", "bob", "transfer", "99",
                   "1", True),
            ]),
        ]
        result = self.importer.replace_from(2, new_blocks, self.cursor)
        self.assertEqual(result["error_code"], TX_CONFLICT)
        # 状态完全不变：链尖、区块、查询、计数
        self.assertEqual(self.importer.confirmed_block_height, 3)
        self.assertEqual(self.importer.confirmed_block_hash, "0xb3")
        page = self.importer.indexer.query(normalize_filters())
        self.assertEqual(hashes(page), ["h1", "h2", "h3", "h4"])
        # 原游标仍可用于一次合法替换
        retry = self.importer.replace_from(
            2,
            [batch("chain-a", 2, "0xc2", [
                tx("h5", 2, "0xc2", 100, "a", "b", "good", "1", "1", True),
            ])],
            self.cursor,
        )
        self.assertEqual(retry["status"], "ok")


class ReplaceFromInvalidBatchTest(unittest.TestCase):
    def setUp(self):
        self.importer = IncrementalImporter()
        result = import_all(self.importer, sample_batches())
        self.cursor = result["next_import_cursor"]

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
        self.assert_invalid(2, {})
        self.assert_invalid(2, "x")
        self.assert_invalid(2, None)

    def test_first_block_start_mismatch(self):
        self.assert_invalid(2, [batch("chain-a", 3, "0xc3", [])])

    def test_non_contiguous_heights(self):
        self.assert_invalid(2, [
            batch("chain-a", 2, "0xc2", []),
            batch("chain-a", 4, "0xc4", []),
        ])

    def test_descending_heights(self):
        self.assert_invalid(2, [
            batch("chain-a", 2, "0xc2", []),
            batch("chain-a", 2, "0xc2b", []),
        ])

    def test_inconsistent_chain_id(self):
        self.assert_invalid(2, [
            batch("chain-a", 2, "0xc2", []),
            batch("chain-b", 3, "0xc3", []),
        ])

    def test_invalid_inner_batch_structure(self):
        self.assert_invalid(2, [{}])
        self.assert_invalid(2, [[]])
        self.assert_invalid(2, [
            batch("chain-a", 2, "0xc2", []),
            "not-a-batch",
        ])

    def test_inner_tx_block_relationship_invalid(self):
        # 交易 block_number / block_hash 与所属批次不一致
        bad_tx = tx("h9", 3, "0xc3", 1, "a", "b", "m", "1", "1", True)
        self.assert_invalid(2, [batch("chain-a", 2, "0xc2", [bad_tx])])
        bad_hash = tx("h9", 2, "0xother", 1, "a", "b", "m", "1", "1", True)
        self.assert_invalid(2, [batch("chain-a", 2, "0xc2", [bad_hash])])

    def test_inner_tx_missing_field(self):
        bad = {"tx_hash": "h9", "block_number": 2}
        self.assert_invalid(2, [batch("chain-a", 2, "0xc2", [bad])])

    def test_invalid_replacement_leaves_state_unchanged(self):
        self.assert_invalid(2, [])
        self.assertEqual(self.importer.confirmed_block_height, 3)
        page = self.importer.indexer.query(normalize_filters())
        self.assertEqual(hashes(page), ["h1", "h2", "h3", "h4"])


class ReplaceFromCursorTest(unittest.TestCase):
    def setUp(self):
        self.importer = IncrementalImporter()
        result = import_all(self.importer, sample_batches())
        self.cursor = result["next_import_cursor"]

    def assert_mismatch(self, start_block, blocks, cursor):
        result = self.importer.replace_from(start_block, blocks, cursor)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error_code"], IMPORT_CURSOR_MISMATCH)

    def test_start_block_not_imported(self):
        self.assert_mismatch(9, [batch("chain-a", 9, "0xb9", [])],
                             self.cursor)

    def test_start_block_below_zero_is_invalid_batch(self):
        # 结构校验先于游标：start_block=-1 时首项高度也为 -1，非负校验失败
        result = self.importer.replace_from(
            -1, [batch("chain-a", -1, "0x", [])], self.cursor
        )
        self.assertEqual(result["error_code"], INVALID_REPLACEMENT_BATCH)

    def test_stale_cursor(self):
        # 第一批次之后的游标在链推进到高度3后即过期
        importer = IncrementalImporter()
        batches = sample_batches()
        first = importer.import_batch(batches[0])["next_import_cursor"]
        cursor = first
        for b in batches[1:]:
            cursor = importer.import_batch(b, cursor=cursor)[
                "next_import_cursor"]
        result = importer.replace_from(
            2, [batch("chain-a", 2, "0xc2", [])], first
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error_code"], IMPORT_CURSOR_MISMATCH)

    def test_missing_cursor(self):
        self.assert_mismatch(2, [batch("chain-a", 2, "0xc2", [])], None)

    def test_fresh_importer(self):
        fresh = IncrementalImporter()
        result = fresh.replace_from(
            1, [batch("chain-a", 1, "0xb1", [])], self.cursor
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error_code"], IMPORT_CURSOR_MISMATCH)

    def test_chain_id_mismatch(self):
        # 结构合法（内部 chain_id 一致）但与已导入链不同 -> 游标/链不匹配
        self.assert_mismatch(2, [batch("chain-b", 2, "0xc2", [])],
                             self.cursor)

    def test_pagination_cursor_not_accepted(self):
        page_cursor = encode_cursor(normalize_filters(), 3, "h4")
        self.assert_mismatch(2, [batch("chain-a", 2, "0xc2", [])],
                             page_cursor)

    def test_malformed_cursor(self):
        for bad in ("", "not-base64!!!", "eyJ2IjoxfQ"):
            self.assert_mismatch(2, [batch("chain-a", 2, "0xc2", [])], bad)

    def test_mismatch_leaves_state_unchanged(self):
        self.assert_mismatch(9, [batch("chain-a", 9, "0xb9", [])],
                             self.cursor)
        # 原游标仍可完成合法替换
        result = self.importer.replace_from(
            3, [batch("chain-a", 3, "0xc3", [])], self.cursor
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["confirmed_block_height"], 3)


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
