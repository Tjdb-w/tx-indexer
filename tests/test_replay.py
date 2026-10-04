"""索引水位与幂等重放测试。"""

import threading
import unittest

from tx_indexer.engine import normalize_filters
from tx_indexer.errors import (
    SourceUnavailableError,
    TransactionConflictError,
)
from tx_indexer.replay import ReplayManager


def tx(tx_hash, block_number, timestamp=100, frm="alice", to="bob",
       method="transfer", amount="10", **extra):
    """构造一笔上游交易（默认字段合法，可注入额外字段或覆盖）。"""
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


def make_fetcher(txs_by_height=None, fail_from=None, fail_heights=None,
                 log=None):
    """构造内存 fetch_blocks。

    - txs_by_height：height -> 交易列表（缺省每块一笔确定性交易）
    - fail_from：起始高度 >= 该值的批次直接抛 SourceUnavailableError
    - fail_heights：集合，命中其中任一高度的批次抛 SourceUnavailableError
    - log：记录每次调用的 (start, end)
    """
    txs_by_height = txs_by_height or {}
    fail_heights = fail_heights or set()

    def fetch(start, end):
        if log is not None:
            log.append((start, end))
        if fail_from is not None and start >= fail_from:
            raise SourceUnavailableError("上游暂时不可用", None)
        if any(h in fail_heights for h in range(start, end + 1)):
            raise SourceUnavailableError("区块暂时缺失", None)
        return [
            block(h, txs_by_height.get(h, [tx_for(h)]))
            for h in range(start, end + 1)
        ]

    return fetch


def heights(indexer):
    page = indexer.query(normalize_filters(), page_size=1000)
    return [t["block_number"] for t in page["transactions"]]


class SubmitValidationTest(unittest.TestCase):
    def setUp(self):
        self.manager = ReplayManager(make_fetcher())

    def test_start_greater_than_end(self):
        with self.assertRaises(ValueError):
            self.manager.submit("c", 5, 4, 10)

    def test_negative_start(self):
        with self.assertRaises(ValueError):
            self.manager.submit("c", -1, 4, 10)

    def test_negative_end(self):
        with self.assertRaises(ValueError):
            self.manager.submit("c", 0, -1, 10)

    def test_batch_size_boundaries(self):
        for bad in (0, -1, 1001, 1.5, True, False, "10", None):
            with self.assertRaises(ValueError):
                self.manager.submit("c", 0, 4, bad)

    def test_batch_size_boundaries_accepted(self):
        self.manager.submit("c", 0, 0, 1)
        manager = ReplayManager(make_fetcher())
        manager.submit("c", 0, 0, 1000)

    def test_chain_id_required(self):
        for bad in ("", "   ", None, 7):
            with self.assertRaises(ValueError):
                self.manager.submit(bad, 0, 1, 10)

    def test_start_end_must_be_ints(self):
        with self.assertRaises(ValueError):
            self.manager.submit("c", 1.0, 2, 10)
        with self.assertRaises(ValueError):
            self.manager.submit("c", 0, "2", 10)

    def test_missing_fetcher(self):
        with self.assertRaises(ValueError):
            ReplayManager().submit("c", 0, 1, 10)

    def test_gap_over_uncommitted_lower_block_rejected(self):
        self.manager.submit("c", 0, 2, 10)
        with self.assertRaises(ValueError):
            self.manager.submit("c", 5, 6, 10)


