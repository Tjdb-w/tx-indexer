"""索引水位与幂等重放。

在查询索引之上提供按链的增量提交入口：同一段链上历史可以被安全地重复
提交（幂等重放），网络中断等上游故障之后也可以从确定位置继续。

公开入口：

- :meth:`WatermarkIndexer.commit_range`：接受链标识、起始区块、结束区块
  和批量大小，按起始区块升序到结束区块的顺序逐批处理。每个批次先从数据
  源取块、校验并标准化交易，再把交易原子地写入现有索引，最后推进该链
  的已提交水位。批次只在整批成功后提交，中途失败不会留下半批结果。
- :meth:`WatermarkIndexer.status`：只读的索引状态入口，返回已提交起始
  区块、已提交结束区块、下一待处理区块和最近成功批次时间；不扫描交易
  明细，也不改变索引。

标准化结果保留交易哈希、区块高度、区块时间、发起地址、接收地址和方法
标识（另保留 ``amount`` 以维持既有查询与聚合口径）；已有查询字段、筛选
语义、排序与分页游标行为不受影响。

错误约定：

- 参数非法（起始区块大于结束区块、起始/结束区块小于零、批量大小不在
  1..1000）抛 :class:`ValueError`；
- 上游暂时无法返回指定区块抛
  :class:`~tx_indexer.errors.SourceUnavailableError`，已完整提交的前序
  批次和水位保持有效，下一次可以原样重放未完成范围；
- 相同交易哈希与相同标准化内容再次出现时视为重复，不新增记录，也不改变
  首次写入结果；相同交易哈希却出现不同区块高度、时间、地址或方法标识时
  抛 :class:`~tx_indexer.errors.TransactionConflictError`，并停止当前
  批次，不推进到冲突交易所在批次的水位。

并发：同一链的提交入口按链串行。相同范围可以合并为幂等结果；不同范围
不能越过尚未提交的低区块形成更高水位——后到请求等待前一范围完成后继续，
最终水位始终覆盖连续已提交区间。
"""

import threading
import time

from .engine import TxIndexer
from .errors import SourceUnavailableError, TransactionConflictError
from .loader import _AMOUNT_RE, _is_nonneg_int, _is_nonempty_text

#: 批量大小合法范围（闭区间）
MIN_BATCH_SIZE = 1
MAX_BATCH_SIZE = 1000

#: 标准化交易保留的字段（amount 用于维持既有查询/聚合口径）
_NORMALIZED_FIELDS = (
    "tx_hash",
    "block_number",
    "timestamp",
    "from_address",
    "to_address",
    "method",
    "amount",
)

#: 重复/冲突判定的决定字段：区块高度、时间、地址、方法标识
_CONFLICT_FIELDS = (
    "block_number",
    "timestamp",
    "from_address",
    "to_address",
    "method",
)


def _validate_range(chain_id, start_block, end_block, batch_size):
    """校验提交入口的公开参数；非法时抛 ValueError。"""
    if not _is_nonempty_text(chain_id):
        raise ValueError("chain_id 必须为非空字符串")
    if not _is_nonneg_int(start_block):
        raise ValueError("start_block 必须为非负整数")
    if not _is_nonneg_int(end_block):
        raise ValueError("end_block 必须为非负整数")
    if start_block > end_block:
        raise ValueError(
            "start_block(%d) 不能大于 end_block(%d)"
            % (start_block, end_block)
        )
    if (
        not isinstance(batch_size, int)
        or isinstance(batch_size, bool)
        or batch_size < MIN_BATCH_SIZE
        or batch_size > MAX_BATCH_SIZE
    ):
        raise ValueError(
            "batch_size 必须为 %d 到 %d 之间的整数"
            % (MIN_BATCH_SIZE, MAX_BATCH_SIZE)
        )


def _interpret_block(block, height):
    """把数据源返回值归一化为 ``(block_timestamp, transactions)``。

    数据源可以返回区块字典（``block_number`` 缺省时视为请求高度，给出时
    必须等于请求高度；``timestamp`` 与 ``transactions`` 可缺省），也可以
    直接返回交易数组。结构非法抛 ValueError。
    """
    if isinstance(block, list):
        return None, block
    if not isinstance(block, dict):
        raise ValueError("区块 %d 的数据必须是 JSON 对象或交易数组" % height)
    number = block.get("block_number", height)
    if number != height:
        raise ValueError(
            "区块数据的高度 %r 与请求高度 %d 不一致" % (number, height)
        )
    timestamp = block.get("timestamp")
    if timestamp is not None and not _is_nonneg_int(timestamp):
        raise ValueError("区块 %d 的 timestamp 必须为非负整数" % height)
    txs = block.get("transactions", [])
    if not isinstance(txs, list):
        raise ValueError("区块 %d 的 transactions 必须为数组" % height)
    return timestamp, txs


