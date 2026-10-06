"""JSON Lines 数据加载与校验。

每行必须是一个 JSON 对象，且恰好包含以下字段：

- ``tx_hash``：非空字符串，全文件唯一
- ``block_number``：非负整数
- ``timestamp``：非负整数（UTC 秒）
- ``from_address`` / ``to_address`` / ``method``：非空字符串
- ``amount``：非负十进制整数字符串（如 ``"1000"``，不接受数字类型、负号、小数点）

另有可选字段：

- ``success``：布尔值，只能是 ``true`` 或 ``false``；缺省视为 ``true``。
  数字（``0`` / ``1``）、字符串、``null`` 等均为非法记录。

任何解析或校验失败抛 :class:`~tx_indexer.errors.InvalidTransactionError`，
``input_line`` 为 1 起始的物理行号；``tx_hash`` 冲突抛
:class:`~tx_indexer.errors.DuplicateTransactionError`。
"""

import json
import re

from .errors import DuplicateTransactionError, InvalidTransactionError

_FIELDS = (
    "tx_hash",
    "block_number",
    "timestamp",
    "from_address",
    "to_address",
    "method",
    "amount",
)
#: 允许出现的可选字段；不在此集合中的额外字段仍为非法记录
_OPTIONAL_FIELDS = ("success",)
_AMOUNT_RE = re.compile(r"[0-9]+")


def _is_nonneg_int(value):
    # bool 是 int 的子类，必须显式排除
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_nonempty_text(value):
    return isinstance(value, str) and value.strip() != ""


def parse_record(obj, line_no):
    """把一个已解析的 JSON 值校验并转换为内部记录。"""
    if not isinstance(obj, dict):
        raise InvalidTransactionError("每行必须是 JSON 对象", line_no)

    keys = set(obj.keys())
    missing = [f for f in _FIELDS if f not in keys]
    if missing:
        raise InvalidTransactionError(
            "缺少字段：%s" % ", ".join(missing), line_no
        )
    allowed = set(_FIELDS) | set(_OPTIONAL_FIELDS)
    extra = sorted(keys - allowed)
    if extra:
        raise InvalidTransactionError(
            "存在未定义字段：%s" % ", ".join(extra), line_no
        )

    tx_hash = obj["tx_hash"]
    if not _is_nonempty_text(tx_hash):
        raise InvalidTransactionError("tx_hash 必须为非空字符串", line_no)

    block_number = obj["block_number"]
    if not _is_nonneg_int(block_number):
        raise InvalidTransactionError(
            "block_number 必须为非负整数", line_no
        )

    timestamp = obj["timestamp"]
    if not _is_nonneg_int(timestamp):
        raise InvalidTransactionError(
            "timestamp 必须为非负 UTC 秒整数", line_no
        )

    for name in ("from_address", "to_address", "method"):
        if not _is_nonempty_text(obj[name]):
            raise InvalidTransactionError(
                "%s 必须为非空字符串" % name, line_no
            )

    amount = obj["amount"]
    if not isinstance(amount, str) or _AMOUNT_RE.fullmatch(amount) is None:
        raise InvalidTransactionError(
            "amount 必须为非负十进制整数字符串", line_no
        )

    # success 可选：缺省视为 true；显式给出时只能是 JSON 布尔值。
    # bool 是 int 的子类，0/1 不会通过 isinstance(..., bool) 检查，
    # 因此数字、字符串、null 等都在此被拒绝。
    success = obj.get("success", True)
    if not isinstance(success, bool):
        raise InvalidTransactionError(
            "success 必须为布尔值 true 或 false", line_no
        )

    return {
        "tx_hash": tx_hash,
        "block_number": block_number,
        "timestamp": timestamp,
        "from_address": obj["from_address"],
        "to_address": obj["to_address"],
        "method": obj["method"],
        "amount": amount,
        "success": success,
    }


def load_lines(lines):
    """从行迭代器加载记录，返回内部记录列表。"""
    records = []
    seen = set()
    for line_no, raw in enumerate(lines, start=1):
        text = raw.strip()
        if text == "":
            raise InvalidTransactionError("空行不是合法记录", line_no)
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            raise InvalidTransactionError(
                "JSON 解析失败：%s" % exc.msg, line_no
            ) from exc

        record = parse_record(obj, line_no)

        if record["tx_hash"] in seen:
            raise DuplicateTransactionError(
                "tx_hash 冲突：%s" % record["tx_hash"], line_no
            )
        seen.add(record["tx_hash"])
        records.append(record)
    return records


def load_file(path):
    """从 JSON Lines 文件路径加载记录。"""
    with open(path, "r", encoding="utf-8") as fh:
        return load_lines(fh)
