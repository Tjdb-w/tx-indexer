#!/usr/bin/env python3
"""Tx Indexer: 链上交易索引与查询引擎。

读取 JSON Lines 交易记录（每行字段：tx_hash、block_number、timestamp、
from_address、to_address、method、amount），支持按地址 / 方法 / 时间窗
筛选的分页查询（query）与聚合统计（stats）。
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import re
import sys

FIELDS = (
    "tx_hash",
    "block_number",
    "timestamp",
    "from_address",
    "to_address",
    "method",
    "amount",
)

DEFAULT_PAGE_SIZE = 100
MIN_PAGE_SIZE = 1
MAX_PAGE_SIZE = 1000

EXIT_OK = 0
EXIT_ERROR = 2

_AMOUNT_RE = re.compile(r"[0-9]+")


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------

class TxIndexerError(Exception):
    """所有领域错误的基类；to_dict() 产出命令行错误对象。"""

    error_code = "error"

    def __init__(self, message, input_line=None):
        super().__init__(message)
        self.message = message
        self.input_line = input_line

    def to_dict(self):
        return {
            "error": self.error_code,
            "message": self.message,
            "input_line": self.input_line,
        }


class InvalidTransactionError(TxIndexerError):
    """输入行解析或校验失败。"""

    error_code = "invalid_transaction"


class DuplicateTransactionError(TxIndexerError):
    """tx_hash 冲突。"""

    error_code = "duplicate_transaction"


class InvalidTimeRangeError(TxIndexerError):
    """时间窗倒置（from_ts > to_ts）或边界非法。"""

    error_code = "invalid_time_range"


class InvalidPageSizeError(TxIndexerError):
    """page_size 越界。"""

    error_code = "invalid_page_size"


class InvalidCursorError(TxIndexerError):
    """游标非法或与当前筛选不匹配。"""

    error_code = "invalid_cursor"


# ---------------------------------------------------------------------------
# 解析与校验
# ---------------------------------------------------------------------------

def _is_non_negative_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_non_empty_str(value):
    return isinstance(value, str) and value != ""


def parse_transaction(raw_line, line_no):
    """解析并校验单行 JSON 交易记录，返回只含规范字段的字典。"""
    try:
        obj = json.loads(raw_line)
    except json.JSONDecodeError as exc:
        raise InvalidTransactionError(
            "line %d: invalid JSON: %s" % (line_no, exc.msg),
            input_line=line_no,
        )
    if not isinstance(obj, dict):
        raise InvalidTransactionError(
            "line %d: transaction must be a JSON object" % line_no,
            input_line=line_no,
        )

    missing = [f for f in FIELDS if f not in obj]
    if missing:
        raise InvalidTransactionError(
            "line %d: missing field(s): %s" % (line_no, ", ".join(missing)),
            input_line=line_no,
        )

    tx_hash = obj["tx_hash"]
    if not _is_non_empty_str(tx_hash):
        raise InvalidTransactionError(
            "line %d: tx_hash must be a non-empty string" % line_no,
            input_line=line_no,
        )

    block_number = obj["block_number"]
    if not _is_non_negative_int(block_number):
        raise InvalidTransactionError(
            "line %d: block_number must be a non-negative integer" % line_no,
            input_line=line_no,
        )

    timestamp = obj["timestamp"]
    if not _is_non_negative_int(timestamp):
        raise InvalidTransactionError(
            "line %d: timestamp must be a non-negative integer" % line_no,
            input_line=line_no,
        )

    from_address = obj["from_address"]
    if not _is_non_empty_str(from_address):
        raise InvalidTransactionError(
            "line %d: from_address must be a non-empty string" % line_no,
            input_line=line_no,
        )

    to_address = obj["to_address"]
    if not _is_non_empty_str(to_address):
        raise InvalidTransactionError(
            "line %d: to_address must be a non-empty string" % line_no,
            input_line=line_no,
        )

    method = obj["method"]
    if not _is_non_empty_str(method):
        raise InvalidTransactionError(
            "line %d: method must be a non-empty string" % line_no,
            input_line=line_no,
        )

    amount = obj["amount"]
    if not isinstance(amount, str) or not _AMOUNT_RE.fullmatch(amount):
        raise InvalidTransactionError(
            "line %d: amount must be a non-negative decimal integer string" % line_no,
            input_line=line_no,
        )

    return {
        "tx_hash": tx_hash,
        "block_number": block_number,
        "timestamp": timestamp,
        "from_address": from_address,
        "to_address": to_address,
        "method": method,
        "amount": amount,
    }


def load_transactions(path):
    """读取 JSON Lines 文件，返回校验通过的交易列表（保持文件顺序）。"""
    transactions = []
    seen_hashes = {}
    try:
        fh = open(path, "r", encoding="utf-8")
    except OSError as exc:
        raise InvalidTransactionError("cannot open input file: %s" % exc)
    with fh:
        for line_no, raw in enumerate(fh, 1):
            if raw.strip() == "":
                continue
            tx = parse_transaction(raw, line_no)
            tx_hash = tx["tx_hash"]
            if tx_hash in seen_hashes:
                raise DuplicateTransactionError(
                    "duplicate tx_hash %r (first seen on line %d)"
                    % (tx_hash, seen_hashes[tx_hash]),
                    input_line=line_no,
                )
            seen_hashes[tx_hash] = line_no
            transactions.append(tx)
    return transactions


# ---------------------------------------------------------------------------
# 筛选
# ---------------------------------------------------------------------------

def matches_filters(tx, address=None, method=None, from_ts=None, to_ts=None):
    """筛选条件取交集：地址精确匹配发送或接收方，方法精确匹配，时间窗左闭右闭。"""
    if address is not None and tx["from_address"] != address and tx["to_address"] != address:
        return False
    if method is not None and tx["method"] != method:
        return False
    if from_ts is not None and tx["timestamp"] < from_ts:
        return False
    if to_ts is not None and tx["timestamp"] > to_ts:
        return False
    return True


def validate_time_range(from_ts, to_ts):
    if from_ts is not None and from_ts < 0:
        raise InvalidTimeRangeError("from_ts must be a non-negative integer")
    if to_ts is not None and to_ts < 0:
        raise InvalidTimeRangeError("to_ts must be a non-negative integer")
    if from_ts is not None and to_ts is not None and from_ts > to_ts:
        raise InvalidTimeRangeError(
            "invalid time range: from_ts (%d) is greater than to_ts (%d)"
            % (from_ts, to_ts)
        )


def sort_key(tx):
    """block_number 升序，同高度按 tx_hash 升序。"""
    return (tx["block_number"], tx["tx_hash"])


# ---------------------------------------------------------------------------
# 游标
# ---------------------------------------------------------------------------

def _filter_fingerprint(address, method, from_ts, to_ts):
    payload = json.dumps(
        {"address": address, "method": method, "from_ts": from_ts, "to_ts": to_ts},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def encode_cursor(block_number, tx_hash, fingerprint):
    raw = json.dumps(
        {"v": 1, "f": fingerprint, "b": block_number, "h": tx_hash},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(cursor, fingerprint):
    try:
        raw = base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True)
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise InvalidCursorError("malformed cursor")
    if not isinstance(obj, dict) or obj.get("v") != 1:
        raise InvalidCursorError("malformed cursor")
    block_number = obj.get("b")
    tx_hash = obj.get("h")
    if (
        not _is_non_negative_int(block_number)
        or not _is_non_empty_str(tx_hash)
        or not isinstance(obj.get("f"), str)
    ):
        raise InvalidCursorError("malformed cursor")
    if obj["f"] != fingerprint:
        raise InvalidCursorError("cursor does not match the current filters")
    return (block_number, tx_hash)


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------

def run_query(path, address=None, method=None, from_ts=None, to_ts=None,
              page_size=DEFAULT_PAGE_SIZE, cursor=None):
    if not (MIN_PAGE_SIZE <= page_size <= MAX_PAGE_SIZE):
        raise InvalidPageSizeError(
            "page_size must be between %d and %d, got %r"
            % (MIN_PAGE_SIZE, MAX_PAGE_SIZE, page_size)
        )
    validate_time_range(from_ts, to_ts)

    fingerprint = _filter_fingerprint(address, method, from_ts, to_ts)
    start_key = None
    if cursor is not None:
        start_key = decode_cursor(cursor, fingerprint)

    transactions = load_transactions(path)
    matched = sorted(
        (tx for tx in transactions
         if matches_filters(tx, address, method, from_ts, to_ts)),
        key=sort_key,
    )
    total = len(matched)

    if start_key is not None:
        matched = [tx for tx in matched if sort_key(tx) > start_key]

    page = matched[:page_size]
    if len(matched) > page_size:
        last = page[-1]
        next_cursor = encode_cursor(last["block_number"], last["tx_hash"], fingerprint)
    else:
        next_cursor = None

    return {
        "transactions": page,
        "total": total,
        "next_cursor": next_cursor,
    }


def run_stats(path, address=None, method=None, from_ts=None, to_ts=None):
    validate_time_range(from_ts, to_ts)

    transactions = load_transactions(path)
    amounts = [
        int(tx["amount"])
        for tx in transactions
        if matches_filters(tx, address, method, from_ts, to_ts)
    ]

    if not amounts:
        return {
            "total_count": 0,
            "total_amount": "0",
            "min_amount": None,
            "max_amount": None,
            "avg_amount": None,
        }

    total_amount = sum(amounts)
    return {
        "total_count": len(amounts),
        "total_amount": str(total_amount),
        "min_amount": str(min(amounts)),
        "max_amount": str(max(amounts)),
        "avg_amount": str(total_amount // len(amounts)),
    }


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------

def _add_common_arguments(parser):
    parser.add_argument("file", nargs="?", default=None,
                        help="JSON Lines 交易文件路径")
    parser.add_argument("--file", dest="file_option", default=None,
                        help="JSON Lines 交易文件路径（同位置参数）")
    parser.add_argument("--address", default=None,
                        help="精确匹配发送方或接收方地址")
    parser.add_argument("--method", default=None,
                        help="精确匹配方法名")
    parser.add_argument("--from-ts", type=int, default=None,
                        help="时间窗下界（UTC 秒，含）")
    parser.add_argument("--to-ts", type=int, default=None,
                        help="时间窗上界（UTC 秒，含）")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="tx-indexer",
        description="链上交易索引与查询引擎",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    query = subparsers.add_parser("query", help="分页查询交易")
    _add_common_arguments(query)
    query.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE,
                       help="每页条数（1-1000，默认 100）")
    query.add_argument("--cursor", default=None, help="分页游标")

    stats = subparsers.add_parser("stats", help="聚合统计（忽略分页）")
    _add_common_arguments(stats)

    return parser


def _resolve_file(args, parser):
    path = args.file_option or args.file
    if path is None:
        parser.error("missing input file (positional argument or --file)")
    return path


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        path = _resolve_file(args, parser)
        if args.command == "query":
            result = run_query(
                path,
                address=args.address,
                method=args.method,
                from_ts=args.from_ts,
                to_ts=args.to_ts,
                page_size=args.page_size,
                cursor=args.cursor,
            )
        else:
            result = run_stats(
                path,
                address=args.address,
                method=args.method,
                from_ts=args.from_ts,
                to_ts=args.to_ts,
            )
    except TxIndexerError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False), file=sys.stderr)
        return EXIT_ERROR

    print(json.dumps(result, ensure_ascii=False))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
