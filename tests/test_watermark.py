"""索引水位与幂等重放测试。"""

import threading
import unittest

from tx_indexer.engine import TxIndexer, normalize_filters
from tx_indexer.errors import SourceUnavailableError, TransactionConflictError
from tx_indexer.watermark import WatermarkIndexer


def tx(tx_hash, frm, to, method, amount=None, timestamp=None):
    result = {
        "tx_hash": tx_hash,
        "from_address": frm,
        "to_address": to,
        "method": method,
    }
    if amount is not None:
        result["amount"] = amount
    if timestamp is not None:
        result["timestamp"] = timestamp
    return result


def block(height, timestamp, transactions):
    return {
        "block_number": height,
        "timestamp": timestamp,
        "transactions": transactions,
    }


def sample_source(chain_id, height):
    """五个区块的样例数据源：每块两笔交易。"""
    data = {
        0: block(0, 100, [
            tx("h0", "alice", "bob", "transfer", "10"),
            tx("h1", "bob", "alice", "approve", "20"),
        ]),
        1: block(1, 200, [
            tx("h2", "alice", "carol", "transfer", "30"),
            tx("h3", "carol", "alice", "transfer", "40"),
        ]),
        2: block(2, 300, [
            tx("h4", "bob", "dave", "transfer", "50"),
            tx("h5", "dave", "bob", "approve", "60"),
        ]),
        3: block(3, 400, [
            tx("h6", "alice", "dave", "transfer", "70"),
            tx("h7", "dave", "alice", "transfer", "80"),
        ]),
        4: block(4, 500, [
            tx("h8", "carol", "bob", "approve", "90"),
            tx("h9", "bob", "carol", "transfer", "100"),
        ]),
    }
    return data.get(height)


class FakeClock:
    def __init__(self):
        self.now = 1000

    def __call__(self):
        self.now += 1
        return self.now


def make_indexer(source=None, **kwargs):
    clock = kwargs.pop("clock", FakeClock())
    return WatermarkIndexer(source=source or sample_source, clock=clock,
                            **kwargs)


