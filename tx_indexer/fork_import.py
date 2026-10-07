"""可重放的分批增量导入与链分叉修正。

导入入口 :meth:`ForkAwareImporter.import_batches` 接收一个批次（或一批
批次），每个批次带来源链标识、批次序号、父区块哈希、区块哈希、区块高度
与该区块内的交易列表；交易列表沿用公开交易记录的字段与语义（见
:mod:`tx_indexer.loader`：``tx_hash`` / ``block_number`` / ``timestamp``
/ ``from_address`` / ``to_address`` / ``method`` / ``amount`` 必填，
``success`` 可选且只能是布尔值）。

批次结构（未定义的额外字段被忽略）：

- ``chain_id``：非空字符串，来源链标识；一次导入的批次必须属于同一条链
- ``batch_seq``：正整数，批次序号（幂等判重键的一部分）
- ``parent_hash``：非空字符串，父区块哈希
- ``block_hash``：非空字符串，本批区块哈希
- ``block_height``：非负整数，本批区块高度
- ``transactions``：数组，该区块内的交易列表，按原始顺序；每笔交易的
  ``block_number`` 必须等于批次 ``block_height``

连续与幂等：

- 批次连续时按区块高度顺序建立或更新索引；首个批次的父区块哈希必须等于
  引擎当前末端区块哈希，否则抛
  :class:`~tx_indexer.errors.BlockLinkMismatchError`，导入前的索引、
  统计与查询结果保持不变（新来源链的首个批次作为链根，不校验父哈希）。
- 重复提交来源链标识、区块哈希与批次序号均相同的批次时返回已应用状态
  （``status == "already_applied"``），不重复写入交易，也不重复累计统计。

链分叉修正：同一条来源链出现更高区块高度的新分支时（首个批次的父区块
哈希命中当前链上某一较早区块，且新分支末端高度超过当前末端高度），引擎
先撤销从分叉点开始被替代区块中交易的地址索引、方法索引、时间窗索引与
聚合贡献，再应用新分支。回滚期间交易哈希相同仍按当前分支内去重：保留
前缀与新分支内已存在的交易哈希不重复写入、不重复累计。

返回结构（机器可读字段稳定）：

.. code-block:: json

    {
      "status": "applied",
      "chain_id": "chain-a",
      "rolled_back_block_count": 2,
      "applied_block_count": 3,
      "tip_block_height": 12,
      "tip_block_hash": "0xc12"
    }

- ``rolled_back_block_count``：本次回滚的区块数量（普通追加为 0）
- ``applied_block_count``：本次实际应用的区块数量
- ``tip_block_hash`` / ``tip_block_height``：当前末端区块哈希与高度
- 已应用状态：``status == "already_applied"``，两个计数均为 0，末端
  字段为当前链尖

确定性错误（都在任何状态修改之前抛出，索引、统计与查询结果不变）：

- 来源链标识为空、区块高度小于零、批次序号非正数、批次内出现重复区块
  哈希，或批次/交易结构不符公开语义 →
  :class:`~tx_indexer.errors.InvalidImportBatchError`
- 父区块哈希与当前末端区块哈希不一致（且不构成可接受的更高新分支）→
  :class:`~tx_indexer.errors.BlockLinkMismatchError`

分叉修正生效后，所有查询只反映当前有效分支，聚合数值与明细过滤结果保持
一致；此前生成的分页游标如果指向已回滚交易，query 抛
:class:`~tx_indexer.errors.CursorOutOfRangeError`，不返回旧分支数据。
"""

from .engine import TxIndexer
from .errors import BlockLinkMismatchError, InvalidImportBatchError
from .loader import _AMOUNT_RE, _is_nonneg_int, _is_nonempty_text

#: 批次必填字段（额外字段被忽略）
_BATCH_REQUIRED = (
    "chain_id",
    "batch_seq",
    "parent_hash",
    "block_hash",
    "block_height",
    "transactions",
)

#: 交易记录字段：与公开交易记录（JSON Lines）完全一致
_TX_FIELDS = (
    "tx_hash",
    "block_number",
    "timestamp",
    "from_address",
    "to_address",
    "method",
    "amount",
)
_TX_OPTIONAL = ("success",)


def _invalid(message):
    return InvalidImportBatchError(message, None)