class BatchingAndWatermarkTest(unittest.TestCase):
    def test_ascending_batches_with_tail_partial(self):
        log = []
        manager = ReplayManager(make_fetcher(log=log))
        result = manager.submit("chain-a", 0, 9, 3)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(log, [(0, 2), (3, 5), (6, 8), (9, 9)])
        self.assertEqual(result["processed_batch_count"], 4)
        self.assertEqual(result["committed_count"], 10)
        self.assertEqual(result["skipped_count"], 0)
        self.assertEqual(result["committed_start_block"], 0)
        self.assertEqual(result["committed_end_block"], 9)
        self.assertEqual(result["next_block"], 10)
        self.assertIsInstance(result["last_batch_committed_at"], float)

    def test_empty_blocks_commit_and_advance(self):
        manager = ReplayManager(
            make_fetcher({2: [], 3: [tx_for(3)]})
        )
        result = manager.submit("c", 0, 3, 2)
        self.assertEqual(result["committed_count"], 3)
        self.assertEqual(result["committed_end_block"], 3)
        self.assertEqual(heights(manager.indexer), [0, 1, 3])

    def test_status_after_submit(self):
        manager = ReplayManager(make_fetcher())
        manager.submit("c", 10, 14, 2)
        status = manager.status("c")
        self.assertEqual(status["committed_start_block"], 10)
        self.assertEqual(status["committed_end_block"], 14)
        self.assertEqual(status["next_block"], 15)
        self.assertIsInstance(status["last_batch_committed_at"], float)

    def test_status_unknown_chain_uses_passed_start(self):
        manager = ReplayManager(make_fetcher())
        status = manager.status("never", start_block=42)
        self.assertIsNone(status["committed_start_block"])
        self.assertIsNone(status["committed_end_block"])
        self.assertEqual(status["next_block"], 42)
        self.assertIsNone(status["last_batch_committed_at"])

    def test_status_unknown_chain_without_start(self):
        manager = ReplayManager(make_fetcher())
        self.assertIsNone(manager.status("never")["next_block"])

    def test_status_is_read_only(self):
        manager = ReplayManager(make_fetcher())
        manager.status("never", start_block=3)
        manager.status("never", start_block=3)
        # 状态查询不创建链、不改变索引
        self.assertEqual(heights(manager.indexer), [])
        result = manager.submit("never", 3, 3, 1)
        self.assertEqual(result["committed_start_block"], 3)

    def test_status_invalid_start_block(self):
        manager = ReplayManager(make_fetcher())
        with self.assertRaises(ValueError):
            manager.status("c", start_block=-1)

    def test_status_does_not_scan_transactions(self):
        # 即便每块很多交易，状态入口开销与交易数无关：这里通过返回结构
        # 只暴露水位字段来固定「不扫描明细」的契约
        manager = ReplayManager(
            make_fetcher({h: [tx_for(h, i) for i in range(50)]
                          for h in range(0, 4)})
        )
        manager.submit("c", 0, 3, 2)
        self.assertEqual(
            set(manager.status("c").keys()),
            {"chain_id", "committed_start_block", "committed_end_block",
             "next_block", "last_batch_committed_at"},
        )

    def test_continue_from_determined_position(self):
        manager = ReplayManager(make_fetcher())
        manager.submit("c", 0, 4, 2)
        result = manager.submit("c", 5, 9, 4)
        self.assertEqual(result["committed_start_block"], 0)
        self.assertEqual(result["committed_end_block"], 9)
        self.assertEqual(result["committed_count"], 5)
        self.assertEqual(heights(manager.indexer), list(range(10)))

    def test_overlapping_range_only_processes_new_tail(self):
        log = []
        manager = ReplayManager(make_fetcher(log=log))
        manager.submit("c", 0, 4, 2)
        log.clear()
        result = manager.submit("c", 3, 6, 2)
        # 已提交的 3、4 不重新拉取，直接从下一待处理区块 5 继续
        self.assertEqual(log, [(5, 6)])
        self.assertEqual(result["committed_end_block"], 6)
        self.assertEqual(result["committed_count"], 2)