class ParameterValidationTest(unittest.TestCase):
    def test_start_greater_than_end(self):
        indexer = make_indexer()
        with self.assertRaises(ValueError):
            indexer.commit_range("chain-a", 5, 4, 10)

    def test_negative_start(self):
        indexer = make_indexer()
        with self.assertRaises(ValueError):
            indexer.commit_range("chain-a", -1, 4, 10)

    def test_negative_end(self):
        indexer = make_indexer()
        with self.assertRaises(ValueError):
            indexer.commit_range("chain-a", 0, -1, 10)

    def test_batch_size_out_of_range(self):
        indexer = make_indexer()
        for bad in (0, -1, 1001, "10", 1.5, True, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                indexer.commit_range("chain-a", 0, 4, bad)

    def test_batch_size_boundaries_ok(self):
        indexer = make_indexer()
        result = indexer.commit_range("chain-a", 0, 0, 1)
        self.assertEqual(result["status"], "ok")
        result = indexer.commit_range("chain-a", 1, 4, 1000)
        self.assertEqual(result["status"], "ok")

    def test_status_rejects_invalid_arguments(self):
        indexer = make_indexer()
        with self.assertRaises(ValueError):
            indexer.status("")
        with self.assertRaises(ValueError):
            indexer.status("chain-a", start_block=-1)


class CommitTest(unittest.TestCase):
    def test_commit_writes_normalized_records(self):
        indexer = make_indexer()
        result = indexer.commit_range("chain-a", 0, 1, 10)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["imported_count"], 4)
        self.assertEqual(result["skipped_count"], 0)
        self.assertEqual(result["committed_start_block"], 0)
        self.assertEqual(result["committed_end_block"], 1)
        self.assertEqual(result["next_block"], 2)

        page = indexer.indexer.query(normalize_filters())
        self.assertEqual(
            [t["tx_hash"] for t in page["transactions"]],
            ["h0", "h1", "h2", "h3"],
        )
        first = page["transactions"][0]
        # 标准化结果保留哈希、高度、区块时间、地址与方法标识
        self.assertEqual(first["tx_hash"], "h0")
        self.assertEqual(first["block_number"], 0)
        self.assertEqual(first["timestamp"], 100)
        self.assertEqual(first["from_address"], "alice")
        self.assertEqual(first["to_address"], "bob")
        self.assertEqual(first["method"], "transfer")

    def test_batch_size_splits_batches_but_result_identical(self):
        indexer = make_indexer()
        indexer.commit_range("chain-a", 0, 4, 2)
        page = indexer.indexer.query(normalize_filters())
        self.assertEqual(page["total"], 10)
        status = indexer.status("chain-a")
        self.assertEqual(status["committed_start_block"], 0)
        self.assertEqual(status["committed_end_block"], 4)
        self.assertEqual(status["next_block"], 5)

    def test_replay_is_idempotent(self):
        indexer = make_indexer()
        first = indexer.commit_range("chain-a", 0, 4, 2)
        second = indexer.commit_range("chain-a", 0, 4, 2)
        self.assertEqual(second["imported_count"], 0)
        self.assertEqual(second["skipped_count"], 0)
        self.assertEqual(
            indexer.indexer.query(normalize_filters())["total"], 10
        )
        self.assertEqual(second["committed_end_block"],
                         first["committed_end_block"])

    def test_partial_overlap_replay_skips_committed_blocks(self):
        calls = []

        def source(chain_id, height):
            calls.append(height)
            return sample_source(chain_id, height)

        indexer = make_indexer(source=source)
        indexer.commit_range("chain-a", 0, 2, 10)
        calls.clear()
        indexer.commit_range("chain-a", 1, 4, 10)
        # 已提交的 1、2 不再访问上游，只取 3、4
        self.assertEqual(calls, [3, 4])
        self.assertEqual(
            indexer.indexer.query(normalize_filters())["total"], 10
        )

    def test_duplicate_tx_same_content_skipped(self):
        shared = tx("h0", "alice", "bob", "transfer", "10")

        def source(chain_id, height):
            # 同一区块内相同哈希、相同标准化内容出现两次
            return block(height, 100 + height, [shared, dict(shared)])

        indexer = make_indexer(source=source)
        result = indexer.commit_range("chain-a", 0, 0, 1)
        # 第二次出现视为重复：不新增记录，首次写入结果不变
        self.assertEqual(result["imported_count"], 1)
        self.assertEqual(result["skipped_count"], 1)
        page = indexer.indexer.query(normalize_filters())
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["transactions"][0]["block_number"], 0)

    def test_duplicate_of_preloaded_record_skipped(self):
        preloaded = {
            "tx_hash": "h0",
            "block_number": 0,
            "timestamp": 100,
            "from_address": "alice",
            "to_address": "bob",
            "method": "transfer",
            "amount": "10",
        }
        engine = TxIndexer([dict(preloaded)])
        indexer = WatermarkIndexer(indexer=engine, source=sample_source,
                                   clock=FakeClock())
        result = indexer.commit_range("chain-a", 0, 0, 1)
        self.assertEqual(result["imported_count"], 1)
        self.assertEqual(result["skipped_count"], 1)
        page = engine.query(normalize_filters())
        self.assertEqual(page["total"], 2)
        # 首次写入结果不变
        self.assertEqual(page["transactions"][0]["amount"], "10")

    def test_conflicting_tx_aborts_batch_without_watermark(self):
        def source(chain_id, height):
            if height == 0:
                return block(0, 100, [tx("h0", "alice", "bob", "transfer")])
            # 相同哈希、不同方法标识：冲突
            return block(1, 200, [tx("h0", "alice", "bob", "approve")])

        indexer = make_indexer(source=source)
        indexer.commit_range("chain-a", 0, 0, 1)
        with self.assertRaises(TransactionConflictError):
            indexer.commit_range("chain-a", 1, 1, 1)
        status = indexer.status("chain-a")
        self.assertEqual(status["committed_end_block"], 0)
        self.assertEqual(status["next_block"], 1)
        self.assertEqual(
            indexer.indexer.query(normalize_filters())["total"], 1
        )

    def test_conflict_stops_current_batch_keeps_prior_batches(self):
        def source(chain_id, height):
            if height < 2:
                return block(height, 100 * (height + 1),
                             [tx("ok%d" % height, "a", "b", "transfer")])
            # 与区块 0 中交易同哈希但不同接收地址
            return block(2, 300, [tx("ok0", "a", "c", "transfer")])

        indexer = make_indexer(source=source)
        with self.assertRaises(TransactionConflictError):
            indexer.commit_range("chain-a", 0, 2, 1)
        # 前序批次（0、1）已完整提交并保持有效；冲突批次水位不推进
        status = indexer.status("chain-a")
        self.assertEqual(status["committed_end_block"], 1)
        self.assertEqual(status["next_block"], 2)
        page = indexer.indexer.query(normalize_filters())
        self.assertEqual(
            [t["tx_hash"] for t in page["transactions"]], ["ok0", "ok1"]
        )

    def test_failed_batch_is_atomic(self):
        def source(chain_id, height):
            # 同一批内第二笔与第一笔冲突：整批不得留下任何记录
            return block(height, 100, [
                tx("x", "a", "b", "transfer"),
                tx("x", "a", "b", "approve"),
            ])

        indexer = make_indexer(source=source)
        with self.assertRaises(TransactionConflictError):
            indexer.commit_range("chain-a", 0, 0, 1)
        self.assertEqual(
            indexer.indexer.query(normalize_filters())["total"], 0
        )
        status = indexer.status("chain-a")
        self.assertIsNone(status["committed_start_block"])
        self.assertIsNone(status["committed_end_block"])


