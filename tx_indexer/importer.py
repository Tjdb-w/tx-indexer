"""增量交易导入与断点续传。

导入入口 :meth:`IncrementalImporter.import_batch` 接收一个按区块高度排列
的批次：链标识、起始区块高度、区块哈希与该区块内的交易列表。单个批次
要么完整写入、要么完整拒绝——所有校验都在任何状态修改之前完成，因此
进程中途退出或同一位置重试都不会产生半个区块或重复聚合。

批次结构（未定义的额外字段被忽略）：

- ``chain_id``：非空字符串，链标识；首个批次确定后不可变更
- ``start_block``：非负整数，本批（区块）高度
- ``block_hash``：非空字符串，本批区块哈希
- ``transactions``：数组，每笔交易至少包含 ``tx_hash``、``block_number``、
  ``block_hash``、``timestamp``、``from_address``、``to_address``、
  ``method``、``amount``、``fee``、``success``。其中 ``block_number``
  必须等于批次起始高度、``block_hash`` 必须等于批次区块哈希；
  ``amount`` / ``fee`` 为非负十进制整数字符串，``success`` 为布尔值

续接规则：首个批次不要求游标；此后每次导入必须携带上一批返回的
``next_import_cursor``，且 ``start_block`` 为已确认高度 +1（新区块），
或指向已导入的高度（同一位置重试，要求区块哈希与交易集合完全一致，
全部交易按重复跳过）。导入游标只绑定链标识、已确认高度与区块哈希，
与分页游标相互独立，不能混用。

返回结构（机器可读字段稳定）：

- 成功：``{"status": "ok", "imported_count", "skipped_count",
  "confirmed_block_height", "confirmed_block_hash",
  "next_import_cursor"}``
- 拒绝：``{"status": "rejected", "error_code", "message"}``，
  ``error_code`` 为 ``INVALID_IMPORT_BATCH`` / ``INVALID_REPLACEMENT_BATCH``
  / ``IMPORT_CURSOR_MISMATCH`` / ``TX_CONFLICT`` / ``BLOCK_CONFLICT`` 之一，
  且索引不产生任何新增数据

链重组：:meth:`IncrementalImporter.replace_from` 在已导入的某一高度上
原子覆盖从该高度到链尖的整个旧后缀，保留更低高度的前缀。它先完整校验
新后缀（结构、连续性、链标识、交易冲突），全部通过后才替换，失败时
索引保持原状。新后缀与保留前缀（或新后缀内部）同名交易字段不一致报
``TX_CONFLICT``；旧后缀同名交易不参与冲突判定，其中完全相同的重复项
保留并计入 ``skipped_count``，字段不同或被截掉的旧记录才删除。
"""

from .cursor import decode_import_cursor, encode_import_cursor
from .engine import TxIndexer
from .errors import TxIndexerError
from .loader import _AMOUNT_RE, _is_nonneg_int, _is_nonempty_text

#: 批次结构非法（缺字段、类型错误或交易与批次声明的区块不一致）
INVALID_IMPORT_BATCH = "INVALID_IMPORT_BATCH"
#: replace_from 的替换批次结构非法（空批、区块结构/连续性/chain_id/交易关系非法）
INVALID_REPLACEMENT_BATCH = "INVALID_REPLACEMENT_BATCH"
#: 游标缺失/非法/与当前状态不一致，或起始区块高度无法无缝续接
IMPORT_CURSOR_MISMATCH = "IMPORT_CURSOR_MISMATCH"
#: 交易哈希已存在且任一决定查询结果的字段不同
TX_CONFLICT = "TX_CONFLICT"
#: 已导入高度上的区块哈希不同，或同一区块哈希对应的交易集合发生变化
BLOCK_CONFLICT = "BLOCK_CONFLICT"

_BATCH_REQUIRED = ("chain_id", "start_block", "block_hash", "transactions")

_TX_REQUIRED = (
    "tx_hash",
    "block_number",
    "block_hash",
    "timestamp",
    "from_address",
    "to_address",
    "method",
    "amount",
    "fee",
    "success",
)