class SourceUnavailableTest(unittest.TestCase):
    def test_failure_after_committed_batches_keeps_watermark(self):
        log = []
        manager = ReplayManager(
            make_fetcher(fail_heights={6}, log=log)
        )
        with self.assertRaises(SourceUnavailableError):
            manager.submit("c", 0, 9, 3)
        # 批次 [0,2]、[3,5] 已完整提交；[6,8] 未提交
        self.assertEqual(log[:2], [(0, 2), (3, 5)])
        status = manager.status("c")
        self.assertEqual(status["committed_start_block"], 0)
        self.assertEqual(status["committed_end_block"], 5)
        self.assertEqual(status["next_block"], 6)
        self.assertEqual(heights(manager.indexer), list(range(6)))

    def test_replay_unfinished_range_as_is(self):
        fetcher = make_fetcher(fail_heights={6})
        manager = ReplayManager(fetcher)
        with self.assertRaises(SourceUnavailableError):
            manager.submit("c", 0, 9, 3)

        # 网络恢复后在同一管理器上原样重放未完成范围
        result = manager.submit("c", 6, 9, 3, fetch_blocks=make_fetcher())
        self.assertEqual(result["committed_end_block"], 9)
        self.assertEqual(result["committed_count"], 4)
        self.assertEqual(heights(manager.indexer), list(range(10)))

    def test_retry_full_range_is_idempotent(self):
        log = []
        manager = ReplayManager(make_fetcher(fail_heights={7}, log=log))
        with self.assertRaises(SourceUnavailableError):
            manager.submit("c", 0, 9, 3)
        # 恢复后用最初的完整参数重试：水位已覆盖的 0..5 不重新拉取，
        # 直接从未完成的 6 续跑，结果与一次性成功完全一致
        result = manager.submit(
            "c", 0, 9, 3, fetch_blocks=make_fetcher(log=log)
        )
        self.assertEqual(result["committed_end_block"], 9)
        self.assertEqual(result["committed_count"], 4)
        self.assertEqual(log[-2:], [(6, 8), (9, 9)])
        self.assertEqual(len(heights(manager.indexer)), 10)

    def test_missing_block_in_batch_response(self):
        def fetch(start, end):
            # 只返回请求区间里的第一块
            return [block(start, [tx_for(start)])]

        manager = ReplayManager(fetch)
        with self.assertRaises(SourceUnavailableError):
            manager.submit("c", 0, 3, 4)
        # 整批未提交
        self.assertIsNone(manager.status("c")["committed_end_block"])

    def test_first_batch_unavailable_leaves_empty_interval(self):
        manager = ReplayManager(make_fetcher(fail_from=0))
        with self.assertRaises(SourceUnavailableError):
            manager.submit("c", 5, 8, 2)
        status = manager.status("c", start_block=5)
        self.assertIsNone(status["committed_start_block"])
        self.assertIsNone(status["committed_end_block"])
        self.assertEqual(status["next_block"], 5)

    def test_failed_first_attempt_pins_configured_start(self):
        # 首个批次失败后链起点已配置：后来的高范围不能越过仍未提交的
        # 低区块；不带 start_block 的状态入口回退到该配置起点
        manager = ReplayManager(make_fetcher(fail_from=5))
        with self.assertRaises(SourceUnavailableError):
            manager.submit("c", 5, 8, 2)
        self.assertEqual(manager.status("c")["next_block"], 5)
        with self.assertRaises(ValueError):
            manager.submit("c", 6, 7, 2, fetch_blocks=make_fetcher())
        with self.assertRaises(ValueError):
            manager.submit("c", 9, 9, 1, fetch_blocks=make_fetcher())
        # 从配置起点原样重放可以成功并形成连续水位
        result = manager.submit("c", 5, 8, 2, fetch_blocks=make_fetcher())
        self.assertEqual(result["committed_start_block"], 5)
        self.assertEqual(result["committed_end_block"], 8)