class SourceUnavailableTest(unittest.TestCase):
    def test_unavailable_source_keeps_committed_prefix(self):
        attempts = []

        def flaky(chain_id, height):
            attempts.append(height)
            if height >= 3:
                raise SourceUnavailableError("upstream down", None)
            return sample_source(chain_id, height)

        indexer = make_indexer(source=flaky)
        with self.assertRaises(SourceUnavailableError):
            indexer.commit_range("chain-a", 0, 4, 2)
        # 已完整提交的前序批次和水位保持有效
        status = indexer.status("chain-a")
        self.assertEqual(status["committed_start_block"], 0)
        self.assertEqual(status["committed_end_block"], 1)
        self.assertEqual(status["next_block"], 2)
        self.assertEqual(
            indexer.indexer.query(normalize_filters())["total"], 4
        )

        # 网络恢复后原样重放未完成范围
        indexer_ok = indexer

        def recovered(chain_id, height):
            return sample_source(chain_id, height)

        result = indexer_ok.commit_range("chain-a", 0, 4, 2,
                                         source=recovered)
        self.assertEqual(result["committed_end_block"], 4)
        self.assertEqual(
            indexer_ok.indexer.query(normalize_filters())["total"], 10
        )

    def test_none_block_treated_as_unavailable(self):
        indexer = make_indexer(source=lambda chain_id, height: None)
        with self.assertRaises(SourceUnavailableError):
            indexer.commit_range("chain-a", 0, 0, 1)

    def test_source_exception_wrapped_as_unavailable(self):
        def boom(chain_id, height):
            raise ConnectionError("reset")

        indexer = make_indexer(source=boom)
        with self.assertRaises(SourceUnavailableError):
            indexer.commit_range("chain-a", 0, 0, 1)


class StatusTest(unittest.TestCase):
    def test_fresh_chain_returns_empty_range(self):
        indexer = make_indexer()
        status = indexer.status("chain-a", start_block=7)
        self.assertIsNone(status["committed_start_block"])
        self.assertIsNone(status["committed_end_block"])
        self.assertEqual(status["next_block"], 7)
        self.assertIsNone(status["last_batch_time"])

    def test_fresh_chain_uses_configured_start(self):
        indexer = make_indexer(start_blocks={"chain-a": 3})
        status = indexer.status("chain-a")
        self.assertEqual(status["next_block"], 3)

    def test_status_after_commit(self):
        clock = FakeClock()
        indexer = make_indexer(clock=clock)
        indexer.commit_range("chain-a", 0, 4, 2)
        status = indexer.status("chain-a")
        self.assertEqual(status["committed_start_block"], 0)
        self.assertEqual(status["committed_end_block"], 4)
        self.assertEqual(status["next_block"], 5)
        self.assertIsNotNone(status["last_batch_time"])

    def test_status_is_read_only(self):
        indexer = make_indexer()
        indexer.commit_range("chain-a", 0, 1, 1)
        before = indexer.status("chain-a")
        indexer.status("chain-a", start_block=99)
        indexer.status("chain-b", start_block=3)
        after = indexer.status("chain-a")
        self.assertEqual(before, after)
        self.assertEqual(
            indexer.indexer.query(normalize_filters())["total"], 4
        )

    def test_watermark_never_skips_uncommitted_lower_blocks(self):
        indexer = make_indexer()
        indexer.commit_range("chain-a", 0, 1, 10)
        # 直接提交更高区间：水位不能越过尚未提交的 2
        indexer.commit_range("chain-a", 3, 4, 10)
        status = indexer.status("chain-a")
        self.assertEqual(status["committed_end_block"], 1)
        self.assertEqual(status["next_block"], 2)
        # 空洞补齐后水位覆盖整个连续已提交区间
        indexer.commit_range("chain-a", 2, 2, 10)
        status = indexer.status("chain-a")
        self.assertEqual(status["committed_start_block"], 0)
        self.assertEqual(status["committed_end_block"], 4)
        self.assertEqual(status["next_block"], 5)

    def test_chains_are_independent(self):
        indexer = make_indexer()
        indexer.commit_range("chain-a", 0, 1, 10)
        status = indexer.status("chain-b", start_block=0)
        self.assertIsNone(status["committed_end_block"])
        self.assertEqual(status["next_block"], 0)


