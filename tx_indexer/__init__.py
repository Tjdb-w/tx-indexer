"""Tx Indexer：链上交易索引与查询引擎。

公开接口：
- errors：七类异常
- engine：查询、游标分页、聚合统计
- loader：JSON Lines 加载与校验
- importer：增量交易导入与断点续传
- cli：命令行入口（tx-indexer query / stats / method-stats）
"""

from .errors import (
    DuplicateTransactionError,
    InvalidCursorError,
    InvalidFilterError,
    InvalidPageSizeError,
    InvalidTimeRangeError,
    InvalidTransactionError,
)
from .importer import (
    BLOCK_CONFLICT,
    IMPORT_CURSOR_MISMATCH,
    INVALID_IMPORT_BATCH,
    TX_CONFLICT,
    IncrementalImporter,
)

__all__ = [
    "BLOCK_CONFLICT",
    "DuplicateTransactionError",
    "IMPORT_CURSOR_MISMATCH",
    "INVALID_IMPORT_BATCH",
    "IncrementalImporter",
    "InvalidCursorError",
    "InvalidFilterError",
    "InvalidPageSizeError",
    "InvalidTimeRangeError",
    "InvalidTransactionError",
    "TX_CONFLICT",
]