def _normalize_tx(tx, height, block_timestamp, index):
    """校验并标准化一笔交易；非法时抛 ValueError。

    标准化结果保留交易哈希、区块高度、区块时间、发起地址、接收地址和
    方法标识；``amount`` 缺省为 ``"0"``，用于维持既有查询与聚合口径。
    """
    if not isinstance(tx, dict):
        raise ValueError(
            "区块 %d 的第 %d 笔交易必须是 JSON 对象" % (height, index)
        )
    for name in ("tx_hash", "from_address", "to_address", "method"):
        if not _is_nonempty_text(tx.get(name)):
            raise ValueError(
                "区块 %d 的第 %d 笔交易 %s 必须为非空字符串"
                % (height, index, name)
            )
    timestamp = tx.get("timestamp", block_timestamp)
    if not _is_nonneg_int(timestamp):
        raise ValueError(
            "区块 %d 的第 %d 笔交易缺少非负整数区块时间" % (height, index)
        )
    amount = tx.get("amount", "0")
    if not isinstance(amount, str) or _AMOUNT_RE.fullmatch(amount) is None:
        raise ValueError(
            "区块 %d 的第 %d 笔交易 amount 必须为非负十进制整数字符串"
            % (height, index)
        )
    return {
        "tx_hash": tx["tx_hash"],
        "block_number": height,
        "timestamp": timestamp,
        "from_address": tx["from_address"],
        "to_address": tx["to_address"],
        "method": tx["method"],
        "amount": amount,
    }


def _fetch_block(source, chain_id, height):
    """从数据源取一个区块；上游暂时不可用抛 SourceUnavailableError。"""
    try:
        block = source(chain_id, height)
    except SourceUnavailableError:
        raise
    except Exception as exc:
        raise SourceUnavailableError(
            "上游暂时无法返回区块 %d" % height, None
        ) from exc
    if block is None:
        raise SourceUnavailableError(
            "上游暂时无法返回区块 %d" % height, None
        )
    return block


def _conflicts(existing, tx):
    """已存在记录与新交易在决定字段上是否不一致。"""
    return any(existing[name] != tx[name] for name in _CONFLICT_FIELDS)


class _ChainState:
    """单条链的提交状态：已提交区块集合与连续水位。"""

    __slots__ = (
        "configured_start",
        "committed",
        "committed_start",
        "committed_end",
        "last_batch_time",
    )

    def __init__(self, configured_start=None):
        #: 该链已配置的起始区块（构造配置或首次提交确定）
        self.configured_start = configured_start
        #: 已提交成功的区块高度集合（可能含水位上方的空洞区间）
        self.committed = set()
        #: 连续已提交区间 [committed_start, committed_end]；为空时为 None
        self.committed_start = None
        self.committed_end = None
        #: 最近成功批次时间（clock() 的返回值）；尚未有成功批次时为 None
        self.last_batch_time = None

    def recompute_watermark(self):
        """重算连续已提交区间：水位不能越过尚未提交的低区块。"""
        if not self.committed:
            self.committed_start = None
            self.committed_end = None
            return
        start = min(self.committed)
        end = start
        while end + 1 in self.committed:
            end += 1
        self.committed_start = start
        self.committed_end = end