class IdempotencyAndConflictTest(unittest.TestCase):
    def test_same_range_resubmitted_adds_nothing(self):
        manager = ReplayManager(make_fetcher())
        first = manager.submit("c", 0, 4, 2)
        second = manager.submit("c", 0, 4, 2)
        self.assertEqual(first["committed_end_block"],
                         second["committed_end_block"])
        self.assertEqual(second["processed_batch_count"], 0)
        self.assertEqual(second["committed_count"], 0)
        self.assertEqual(second["skipped_count"], 0)
        self.assertEqual(len(heights(manager.indexer)), 5)

    def test_duplicate_same_identity_skipped_within_batch(self):
        duplicate = tx_for(0)
        fetcher = make_fetcher({0: [tx_for(0), duplicate]})
        manager = ReplayManager(fetcher)
        result = manager.submit("c", 0, 0, 1)
        self.assertEqual(result["committed_count"], 1)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(len(heights(manager.indexer)), 1)

    def test_first_write_wins_even_if_amount_differs(self):
        # 金额不属于冲突判定的标准化身份字段；同哈希同身份在同一批再次
        # 出现时不新增记录，也不改变首次写入结果
        first = tx_for(2, amount="100")
        changed_amount = tx_for(2, amount="999")
        manager = ReplayManager(
            make_fetcher({2: [first, changed_amount]})
        )
        result = manager.submit("c", 0, 2, 5)
        self.assertEqual(result["committed_count"], 3)
        self.assertEqual(result["skipped_count"], 1)
        page = manager.indexer.query(normalize_filters(), page_size=1000)
        record = next(t for t in page["transactions"]
                      if t["tx_hash"] == "h-2-0")
        self.assertEqual(record["amount"], "100")

    def test_conflicting_block_number_stops_batch(self):
        # h-0-0 已在区块 0 提交；区块 2 中再次出现但声明高度为 2（与其
        # 首次出现的高度不一致）-> TransactionConflictError，所在整批拒绝
        conflicting = dict(tx_for(0), block_number=2)
        fetcher = make_fetcher({
            2: [tx_for(2), conflicting],
        })
        manager = ReplayManager(fetcher)
        manager.submit("c", 0, 1, 1)
        with self.assertRaises(TransactionConflictError):
            manager.submit("c", 2, 2, 1)
        # 冲突批次未提交、水位停在 1，批次内其它新交易也不可见
        self.assertEqual(manager.status("c")["committed_end_block"], 1)
        self.assertEqual(heights(manager.indexer), [0, 1])

    def test_conflicting_timestamp_address_or_method(self):
        manager = ReplayManager(make_fetcher({0: [tx_for(0)]}))
        manager.submit("c", 0, 0, 1)
        for field, value in (
            ("timestamp", 9999),
            ("from_address", "someone-else"),
            ("to_address", "another"),
            ("method", "upgrade"),
        ):
            changed = dict(tx_for(0))
            changed[field] = value
            # 让冲突交易出现在紧邻的新区块（身份不同 -> 冲突）
            changed["block_number"] = 1
            rm = ReplayManager(make_fetcher({0: [tx_for(0)]}))
            rm.submit("c", 0, 0, 1)
            with self.assertRaises(TransactionConflictError):
                rm.submit(
                    "c", 1, 1, 1,
                    fetch_blocks=make_fetcher({1: [changed]}),
                )
            self.assertEqual(rm.status("c")["committed_end_block"], 0)

    def test_conflict_in_later_batch_keeps_earlier_batches(self):
        changed = dict(tx_for(1), block_number=6)
        fetcher = make_fetcher({6: [tx_for(6), changed]})
        manager = ReplayManager(fetcher)
        with self.assertRaises(TransactionConflictError):
            manager.submit("c", 0, 9, 3)
        # [0,2]、[3,5] 已提交；冲突所在的 [6,8] 整批不提交
        self.assertEqual(manager.status("c")["committed_end_block"], 5)
        self.assertEqual(heights(manager.indexer), list(range(6)))

    def test_no_half_batch_on_validation_error(self):
        bad = {"tx_hash": "bad", "block_number": 1, "timestamp": 1,
               "from_address": "a", "to_address": "b", "method": "m"}
        # bad 缺 amount；批次内它前面有一笔合法交易，也不得写入
        fetcher = make_fetcher({1: [tx_for(1), bad]})
        manager = ReplayManager(fetcher)
        manager.submit("c", 0, 0, 1)
        with self.assertRaises(ValueError):
            manager.submit("c", 1, 1, 1)
        self.assertEqual(manager.status("c")["committed_end_block"], 0)
        self.assertEqual(heights(manager.indexer), [0])

    def test_normalization_strips_extra_fields(self):
        rich = tx_for(0, fee="1", success=True, block_hash="0xabc")
        manager = ReplayManager(make_fetcher({0: [rich]}))
        manager.submit("c", 0, 0, 1)
        record = manager.indexer.query(
            normalize_filters(), page_size=10
        )["transactions"][0]
        self.assertEqual(
            set(record.keys()),
            {"tx_hash", "block_number", "timestamp", "from_address",
             "to_address", "method", "amount"},
        )


class FetcherResponseValidationTest(unittest.TestCase):
    def test_out_of_range_block_rejected(self):
        def fetch(start, end):
            blocks = [block(h) for h in range(start, end + 1)]
            blocks.append(block(end + 1))
            return blocks

        manager = ReplayManager(fetch)
        with self.assertRaises(ValueError):
            manager.submit("c", 0, 1, 5)

    def test_duplicate_height_in_response_rejected(self):
        def fetch(start, end):
            return [block(start), block(start)]

        manager = ReplayManager(fetch)
        with self.assertRaises(ValueError):
            manager.submit("c", 0, 0, 1)

    def test_transactions_must_be_list(self):
        manager = ReplayManager(
            lambda s, e: [{"block_number": s, "transactions": {}}]
        )
        with self.assertRaises(ValueError):
            manager.submit("c", 0, 0, 1)

    def test_transaction_block_number_must_match(self):
        wrong = dict(tx_for(0), block_number=1)
        manager = ReplayManager(make_fetcher({0: [wrong]}))
        with self.assertRaises(ValueError):
            manager.submit("c", 0, 0, 1)


