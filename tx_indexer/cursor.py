"""不透明分页游标。

游标为自包含令牌：base64url 编码的 JSON，记录签发时的筛选条件与
本页最后一条记录的排序键 ``(block_number, tx_hash)``（exclusive marker）。

- 格式错误、无法解码或字段非法 → InvalidCursorError
- 游标内筛选与当前请求筛选不一致 → InvalidCursorError
"""

import base64
import binascii
import json

from .errors import InvalidCursorError

_CURSOR_VERSION = 1


def encode_cursor(filters, after_block, after_tx_hash):
    payload = {
        "v": _CURSOR_VERSION,
        "f": {
            "address": filters.get("address"),
            "method": filters.get("method"),
            "start_time": filters.get("start_time"),
            "end_time": filters.get("end_time"),
        },
        "after": [after_block, after_tx_hash],
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(token):
    try:
        padding = "=" * (-len(token) % 4)
        return base64.urlsafe_b64decode(token + padding)
    except (ValueError, binascii.Error) as exc:
        raise InvalidCursorError("游标编码非法", None) from exc


def decode_cursor(token, filters):
    """解码并校验游标，返回 exclusive marker ``(block_number, tx_hash)``。"""
    if not isinstance(token, str) or token == "":
        raise InvalidCursorError("游标为空或类型非法", None)

    raw = _b64url_decode(token)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidCursorError("游标内容非法", None) from exc

    if not isinstance(payload, dict) or payload.get("v") != _CURSOR_VERSION:
        raise InvalidCursorError("游标版本不受支持", None)

    saved = payload.get("f")
    if not isinstance(saved, dict):
        raise InvalidCursorError("游标缺少筛选信息", None)

    current = {
        "address": filters.get("address"),
        "method": filters.get("method"),
        "start_time": filters.get("start_time"),
        "end_time": filters.get("end_time"),
    }
    if any(saved.get(key) != current[key] for key in current):
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

    return after[0], after[1]
