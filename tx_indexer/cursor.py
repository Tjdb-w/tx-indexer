"""不透明分页游标。

游标为自包含令牌：base64url 编码的 JSON，记录签发命令、签发时的完整
筛选条件与本页最后一条记录的排序键（exclusive marker）。

- 命令作用域：``query`` 的 marker 为 ``(block_number, tx_hash)``；
  ``method-stats`` 的 marker 为 ``(total_amount, total_count, method)``
- 格式错误、无法解码、字段非法或命令不匹配 → InvalidCursorError
- 游标内筛选与当前请求筛选不一致 → InvalidCursorError

筛选快照中集合类条件（from_address / to_address / method）统一存为
排序后的列表；解码比较时同样归一化，因此旧版游标（method 为单个字符串、
无集合字段）在筛选等价时仍可续页。
"""

import base64
import binascii
import json

from .errors import InvalidCursorError

_CURSOR_VERSION = 1

COMMAND_QUERY = "query"
COMMAND_METHOD_STATS = "method-stats"
_COMMANDS = (COMMAND_QUERY, COMMAND_METHOD_STATS)


def _as_sorted_list(value):
    """集合类筛选的规范形：None → None，单字符串 → [值]，其余 → 排序列表。"""
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    return sorted(value)


def _canonical_filters(filters):
    """提取用于游标绑定比对的筛选快照（集合归一化为排序列表）。"""
    return {
        "address": filters.get("address"),
        "from_address": _as_sorted_list(filters.get("from_address")),
        "to_address": _as_sorted_list(filters.get("to_address")),
        "method": _as_sorted_list(filters.get("method")),
        "start_time": filters.get("start_time"),
        "end_time": filters.get("end_time"),
    }


def encode_cursor(filters, command, marker):
    """签发游标。``marker`` 为对应命令排序键的元组。"""
    payload = {
        "v": _CURSOR_VERSION,
        "c": command,
        "f": _canonical_filters(filters),
        "after": list(marker),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(token):
    try:
        padding = "=" * (-len(token) % 4)
        return base64.urlsafe_b64decode(token + padding)
    except (ValueError, binascii.Error) as exc:
        raise InvalidCursorError("游标编码非法", None) from exc


def _is_uint(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_marker(command, after):
    if not isinstance(after, list):
        raise InvalidCursorError("游标位置信息非法", None)
    if command == COMMAND_QUERY:
        if (
            len(after) != 2
            or not _is_uint(after[0])
            or not isinstance(after[1], str)
            or after[1] == ""
        ):
            raise InvalidCursorError("游标位置信息非法", None)
    else:
        if (
            len(after) != 3
            or not _is_uint(after[0])
            or not _is_uint(after[1])
            or not isinstance(after[2], str)
            or after[2] == ""
        ):
            raise InvalidCursorError("游标位置信息非法", None)


def decode_cursor(token, filters, command):
    """解码并校验游标，返回对应命令的 exclusive marker 元组。"""
    if not isinstance(token, str) or token == "":
        raise InvalidCursorError("游标为空或类型非法", None)
    if command not in _COMMANDS:
        raise InvalidCursorError("游标命令不受支持", None)

    raw = _b64url_decode(token)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidCursorError("游标内容非法", None) from exc

    if not isinstance(payload, dict) or payload.get("v") != _CURSOR_VERSION:
        raise InvalidCursorError("游标版本不受支持", None)

    saved_command = payload.get("c")
    if saved_command not in _COMMANDS or saved_command != command:
        raise InvalidCursorError("游标与当前命令不匹配", None)

    saved = payload.get("f")
    if not isinstance(saved, dict):
        raise InvalidCursorError("游标缺少筛选信息", None)

    try:
        saved_canonical = _canonical_filters(saved)
        current_canonical = _canonical_filters(filters)
    except (TypeError, ValueError) as exc:
        raise InvalidCursorError("游标筛选信息非法", None) from exc
    if saved_canonical != current_canonical:
        raise InvalidCursorError("游标与当前筛选条件不匹配", None)

    after = payload.get("after")
    _validate_marker(command, after)

    if command == COMMAND_QUERY:
        return after[0], after[1]
    return after[0], after[1], after[2]
