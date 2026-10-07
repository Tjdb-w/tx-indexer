"""可重放的分批增量导入与链分叉修正。

:class:`ForkAwareImporter` 在现有查询索引
（:class:`~tx_indexer.engine.TxIndexer`）之上提供按批次的增量导入：
每个批次携带来源链标识、批次序号、父区块哈希、区块哈希、区块高度与
交易列表（交易沿用公开交易记录的字段与语义）。批次通过父区块哈希
链接到引擎当前末端：链接失败抛
:class:`~tx_indexer.errors.BlockLinkMismatchError`，导入前的索引、
统计与查询结果保持不变；相同（来源链标识、区块哈希、批次序号）的
批次重复提交时返回已应用状态，不重复写入交易，也不重复累计统计。

链分叉修正：:meth:`ForkAwareImporter.apply_branch` 接收同一条来源链
上更高区块高度的新分支批次列表，先撤销从分叉点开始被替代区块中交易
的地址索引、方法索引、时间窗索引与聚合贡献，再按区块高度顺序应用
新分支，最后返回包含已回滚区块数量、已应用区块数量与当前末端区块
哈希的确定结果。回滚期间交易哈希相同仍按当前分支内去重：保留前缀
与新分支内已存在的交易哈希只写入一次，不重复计数。

分叉修正后，此前生成的分页游标若指向已回滚交易，
:meth:`ForkAwareImporter.query` 抛
:class:`~tx_indexer.errors.CursorOutOfRangeError`，不返回旧分支
数据；其余筛选、时间边界、空结果、排序顺序与游标稳定性语义与
:class:`~tx_indexer.engine.TxIndexer` 完全一致。
"""

from .cursor import decode_cursor
from .engine import DEFAULT_PAGE_SIZE, TxIndexer
from .errors import (
    BlockLinkMismatchError,
    CursorOutOfRangeError,
    InvalidImportBatchError,
)
from .loader import _AMOUNT_RE, _is_nonneg_int, _is_nonempty_text

#: 批次字段的等价别名（按优先级取第一个出现的字段）
_CHAIN_ID_FIELDS = ("source_chain_id", "chain_id")
_BATCH_SEQ_FIELDS = ("batch_seq", "batch_sequence", "seq")
_PARENT_HASH_FIELDS = ("parent_block_hash", "parent_hash")
_BLOCK_HEIGHT_FIELDS = ("block_height", "block_number", "height")

#: 交易必填字段（沿用公开交易记录语义；success 为唯一可选字段）
_TX_REQUIRED = (
    "tx_hash",
    "block_number",
    "timestamp",
    "from_address",
    "to_address",
    "method",
    "amount",
)

#: 写入索引的归一化字段（success 始终携带布尔值）
_NORMALIZED_FIELDS = _TX_REQUIRED + ("success",)


def _pick(batch, names):
    """按别名优先级取批次字段；全部缺失时返回 None。"""
    for name in names:
        if name in batch:
            return batch[name]
    return None


def _validate_tx(raw, index, block_height):
    """校验并归一化一笔批次交易；任何结构或类型问题抛
    InvalidImportBatchError。

    交易沿用公开交易记录的字段与语义：``tx_hash`` 非空字符串、
    ``block_number`` 非负整数且必须等于批次区块高度、``timestamp``
    非负 UTC 秒整数、``from_address`` / ``to_address`` / ``method``
    非空字符串、``amount`` 非负十进制整数字符串；``success`` 可选，
    只能是布尔值，缺省视为 ``true``。未定义的额外字段被忽略。
    """
    if not isinstance(raw, dict):
        raise InvalidImportBatchError(
            "transactions[%d] 必须是 JSON 对象" % index, None
        )
    missing = [f for f in _TX_REQUIRED if f not in raw]
    if missing:
        raise InvalidImportBatchError(
            "transactions[%d] 缺少字段：%s" % (index, ", ".join(missing)),
            None,
        )
    if not _is_nonempty_text(raw["tx_hash"]):
        raise InvalidImportBatchError(
            "transactions[%d] tx_hash 必须为非空字符串" % index, None
        )
    if not _is_nonneg_int(raw["block_number"]):
        raise InvalidImportBatchError(
            "transactions[%d] block_number 必须为非负整数" % index, None
        )
    if raw["block_number"] != block_height:
        raise InvalidImportBatchError(
            "transactions[%d] block_number 与批次区块高度不一致" % index,
            None,
        )
    if not _is_nonneg_int(raw["timestamp"]):
        raise InvalidImportBatchError(
            "transactions[%d] timestamp 必须为非负 UTC 秒整数" % index,
            None,
        )
    for name in ("from_address", "to_address", "method"):
        if not _is_nonempty_text(raw[name]):
            raise InvalidImportBatchError(
                "transactions[%d] %s 必须为非空字符串" % (index, name),
                None,
            )
    amount = raw["amount"]
    if not isinstance(amount, str) or _AMOUNT_RE.fullmatch(amount) is None:
        raise InvalidImportBatchError(
            "transactions[%d] amount 必须为非负十进制整数字符串" % index,
            None,
        )
    if "success" in raw and not isinstance(raw["success"], bool):
        raise InvalidImportBatchError(
            "transactions[%d] success 必须为布尔值 true 或 false" % index,
            None,
        )

    normalized = {name: raw[name] for name in _TX_REQUIRED}
    normalized["success"] = raw["success"] if "success" in raw else True
    return normalized