#: 决定查询结果、用于重试判重与冲突检测的交易字段
_CONFLICT_FIELDS = (
    "block_number",
    "block_hash",
    "timestamp",
    "from_address",
    "to_address",
    "method",
    "amount",
    "fee",
    "success",
)


def _rejected(error_code, message):
    return {"status": "rejected", "error_code": error_code, "message": message}


def _validate_tx(tx, index, start_block, block_hash):
    """校验并归一化一笔导入交易；失败返回错误消息，成功返回 None。

    归一化结果写入 ``tx`` 的调用方列表由 :func:`_validate_batch` 负责。
    """
    if not isinstance(tx, dict):
        return "transactions[%d] 必须是 JSON 对象" % index
    missing = [f for f in _TX_REQUIRED if f not in tx]
    if missing:
        return "transactions[%d] 缺少字段：%s" % (index, ", ".join(missing))

    if not _is_nonempty_text(tx["tx_hash"]):
        return "transactions[%d] tx_hash 必须为非空字符串" % index
    if not _is_nonneg_int(tx["block_number"]):
        return "transactions[%d] block_number 必须为非负整数" % index
    if tx["block_number"] != start_block:
        return (
            "transactions[%d] block_number 与批次起始区块高度不一致" % index
        )
    if not _is_nonempty_text(tx["block_hash"]):
        return "transactions[%d] block_hash 必须为非空字符串" % index
    if tx["block_hash"] != block_hash:
        return "transactions[%d] block_hash 与批次区块哈希不一致" % index
    if not _is_nonneg_int(tx["timestamp"]):
        return "transactions[%d] timestamp 必须为非负 UTC 秒整数" % index
    for name in ("from_address", "to_address", "method"):
        if not _is_nonempty_text(tx[name]):
            return "transactions[%d] %s 必须为非空字符串" % (index, name)
    for name in ("amount", "fee"):
        value = tx[name]
        if not isinstance(value, str) or _AMOUNT_RE.fullmatch(value) is None:
            return (
                "transactions[%d] %s 必须为非负十进制整数字符串"
                % (index, name)
            )
    if not isinstance(tx["success"], bool):
        return "transactions[%d] success 必须为布尔值" % index
    return None


def _validate_batch(batch):
    """校验批次结构。

    成功返回 ``(chain_id, start_block, block_hash, transactions)``
    （交易已按原顺序归一化为固定字段字典）；失败返回错误消息字符串。
    """
    if not isinstance(batch, dict):
        return "批次必须是 JSON 对象"
    missing = [f for f in _BATCH_REQUIRED if f not in batch]
    if missing:
        return "批次缺少字段：%s" % ", ".join(missing)

    chain_id = batch["chain_id"]
    if not _is_nonempty_text(chain_id):
        return "chain_id 必须为非空字符串"
    start_block = batch["start_block"]
    if not _is_nonneg_int(start_block):
        return "start_block 必须为非负整数"
    block_hash = batch["block_hash"]
    if not _is_nonempty_text(block_hash):
        return "block_hash 必须为非空字符串"
    txs = batch["transactions"]
    if not isinstance(txs, list):
        return "transactions 必须为数组"

    normalized = []
    for index, tx in enumerate(txs):
        error = _validate_tx(tx, index, start_block, block_hash)
        if error is not None:
            return error
        normalized.append({name: tx[name] for name in _TX_REQUIRED})
    return chain_id, start_block, block_hash, normalized


def _conflicts(existing, tx):
    """已存在记录与新交易在决定查询结果的字段上是否不一致。"""
    return any(existing[name] != tx[name] for name in _CONFLICT_FIELDS)


