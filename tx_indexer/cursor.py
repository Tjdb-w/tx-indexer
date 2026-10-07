"""不透明分页游标。

游标为自包含令牌：base64url 编码的 JSON，记录签发命令、签发时的完整
筛选条件与本页最后一项的排序键（exclusive marker）。

- 格式错误、无法解码或字段非法 → InvalidCursorError
- 游标内命令与当前命令不一致（跨命令复用）→ InvalidCursorError
- 游标内筛选与当前请求筛选不一致 → InvalidCursorError

筛选快照中集合类条件（from_address / to_address / method）统一存为
排序后的列表；解码比较时同样归一化，因此旧版游标（method 为单个字符串、
无集合字段、无命令字段）在 query 下筛选等价时仍可续页。

金额区间端点（min_amount / max_amount）统一存为无前导零的十进制整数
字符串并按数值等价比较：只调整前导零可继续翻页，增加、删除或改变任一
边界后复用旧游标报 InvalidCursorError；未携带金额字段的旧游标只在
未指定金额边界时继续有效。

区块高度边界（min_block / max_block）统一存为非负整数并按数值等价
比较：CLI 侧前导零在解析时归一化（``007`` 与 ``7`` 等价），增加、
删除或改变任一边界后复用旧游标报 InvalidCursorError；未携带区块
边界字段的旧游标只在本次未指定任一边界时可续翻。

状态筛选（status：``"success"`` / ``"failure"`` / 缺省 None）同样
进入筛选快照：改变状态后复用旧游标报 InvalidCursorError；未携带
status 字段的旧游标只在本次未指定状态时可续翻。

时间分桶聚合游标（scope ``time-bucket-aggregation``）只绑定该入口的
完整查询条件（address、method、起止时间）与桶粒度（hour/day），
解码失败或条件不一致抛 InvalidAggregationCursor，与其余分页游标
（InvalidCursorError）相互独立、不能跨命令复用。

method 时间序列游标（scope ``method-time-series``）绑定等价筛选
（含 status、金额与区块数值边界）、起止时间窗与桶粒度（hour/day），
marker 为上一页最后一点的 ``(bucket_start, method)``；解码失败、
篡改、跨命令复用或条件不一致抛 InvalidSeriesCursorError，与其余
分页游标相互独立、不能跨命令复用。
"""

import base64
import binascii
import json

from .errors import (
    InvalidAggregationCursor,
    InvalidCursorError,
    InvalidSeriesCursorError,
)
from .loader import _AMOUNT_RE

_CURSOR_VERSION = 1

#: 各命令在游标中的作用域标识
SCOPE_QUERY = "query"
#: 多链入口 query 的作用域标识（额外绑定 chain_id，与单索引 query 互不可复用）
SCOPE_MULTICHAIN_QUERY = "multichain-query"
SCOPE_METHOD_STATS = "method-stats"
#: 按 method 拆分成功/失败计数与金额的分页统计作用域标识
SCOPE_METHOD_STATUS_STATS = "method-status-stats"
SCOPE_ADDRESS_STATS = "address-stats"
#: 地址 × method 交叉汇总（成功/失败计数拆分）的作用域标识
SCOPE_ADDRESS_METHOD_STATS = "address-method-stats"
SCOPE_COUNTERPARTY_STATS = "counterparty-stats"
SCOPE_TIME_STATS = "time-stats"
SCOPE_PAIR_STATS = "pair-stats"
SCOPE_ADDRESS_TIME_STATS = "address-time-stats"
#: 时间区间 × method 联合汇总（成功/失败计数拆分）的作用域标识
SCOPE_METHOD_TIME_STATS = "method-time-stats"
#: 地址资金流向统计（sent/received/net 拆分）的作用域标识
SCOPE_ADDRESS_FLOW_STATS = "address-flow-stats"
#: 时间分桶聚合（连续 hour/day 桶、含空桶）的作用域标识
SCOPE_TIME_BUCKET_AGGREGATION = "time-bucket-aggregation"
#: method 时间序列（连续 hour/day 桶 × method、含空桶）的作用域标识
SCOPE_METHOD_TIME_SERIES = "method-time-series"
#: 增量导入游标的作用域标识（与所有分页游标相互独立，不能混用）
SCOPE_IMPORT = "import"


