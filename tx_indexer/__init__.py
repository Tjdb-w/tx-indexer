"""Tx Indexer：链上交易索引与查询引擎。

公开接口：
- errors：七类异常
- engine：查询、游标分页、聚合统计
- loader：JSON Lines 加载与校验
- cli：命令行入口（tx-indexer query / stats / method-stats /
  address-stats / counterparty-stats / time-stats）
"""

from .errors import (
    DuplicateTransactionError,
    InvalidBucketSizeError,
    InvalidCursorError,
    InvalidFilterError,
    InvalidPageSizeError,
    InvalidTimeRangeError,
    InvalidTransactionError,
)

__all__ = [
    "DuplicateTransactionError",
    "InvalidBucketSizeError",
    "InvalidCursorError",
    "InvalidFilterError",
    "InvalidPageSizeError",
    "InvalidTimeRangeError",
    "InvalidTransactionError",
]