class QueryCompatibilityTest(unittest.TestCase):
    def test_query_ordering_and_filters_unchanged(self):
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
        manager = ReplayManager(make_fetcher(txs))
        manager.submit("c", 0, 1, 5)
        indexer = manager.indexer

        page = indexer.query(normalize_filters())
        self.assertEqual(
            [(t["block_number"], t["tx_hash"]) for t in page["transactions"]],
            [(0, "a1"), (0, "a2"), (1, "a3")],
        )
        self.assertEqual(page["total"], 3)
        self.assertIsNone(page["next_cursor"])

        alice = indexer.query(normalize_filters(address="alice"))
        self.assertEqual(
            [t["tx_hash"] for t in alice["transactions"]],
            ["a1", "a2", "a3"],
        )
        transfer = indexer.query(normalize_filters(method="transfer"))
        self.assertEqual(
            [t["tx_hash"] for t in transfer["transactions"]], ["a1", "a3"]
        )
        window = indexer.query(
            normalize_filters(start_time=15, end_time=30)
        )
        self.assertEqual(
            [t["tx_hash"] for t in window["transactions"]], ["a2", "a3"]
        )

        stats = indexer.stats(normalize_filters())
        self.assertEqual(stats["total_count"], 3)
        self.assertEqual(stats["total_amount"], "112")
        self.assertEqual(stats["avg_amount"], "37")

    def test_cursor_paging_unchanged(self):
        manager = ReplayManager(make_fetcher())
        manager.submit("c", 0, 4, 10)
        indexer = manager.indexer
        first = indexer.query(normalize_filters(), page_size=2)
        self.assertEqual(len(first["transactions"]), 2)
        self.assertEqual(first["total"], 5)
        second = indexer.query(
            normalize_filters(), page_size=2, cursor=first["next_cursor"]
        )
        self.assertEqual(
            [t["block_number"] for t in second["transactions"]], [2, 3]
        )
        third = indexer.query(
            normalize_filters(), page_size=2, cursor=second["next_cursor"]
        )
        self.assertEqual(
            [t["block_number"] for t in third["transactions"]], [4]
        )
        self.assertIsNone(third["next_cursor"])

    def test_aggregations_unchanged(self):
        manager = ReplayManager(make_fetcher())
        manager.submit("c", 0, 3, 10)
        indexer = manager.indexer
        methods = indexer.method_stats(normalize_filters())
        self.assertEqual(
            sorted(g["method"] for g in methods["groups"]),
            ["m0", "m1", "m2"],
        )
        self.assertEqual(methods["total_groups"], 3)
        pairs = indexer.pair_stats(normalize_filters())
        self.assertEqual(pairs["total_groups"], 4)
        time_stats = indexer.time_stats(
            normalize_filters(), bucket_size=1000
        )
        # 时间戳 1000..1003 同处 [1000, 2000) 区间
        self.assertEqual(time_stats["total_groups"], 1)
        self.assertEqual(time_stats["groups"][0]["total_count"], 4)


class MultiChainTest(unittest.TestCase):
    def test_chains_have_independent_watermarks(self):
        manager = ReplayManager(make_fetcher())
        manager.submit("chain-a", 0, 4, 2)
        manager.submit("chain-b", 10, 11, 2)
        self.assertEqual(manager.status("chain-a")["committed_end_block"], 4)
        self.assertEqual(manager.status("chain-b")["committed_start_block"], 10)
        self.assertEqual(manager.status("chain-b")["next_block"], 12)
        # 未知链不受其它链影响
        self.assertIsNone(manager.status("chain-c")["committed_end_block"])

    def test_same_hash_same_content_shared_across_chains(self):
        # tx_hash 是全局身份：不同链提交同哈希同标准化内容时按重复跳过，
        # 索引中不出现两条记录
        shared = tx_for(0)
        data = {0: [shared]}
        manager = ReplayManager(make_fetcher(data))
        r1 = manager.submit("chain-a", 0, 0, 1)
        r2 = manager.submit("chain-b", 0, 0, 1)
        self.assertEqual(r1["committed_count"], 1)
        self.assertEqual(r2["committed_count"], 0)
        self.assertEqual(r2["skipped_count"], 1)
        self.assertEqual(len(heights(manager.indexer)), 1)


