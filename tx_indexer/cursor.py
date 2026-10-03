"""不透明分页游标。

游标为自包含令牌：base64url 编码的 JSON，记录签发命令、签发时的完整
筛选条件与本页最后一项的排序键（exclusive marker）。

- 格式错误、无法解码或字段非法 → InvalidCursorError
- 游标内命令与当前命令不一致（跨命令复用）→ InvalidCursorError
- 游标内筛选与当前请求筛选不一致 → InvalidCursorError

筛选快照中集合类条件（from_address / to_address / method）统一存为
排序后的列表；解码比较时同样归一化，因此旧版游标（method 为单个字符串、
无集合字段、无命令字段）在 query 下筛选等价时仍可续页。
"""

import base64
import binascii
import json

from .errors import InvalidCursorError

_CURSOR_VERSION = 1

#: 各命令在游标中的作用域标识
SCOPE_QUERY = "query"
SCOPE_METHOD_STATS = "method-stats"
SCOPE_ADDRESS_STATS = "address-stats"


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


def _encode_payload(payload):
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(token):
    try:
        padding = "=" * (-len(token) % 4)
        return base64.urlsafe_b64decode(token + padding)
    except (ValueError, binascii.Error) as exc:
        raise InvalidCursorError("游标编码非法", None) from exc


def _decode_payload(token, filters, expected_scope):
    """解码令牌并完成版本、命令作用域、筛选绑定的通用校验。

    返回 payload；``expected_scope`` 为当前命令标识。旧版 query 游标
    （缺少 ``c`` 字段）仅允许在 query 下续用。
    """
    if not isinstance(token, str) or token == "":
        raise InvalidCursorError("游标为空或类型非法", None)

    raw = _b64url_decode(token)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidCursorError("游标内容非法", None) from exc

    if not isinstance(payload, dict) or payload.get("v") != _CURSOR_VERSION:
        raise InvalidCursorError("游标版本不受支持", None)

    scope = payload.get("c", SCOPE_QUERY)
    if scope != expected_scope:
        raise InvalidCursorError("游标不属于当前命令", None)

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

    return payload


def encode_cursor(filters, after_block, after_tx_hash):
    payload = {
        "v": _CURSOR_VERSION,
        "c": SCOPE_QUERY,
        "f": _canonical_filters(filters),
        "after": [after_block, after_tx_hash],
    }
    return _encode_payload(payload)


def decode_cursor(token, filters):
    """解码并校验 query 游标，返回 exclusive marker ``(block_number, tx_hash)``。"""
    payload = _decode_payload(token, filters, SCOPE_QUERY)

    after = payload.get("after")
    if (
        not isinstance(after, list)
        or len(after) != 2
        or not isinstance(after[0], int)
        or isinstance(after[0], bool)
        or after[0] < 0
        or not isinstance(after[1], str)
        or after[1] == ""
    ):
        raise InvalidCursorError("游标位置信息非法", None)

    return after[0], after[1]


def encode_method_stats_cursor(filters, after_total_amount, after_count,
                               after_method):
    payload = {
        "v": _CURSOR_VERSION,
        "c": SCOPE_METHOD_STATS,
        "f": _canonical_filters(filters),
        "after": [after_total_amount, after_count, after_method],
    }
    return _encode_payload(payload)


def decode_method_stats_cursor(token, filters):
    """解码并校验 method-stats 游标。

    返回 exclusive marker ``(total_amount, total_count, method)``。
    """
    payload = _decode_payload(token, filters, SCOPE_METHOD_STATS)

    after = payload.get("after")
    if (
        not isinstance(after, list)
        or len(after) != 3
        or not isinstance(after[0], int)
        or isinstance(after[0], bool)
        or after[0] < 0
        or not isinstance(after[1], int)
        or isinstance(after[1], bool)
        or after[1] < 1
        or not isinstance(after[2], str)
        or after[2] == ""
    ):
        raise InvalidCursorError("游标位置信息非法", None)

    return after[0], after[1], after[2]


def encode_address_stats_cursor(filters, after_total_amount, after_count,
                                after_send_count, after_receive_count,
                                after_address):
    payload = {
        "v": _CURSOR_VERSION,
        "c": SCOPE_ADDRESS_STATS,
        "f": _canonical_filters(filters),
        "after": [
            after_total_amount,
            after_count,
            after_send_count,
            after_receive_count,
            after_address,
        ],
    }
    return _encode_payload(payload)


def decode_address_stats_cursor(token, filters):
    """解码并校验 address-stats 游标。

    返回 exclusive marker ``(total_amount, total_count, send_count,
    receive_count, address)``。
    """
    payload = _decode_payload(token, filters, SCOPE_ADDRESS_STATS)

    after = payload.get("after")
    if (
        not isinstance(after, list)
        or len(after) != 5
        or not isinstance(after[0], int)
        or isinstance(after[0], bool)
        or after[0] < 0
        or not isinstance(after[1], int)
        or isinstance(after[1], bool)
        or after[1] < 1
        or not isinstance(after[2], int)
        or isinstance(after[2], bool)
        or after[2] < 0
        or not isinstance(after[3], int)
        or isinstance(after[3], bool)
        or after[3] < 0
        or not isinstance(after[4], str)
        or after[4] == ""
    ):
        raise InvalidCursorError("游标位置信息非法", None)

    return after[0], after[1], after[2], after[3], after[4]