def _validate_batch(batch, label="批次"):
    """校验批次结构并归一化；任何结构问题抛 InvalidImportBatchError。

    来源链标识为空（或仅含空白）、区块高度小于零（或类型非法）、批次
    序号非正数（或类型非法）都在此拒绝；返回固定键的归一化字典。
    """
    if not isinstance(batch, dict):
        raise InvalidImportBatchError("%s必须是 JSON 对象" % label, None)

    chain_id = _pick(batch, _CHAIN_ID_FIELDS)
    if not _is_nonempty_text(chain_id):
        raise InvalidImportBatchError("来源链标识必须为非空字符串", None)
    batch_seq = _pick(batch, _BATCH_SEQ_FIELDS)
    if (
        not isinstance(batch_seq, int)
        or isinstance(batch_seq, bool)
        or batch_seq <= 0
    ):
        raise InvalidImportBatchError("批次序号必须为正整数", None)
    parent_block_hash = _pick(batch, _PARENT_HASH_FIELDS)
    if not _is_nonempty_text(parent_block_hash):
        raise InvalidImportBatchError("父区块哈希必须为非空字符串", None)
    block_hash = batch.get("block_hash")
    if not _is_nonempty_text(block_hash):
        raise InvalidImportBatchError("区块哈希必须为非空字符串", None)
    block_height = _pick(batch, _BLOCK_HEIGHT_FIELDS)
    if not _is_nonneg_int(block_height):
        raise InvalidImportBatchError("区块高度必须为非负整数", None)
    txs = batch.get("transactions")
    if not isinstance(txs, list):
        raise InvalidImportBatchError("交易列表必须为数组", None)

    normalized = [
        _validate_tx(raw, index, block_height)
        for index, raw in enumerate(txs)
    ]
    return {
        "chain_id": chain_id,
        "batch_seq": batch_seq,
        "parent_block_hash": parent_block_hash,
        "block_hash": block_hash,
        "block_height": block_height,
        "transactions": normalized,
    }


