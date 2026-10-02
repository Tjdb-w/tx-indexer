"""查询、游标分页与聚合统计引擎。

筛选条件（全部取交集，均可省略）：
- ``address``：精确匹配 from_address 或 to_address
  （不能与 ``from_addresses`` / ``to_addresses`` 并用）
- ``from_addresses`` / ``to_addresses``：付款方 / 收款方集合，集合内任一命中
- ``methods``：method 集合，集合内任一命中
- ``start_time`` / ``end_time``：时间窗，左闭右闭（UTC 秒）

排序：block_number 升序，同高度按 tx_hash 升序。
分页：不透明 keyset 游标（见 :mod:`tx_indexer.cursor`），相同筛选下
不跳过、不重复、不乱序；``total`` 始终为全部匹配数。
"""

import bisect

from .cursor import decode_cursor, encode_cursor
from .errors import InvalidFilterError, InvalidPageSizeError, InvalidTimeRangeError

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 1000

_PUBLIC_FIELDS = (
    "tx_hash",
    "block_number",
    "timestamp",
    "from_address",
    "to_address",
    "method",
    "amount",
)


def _normalize_str_set(values, name):
    """把单个字符串或字符串集合归一化为去重排序后的列表。

    重复值等同一个条件；空或仅含空白的值抛 :class:`InvalidFilterError`。
    """
    if values is None:
        return None
    if isinstance(values, str):
        values = [values]
    result = set()
    for value in values:
        if not isinstance(value, str) or value.strip() == "":
            raise InvalidFilterError(
                "%s 筛选值为空或仅含空白" % name, None
            )
        result.add(value)
    if not result:
        return None
    return sorted(result)


def normalize_filters(address=None, method=None, start_time=None, end_time=None,
                      from_addresses=None, to_addresses=None):
    """校验并归一化筛选条件。

    ``method`` / ``from_addresses`` / ``to_addresses`` 均接受单个字符串或
    字符串集合，归一化为去重排序后的列表（``None`` 表示不筛选）。
    """
    methods = _normalize_str_set(method, "method")
    from_set = _normalize_str_set(from_addresses, "from_address")
    to_set = _normalize_str_set(to_addresses, "to_address")
    if address is not None:
        if not isinstance(address, str) or address.strip() == "":
            raise InvalidFilterError("address 筛选值为空或仅含空白", None)
        if from_set is not None or to_set is not None:
            raise InvalidFilterError(
                "address 不能与 from_address/to_address 筛选同时使用", None
            )
    for name, value in (("start_time", start_time), ("end_time", end_time)):
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            raise ValueError("%s 必须为非负整数" % name)
    if start_time is not None and end_time is not None and start_time > end_time:
        raise InvalidTimeRangeError(
            "时间窗倒置：start_time(%d) 大于 end_time(%d)"
            % (start_time, end_time),
            None,
        )
    return {
        "address": address,
        "methods": methods,
        "from_addresses": from_set,
        "to_addresses": to_set,
        "start_time": start_time,
        "end_time": end_time,
    }


def _matches(record, filters):
    if filters["address"] is not None:
        addr = filters["address"]
        if record["from_address"] != addr and record["to_address"] != addr:
            return False
    methods = filters["methods"]
    if methods is not None and record["method"] not in methods:
        return False
    from_set = filters["from_addresses"]
    if from_set is not None and record["from_address"] not in from_set:
        return False
    to_set = filters["to_addresses"]
    if to_set is not None and record["to_address"] not in to_set:
        return False
    ts = record["timestamp"]
    if filters["start_time"] is not None and ts < filters["start_time"]:
        return False
    if filters["end_time"] is not None and ts > filters["end_time"]:
        return False
    return True


def to_public(record):
    """按公开字段顺序输出一条交易。"""
    return {name: record[name] for name in _PUBLIC_FIELDS}


class TxIndexer:
    """内存中的交易索引（无额外持久化）。"""

    def __init__(self, records):
        # 加载顺序保留；查询时统一排序，不修改入参列表语义
        self._records = list(records)

    def _matched(self, filters):
        matched = [r for r in self._records if _matches(r, filters)]
        matched.sort(key=lambda r: (r["block_number"], r["tx_hash"]))
        return matched

    @staticmethod
    def _validate_page_size(page_size):
        if (
            not isinstance(page_size, int)
            or isinstance(page_size, bool)
            or page_size < 1
            or page_size > MAX_PAGE_SIZE
        ):
            raise InvalidPageSizeError(
                "page_size 必须为 1 到 %d 之间的整数（默认 %d）"
                % (MAX_PAGE_SIZE, DEFAULT_PAGE_SIZE),
                None,
            )

    def query(self, filters, page_size=DEFAULT_PAGE_SIZE, cursor=None):
        """分页查询。返回 {transactions, total, next_cursor}。"""
        self._validate_page_size(page_size)

        matched = self._matched(filters)
        total = len(matched)

        start = 0
        if cursor is not None:
            after_block, after_tx_hash = decode_cursor(cursor, filters)
            # keyset 续页：排序键严格大于 marker 的第一个位置。
            # marker 位于两键之间也安全（bisect 取下一键），不会跳过或重复。
            keys = [(r["block_number"], r["tx_hash"]) for r in matched]
            start = bisect.bisect_left(keys, (after_block, after_tx_hash))
            if start < total and keys[start] == (after_block, after_tx_hash):
                # marker 命中现存记录本身：从其后一条开始
                start += 1

        end = start + page_size
        page = matched[start:end]
        if end < total:
            last = page[-1]
            next_cursor = encode_cursor(
                filters, last["block_number"], last["tx_hash"]
            )
        else:
            next_cursor = None

        return {
            "transactions": [to_public(r) for r in page],
            "total": total,
            "next_cursor": next_cursor,
        }

    def stats(self, filters):
        """聚合统计（忽略分页）。"""
        total_amount = 0
        min_amount = None
        max_amount = None
        count = 0
        for record in self._records:
            if not _matches(record, filters):
                continue
            value = int(record["amount"])
            count += 1
            total_amount += value
            if min_amount is None or value < min_amount:
                min_amount = value
            if max_amount is None or value > max_amount:
                max_amount = value

        if count == 0:
            return {
                "total_count": 0,
                "total_amount": "0",
                "min_amount": None,
                "max_amount": None,
                "avg_amount": None,
            }
        return {
            "total_count": count,
            "total_amount": str(total_amount),
            "min_amount": str(min_amount),
            "max_amount": str(max_amount),
            "avg_amount": str(total_amount // count),
        }