def _as_sorted_list(value):
    """集合类筛选的规范形：None → None，单字符串 → [值]，其余 → 排序列表。"""
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    return sorted(value)


def _as_canonical_amount(value):
    """金额端点的规范形：None → None，否则必须为非负十进制整数字符串，
    去掉前导零后按数值等价表示（``"007"`` 与 ``"7"`` 等价）。"""
    if value is None:
        return None
    if not isinstance(value, str) or _AMOUNT_RE.fullmatch(value) is None:
        raise ValueError("金额边界必须为非负十进制整数字符串")
    return str(int(value))


def _as_canonical_block(value):
    """区块高度边界的规范形：None → None，否则必须为非负整数（布尔值
    非法），按数值等价表示。"""
    if value is None:
        return None
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
    ):
        raise ValueError("区块高度边界必须为非负整数")
    return value


def _canonical_filters(filters):
    """提取用于游标绑定比对的筛选快照（集合归一化为排序列表、金额归一化
    为无前导零字符串、区块边界归一化为非负整数）。"""
    return {
        "address": filters.get("address"),
        "from_address": _as_sorted_list(filters.get("from_address")),
        "to_address": _as_sorted_list(filters.get("to_address")),
        "method": _as_sorted_list(filters.get("method")),
        "start_time": filters.get("start_time"),
        "end_time": filters.get("end_time"),
        "min_amount": _as_canonical_amount(filters.get("min_amount")),
        "max_amount": _as_canonical_amount(filters.get("max_amount")),
        "min_block": _as_canonical_block(filters.get("min_block")),
        "max_block": _as_canonical_block(filters.get("max_block")),
        "status": filters.get("status"),
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


def encode_multichain_query_cursor(chain_id, filters, after_block,
                                   after_tx_hash):
    """多链入口 query 游标：除命令作用域与筛选外还绑定 chain_id。"""
    payload = {
        "v": _CURSOR_VERSION,
        "c": SCOPE_MULTICHAIN_QUERY,
        "chain": chain_id,
        "f": _canonical_filters(filters),
        "after": [after_block, after_tx_hash],
    }
    return _encode_payload(payload)


def decode_multichain_query_cursor(token, chain_id, filters):
    """解码并校验多链入口 query 游标。

    返回 exclusive marker ``(block_number, tx_hash)``。游标必须签发自
    多链 query（不能与单索引 query 游标混用），且内绑 chain_id 与筛选
    与当前请求完全一致，否则抛 InvalidCursorError。
    """
    payload = _decode_payload(token, filters, SCOPE_MULTICHAIN_QUERY)

    saved_chain = payload.get("chain")
    if not isinstance(saved_chain, str) or saved_chain == "":
        raise InvalidCursorError("游标链标识非法", None)
    if saved_chain != chain_id:
        raise InvalidCursorError("游标与当前链不匹配", None)

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


def encode_method_status_stats_cursor(filters, after_total_amount,
                                      after_total_count, after_success_count,
                                      after_failure_count, after_method):
    """method-status-stats 游标：绑定命令与等价筛选（含 status），不绑定
    page_size。marker 为上一页最后一组的完整排序键。"""
    payload = {
        "v": _CURSOR_VERSION,
        "c": SCOPE_METHOD_STATUS_STATS,
        "f": _canonical_filters(filters),
        "after": [
            after_total_amount,
            after_total_count,
            after_success_count,
            after_failure_count,
            after_method,
        ],
    }
    return _encode_payload(payload)


def decode_method_status_stats_cursor(token, filters):
    """解码并校验 method-status-stats 游标。

    返回 exclusive marker ``(total_amount, total_count, success_count,
    failure_count, method)``。
    """
    payload = _decode_payload(token, filters, SCOPE_METHOD_STATUS_STATS)

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


def encode_address_method_stats_cursor(filters, after_total_amount,
                                       after_total_count, after_send_count,
                                       after_receive_count, after_address,
                                       after_method):
    """address-method-stats 游标：绑定命令与等价筛选（含 status、金额与
    区块数值边界），不绑定 page_size。marker 为上一页最后一组的完整
    排序键（address、method 为组键）。"""
    payload = {
        "v": _CURSOR_VERSION,
        "c": SCOPE_ADDRESS_METHOD_STATS,
        "f": _canonical_filters(filters),
        "after": [
            after_total_amount,
            after_total_count,
            after_send_count,
            after_receive_count,
            after_address,
            after_method,
        ],
    }
    return _encode_payload(payload)


def decode_address_method_stats_cursor(token, filters):
    """解码并校验 address-method-stats 游标。

    返回 exclusive marker ``(total_amount, total_count, send_count,
    receive_count, address, method)``。
    """
    payload = _decode_payload(token, filters, SCOPE_ADDRESS_METHOD_STATS)

    after = payload.get("after")
    if (
        not isinstance(after, list)
        or len(after) != 6
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
        or not isinstance(after[5], str)
        or after[5] == ""
    ):
        raise InvalidCursorError("游标位置信息非法", None)

    return after[0], after[1], after[2], after[3], after[4], after[5]


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


def encode_method_time_stats_cursor(filters, bucket_size, after_bucket_start,
                                    after_total_amount, after_total_count,
                                    after_success_count, after_failure_count,
                                    after_method):
    """method-time-stats 游标：除筛选外还绑定 bucket_size，不绑定 page_size。"""
    payload = {
        "v": _CURSOR_VERSION,
        "c": SCOPE_METHOD_TIME_STATS,
        "f": _canonical_filters(filters),
        "b": bucket_size,
        "after": [
            after_bucket_start,
            after_total_amount,
            after_total_count,
            after_success_count,
            after_failure_count,
            after_method,
        ],
    }
    return _encode_payload(payload)


def decode_method_time_stats_cursor(token, filters, bucket_size):
    """解码并校验 method-time-stats 游标。

    返回 exclusive marker ``(bucket_start, total_amount, total_count,
    success_count, failure_count, method)``。游标内 bucket_size 与当前
    请求不一致时报 InvalidCursorError。
    """
    payload = _decode_payload(token, filters, SCOPE_METHOD_TIME_STATS)

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


def encode_address_flow_stats_cursor(filters, after_net_amount,
                                     after_sent_amount, after_received_amount,
                                     after_total_count, after_address):
    """address-flow-stats 游标：绑定命令与等价筛选，不绑定 page_size。"""
    payload = {
        "v": _CURSOR_VERSION,
        "c": SCOPE_ADDRESS_FLOW_STATS,
        "f": _canonical_filters(filters),
        "after": [
            after_net_amount,
            after_sent_amount,
            after_received_amount,
            after_total_count,
            after_address,
        ],
    }
    return _encode_payload(payload)


def decode_address_flow_stats_cursor(token, filters):
    """解码并校验 address-flow-stats 游标。

    返回 exclusive marker ``(net_amount, sent_amount, received_amount,
    total_count, address)``。net_amount 可为负整数或零；sent_amount /
    received_amount 为非负整数，total_count 为正整数。
    """
    payload = _decode_payload(token, filters, SCOPE_ADDRESS_FLOW_STATS)

    after = payload.get("after")
    if (
        not isinstance(after, list)
        or len(after) != 5
        or not isinstance(after[0], int)
        or isinstance(after[0], bool)
        or not isinstance(after[1], int)
        or isinstance(after[1], bool)
        or after[1] < 0
        or not isinstance(after[2], int)
        or isinstance(after[2], bool)
        or after[2] < 0
        or not isinstance(after[3], int)
        or isinstance(after[3], bool)
        or after[3] < 1
        or not isinstance(after[4], str)
        or after[4] == ""
    ):
        raise InvalidCursorError("游标位置信息非法", None)

    return after[0], after[1], after[2], after[3], after[4]


#: 时间分桶聚合支持的桶粒度及其桶宽（UTC 秒）
AGGREGATION_GRANULARITIES = {"hour": 3600, "day": 86400}


def _canonical_aggregation_filters(filters):
    """时间分桶聚合的筛选快照：只绑定 address、method 集合与起止时间。

    集合类 method 归一化为排序列表；地址与时间必须保持原始公开类型
    （非空字符串 / 非负整数），否则视为非法快照。
    """
    address = filters.get("address")
    if address is not None and (
        not isinstance(address, str) or address == ""
    ):
        raise ValueError("address 必须为非空字符串")
    method = _as_sorted_list(filters.get("method"))
    if method is not None and any(
        not isinstance(value, str) or value == "" for value in method
    ):
        raise ValueError("method 必须为非空字符串")
    start_time = filters.get("start_time")
    end_time = filters.get("end_time")
    for name, value in (("start_time", start_time), ("end_time", end_time)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("%s 必须为非负整数" % name)
    return {
        "address": address,
        "method": method,
        "start_time": start_time,
        "end_time": end_time,
    }


def encode_time_bucket_aggregation_cursor(filters, granularity,
                                          after_bucket_start):
    """时间分桶聚合游标：绑定完整查询条件、桶粒度与上一页最后一桶起点。

    不绑定 page_size；payload 自包含、base64url 不透明。
    """
    payload = {
        "v": _CURSOR_VERSION,
        "c": SCOPE_TIME_BUCKET_AGGREGATION,
        "f": _canonical_aggregation_filters(filters),
        "g": granularity,
        "after": after_bucket_start,
    }
    return _encode_payload(payload)


def decode_time_bucket_aggregation_cursor(token, filters, granularity):
    """解码并校验时间分桶聚合游标，返回 exclusive marker ``bucket_start``。

    游标格式错误、被篡改、作用域不符、桶粒度或查询条件（address /
    method / 起止时间）与当前请求不一致，或 marker 不是范围内合法桶
    起点时，都抛 InvalidAggregationCursor，绝不返回部分分页数据。
    """
    try:
        if not isinstance(token, str) or token == "":
            raise InvalidCursorError("游标为空或类型非法", None)

        raw = _b64url_decode(token)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidCursorError("游标内容非法", None) from exc

        if not isinstance(payload, dict) or payload.get("v") != _CURSOR_VERSION:
            raise InvalidCursorError("游标版本不受支持", None)

        if payload.get("c") != SCOPE_TIME_BUCKET_AGGREGATION:
            raise InvalidCursorError("游标不属于当前命令", None)

        saved_granularity = payload.get("g")
        if saved_granularity not in AGGREGATION_GRANULARITIES:
            raise InvalidCursorError("游标桶粒度非法", None)
        if saved_granularity != granularity:
            raise InvalidCursorError("游标与当前桶粒度不匹配", None)

        saved = payload.get("f")
        if not isinstance(saved, dict):
            raise InvalidCursorError("游标缺少筛选信息", None)

        try:
            saved_canonical = _canonical_aggregation_filters(saved)
            current_canonical = _canonical_aggregation_filters(filters)
        except (TypeError, ValueError, AttributeError) as exc:
            raise InvalidCursorError("游标筛选信息非法", None) from exc
        if saved_canonical != current_canonical:
            raise InvalidCursorError("游标与当前查询条件不匹配", None)

        after = payload.get("after")
        if (
            not isinstance(after, int)
            or isinstance(after, bool)
            or after < 0
        ):
            raise InvalidCursorError("游标位置信息非法", None)

        width = AGGREGATION_GRANULARITIES[saved_granularity]
        start_time = current_canonical["start_time"]
        end_time = current_canonical["end_time"]
        # marker 必须是查询范围内一个实际返回桶的起点：桶按纪元整点/
        # 整日对齐，首桶可早于 start_time、末桶只覆盖 end_time 之前的
        # 数据。任何越界或错位的篡改值都在此被拒绝（合法游标始终满足
        # 这些条件）。
        first_start = (start_time // width) * width
        last_start = ((end_time - 1) // width) * width
        if after % width != 0 or after < first_start or after > last_start:
            raise InvalidCursorError("游标位置不在合法桶边界上", None)

        return after
    except InvalidCursorError as exc:
        # 时间分桶聚合对外统一使用独立的聚合游标错误类型，不复用
        # invalid_cursor，避免与既有命令的错误语义混淆
        raise InvalidAggregationCursor(exc.message, None) from exc


def encode_method_time_series_cursor(filters, start_time, end_time, bucket,
                                     after_bucket_start, after_method):
    """method-time-series 游标：绑定等价筛选、时间窗、桶粒度与上一页最后
    一点的排序键 ``(bucket_start, method)``。

    不绑定 page_size；payload 自包含、base64url 不透明。
    """
    payload = {
        "v": _CURSOR_VERSION,
        "c": SCOPE_METHOD_TIME_SERIES,
        "f": _canonical_filters(filters),
        "w": [start_time, end_time],
        "g": bucket,
        "after": [after_bucket_start, after_method],
    }
    return _encode_payload(payload)


def decode_method_time_series_cursor(token, filters, start_time, end_time,
                                     bucket):
    """解码并校验 method-time-series 游标。

    返回 exclusive marker ``(bucket_start, method)``。游标格式错误、被
    篡改、无法解码、作用域不符（跨命令复用）、筛选/时间窗/桶粒度与当前
    请求不一致，或 marker 不是范围内合法桶起点时，都抛
    InvalidSeriesCursorError，绝不返回部分分页数据。
    """
    try:
        if not isinstance(token, str) or token == "":
            raise InvalidCursorError("游标为空或类型非法", None)

        raw = _b64url_decode(token)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidCursorError("游标内容非法", None) from exc

        if not isinstance(payload, dict) or payload.get("v") != _CURSOR_VERSION:
            raise InvalidCursorError("游标版本不受支持", None)

        if payload.get("c") != SCOPE_METHOD_TIME_SERIES:
            raise InvalidCursorError("游标不属于当前命令", None)

        saved_granularity = payload.get("g")
        if saved_granularity not in AGGREGATION_GRANULARITIES:
            raise InvalidCursorError("游标桶粒度非法", None)
        if saved_granularity != bucket:
            raise InvalidCursorError("游标与当前桶粒度不匹配", None)

        saved_window = payload.get("w")
        if (
            not isinstance(saved_window, list)
            or len(saved_window) != 2
            or saved_window != [start_time, end_time]
        ):
            raise InvalidCursorError("游标与当前时间窗不匹配", None)

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

        width = AGGREGATION_GRANULARITIES[saved_granularity]
        # marker 的 bucket_start 必须是查询范围内一个实际返回桶的起点：
        # 桶按纪元整点/整日对齐，首桶可早于 start_time、末桶只覆盖
        # end_time 之前的数据。任何越界或错位的篡改值都在此被拒绝。
        first_start = (start_time // width) * width
        last_start = ((end_time - 1) // width) * width
        if (
            after[0] % width != 0
            or after[0] < first_start
            or after[0] > last_start
        ):
            raise InvalidCursorError("游标位置不在合法桶边界上", None)

        return after[0], after[1]
    except InvalidCursorError as exc:
        # method-time-series 对外统一使用独立的序列游标错误类型，不复用
        # invalid_cursor，避免与既有命令的错误语义混淆
        raise InvalidSeriesCursorError(exc.message, None) from exc
