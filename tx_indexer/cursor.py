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
SCOPE_COUNTERPARTY_STATS = "counterparty-stats"
SCOPE_TIME_STATS = "time-stats"
SCOPE_PAIR_STATS = "pair-stats"
SCOPE_ADDRESS_TIME_STATS = "address-time-stats"
#: 增量导入游标的作用域标识（与所有分页游标相互独立，不能混用）
SCOPE_IMPORT = "import"


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


def encode_address_stats_cursor(filters, after_total_amount, after_total_count,
                                after_send_count, after_receive_count,
                                after_address):
    return _encode_stats_cursor(
        SCOPE_ADDRESS_STATS, filters, after_total_amount, after_total_count,
        after_send_count, after_receive_count, after_address,
    )


def decode_address_stats_cursor(token, filters):
    """解码并校验 address-stats 游标。

    返回 exclusive marker ``(total_amount, total_count, send_count,
    receive_count, address)``。
    """
    return _decode_stats_cursor(token, filters, SCOPE_ADDRESS_STATS)


def encode_counterparty_stats_cursor(filters, after_total_amount,
                                     after_total_count, after_send_count,
                                     after_receive_count, after_counterparty):
    return _encode_stats_cursor(
        SCOPE_COUNTERPARTY_STATS, filters, after_total_amount,
        after_total_count, after_send_count, after_receive_count,
        after_counterparty,
    )


def decode_counterparty_stats_cursor(token, filters):
    """解码并校验 counterparty-stats 游标。

    返回 exclusive marker ``(total_amount, total_count, send_count,
    receive_count, counterparty)``。
    """
    return _decode_stats_cursor(token, filters, SCOPE_COUNTERPARTY_STATS)


def _encode_stats_cursor(scope, filters, after_total_amount,
                         after_total_count, after_send_count,
                         after_receive_count, after_key):
    """address-stats / counterparty-stats 共用的五元组排序键游标编码。"""
    payload = {
        "v": _CURSOR_VERSION,
        "c": scope,
        "f": _canonical_filters(filters),
        "after": [
            after_total_amount,
            after_total_count,
            after_send_count,
            after_receive_count,
            after_key,
        ],
    }
    return _encode_payload(payload)


def _decode_stats_cursor(token, filters, expected_scope):
    """五元组排序键游标的通用解码（末位为地址或对手字符串键）。"""
    payload = _decode_payload(token, filters, expected_scope)

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


def encode_time_stats_cursor(filters, bucket_size, after_bucket_start):
    """time-stats 游标：除筛选外还绑定 bucket_size，不绑定 page_size。"""
    payload = {
        "v": _CURSOR_VERSION,
        "c": SCOPE_TIME_STATS,
        "f": _canonical_filters(filters),
        "b": bucket_size,
        "after": after_bucket_start,
    }
    return _encode_payload(payload)


def decode_time_stats_cursor(token, filters, bucket_size):
    """解码并校验 time-stats 游标，返回 exclusive marker ``bucket_start``。

    游标内 bucket_size 与当前请求不一致时报 InvalidCursorError。
    """
    payload = _decode_payload(token, filters, SCOPE_TIME_STATS)

    saved_bucket_size = payload.get("b")
    if (
        not isinstance(saved_bucket_size, int)
        or isinstance(saved_bucket_size, bool)
        or saved_bucket_size != bucket_size
    ):
        raise InvalidCursorError("游标与当前 bucket_size 不匹配", None)

    after = payload.get("after")
    if (
        not isinstance(after, int)
        or isinstance(after, bool)
        or after < 0
    ):
        raise InvalidCursorError("游标位置信息非法", None)

    return after


def encode_import_cursor(chain_id, confirmed_height, block_hash):
    """导入游标：绑定链标识、已确认区块高度与对应区块哈希。

    不携带筛选快照，作用域为 ``import``，与全部分页游标相互独立：
    分页游标解码要求各自命令作用域，导入游标用于分页会报
    InvalidCursorError，反之亦然。
    """
    payload = {
        "v": _CURSOR_VERSION,
        "c": SCOPE_IMPORT,
        "chain": chain_id,
        "height": confirmed_height,
        "hash": block_hash,
    }
    return _encode_payload(payload)