class ConcurrencyTest(unittest.TestCase):
    def test_higher_range_waits_for_lower_range(self):
        gate = threading.Event()
        calls = []

        def fetch(start, end):
            calls.append((start, end))
            if start == 0:
                gate.wait(5)
            return [block(h, [tx_for(h)]) for h in range(start, end + 1)]

        manager = ReplayManager(fetch)
        errors = []

        def low():
            try:
                manager.submit("c", 0, 1, 1)
            except Exception as exc:  # pragma: no cover - 失败时记录
                errors.append(exc)

        def high():
            try:
                manager.submit("c", 2, 3, 1)
            except Exception as exc:  # pragma: no cover - 失败时记录
                errors.append(exc)

        t_low = threading.Thread(target=low)
        t_high = threading.Thread(target=high)
        t_low.start()
        # 等低范围进入首批拉取后再启动高范围
        while not calls:
            threading.Event().wait(0.001)
        t_high.start()
        threading.Event().wait(0.1)
        # 高范围不能越过未提交的低区块：水位仍为空
        self.assertIsNone(manager.status("c")["committed_end_block"])
        gate.set()
        t_low.join(5)
        t_high.join(5)
        self.assertFalse(errors)
        self.assertEqual(t_low.is_alive(), False)
        self.assertEqual(t_high.is_alive(), False)
        self.assertEqual(manager.status("c")["committed_end_block"], 3)
        self.assertEqual(calls, [(0, 0), (1, 1), (2, 2), (3, 3)])
        self.assertEqual(heights(manager.indexer), [0, 1, 2, 3])

    def test_identical_concurrent_ranges_merge(self):
        gate = threading.Event()
        fetch_count = [0]
        lock = threading.Lock()

        def fetch(start, end):
            with lock:
                fetch_count[0] += 1
                first = fetch_count[0] == 1
            if first:
                gate.wait(5)
            return [block(h, [tx_for(h)]) for h in range(start, end + 1)]

        manager = ReplayManager(fetch)
        results = [None, None]

        def run(slot):
            results[slot] = manager.submit("c", 0, 3, 2)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        threading.Event().wait(0.1)
        gate.set()
        for t in threads:
            t.join(5)

        for result in results:
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["committed_end_block"], 3)
        # 只有一个提交者真正拉取执行了 2 个批次，另一个合并为幂等结果
        self.assertEqual(fetch_count[0], 2)
        batches = sorted(r["processed_batch_count"] for r in results)
        self.assertEqual(batches, [0, 2])
        self.assertEqual(len(heights(manager.indexer)), 4)

    def test_different_chains_do_not_block_each_other(self):
        gate_a = threading.Event()

        def fetch_a(start, end):
            gate_a.wait(5)
            return [block(h, [tx_for(h)]) for h in range(start, end + 1)]

        manager = ReplayManager(fetch_a)
        done_b = threading.Event()

        def run_a():
            manager.submit("chain-a", 0, 1, 2)

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
        # 不同链不互相阻塞：B 应在 A 被挂起期间完成
        self.assertTrue(done_b.wait(2))
        self.assertIsNone(manager.status("chain-a")["committed_end_block"])
        self.assertEqual(manager.status("chain-b")["committed_end_block"], 11)
        gate_a.set()
        t_a.join(5)
        t_b.join(5)
        self.assertEqual(manager.status("chain-a")["committed_end_block"], 1)

    def test_concurrent_conflict_leaves_continuous_watermark(self):
        # 低范围正常；高范围在等待后执行，其交易与低范围冲突时整批拒绝，
        # 已提交的连续区间保持有效
        gate = threading.Event()

        def fetch_low(start, end):
            gate.wait(5)
            return [block(0, [tx_for(0)])]

        manager = ReplayManager(fetch_low)
        conflict_error = []

        def low():
            manager.submit("c", 0, 0, 1)

        def high():
            try:
                conflicting = dict(tx_for(0), block_number=1)
                manager.submit(
                    "c", 1, 1, 1,
                    fetch_blocks=make_fetcher({1: [conflicting]}),
                )
            except TransactionConflictError as exc:
                conflict_error.append(exc)

        t_low = threading.Thread(target=low)
        t_high = threading.Thread(target=high)
        t_low.start()
        threading.Event().wait(0.1)
        t_high.start()
        gate.set()
        t_low.join(5)
        t_high.join(5)
        self.assertEqual(len(conflict_error), 1)
        status = manager.status("c")
        self.assertEqual(status["committed_start_block"], 0)
        self.assertEqual(status["committed_end_block"], 0)
        self.assertEqual(status["next_block"], 1)
        self.assertEqual(heights(manager.indexer), [0])


if __name__ == "__main__":
    unittest.main()
