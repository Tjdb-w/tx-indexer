"""增量交易导入与断点续传。

:class:`IncrementalImporter` 在内存索引（:class:`~tx_indexer.engine.TxIndexer`）
之上提供按区块高度排列的批次导入入口 :meth:`IncrementalImporter.import_batch`。

批次结构（JSON 可序列化字典）：

- ``chain_id``：非空字符串，链标识；首次导入后固定，后续批次必须一致
- ``block_number``：非负整数，本批起始（且唯一）区块高度
- ``block_hash``：非空字符串，该高度的区块哈希
- ``transactions``：交易列表，每笔至少包含 ``tx_hash``、``block_number``、
  ``block_hash``、``timestamp``、``from_address``、``to_address``、
  ``method``、``amount``、``fee``、``success``；其中 ``block_number`` /
  ``block_hash`` 必须与批次一致，``amount`` / ``fee`` 为非负十进制整数
  字符串，``success`` 为布尔值

导入结果始终为结构化字典，机器可读字段稳定：

- 成功：``{"status": "imported", "chain_id", "imported_count",
  "skipped_count", "confirmed_block_number", "confirmed_block_hash",
  "next_import_cursor"}``
- 拒绝：``{"status": "rejected", "error_code", "message"}``，
  ``error_code`` 为 ``INVALID_IMPORT_BATCH`` / ``IMPORT_CURSOR_MISMATCH``
  / ``BLOCK_CONFLICT`` / ``TX_CONFLICT`` 之一

语义：

- 单个批次要么完整写入、要么完整拒绝；任何拒绝都不会改动索引与
  导入状态，也不会留下半个区块或重复聚合。
- 同一 ``tx_hash`` 再次出现且区块、时间、地址、方法、数值、费用、
  成功状态完全一致时视为重试，跳过且不计入新增；任一上述字段不同
  则整批以 ``TX_CONFLICT`` 拒绝。
- 批次高度必须等于已确认高度 + 1（新区块），或落在已导入区间内
  （幂等重试）；出现空洞或回退到导入起点之前时以
  ``IMPORT_CURSOR_MISMATCH`` 拒绝。重试批次与已导入内容（区块哈希、
  交易集合）不一致时以 ``BLOCK_CONFLICT`` 拒绝。
- 导入游标为不透明令牌，绑定链标识、已确认高度、区块哈希与已索引
  交易数；与分页游标（:mod:`tx_indexer.cursor`）完全独立，互不通用。
"""

import base64
import binascii
import json
import re

from .engine import TxIndexer

#: 导入结果错误码（机器可读，保持稳定）
INVALID_IMPORT_BATCH = "INVALID_IMPORT_BATCH"
IMPORT_CURSOR_MISMATCH = "IMPORT_CURSOR_MISMATCH"
BLOCK_CONFLICT = "BLOCK_CONFLICT"
TX_CONFLICT = "TX_CONFLICT"

_IMPORT_CURSOR_VERSION = 1
_IMPORT_CURSOR_SCOPE = "import"

_AMOUNT_RE = re.compile(r"[0-9]+")