def _validate_tx(tx, index, block_height):
    """按公开交易记录语义校验并归一化一笔交易；非法抛 InvalidImportBatchError。"""
    if not isinstance(tx, dict):
        raise _invalid("transactions[%d] 必须是 JSON 对象" % index)
    keys = set(tx.keys())
    missing = [f for f in _TX_FIELDS if f not in keys]
    if missing:
        raise _invalid(
            "transactions[%d] 缺少字段：%s" % (index, ", ".join(missing))
        )
    extra = sorted(keys - set(_TX_FIELDS) - set(_TX_OPTIONAL))
    if extra:
        raise _invalid(
            "transactions[%d] 存在未定义字段：%s" % (index, ", ".join(extra))
        )

    if not _is_nonempty_text(tx["tx_hash"]):
        raise _invalid("transactions[%d] tx_hash 必须为非空字符串" % index)
    if not _is_nonneg_int(tx["block_number"]):
        raise _invalid(
            "transactions[%d] block_number 必须为非负整数" % index
        )
    if tx["block_number"] != block_height:
        raise _invalid(
            "transactions[%d] block_number 与批次区块高度不一致" % index
        )
    if not _is_nonneg_int(tx["timestamp"]):
        raise _invalid(
            "transactions[%d] timestamp 必须为非负 UTC 秒整数" % index
        )
    for name in ("from_address", "to_address", "method"):
        if not _is_nonempty_text(tx[name]):
            raise _invalid(
                "transactions[%d] %s 必须为非空字符串" % (index, name)
            )
    amount = tx["amount"]
    if not isinstance(amount, str) or _AMOUNT_RE.fullmatch(amount) is None:
        raise _invalid(
            "transactions[%d] amount 必须为非负十进制整数字符串" % index
        )
    success = tx.get("success", True)
    if not isinstance(success, bool):
        raise _invalid(
            "transactions[%d] success 必须为布尔值 true 或 false" % index
        )

    return {
        "tx_hash": tx["tx_hash"],
        "block_number": tx["block_number"],
        "timestamp": tx["timestamp"],
        "from_address": tx["from_address"],
        "to_address": tx["to_address"],
        "method": tx["method"],
        "amount": amount,
        "success": success,
    }


def _validate_batch(batch, index):
    """校验单个批次结构，返回归一化批次字典；非法抛 InvalidImportBatchError。"""
    where = "batches[%d]" % index
    if not isinstance(batch, dict):
        raise _invalid("%s 必须是 JSON 对象" % where)
    missing = [f for f in _BATCH_REQUIRED if f not in batch]
    if missing:
        raise _invalid("%s 缺少字段：%s" % (where, ", ".join(missing)))

    chain_id = batch["chain_id"]
    if not _is_nonempty_text(chain_id):
        raise _invalid("%s chain_id（来源链标识）不能为空" % where)
    batch_seq = batch["batch_seq"]
    if (
        not isinstance(batch_seq, int)
        or isinstance(batch_seq, bool)
        or batch_seq <= 0
    ):
        raise _invalid("%s batch_seq（批次序号）必须为正整数" % where)
    block_height = batch["block_height"]
    if not _is_nonneg_int(block_height):
        raise _invalid(
            "%s block_height（区块高度）必须为非负整数" % where
        )
    for name in ("parent_hash", "block_hash"):
        if not _is_nonempty_text(batch[name]):
            raise _invalid("%s %s 必须为非空字符串" % (where, name))
    txs = batch["transactions"]
    if not isinstance(txs, list):
        raise _invalid("%s transactions 必须为数组" % where)

    normalized = [
        _validate_tx(tx, tx_index, block_height)
        for tx_index, tx in enumerate(txs)
    ]
    return {
        "chain_id": chain_id,
        "batch_seq": batch_seq,
        "parent_hash": batch["parent_hash"],
        "block_hash": batch["block_hash"],
        "block_height": block_height,
        "transactions": normalized,
    }


class _ChainState:
    """单条来源链的当前有效分支状态。"""

    __slots__ = ("blocks", "by_hash", "applied", "root_parent")

    def __init__(self):
        # 当前有效分支的区块（按高度升序）：每项为
        # {"batch_seq", "parent_hash", "block_hash", "block_height",
        #  "records": [写入索引的交易记录]}
        self.blocks = []
        # 当前分支内 tx_hash -> 交易记录（分支内去重台账）
        self.by_hash = {}
        # 已应用批次幂等键集合：(block_hash, batch_seq)
        self.applied = set()
        # 链根区块的父区块哈希（分叉点可以位于根区块之前）
        self.root_parent = None

    @property
    def tip(self):
        return self.blocks[-1] if self.blocks else None

    def fork_index(self, parent_hash):
        """parent_hash 作为分叉点时被保留的最后一个区块下标。

        命中某个已应用区块（含链尖）返回其下标；命中链根父哈希返回
        -1（回滚整条分支）；都不命中返回 None。
        """
        for index, block in enumerate(self.blocks):
            if block["block_hash"] == parent_hash:
                return index
        if parent_hash == self.root_parent:
            return -1
        return None

    def has_block(self, block_hash):
        """block_hash 是否已存在于当前分支。"""
        return any(
            block["block_hash"] == block_hash for block in self.blocks
        )


