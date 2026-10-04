"""Tx Indexer 的异常类型。

所有用户可见错误都携带 ``error``（错误码）与 ``message``（人类可读说明）；
由输入数据行引起的错误还携带 ``input_line``（1 起始行号），其余错误为 None。
"""


class TxIndexerError(Exception):
    """所有 Tx Indexer 错误的基类。"""

    #: 错误码，子类覆盖
    error = "error"

    def __init__(self, message, input_line=None):
        super().__init__(message)
        self.message = message
        self.input_line = input_line

    def to_dict(self):
        """返回结构化错误对象：error、message、input_line。"""
        return {
            "error": self.error,
            "message": self.message,
            "input_line": self.input_line,
        }


class InvalidTransactionError(TxIndexerError):
    """记录解析或字段校验失败。"""

    error = "invalid_transaction"


class DuplicateTransactionError(TxIndexerError):
    """tx_hash 与已加载记录冲突。"""

    error = "duplicate_transaction"


class InvalidTimeRangeError(TxIndexerError):
    """时间窗倒置（start_time > end_time）。"""

    error = "invalid_time_range"


class InvalidFilterError(TxIndexerError):
    """筛选条件非法（值为空白）或组合冲突（address 与 from/to 并用）。"""

    error = "invalid_filter"


class InvalidPageSizeError(TxIndexerError):
    """page_size 越界（不在 1..1000）或无法解析。"""

    error = "invalid_page_size"


class InvalidCursorError(TxIndexerError):
    """游标非法（格式错误、解码失败）或与当前筛选不匹配。"""

    error = "invalid_cursor"


class InvalidBucketSizeError(TxIndexerError):
    """bucket_size 缺失、无法解析为整数或不大于 0。"""

    error = "invalid_bucket_size"


class SourceUnavailableError(TxIndexerError):
    """上游暂时无法返回指定区块；已提交批次与水位保持有效。"""

    error = "source_unavailable"


class TransactionConflictError(TxIndexerError):
    """相同 tx_hash 再次出现但标准化内容（区块、时间、地址、方法）不一致。"""

    error = "transaction_conflict"