#: 交易必填字段（除此之外的字段被忽略，不参与存储与判重）
_TX_REQUIRED_FIELDS = (
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


def _is_nonneg_int(value):
    # bool 是 int 的子类，必须显式排除
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_nonempty_text(value):
    return isinstance(value, str) and value.strip() != ""


def _is_amount_text(value):
    return isinstance(value, str) and _AMOUNT_RE.fullmatch(value) is not None


def _rejected(error_code, message):
    return {"status": "rejected", "error_code": error_code, "message": message}


def _encode_import_cursor(chain_id, confirmed_height, confirmed_hash, tx_count):
    payload = {
        "v": _IMPORT_CURSOR_VERSION,
        "c": _IMPORT_CURSOR_SCOPE,
        "chain": chain_id,
        "h": confirmed_height,
        "b": confirmed_hash,
        "n": tx_count,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_import_cursor(token):
    """解码导入游标；任何非法都返回 None（由调用方映射为游标不匹配）。"""
    if not isinstance(token, str) or token == "":
        return None
    try:
        padding = "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(token + padding)
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("v") != _IMPORT_CURSOR_VERSION:
        return None
    if payload.get("c") != _IMPORT_CURSOR_SCOPE:
        return None
    chain = payload.get("chain")
    height = payload.get("h")
    block_hash = payload.get("b")
    tx_count = payload.get("n")
    if not _is_nonempty_text(chain):
        return None
    if not _is_nonneg_int(height):
        return None
    if not _is_nonempty_text(block_hash):
        return None
    if not _is_nonneg_int(tx_count):
        return None
    return {
        "chain_id": chain,
        "confirmed_block_number": height,
        "confirmed_block_hash": block_hash,
        "tx_count": tx_count,
    }


def _validate_tx(obj, block_number, block_hash):
    """校验并归一化一笔导入交易；失败返回错误消息，成功返回身份字典。

    身份字典包含全部决定查询结果与判重的字段；多余字段被忽略。
    """
    if not isinstance(obj, dict):
        return "每笔交易必须是 JSON 对象"
    missing = [f for f in _TX_REQUIRED_FIELDS if f not in obj]
    if missing:
        return "交易缺少字段：%s" % ", ".join(missing)

    tx_hash = obj["tx_hash"]
    if not _is_nonempty_text(tx_hash):
        return "tx_hash 必须为非空字符串"
    if not _is_nonneg_int(obj["block_number"]):
        return "block_number 必须为非负整数"
    if obj["block_number"] != block_number:
        return "交易 block_number 与批次不一致：%s" % tx_hash
    if not _is_nonempty_text(obj["block_hash"]):
        return "block_hash 必须为非空字符串"
    if obj["block_hash"] != block_hash:
        return "交易 block_hash 与批次不一致：%s" % tx_hash
    if not _is_nonneg_int(obj["timestamp"]):
        return "timestamp 必须为非负 UTC 秒整数"
    for name in ("from_address", "to_address", "method"):
        if not _is_nonempty_text(obj[name]):
            return "%s 必须为非空字符串" % name
    if not _is_amount_text(obj["amount"]):
        return "amount 必须为非负十进制整数字符串"
    if not _is_amount_text(obj["fee"]):
        return "fee 必须为非负十进制整数字符串"
    if not isinstance(obj["success"], bool):
        return "success 必须为布尔值"

    return {
        "tx_hash": tx_hash,
        "block_number": obj["block_number"],
        "block_hash": obj["block_hash"],
        "timestamp": obj["timestamp"],
        "from_address": obj["from_address"],
        "to_address": obj["to_address"],
        "method": obj["method"],
        "amount": obj["amount"],
        "fee": obj["fee"],
        "success": obj["success"],
    }


class IncrementalImporter:
    """按区块高度排列的增量交易导入器（内存态，无额外持久化）。

    通过 :attr:`indexer` 暴露底层查询引擎；导入返回成功后，本批全部
    交易立即可被查询与聚合观察到。
    """

    def __init__(self, indexer=None):
        self._indexer = indexer if indexer is not None else TxIndexer([])
        self._chain_id = None
        # 已导入区间 [start_height, confirmed_height]（含端点，连续无空洞）
        self._start_height = None
        self._confirmed_height = None
        self._confirmed_hash = None
        # 高度 -> {"block_hash": str, "identities": {tx_hash: identity}}
        self._blocks = {}
        self._hash_to_height = {}
        # tx_hash -> identity（判重与冲突检测用，已存在数据不被覆盖）
        self._tx_index = {}

    @property
    def indexer(self):
        """底层查询引擎（TxIndexer），用于全部既有查询与聚合。"""
        return self._indexer

    @property
    def chain_id(self):
        return self._chain_id

    @property
    def confirmed_block_number(self):
        return self._confirmed_height

    @property
    def confirmed_block_hash(self):
        return self._confirmed_hash

    def _cursor(self):
        return _encode_import_cursor(
            self._chain_id,
            self._confirmed_height,
            self._confirmed_hash,
            len(self._tx_index),
        )

    def _success(self, imported_count, skipped_count):
        return {
            "status": "imported",
            "chain_id": self._chain_id,
            "imported_count": imported_count,
            "skipped_count": skipped_count,
            "confirmed_block_number": self._confirmed_height,
            "confirmed_block_hash": self._confirmed_hash,
            "next_import_cursor": self._cursor(),
        }

    def import_batch(self, batch, cursor=None):
        """导入一个批次，返回结构化结果（成功或拒绝），绝不部分写入。

        ``cursor`` 为上一批返回的 ``next_import_cursor``；给定则必须与
        当前导入状态一致，否则整批以 ``IMPORT_CURSOR_MISMATCH`` 拒绝。
        """
        # 1) 批次结构校验（INVALID_IMPORT_BATCH）
        if not isinstance(batch, dict):
            return _rejected(INVALID_IMPORT_BATCH, "批次必须是 JSON 对象")
        chain_id = batch.get("chain_id")
        if not _is_nonempty_text(chain_id):
            return _rejected(INVALID_IMPORT_BATCH, "chain_id 必须为非空字符串")
        block_number = batch.get("block_number")
        if not _is_nonneg_int(block_number):
            return _rejected(
                INVALID_IMPORT_BATCH, "block_number 必须为非负整数"
            )
        block_hash = batch.get("block_hash")
        if not _is_nonempty_text(block_hash):
            return _rejected(INVALID_IMPORT_BATCH, "block_hash 必须为非空字符串")
        transactions = batch.get("transactions")
        if not isinstance(transactions, list):
            return _rejected(INVALID_IMPORT_BATCH, "transactions 必须为列表")

        identities = []
        for obj in transactions:
            identity = _validate_tx(obj, block_number, block_hash)
            if isinstance(identity, str):
                return _rejected(INVALID_IMPORT_BATCH, identity)
            identities.append(identity)

        # 2) 导入游标校验（IMPORT_CURSOR_MISMATCH）
        if cursor is not None:
            saved = _decode_import_cursor(cursor)
            if saved is None:
                return _rejected(IMPORT_CURSOR_MISMATCH, "导入游标非法")
            if (
                self._chain_id is None
                or saved["chain_id"] != self._chain_id
                or saved["confirmed_block_number"] != self._confirmed_height
                or saved["confirmed_block_hash"] != self._confirmed_hash
                or saved["tx_count"] != len(self._tx_index)
            ):
                return _rejected(
                    IMPORT_CURSOR_MISMATCH, "导入游标与当前导入状态不一致"
                )

        # 3) 链标识与高度连续性（IMPORT_CURSOR_MISMATCH）
        if self._chain_id is not None and chain_id != self._chain_id:
            return _rejected(
                IMPORT_CURSOR_MISMATCH,
                "chain_id 与已导入链不一致：%s" % chain_id,
            )
        if self._chain_id is not None:
            if block_number > self._confirmed_height + 1:
                return _rejected(
                    IMPORT_CURSOR_MISMATCH,
                    "批次起始高度 %d 无法从已确认高度 %d 续接"
                    % (block_number, self._confirmed_height),
                )
            if block_number < self._start_height:
                return _rejected(
                    IMPORT_CURSOR_MISMATCH,
                    "批次起始高度 %d 早于已导入起点 %d"
                    % (block_number, self._start_height),
                )

        # 4) 区块内容一致性（BLOCK_CONFLICT）
        known_height = self._hash_to_height.get(block_hash)
        if known_height is not None and known_height != block_number:
            return _rejected(
                BLOCK_CONFLICT,
                "区块哈希 %s 已对应高度 %d" % (block_hash, known_height),
            )
        if (
            self._chain_id is not None
            and self._start_height <= block_number <= self._confirmed_height
        ):
            # 已导入区间内的重试批次：内容必须完全一致
            stored = self._blocks[block_number]
            if stored["block_hash"] != block_hash:
                return _rejected(
                    BLOCK_CONFLICT,
                    "高度 %d 的区块哈希已确认为 %s"
                    % (block_number, stored["block_hash"]),
                )
            stored_identities = stored["identities"]
            if len(stored_identities) != len(
                {tx["tx_hash"] for tx in identities}
            ) or any(
                stored_identities.get(tx["tx_hash"]) != tx for tx in identities
            ):
                return _rejected(
                    BLOCK_CONFLICT,
                    "高度 %d 的交易集合与已导入内容不一致" % block_number,
                )
            # 幂等重试：全部跳过，状态不变，返回相同游标
            return self._success(0, len(identities))

        # 5) 交易级冲突检测（TX_CONFLICT）；完全一致的视为重试跳过
        batch_seen = {}
        new_identities = []
        skipped_count = 0
        for identity in identities:
            tx_hash = identity["tx_hash"]
            existing = batch_seen.get(tx_hash)
            if existing is None:
                existing = self._tx_index.get(tx_hash)
            if existing is not None:
                if existing != identity:
                    return _rejected(
                        TX_CONFLICT,
                        "tx_hash 冲突且字段不一致：%s" % tx_hash,
                    )
                skipped_count += 1
                continue
            batch_seen[tx_hash] = identity
            new_identities.append(identity)

        # 6) 全部校验通过，一次性落库（此前无任何状态修改）
        if self._chain_id is None:
            self._chain_id = chain_id
            self._start_height = block_number
        self._blocks[block_number] = {
            "block_hash": block_hash,
            "identities": {tx["tx_hash"]: tx for tx in new_identities},
        }
        self._hash_to_height[block_hash] = block_number
        self._confirmed_height = block_number
        self._confirmed_hash = block_hash
        self._tx_index.update(batch_seen)
        self._indexer.add_records([dict(tx) for tx in new_identities])

        return self._success(len(new_identities), skipped_count)