def decode_import_cursor(token):
    """解码并校验导入游标。

    返回 ``(chain_id, confirmed_height, block_hash)``；格式错误、版本
    不符、作用域不符或字段非法都抛 InvalidCursorError。
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

    if payload.get("c") != SCOPE_IMPORT:
        raise InvalidCursorError("游标不属于导入命令", None)

    chain_id = payload.get("chain")
    if not isinstance(chain_id, str) or chain_id.strip() == "":
        raise InvalidCursorError("游标链标识非法", None)

    height = payload.get("height")
    if (
        not isinstance(height, int)
        or isinstance(height, bool)
        or height < 0
    ):
        raise InvalidCursorError("游标区块高度非法", None)

    block_hash = payload.get("hash")
    if not isinstance(block_hash, str) or block_hash.strip() == "":
        raise InvalidCursorError("游标区块哈希非法", None)

    return chain_id, height, block_hash


def encode_pair_stats_cursor(filters, after_total_amount, after_count,
                             after_from_address, after_to_address):
    payload = {
        "v": _CURSOR_VERSION,
        "c": SCOPE_PAIR_STATS,
        "f": _canonical_filters(filters),
        "after": [
            after_total_amount,
            after_count,
            after_from_address,
            after_to_address,
        ],
    }
    return _encode_payload(payload)

def decode_pair_stats_cursor(token, filters):
    """解码并校验 pair-stats 游标。

    返回 exclusive marker ``(total_amount, total_count, from_address,
    to_address)``。
    """
    payload = _decode_payload(token, filters, SCOPE_PAIR_STATS)

    after = payload.get("after")
    if (
        not isinstance(after, list)
        or len(after) != 4
        or not isinstance(after[0], int)
        or isinstance(after[0], bool)
        or after[0] < 0
        or not isinstance(after[1], int)
        or isinstance(after[1], bool)
        or after[1] < 1
        or not isinstance(after[2], str)
        or after[2] == ""
        or not isinstance(after[3], str)
        or after[3] == ""
    ):
        raise InvalidCursorError("游标位置信息非法", None)

    return after[0], after[1], after[2], after[3]


def encode_address_time_stats_cursor(filters, bucket_size,
                                     after_bucket_start, after_total_amount,
                                     after_total_count, after_send_count,
                                     after_receive_count, after_address):
    """address-time-stats 游标：除筛选外还绑定 bucket_size，不绑定 page_size。"""
    payload = {
        "v": _CURSOR_VERSION,
        "c": SCOPE_ADDRESS_TIME_STATS,
        "f": _canonical_filters(filters),
        "b": bucket_size,
        "after": [
            after_bucket_start,
            after_total_amount,
            after_total_count,
            after_send_count,
            after_receive_count,
            after_address,
        ],
    }
    return _encode_payload(payload)


def decode_address_time_stats_cursor(token, filters, bucket_size):
    """解码并校验 address-time-stats 游标。

    返回 exclusive marker ``(bucket_start, total_amount, total_count,
    send_count, receive_count, address)``。游标内 bucket_size 与当前
    请求不一致时报 InvalidCursorError。
    """
    payload = _decode_payload(token, filters, SCOPE_ADDRESS_TIME_STATS)

    saved_bucket_size = payload.get("b")
    if (
        not isinstance(saved_bucket_size, int)
        or isinstance(saved_bucket_size, bool)
        or saved_bucket_size != bucket_size
    ):
        raise InvalidCursorError("游标与当前 bucket_size 不匹配", None)

    after = payload.get("after")
    if (
        not isinstance(after, list)
        or len(after) != 6
        or not isinstance(after[0], int)
        or isinstance(after[0], bool)
        or after[0] < 0
        or not isinstance(after[1], int)
        or isinstance(after[1], bool)
        or after[1] < 0
        or not isinstance(after[2], int)
        or isinstance(after[2], bool)
        or after[2] < 1
        or not isinstance(after[3], int)
        or isinstance(after[3], bool)
        or after[3] < 0
        or not isinstance(after[4], int)
        or isinstance(after[4], bool)
        or after[4] < 0
        or not isinstance(after[5], str)
        or after[5] == ""
    ):
        raise InvalidCursorError("游标位置信息非法", None)

    return after[0], after[1], after[2], after[3], after[4], after[5]