class ConcurrencyTest(unittest.TestCase):
    def test_concurrent_same_range_merges_idempotently(self):
        indexer = make_indexer()
        results = []

        def run():
            results.append(indexer.commit_range("chain-a", 0, 4, 1))

        threads = [threading.Thread(target=run) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(
            indexer.indexer.query(normalize_filters())["total"], 10
        )
        for result in results:
            self.assertEqual(result["committed_end_block"], 4)

    def test_concurrent_disjoint_ranges_keep_contiguous_watermark(self):
        indexer = make_indexer()
        errors = []

        def run(start, end):
            try:
                indexer.commit_range("chain-a", start, end, 1)
            except Exception as exc:  # pragma: no cover - 便于诊断
                errors.append(exc)

        # 乱序并发提交不同范围，含暂时空洞
        threads = [
            threading.Thread(target=run, args=(3, 4)),
            threading.Thread(target=run, args=(0, 1)),
            threading.Thread(target=run, args=(2, 2)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        status = indexer.status("chain-a")
        self.assertEqual(status["committed_start_block"], 0)
        self.assertEqual(status["committed_end_block"], 4)
        self.assertEqual(status["next_block"], 5)
        self.assertEqual(
            indexer.indexer.query(normalize_filters())["total"], 10
        )


class ExistingBehaviorTest(unittest.TestCase):
    def test_queries_stats_and_cursors_unchanged(self):
        indexer = make_indexer()
        indexer.commit_range("chain-a", 0, 4, 2)
        engine = indexer.indexer

        filters = normalize_filters(address="alice")
        page = engine.query(filters, page_size=2)
        self.assertEqual(page["total"], 6)
        self.assertEqual(len(page["transactions"]), 2)
        self.assertIsNotNone(page["next_cursor"])
        rest = engine.query(filters, page_size=4,
                            cursor=page["next_cursor"])
        self.assertEqual(
            [t["tx_hash"] for t in page["transactions"]]
            + [t["tx_hash"] for t in rest["transactions"]],
            ["h0", "h1", "h2", "h3", "h6", "h7"],
        )

        stats = engine.stats(normalize_filters())
        self.assertEqual(stats["total_count"], 10)
        self.assertEqual(stats["total_amount"], "550")

        method_stats = engine.method_stats(normalize_filters())
        self.assertEqual(method_stats["total_groups"], 2)

    def test_shared_indexer_with_preloaded_records(self):
        preloaded = [{
            "tx_hash": "pre",
            "block_number": 0,
            "timestamp": 50,
            "from_address": "alice",
            "to_address": "bob",
            "method": "transfer",
            "amount": "5",
        }]
        engine = TxIndexer(list(preloaded))
        indexer = WatermarkIndexer(indexer=engine, source=sample_source,
                                   clock=FakeClock())
        # 相同哈希不同内容：与索引中既有记录冲突
        def source(chain_id, height):
            return block(0, 100, [
                tx("pre", "alice", "bob", "approve"),
            ])

        with self.assertRaises(TransactionConflictError):
            indexer.commit_range("chain-a", 0, 0, 1, source=source)
        self.assertEqual(engine.query(normalize_filters())["total"], 1)


if __name__ == "__main__":
    unittest.main()
