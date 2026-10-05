"""Tx Indexer：链上交易索引与查询引擎。

公开接口：
- errors：异常类型
- engine：查询、游标分页、聚合统计
- loader：JSON Lines 加载与校验
- importer：增量交易导入与断点续传
- replay：索引水位与幂等重放
- cli：命令行入口（tx-indexer query / stats / method-stats）
"""

from .errors import (
    DuplicateTransactionError,
    InvalidAmountFilterError,
    InvalidAmountRangeError,
    InvalidCursorError,
    InvalidFilterError,
    InvalidPageSizeError,
    InvalidTimeRangeError,
    InvalidTransactionError,
    SourceUnavailableError,
    TransactionConflictError,
)
from .importer import (
    BLOCK_CONFLICT,
    IMPORT_CURSOR_MISMATCH,
    INVALID_IMPORT_BATCH,
    INVALID_REPLACEMENT_BATCH,
    TX_CONFLICT,
    IncrementalImporter,
)
from .replay import MultiChainReplayManager, ReplayManager

__all__ = [
    "BLOCK_CONFLICT",
    "DuplicateTransactionError",
    "IMPORT_CURSOR_MISMATCH",
    "INVALID_IMPORT_BATCH",
    "INVALID_REPLACEMENT_BATCH",
    "IncrementalImporter",
    "InvalidAmountFilterError",
    "InvalidAmountRangeError",
    "InvalidCursorError",
    "InvalidFilterError",
    "InvalidPageSizeError",
    "InvalidTimeRangeError",
    "InvalidTransactionError",
    "MultiChainReplayManager",
    "ReplayManager",
    "SourceUnavailableError",
    "TX_CONFLICT",
    "TransactionConflictError",
]