def _validate_replacement(start_block, blocks):
    """校验 replace_from 的新后缀（区块批次数组）。

    成功返回 ``[(chain_id, height, block_hash, transactions), ...]``
    （交易已按原顺序归一化为固定字段字典，高度连续升序）；失败返回
    错误消息字符串。
    """
    if not _is_nonneg_int(start_block):
        return "start_block 必须为非负整数"
    if not isinstance(blocks, list):
        return "blocks 必须为批次数组"
    if len(blocks) == 0:
        return "blocks 不能为空：替换后缀至少包含一个区块"

    validated = []
    for index, raw in enumerate(blocks):
        result = _validate_batch(raw)
        if isinstance(result, str):
            return "blocks[%d]：%s" % (index, result)
        validated.append(result)

    chain_id, first_height, _, _ = validated[0]
    if first_height != start_block:
        return (
            "blocks[0].start_block 为 %d，必须等于 start_block 参数 %d"
            % (first_height, start_block)
        )

    expected = first_height
    for index, (cid, height, _, _) in enumerate(validated):
        if cid != chain_id:
            return (
                "blocks[%d] chain_id 为 %s，与首个区块的 %s 不一致"
                % (index, cid, chain_id)
            )
        if index > 0 and height != expected:
            return (
                "blocks[%d] 高度 %d 不连续，期望高度 %d"
                % (index, height, expected)
            )
        expected = height + 1
    return validated