class ForkAwareImporter:
    """可重放的分批增量导入器，支持链分叉修正。

    通过 :attr:`indexer` 暴露底层 :class:`~tx_indexer.engine.TxIndexer`；
    批次应用成功后，现有查询、聚合统计与分页游标立即观察到当前有效
    分支的全部交易，格式与语义不变。
    """

    def __init__(self, indexer=None):
        self._indexer = indexer if indexer is not None else TxIndexer([])
        # 首个批次确定的来源链标识；尚未导入时为 None
        self._chain_id = None
        # 当前有效分支上的区块（按高度升序）：
        # {"height", "block_hash", "parent_block_hash", "batch_seq", "tx_hashes"}
        self._blocks = []
        # tx_hash -> 当前有效分支上的归一化交易（分支内去重台账）
        self._ledger = {}

    @property
    def indexer(self):
        """底层查询索引；与导入共享同一份记录存储。"""
        return self._indexer

    @property
    def chain_id(self):
        """首个批次确定的来源链标识；尚未导入时为 None。"""
        return self._chain_id

    #: ``chain_id`` 的等价别名
    source_chain_id = chain_id

    @property
    def tip_block_height(self):
        """当前末端区块高度；尚未导入时为 None。"""
        return self._blocks[-1]["height"] if self._blocks else None

    @property
    def tip_block_hash(self):
        """当前末端区块哈希；尚未导入时为 None。"""
        return self._blocks[-1]["block_hash"] if self._blocks else None

    def _tip_info(self):
        return {
            "tip_block_height": self.tip_block_height,
            "tip_block_hash": self.tip_block_hash,
        }

    def import_batch(self, batch):
        """导入一个批次，返回结构化结果；失败抛异常且不产生任何写入。

        批次的父区块哈希必须等于引擎当前末端区块哈希（首个批次除外），
        区块高度必须在父区块高度上 +1，否则抛
        :class:`~tx_indexer.errors.BlockLinkMismatchError`；来源链标识
        为空、区块高度小于零、批次序号非正数等结构问题抛
        :class:`~tx_indexer.errors.InvalidImportBatchError`。重复提交
        （来源链标识、区块哈希、批次序号均相同）的批次返回
        ``already_applied`` 状态，不重复写入交易、不重复累计统计。
        """
        validated = _validate_batch(batch)

        # 幂等重放：来源链标识、区块哈希、批次序号均相同的已应用批次
        for block in self._blocks:
            if (
                block["block_hash"] == validated["block_hash"]
                and block["batch_seq"] == validated["batch_seq"]
                and self._chain_id == validated["chain_id"]
            ):
                return dict(
                    {
                        "status": "already_applied",
                        "imported_count": 0,
                        "skipped_count": 0,
                    },
                    **self._tip_info(),
                )

        if self._chain_id is not None and validated["chain_id"] != self._chain_id:
            raise BlockLinkMismatchError(
                "批次来源链标识 %s 与当前分支 %s 不一致"
                % (validated["chain_id"], self._chain_id),
                None,
            )
        if any(
            block["block_hash"] == validated["block_hash"]
            for block in self._blocks
        ):
            raise InvalidImportBatchError(
                "区块哈希 %s 已存在于当前分支（重复区块哈希）"
                % validated["block_hash"],
                None,
            )

        if self._blocks:
            tip = self._blocks[-1]
            if validated["parent_block_hash"] != tip["block_hash"]:
                raise BlockLinkMismatchError(
                    "批次父区块哈希 %s 与引擎当前末端区块哈希 %s 不一致"
                    % (validated["parent_block_hash"], tip["block_hash"]),
                    None,
                )
            if validated["block_height"] != tip["height"] + 1:
                raise BlockLinkMismatchError(
                    "批次区块高度 %d 无法衔接当前末端高度 %d"
                    % (validated["block_height"], tip["height"]),
                    None,
                )

        imported, skipped = self._apply_validated([validated])
        self._blocks.append(self._block_entry(validated))
        return dict(
            {
                "status": "ok",
                "imported_count": imported,
                "skipped_count": skipped,
            },
            **self._tip_info(),
        )

    def apply_branch(self, batches):
        """链分叉修正：用更高区块高度的新分支取代从分叉点开始的旧分支。

        ``batches`` 为新分支批次组成的非空列表：批次内出现重复区块哈希
        抛 :class:`~tx_indexer.errors.InvalidImportBatchError`；首个批次
        的父区块哈希必须等于当前分支上某一区块的哈希（分叉点父块），
        列表内部父哈希与区块高度必须连续衔接，且新分支末端高度必须高于
        当前末端高度，否则抛
        :class:`~tx_indexer.errors.BlockLinkMismatchError`。所有校验都在
        任何状态修改之前完成，失败时索引、统计与查询结果保持不变。

        成功时先撤销被替代区块中交易的全部索引与聚合贡献，再按区块高度
        顺序应用新分支（交易哈希相同仍按当前分支内去重），返回包含
        ``rolled_back_block_count``、``applied_block_count`` 与
        ``tip_block_hash`` 的确定结果。
        """
        if not isinstance(batches, (list, tuple)) or len(batches) == 0:
            raise InvalidImportBatchError(
                "新分支批次列表必须为非空数组", None
            )
        validated = [
            _validate_batch(raw, label="batches[%d]" % index)
            for index, raw in enumerate(batches)
        ]

        # 批次内重复区块哈希：新分支列表内部、以及相对保留前缀都不得重复
        hashes = [v["block_hash"] for v in validated]
        if len(set(hashes)) != len(hashes):
            raise InvalidImportBatchError("批次内出现重复区块哈希", None)

        chain_id = validated[0]["chain_id"]
        if any(v["chain_id"] != chain_id for v in validated):
            raise InvalidImportBatchError("新分支批次的来源链标识不一致", None)
        if self._chain_id is not None and chain_id != self._chain_id:
            raise BlockLinkMismatchError(
                "新分支来源链标识 %s 与当前分支 %s 不一致"
                % (chain_id, self._chain_id),
                None,
            )

        # 新分支内部链接：父哈希衔接、高度连续 +1
        for previous, current in zip(validated, validated[1:]):
            if current["parent_block_hash"] != previous["block_hash"]:
                raise BlockLinkMismatchError(
                    "区块哈希 %s 的父区块哈希 %s 与前一批次区块哈希 %s 不一致"
                    % (
                        current["block_hash"],
                        current["parent_block_hash"],
                        previous["block_hash"],
                    ),
                    None,
                )
            if current["block_height"] != previous["block_height"] + 1:
                raise BlockLinkMismatchError(
                    "区块高度 %d 无法衔接前一批次高度 %d"
                    % (current["block_height"], previous["block_height"]),
                    None,
                )

        # 定位分叉点：首个批次的父区块哈希必须等于当前分支上某一区块
        # 的哈希（等于末端哈希时为纯追加，不回滚任何区块）
        fork_index = -1
        if self._blocks:
            first = validated[0]
            fork_index = next(
                (
                    index
                    for index, block in enumerate(self._blocks)
                    if block["block_hash"] == first["parent_block_hash"]
                ),
                None,
            )
            if fork_index is None:
                raise BlockLinkMismatchError(
                    "首个批次的父区块哈希 %s 不在当前分支上"
                    % first["parent_block_hash"],
                    None,
                )
            if first["block_height"] != self._blocks[fork_index]["height"] + 1:
                raise BlockLinkMismatchError(
                    "首个批次区块高度 %d 无法衔接分叉点高度 %d"
                    % (first["block_height"], self._blocks[fork_index]["height"]),
                    None,
                )
            if validated[-1]["block_height"] <= self._blocks[-1]["height"]:
                raise BlockLinkMismatchError(
                    "新分支末端高度 %d 必须高于当前末端高度 %d"
                    % (validated[-1]["block_height"], self._blocks[-1]["height"]),
                    None,
                )
            retained_hashes = {
                block["block_hash"] for block in self._blocks[: fork_index + 1]
            }
            if any(block_hash in retained_hashes for block_hash in hashes):
                raise InvalidImportBatchError("批次内出现重复区块哈希", None)

        rolled_back = self._blocks[fork_index + 1:]
        remove_hashes = {
            tx_hash for block in rolled_back for tx_hash in block["tx_hashes"]
        }

        # 先撤销被替代区块的交易（索引与聚合贡献随记录一并移除），
        # 再应用新分支；交易哈希相同仍按当前分支内去重
        imported, skipped = self._apply_validated(
            validated, remove_hashes=remove_hashes
        )
        self._blocks[fork_index + 1:] = []
        for v in validated:
            self._blocks.append(self._block_entry(v))

        result = dict(
            {
                "status": "ok",
                "rolled_back_block_count": len(rolled_back),
                "applied_block_count": len(validated),
                "rolled_back_transaction_count": len(remove_hashes),
                "imported_count": imported,
                "skipped_count": skipped,
            },
            **self._tip_info(),
        )
        # 等价别名：当前末端区块哈希
        result["current_tip_block_hash"] = result["tip_block_hash"]
        return result

    #: ``apply_branch`` 的等价别名
    replace_branch = apply_branch

    def _block_entry(self, validated):
        return {
            "height": validated["block_height"],
            "block_hash": validated["block_hash"],
            "parent_block_hash": validated["parent_block_hash"],
            "batch_seq": validated["batch_seq"],
            "tx_hashes": tuple(
                tx["tx_hash"] for tx in validated["transactions"]
            ),
        }

    def _apply_validated(self, validated, remove_hashes=()):
        """把已校验批次写入索引与台账（调用前已完成全部链接校验）。

        返回 ``(imported_count, skipped_count)``：交易哈希在当前分支
        台账中已存在时按重复跳过，不覆盖、不重复计数。
        """
        if remove_hashes:
            for tx_hash in remove_hashes:
                self._ledger.pop(tx_hash, None)
        imported = []
        skipped = 0
        for v in validated:
            for tx in v["transactions"]:
                tx_hash = tx["tx_hash"]
                if tx_hash in self._ledger:
                    skipped += 1
                    continue
                self._ledger[tx_hash] = tx
                imported.append(tx)
        if remove_hashes or imported:
            self._indexer.replace_records(remove_hashes, imported)
        if self._chain_id is None:
            self._chain_id = validated[0]["chain_id"]
        return len(imported), skipped

    def query(self, filters, page_size=DEFAULT_PAGE_SIZE, cursor=None):
        """分页查询当前有效分支，返回 ``{transactions, total, next_cursor}``。

        筛选、排序、返回结构与 :meth:`TxIndexer.query` 完全一致；区别
        仅在于：此前生成的游标若指向已回滚（不再属于当前有效分支）的
        交易，抛 :class:`~tx_indexer.errors.CursorOutOfRangeError`，
        不返回旧分支数据。游标的格式、筛选绑定等其他校验沿用既有语义。
        """
        self._indexer._validate_page_size(page_size)
        if cursor is not None:
            after_block, after_tx_hash = decode_cursor(cursor, filters)
            marker_exists = any(
                record["block_number"] == after_block
                and record["tx_hash"] == after_tx_hash
                for record in self._indexer._records
            )
            if not marker_exists:
                raise CursorOutOfRangeError(
                    "游标指向的交易已不在当前有效分支上（已被回滚）", None
                )
        return self._indexer.query(filters, page_size, cursor)

    def stats(self, filters):
        """聚合统计（忽略分页），口径与 :meth:`TxIndexer.stats` 一致。"""
        return self._indexer.stats(filters)


#: :class:`ForkAwareImporter` 的等价别名
ForkCorrectingImporter = ForkAwareImporter