class WatermarkIndexer:
    """索引水位与幂等重放器：把链上历史按批次原子地写入查询索引。

    通过 :attr:`indexer` 暴露底层 :class:`~tx_indexer.engine.TxIndexer`；
    批次提交成功后，查询与聚合立即观察到本批全部交易。

    ``source`` 为取数回调：``source(chain_id, block_number)`` 返回区块
    字典（含 ``transactions``，可选 ``block_number`` / ``timestamp``）或
    交易数组；返回 ``None`` 或抛出异常视为上游暂时不可用。``start_blocks``
    可预配置各链的起始区块；``clock`` 用于记录最近成功批次时间，默认
    ``time.time``。
    """

    def __init__(self, indexer=None, source=None, start_blocks=None,
                 clock=None):
        self._indexer = indexer if indexer is not None else TxIndexer([])
        self._source = source
        self._start_blocks = dict(start_blocks) if start_blocks else {}
        self._clock = clock if clock is not None else time.time
        self._chains = {}
        self._locks = {}
        self._locks_guard = threading.Lock()
        # tx_hash -> 已写入的标准化交易（含索引中既有记录，用于判重/冲突）
        self._by_hash = {}
        for record in getattr(self._indexer, "_records", []):
            self._by_hash.setdefault(record["tx_hash"], record)

    @property
    def indexer(self):
        """底层查询索引；与提交共享同一份记录存储。"""
        return self._indexer

    def _lock_for(self, chain_id):
        with self._locks_guard:
            lock = self._locks.get(chain_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[chain_id] = lock
            return lock

    def _state_for(self, chain_id):
        state = self._chains.get(chain_id)
        if state is None:
            state = _ChainState(self._start_blocks.get(chain_id))
            self._chains[chain_id] = state
        return state

    def commit_range(self, chain_id, start_block, end_block, batch_size,
                     source=None):
        """把 ``[start_block, end_block]`` 的链上历史提交到索引。

        按起始区块升序到结束区块的顺序，每 ``batch_size`` 个区块为一批：
        先校验并标准化交易，再整批原子写入索引，最后推进该链的已提交
        水位。已提交的区块直接跳过（幂等），重复提交同一范围得到相同
        结果。返回机器可读的提交结果。
        """
        _validate_range(chain_id, start_block, end_block, batch_size)
        source = source if source is not None else self._source
        if source is None:
            raise ValueError("缺少数据源：请在构造时或调用时提供 source")

        lock = self._lock_for(chain_id)
        with lock:
            state = self._state_for(chain_id)
            if state.configured_start is None:
                state.configured_start = start_block

            imported = 0
            skipped = 0
            height = start_block
            while height <= end_block:
                batch_end = min(height + batch_size - 1, end_block)
                pending = [
                    h for h in range(height, batch_end + 1)
                    if h not in state.committed
                ]
                if not pending:
                    # 整批已提交：幂等合并，不再访问上游
                    height = batch_end + 1
                    continue

                # 1. 取数：任一区块不可用则本批失败，水位保持，
                #    已完整提交的前序批次不受影响
                blocks = [
                    (h, _fetch_block(source, chain_id, h)) for h in pending
                ]

                # 2. 校验并标准化（任何非法都在写入之前抛出）
                normalized = []
                for h, raw in blocks:
                    block_timestamp, txs = _interpret_block(raw, h)
                    for index, tx in enumerate(txs):
                        normalized.append(
                            _normalize_tx(tx, h, block_timestamp, index)
                        )

                # 3. 冲突检测：对照已写入记录与本批内部；相同哈希相同
                #    内容为重复，相同哈希不同决定字段为冲突
                seen_in_batch = {}
                for tx in normalized:
                    tx_hash = tx["tx_hash"]
                    existing = self._by_hash.get(tx_hash)
                    if existing is None:
                        existing = seen_in_batch.get(tx_hash)
                    if existing is not None and _conflicts(existing, tx):
                        raise TransactionConflictError(
                            "tx_hash %s 已存在且区块高度、时间、地址或"
                            "方法标识不一致" % tx_hash,
                            None,
                        )
                    seen_in_batch.setdefault(tx_hash, tx)

                # 4. 全部校验通过，原子写入：重复交易跳过且不覆盖
                new_records = []
                for tx in normalized:
                    tx_hash = tx["tx_hash"]
                    if tx_hash in self._by_hash:
                        skipped += 1
                        continue
                    self._by_hash[tx_hash] = tx
                    new_records.append(tx)
                    imported += 1
                if new_records:
                    self._indexer.append_records(new_records)

                # 5. 推进该链的已提交水位（只覆盖连续已提交区间）
                state.committed.update(pending)
                state.recompute_watermark()
                state.last_batch_time = self._clock()

                height = batch_end + 1

            return {
                "status": "ok",
                "chain_id": chain_id,
                "committed_start_block": state.committed_start,
                "committed_end_block": state.committed_end,
                "next_block": state.committed_end + 1,
                "imported_count": imported,
                "skipped_count": skipped,
            }

    #: commit_range 的别名
    def commit(self, chain_id, start_block, end_block, batch_size,
               source=None):
        return self.commit_range(
            chain_id, start_block, end_block, batch_size, source=source
        )

    def status(self, chain_id, start_block=None):
        """只读的索引状态入口：不扫描交易明细，也不改变索引。

        返回已提交起始区块、已提交结束区块、下一待处理区块和最近成功
        批次时间。尚未开始索引的链返回已提交区间为空，下一待处理区块
        等于本次传入的起始区块或该链已配置的起始区块。
        """
        if not _is_nonempty_text(chain_id):
            raise ValueError("chain_id 必须为非空字符串")
        if start_block is not None and not _is_nonneg_int(start_block):
            raise ValueError("start_block 必须为非负整数")

        state = self._chains.get(chain_id)
        if state is None:
            return {
                "chain_id": chain_id,
                "committed_start_block": None,
                "committed_end_block": None,
                "next_block": (
                    start_block
                    if start_block is not None
                    else self._start_blocks.get(chain_id)
                ),
                "last_batch_time": None,
            }

        lock = self._lock_for(chain_id)
        with lock:
            if state.committed_start is None:
                next_block = (
                    start_block
                    if start_block is not None
                    else state.configured_start
                )
                return {
                    "chain_id": chain_id,
                    "committed_start_block": None,
                    "committed_end_block": None,
                    "next_block": next_block,
                    "last_batch_time": state.last_batch_time,
                }
            return {
                "chain_id": chain_id,
                "committed_start_block": state.committed_start,
                "committed_end_block": state.committed_end,
                "next_block": state.committed_end + 1,
                "last_batch_time": state.last_batch_time,
            }