class ForkAwareImporter:
    """可重放的分批增量导入器：按来源链维护当前有效分支，支持链分叉修正。

    通过 :attr:`indexer` 暴露底层 :class:`~tx_indexer.engine.TxIndexer`；
    导入返回后，查询与全部聚合统计立即只观察当前有效分支。
    """

    def __init__(self, indexer=None):
        self._indexer = indexer if indexer is not None else TxIndexer([])
        # chain_id -> _ChainState；不同来源链的分支状态相互独立
        self._chains = {}

    @property
    def indexer(self):
        """底层查询索引；与导入共享同一份记录存储。"""
        return self._indexer

    def import_batch(self, batch):
        """导入单个批次；等价于 ``import_batches([batch])``。"""
        return self.import_batches([batch])

    def import_batches(self, batches):
        """导入一批批次，返回结构化结果；任何错误都在状态修改前抛出。"""
        # 1. 结构校验：全部批次先校验，任何非法都不改变现有数据
        if isinstance(batches, dict):
            batches = [batches]
        if not isinstance(batches, list) or not batches:
            raise _invalid("batches 必须为非空批次数组")
        validated = [
            _validate_batch(batch, index)
            for index, batch in enumerate(batches)
        ]

        chain_id = validated[0]["chain_id"]
        seen_hashes = set()
        for batch in validated:
            if batch["chain_id"] != chain_id:
                raise _invalid(
                    "一次导入的批次必须属于同一条来源链（期望 %s，收到 %s）"
                    % (chain_id, batch["chain_id"])
                )
            block_hash = batch["block_hash"]
            if block_hash in seen_hashes:
                raise _invalid(
                    "批次内出现重复区块哈希：%s" % block_hash
                )
            seen_hashes.add(block_hash)

        chain = self._chains.get(chain_id)

        # 2. 幂等：来源链标识、区块哈希、批次序号均相同的批次已应用过，
        #    不重复写入、不重复累计
        remaining = []
        for batch in validated:
            if chain is not None and (
                (batch["block_hash"], batch["batch_seq"]) in chain.applied
            ):
                continue
            remaining.append(batch)
        if not remaining:
            tip = chain.tip
            return {
                "status": "already_applied",
                "chain_id": chain_id,
                "rolled_back_block_count": 0,
                "applied_block_count": 0,
                "tip_block_height": tip["block_height"],
                "tip_block_hash": tip["block_hash"],
            }

        # 已存在于当前分支、但批次序号不同的区块哈希同样是重复区块哈希
        if chain is not None:
            for batch in remaining:
                if chain.has_block(batch["block_hash"]):
                    raise _invalid(
                        "区块哈希 %s 已存在于当前分支" % batch["block_hash"]
                    )

        # 3. 批次连续时按区块高度顺序应用；批次之间必须哈希相连
        remaining.sort(key=lambda b: b["block_height"])
        for previous, current in zip(remaining, remaining[1:]):
            if current["parent_hash"] != previous["block_hash"]:
                raise BlockLinkMismatchError(
                    "批次 %s 的父区块哈希与前一区块哈希 %s 不一致"
                    % (current["block_hash"], previous["block_hash"]),
                    None,
                )

        # 4. 与引擎当前末端链接：普通追加或更高新分支的分叉修正
        first = remaining[0]
        rolled_back = []
        if chain is not None and chain.blocks:
            fork_index = chain.fork_index(first["parent_hash"])
            if fork_index is None:
                raise BlockLinkMismatchError(
                    "批次父区块哈希 %s 与引擎当前末端区块哈希 %s 不一致"
                    % (first["parent_hash"],
                       chain.tip["block_hash"]),
                    None,
                )
            if fork_index < len(chain.blocks) - 1:
                # 分叉修正：只接受更高区块高度的新分支，
                # 新分支末端高度必须超过当前末端高度
                if (
                    remaining[-1]["block_height"]
                    <= chain.tip["block_height"]
                ):
                    raise BlockLinkMismatchError(
                        "新分支末端高度 %d 未超过当前末端高度 %d"
                        % (remaining[-1]["block_height"],
                           chain.tip["block_height"]),
                        None,
                    )
                rolled_back = chain.blocks[fork_index + 1:]
        # 新来源链的首个批次作为链根，不校验父区块哈希

        # 5. 全部校验通过：先撤销被替代区块的交易与聚合贡献，再应用新分支
        if rolled_back:
            self._indexer.remove_records(
                [
                    record
                    for block in rolled_back
                    for record in block["records"]
                ]
            )
            for block in rolled_back:
                for record in block["records"]:
                    chain.by_hash.pop(record["tx_hash"], None)
                chain.applied.discard(
                    (block["block_hash"], block["batch_seq"])
                )
            del chain.blocks[fork_index + 1:]

        if chain is None:
            chain = _ChainState()
            chain.root_parent = first["parent_hash"]
            self._chains[chain_id] = chain

        # 应用新分支：交易哈希相同仍按当前分支内去重，不重复写入与累计
        for batch in remaining:
            records = []
            for tx in batch["transactions"]:
                tx_hash = tx["tx_hash"]
                if tx_hash in chain.by_hash:
                    continue
                chain.by_hash[tx_hash] = tx
                records.append(tx)
            if records:
                self._indexer.append_records(records)
            chain.blocks.append({
                "batch_seq": batch["batch_seq"],
                "parent_hash": batch["parent_hash"],
                "block_hash": batch["block_hash"],
                "block_height": batch["block_height"],
                "records": records,
            })
            chain.applied.add((batch["block_hash"], batch["batch_seq"]))

        tip = chain.tip
        return {
            "status": "applied",
            "chain_id": chain_id,
            "rolled_back_block_count": len(rolled_back),
            "applied_block_count": len(remaining),
            "tip_block_height": tip["block_height"],
            "tip_block_hash": tip["block_hash"],
        }