class IncrementalImporter:
    """增量导入器：把按区块高度排列的批次原子地写入查询索引。

    通过 :attr:`indexer` 暴露底层 :class:`~tx_indexer.engine.TxIndexer`；
    导入返回成功后，查询与聚合立即观察到本批全部交易。
    """

    def __init__(self, indexer=None):
        self._indexer = indexer if indexer is not None else TxIndexer([])
        self._chain_id = None
        self._confirmed_height = None
        self._confirmed_hash = None
        # 已导入高度 -> {"block_hash": str, "tx_hashes": frozenset}
        self._blocks = {}
        # tx_hash -> 已写入的归一化交易（含 fee/success/block_hash）
        self._by_hash = {}

    @property
    def indexer(self):
        """底层查询索引；与导入共享同一份记录存储。"""
        return self._indexer

    @property
    def chain_id(self):
        """首个批次确定的链标识；尚未导入时为 None。"""
        return self._chain_id

    @property
    def confirmed_block_height(self):
        """当前已确认区块高度；尚未导入时为 None。"""
        return self._confirmed_height

    @property
    def confirmed_block_hash(self):
        """当前已确认区块哈希；尚未导入时为 None。"""
        return self._confirmed_hash

    def import_batch(self, batch, cursor=None):
        """导入一个批次，返回结构化结果（成功或拒绝，不产生部分写入）。"""
        # 1. 结构校验：任何字段问题都在状态检查之前拒绝
        validated = _validate_batch(batch)
        if isinstance(validated, str):
            return _rejected(INVALID_IMPORT_BATCH, validated)
        chain_id, start_block, block_hash, txs = validated

        # 2. 游标与续接校验
        if self._chain_id is None:
            if cursor is not None:
                return _rejected(
                    IMPORT_CURSOR_MISMATCH,
                    "空索引不接受导入游标，请从首个批次开始导入",
                )
        else:
            if cursor is None:
                return _rejected(
                    IMPORT_CURSOR_MISMATCH,
                    "缺少上一批返回的导入游标",
                )
            try:
                saved = decode_import_cursor(cursor)
            except TxIndexerError:
                return _rejected(
                    IMPORT_CURSOR_MISMATCH, "导入游标无法解码或非法"
                )
            current = (self._chain_id, self._confirmed_height,
                       self._confirmed_hash)
            if saved != current:
                return _rejected(
                    IMPORT_CURSOR_MISMATCH,
                    "导入游标与当前已确认状态不一致",
                )
            if chain_id != self._chain_id:
                return _rejected(
                    IMPORT_CURSOR_MISMATCH,
                    "批次 chain_id 与已导入链不一致",
                )

        # 3. 高度续接与区块冲突校验
        is_new_block = (
            self._confirmed_height is None
            or start_block == self._confirmed_height + 1
        )
        if not is_new_block:
            recorded = self._blocks.get(start_block)
            if recorded is None:
                return _rejected(
                    IMPORT_CURSOR_MISMATCH,
                    "起始区块高度无法与已导入区间无缝续接",
                )
            if recorded["block_hash"] != block_hash:
                return _rejected(
                    BLOCK_CONFLICT,
                    "高度 %d 已确认区块哈希为 %s，与批次区块哈希 %s 冲突"
                    % (start_block, recorded["block_hash"], block_hash),
                )
            if recorded["tx_hashes"] != frozenset(
                tx["tx_hash"] for tx in txs
            ):
                return _rejected(
                    BLOCK_CONFLICT,
                    "区块哈希 %s 对应的交易集合与已导入数据不一致"
                    % block_hash,
                )

        # 4. 交易级冲突校验（含本批内部的重复哈希）
        seen_in_batch = {}
        for tx in txs:
            tx_hash = tx["tx_hash"]
            existing = self._by_hash.get(tx_hash)
            if existing is None:
                existing = seen_in_batch.get(tx_hash)
            if existing is not None and _conflicts(existing, tx):
                return _rejected(
                    TX_CONFLICT,
                    "tx_hash %s 已存在且决定查询结果的字段不一致" % tx_hash,
                )
            seen_in_batch.setdefault(tx_hash, tx)

        # 5. 全部校验通过，原子写入：重复交易跳过且不覆盖已存在数据
        imported = []
        skipped = 0
        for tx in txs:
            if tx["tx_hash"] in self._by_hash:
                skipped += 1
                continue
            self._by_hash[tx["tx_hash"]] = tx
            imported.append(tx)
        if imported:
            self._indexer.append_records(imported)

        if self._chain_id is None:
            self._chain_id = chain_id
        if is_new_block:
            self._blocks[start_block] = {
                "block_hash": block_hash,
                "tx_hashes": frozenset(tx["tx_hash"] for tx in txs),
            }
            self._confirmed_height = start_block
            self._confirmed_hash = block_hash

        return {
            "status": "ok",
            "imported_count": len(imported),
            "skipped_count": skipped,
            "confirmed_block_height": self._confirmed_height,
            "confirmed_block_hash": self._confirmed_hash,
            "next_import_cursor": encode_import_cursor(
                self._chain_id, self._confirmed_height, self._confirmed_hash
            ),
        }

    def replace_from(self, start_block, blocks, cursor):
        """链重组：原子覆盖 ``start_block`` 至链尖的旧后缀。

        ``blocks`` 为非空的 import_batch 批次数组，首个批次的
        ``start_block`` 必须等于参数值，高度连续、chain_id 一致。
        保留更低高度的前缀；新后缀先完整校验（结构、游标、与保留前缀
        及后缀内部的交易冲突），全部通过后才替换，失败时索引不变。

        旧后缀同名交易不参与冲突判定：决定字段完全相同的保留并计入
        skipped，字段不同或新后缀不再包含的旧记录才删除。
        """
        # 1. 新后缀结构校验：任何结构问题都先于状态/游标检查拒绝
        validated = _validate_replacement(start_block, blocks)
        if isinstance(validated, str):
            return _rejected(INVALID_REPLACEMENT_BATCH, validated)
        chain_id = validated[0][0]

        # 2. 游标、链标识与起始高度校验
        if self._chain_id is None or cursor is None:
            return _rejected(
                IMPORT_CURSOR_MISMATCH,
                "replace_from 要求已导入数据且携带当前导入游标",
            )
        try:
            saved = decode_import_cursor(cursor)
        except TxIndexerError:
            return _rejected(
                IMPORT_CURSOR_MISMATCH, "导入游标无法解码或非法"
            )
        current = (self._chain_id, self._confirmed_height,
                   self._confirmed_hash)
        if saved != current:
            return _rejected(
                IMPORT_CURSOR_MISMATCH,
                "导入游标与当前已确认状态不一致",
            )
        if chain_id != self._chain_id:
            return _rejected(
                IMPORT_CURSOR_MISMATCH,
                "替换批次 chain_id 与已导入链不一致",
            )
        if self._blocks.get(start_block) is None:
            return _rejected(
                IMPORT_CURSOR_MISMATCH,
                "start_block %d 不是已导入高度" % start_block,
            )

        old_tip = self._confirmed_height

        # 3. 交易级冲突校验：仅对照保留前缀与新后缀内部；
        #    旧后缀同名交易先删除，不参与冲突判定。
        prefix = {
            h: tx for h, tx in self._by_hash.items()
            if tx["block_number"] < start_block
        }
        seen_in_suffix = {}
        for _, _, _, txs in validated:
            for tx in txs:
                tx_hash = tx["tx_hash"]
                existing = prefix.get(tx_hash)
                if existing is None:
                    existing = seen_in_suffix.get(tx_hash)
                if existing is not None and _conflicts(existing, tx):
                    return _rejected(
                        TX_CONFLICT,
                        "tx_hash %s 与保留数据同名交易的决定字段不一致"
                        % tx_hash,
                    )
                seen_in_suffix.setdefault(tx_hash, tx)

        # 4. 全部校验通过，原子替换。旧后缀同名交易分两类：决定字段完全
        #    相同的重复项原样保留、计入 skipped；字段不同或新后缀不再包含
        #    的旧记录才真正删除。保留前缀与新后缀内部的相同重复项同理跳过。
        old_suffix = {
            h: tx for h, tx in self._by_hash.items()
            if start_block <= tx["block_number"] <= old_tip
        }

        remove_hashes = set()
        retained_hashes = set()
        imported_records = []
        imported_hashes = set()
        skipped = 0
        for _, _, _, txs in validated:
            for tx in txs:
                tx_hash = tx["tx_hash"]
                if tx_hash in imported_hashes:
                    # 新后缀内部先前已写入的相同重复项（校验已排除字段冲突）
                    skipped += 1
                    continue
                old = old_suffix.get(tx_hash)
                if old is not None and not _conflicts(old, tx):
                    # 旧后缀同名交易且决定字段完全一致：保留、跳过
                    retained_hashes.add(tx_hash)
                    skipped += 1
                    continue
                if old is not None:
                    # 旧后缀同名交易但字段不同（重组）：删除旧记录后写入
                    remove_hashes.add(tx_hash)
                imported_hashes.add(tx_hash)
                imported_records.append(tx)
        # 新后缀不再包含的旧记录一律删除
        for tx_hash in old_suffix:
            if tx_hash not in retained_hashes and tx_hash not in imported_hashes:
                remove_hashes.add(tx_hash)

        removed_block_count = old_tip - start_block + 1
        removed_transaction_count = len(remove_hashes)

        self._indexer.replace_records(remove_hashes, imported_records)

        # 元数据：删除旧后缀区块、登记新区块
        for height in range(start_block, old_tip + 1):
            self._blocks.pop(height, None)
        for _, height, block_hash, txs in validated:
            self._blocks[height] = {
                "block_hash": block_hash,
                "tx_hashes": frozenset(tx["tx_hash"] for tx in txs),
            }
        # 交易哈希表：删除被替换的旧记录，写入新记录；保留记录不动
        for tx_hash in remove_hashes:
            self._by_hash.pop(tx_hash, None)
        for tx in imported_records:
            self._by_hash[tx["tx_hash"]] = tx

        last = validated[-1]
        self._confirmed_height = last[1]
        self._confirmed_hash = last[2]

        return {
            "status": "ok",
            "removed_block_count": removed_block_count,
            "removed_transaction_count": removed_transaction_count,
            "imported_block_count": len(validated),
            "imported_count": len(imported_records),
            "skipped_count": skipped,
            "confirmed_block_height": self._confirmed_height,
            "confirmed_block_hash": self._confirmed_hash,
            "next_import_cursor": encode_import_cursor(
                self._chain_id, self._confirmed_height, self._confirmed_hash
            ),
        }
