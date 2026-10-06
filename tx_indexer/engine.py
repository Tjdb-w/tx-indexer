"""查询、游标分页与聚合统计引擎。

筛选条件（不同条件之间取交集，均可省略）：
- ``address``：精确匹配 from_address 或 to_address（不可与
  ``from_address`` / ``to_address`` 并用）
- ``from_address`` / ``to_address`` / ``method``：值集合，集合内部
  任一命中即可；可重复给定，重复值等同一个条件
- ``start_time`` / ``end_time``：时间窗，左闭右闭（UTC 秒）
- ``min_amount`` / ``max_amount``：金额闭区间（按十进制整数数值比较，
  交易 amount 的非负十进制整数字符串形式；前导零不改变数值含义），
  只给一端时另一端不限制
- ``min_block`` / ``max_block``：区块高度闭区间（非负整数，含端点），
  只给一端时另一端不限制

query 排序：block_number 升序，同高度按 tx_hash 升序。
method-stats 排序：total_amount 降序、total_count 降序、method 码点升序。
address-stats 排序：total_amount 降序、total_count 降序、send_count 降序、
receive_count 降序、address 码点升序。
counterparty-stats 排序键与 address-stats 相同，末级改为 counterparty
码点升序；每笔匹配交易只归入一个对手（发送方为观察地址时归
to_address，接收方为观察地址时归 from_address，自转账归观察地址本身）。
time-stats 把匹配交易按从 Unix 纪元对齐、左闭右开的固定宽度时间区间
分桶，只返回非空区间，按 bucket_start 升序分页。
pair-stats 把匹配交易按 from_address 到 to_address 的原字符串有向
组合分组（自转账也累计一次），顺序为 total_amount 降序、total_count
降序、from_address 码点升序、to_address 码点升序。
address-flow-stats 拆分发送与接收资金流向：每笔匹配交易的 amount 分别
计入 from_address 的 sent_amount 与 to_address 的 received_amount，
自转账两方各计一次；net_amount = received_amount - sent_amount（可负），
顺序为 net_amount 降序、sent_amount 降序、received_amount 降序、
total_count 降序、address 码点升序。
address-time-stats 先按从 Unix 纪元对齐、左闭右开的固定宽度时间区间
分桶，再按参与地址在各区间内聚合（口径与 address-stats 相同），只
返回非空（区间, 地址）组，顺序为 bucket_start 升序、total_amount
降序、total_count 降序、send_count 降序、receive_count 降序、
address 码点升序。
time-bucket-aggregation 是与上述入口相互独立的时间分桶聚合：必填
左闭右开时间窗（UTC 秒）与 hour/day 桶粒度，可选 address/method；
按桶起点升序返回连续桶（含无交易的零计数桶），每桶含
total_count、success_count、failure_count，游标绑定完整查询条件与
桶粒度，错误使用独立的 InvalidAggregation* 异常类型。
分页：不透明 keyset 游标（见 :mod:`tx_indexer.cursor`），相同命令与
筛选下不跳过、不重复、不乱序；``total`` / ``total_groups`` 始终为
全部匹配数 / 全部分组数。
"""

import bisect

from .cursor import (
    decode_address_flow_stats_cursor,
    decode_address_stats_cursor,
    decode_address_time_stats_cursor,
    decode_counterparty_stats_cursor,
    decode_cursor,
    decode_method_stats_cursor,
    decode_pair_stats_cursor,
    decode_time_bucket_aggregation_cursor,
    decode_time_stats_cursor,
    encode_address_flow_stats_cursor,
    encode_address_stats_cursor,
    encode_address_time_stats_cursor,
    encode_counterparty_stats_cursor,
    encode_cursor,
    encode_method_stats_cursor,
    encode_pair_stats_cursor,
    encode_time_bucket_aggregation_cursor,
    encode_time_stats_cursor,
)
from .errors import (
    InvalidAggregationFilter,
    InvalidAggregationRange,
    InvalidAmountFilterError,
    InvalidAmountRangeError,
    InvalidBlockFilterError,
    InvalidBlockRangeError,
    InvalidBucketSizeError,
    InvalidFilterError,
    InvalidPageSizeError,
    InvalidStatusFilterError,
    InvalidTimeRangeError,
    UnsupportedAggregationBucket,
)
from .loader import _AMOUNT_RE

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 1000

#: 状态筛选：只匹配成功交易（记录 success 为 true 或缺省）
STATUS_SUCCESS = "success"
#: 状态筛选：只匹配失败交易（记录 success 为 false）
STATUS_FAILURE = "failure"
#: 状态筛选允许的取值（缺省 None 表示不按状态筛选）
_STATUS_VALUES = (STATUS_SUCCESS, STATUS_FAILURE)

#: 时间分桶聚合的小时桶粒度
AGGREGATION_GRANULARITY_HOUR = "hour"
#: 时间分桶聚合的自然日桶粒度
AGGREGATION_GRANULARITY_DAY = "day"
#: 各桶粒度对应的 UTC 秒桶宽（小时按整点、自然日按 UTC 日期切分）
_AGGREGATION_GRANULARITY_SECONDS = {
    AGGREGATION_GRANULARITY_HOUR: 3600,
    AGGREGATION_GRANULARITY_DAY: 86400,
}

_PUBLIC_FIELDS = (
    "tx_hash",
    "block_number",
    "timestamp",
    "from_address",
    "to_address",
    "method",
    "amount",
)


def _normalize_value_set(name, values):
    """把单个值或值迭代器归一化为去重后的 frozenset；空集合返回 None。"""
    if values is None:
        return None
    if isinstance(values, str):
        values = (values,)
    result = set()
    for value in values:
        if not isinstance(value, str) or value.strip() == "":
            raise InvalidFilterError(
                "%s 筛选值不能为空或仅含空白" % name, None
            )
        result.add(value)
    if not result:
        return None
    return frozenset(result)


def _normalize_amount_bound(name, value):
    """校验金额区间端点并规范化为无前导零的十进制整数字符串。

    金额筛选沿用交易 amount 格式：必须是非负十进制整数字符串，不接受
    非字符串、空字符串、正负号或小数点；前导零不改变数值含义。
    """
    if not isinstance(value, str) or _AMOUNT_RE.fullmatch(value) is None:
        raise InvalidAmountFilterError(
            "%s 必须为非负十进制整数字符串" % name, None
        )
    return str(int(value))


def _normalize_block_bound(name, value):
    """校验区块高度区间端点：必须为非负整数（布尔值等非整数值非法）。"""
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
    ):
        raise InvalidBlockFilterError(
            "%s 必须为非负整数区块高度" % name, None
        )
    return value


def normalize_filters(address=None, method=None, start_time=None, end_time=None,
                      from_address=None, to_address=None, min_amount=None,
                      max_amount=None, min_block=None, max_block=None,
                      status=None):
    """校验并归一化筛选条件。

    ``method`` / ``from_address`` / ``to_address`` 接受单个字符串或
    字符串迭代器，归一化为去重集合（frozenset）；不给或为空则为 None。
    ``min_amount`` / ``max_amount`` 接受非负十进制整数字符串，按数值
    比较、闭区间，不给为 None；非法值抛 InvalidAmountFilterError，
    最小值数值大于最大值抛 InvalidAmountRangeError。
    ``min_block`` / ``max_block`` 接受非负整数（不含布尔值），闭区间，
    不给为 None；非整数值、布尔值或负数抛 InvalidBlockFilterError，
    最小值大于最大值抛 InvalidBlockRangeError。
    ``status`` 只接受精确字符串 ``"success"`` / ``"failure"``，不给为
    None（不按状态筛选）；空字符串、纯空白、大小写变体、其他文本或
    Python 非字符串值抛 InvalidStatusFilterError。
    """
    if address is not None and (
        not isinstance(address, str) or address.strip() == ""
    ):
        raise InvalidFilterError(
            "address 筛选值不能为空或仅含空白", None
        )
    from_set = _normalize_value_set("from_address", from_address)
    to_set = _normalize_value_set("to_address", to_address)
    method_set = _normalize_value_set("method", method)
    if address is not None and (from_set is not None or to_set is not None):
        raise InvalidFilterError(
            "address 不能与 from_address / to_address 同时使用", None
        )
    # status 必须缺省或为精确的 success/failure：空白、大小写变体
    # （"Success"）、其他文本或 Python 非字符串（0/1/True/None 等）
    # 都在读取数据文件前报 invalid_status_filter
    if status is not None and (
        not isinstance(status, str) or status not in _STATUS_VALUES
    ):
        raise InvalidStatusFilterError(
            "status 筛选只能是 success 或 failure", None
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
    min_bound = (
        _normalize_amount_bound("min_amount", min_amount)
        if min_amount is not None else None
    )
    max_bound = (
        _normalize_amount_bound("max_amount", max_amount)
        if max_amount is not None else None
    )
    if min_bound is not None and max_bound is not None and (
        int(min_bound) > int(max_bound)
    ):
        raise InvalidAmountRangeError(
            "金额区间倒置：min_amount(%s) 大于 max_amount(%s)"
            % (min_bound, max_bound),
            None,
        )
    min_block_bound = (
        _normalize_block_bound("min_block", min_block)
        if min_block is not None else None
    )
    max_block_bound = (
        _normalize_block_bound("max_block", max_block)
        if max_block is not None else None
    )
    if min_block_bound is not None and max_block_bound is not None and (
        min_block_bound > max_block_bound
    ):
        raise InvalidBlockRangeError(
            "区块高度区间倒置：min_block(%d) 大于 max_block(%d)"
            % (min_block_bound, max_block_bound),
            None,
        )
    return {
        "address": address,
        "from_address": from_set,
        "to_address": to_set,
        "method": method_set,
        "start_time": start_time,
        "end_time": end_time,
        "min_amount": min_bound,
        "max_amount": max_bound,
        "min_block": min_block_bound,
        "max_block": max_block_bound,
        "status": status,
    }


def _matches(record, filters):
    if filters["address"] is not None:
        addr = filters["address"]
        if record["from_address"] != addr and record["to_address"] != addr:
            return False
    if (
        filters["from_address"] is not None
        and record["from_address"] not in filters["from_address"]
    ):
        return False
    if (
        filters["to_address"] is not None
        and record["to_address"] not in filters["to_address"]
    ):
        return False
    if (
        filters["method"] is not None
        and record["method"] not in filters["method"]
    ):
        return False
    ts = record["timestamp"]
    if filters["start_time"] is not None and ts < filters["start_time"]:
        return False
    if filters["end_time"] is not None and ts > filters["end_time"]:
        return False
    amount = int(record["amount"])
    if filters["min_amount"] is not None and amount < int(filters["min_amount"]):
        return False
    if filters["max_amount"] is not None and amount > int(filters["max_amount"]):
        return False
    block_number = record["block_number"]
    if filters["min_block"] is not None and block_number < filters["min_block"]:
        return False
    if filters["max_block"] is not None and block_number > filters["max_block"]:
        return False
    status = filters.get("status")
    if status is not None:
        # 与其余筛选取交集：success 只匹配 true（缺省亦为 true），
        # failure 只匹配 false
        record_success = record.get("success", True)
        if status == STATUS_SUCCESS and record_success is not True:
            return False
        if status == STATUS_FAILURE and record_success is not False:
            return False
    return True


def to_public(record):
    """按公开字段顺序输出一条交易。"""
    return {name: record[name] for name in _PUBLIC_FIELDS}


def validate_time_bucket_aggregation_params(start_time, end_time, bucket,
                                            address=None, method=None):
    """时间分桶聚合的入参校验（不依赖数据，可在读取数据文件前执行）。

    返回 ``(method_set, width)``：method_set 为去重后的 frozenset 或
    None，width 为桶宽（UTC 秒）。时间范围缺失/倒置抛
    InvalidAggregationRange，桶粒度非法抛 UnsupportedAggregationBucket，
    地址或方法不符公开语义抛 InvalidAggregationFilter。
    """
    # 1. 时间范围：必填、非负 UTC 秒整数、左闭右开且非空
    if (
        not isinstance(start_time, int)
        or isinstance(start_time, bool)
        or not isinstance(end_time, int)
        or isinstance(end_time, bool)
        or start_time < 0
        or end_time < 0
        or end_time <= start_time
    ):
        raise InvalidAggregationRange(
            "时间分桶聚合要求 start_time、end_time 为非负 UTC 秒整数，"
            "且 end_time 严格大于 start_time（左闭右开）",
            None,
        )

    # 2. 桶粒度：只支持小时与 UTC 自然日
    if not isinstance(bucket, str):
        raise UnsupportedAggregationBucket(
            "时间分桶聚合的桶粒度必须是 hour 或 day，收到：%r" % (bucket,),
            None,
        )
    width = _AGGREGATION_GRANULARITY_SECONDS.get(bucket)
    if width is None:
        raise UnsupportedAggregationBucket(
            "时间分桶聚合的桶粒度必须是 hour 或 day，收到：%r" % (bucket,),
            None,
        )

    # 3. 地址 / 方法：沿用现有公开筛选语义，错误归入聚合筛选错误
    if address is not None and (
        not isinstance(address, str) or address.strip() == ""
    ):
        raise InvalidAggregationFilter(
            "address 筛选值必须为非空字符串", None
        )
    try:
        method_set = _normalize_value_set("method", method)
    except (InvalidFilterError, TypeError) as exc:
        message = (
            exc.message if isinstance(exc, InvalidFilterError)
            else "method 筛选值必须为字符串或字符串集合"
        )
        raise InvalidAggregationFilter(message, None) from exc

    return method_set, width


class TxIndexer:
    """内存中的交易索引（无额外持久化）。"""

    def __init__(self, records):
        # 加载顺序保留；查询时统一排序，不修改入参列表语义
        self._records = list(records)

    def append_records(self, records):
        """追加已校验记录（增量导入用），保持追加顺序。

        追加后查询与聚合立即可见新记录；既有记录的顺序与内容不变，
        已签发的分页游标语义不受影响。
        """
        self._records.extend(records)

    def replace_records(self, remove_hashes, records):
        """删除指定 tx_hash 的记录后追加新记录（链重组替换用）。

        仅删除 ``remove_hashes`` 中的记录，其余记录（含更低高度前缀
        以及内容完全相同、被保留的旧记录）原样保留，再按给定顺序追加
        新记录。替换后查询与全部统计入口立即只观察替换后的数据，排序
        仍在查询时统一进行。
        """
        if remove_hashes:
            self._records = [
                r for r in self._records if r["tx_hash"] not in remove_hashes
            ]
        self._records.extend(records)

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

    def status_stats(self, filters):
        """按成功/失败状态聚合统计（忽略分页，复用与 query 相同的筛选）。

        返回 ``{total_count, success_count, failure_count,
        success_amount, failure_amount}``：金额均为十进制整数字符串，
        分别累计成功（success 为 true 或缺省）与失败（success 为 false）
        交易的金额，``total_count = success_count + failure_count``。
        无匹配时全部计数为 0、金额为 ``"0"``。当筛选本身指定
        ``status="success"`` / ``"failure"`` 时，另一状态的计数与金额
        恒为 0（命不中的状态不会出现在聚合中）。
        """
        success_count = 0
        failure_count = 0
        success_amount = 0
        failure_amount = 0
        for record in self._records:
            if not _matches(record, filters):
                continue
            if record.get("success", True):
                success_count += 1
                success_amount += int(record["amount"])
            else:
                failure_count += 1
                failure_amount += int(record["amount"])

        return {
            "total_count": success_count + failure_count,
            "success_count": success_count,
            "failure_count": failure_count,
            "success_amount": str(success_amount),
            "failure_amount": str(failure_amount),
        }

    def method_stats(self, filters, page_size=DEFAULT_PAGE_SIZE, cursor=None):
        """按 method 分组的分页统计。

        返回 {groups, total_groups, next_cursor}；每组含 method、
        total_count、total_amount、avg_amount（金额为十进制整数字符串，
        平均值向下取整）。顺序：total_amount 降序、total_count 降序、
        method 的 Unicode 码点升序。
        """
        self._validate_page_size(page_size)

        totals = {}
        counts = {}
        for record in self._records:
            if not _matches(record, filters):
                continue
            method = record["method"]
            totals[method] = totals.get(method, 0) + int(record["amount"])
            counts[method] = counts.get(method, 0) + 1

        # (-total, -count, method) 升序即 total/count 降序、method 升序，
        # 同时得到可直接 bisect 的单调递增键
        methods = sorted(
            totals, key=lambda m: (-totals[m], -counts[m], m)
        )
        total_groups = len(methods)

        start = 0
        if cursor is not None:
            (
                after_total,
                after_count,
                after_method,
            ) = decode_method_stats_cursor(cursor, filters)
            keys = [(-totals[m], -counts[m], m) for m in methods]
            marker = (-after_total, -after_count, after_method)
            # keyset 续页：排序键严格大于 marker 的第一个位置。
            # marker 位于两键之间也安全（bisect 取下一键），不会跳过或重复。
            start = bisect.bisect_left(keys, marker)
            if start < total_groups and keys[start] == marker:
                # marker 命中现存分组本身：从其后一组开始
                start += 1

        end = start + page_size
        page_methods = methods[start:end]
        groups = [
            {
                "method": method,
                "total_count": counts[method],
                "total_amount": str(totals[method]),
                "avg_amount": str(totals[method] // counts[method]),
            }
            for method in page_methods
        ]
        if end < total_groups:
            last = page_methods[-1]
            next_cursor = encode_method_stats_cursor(
                filters, totals[last], counts[last], last
            )
        else:
            next_cursor = None

        return {
            "groups": groups,
            "total_groups": total_groups,
            "next_cursor": next_cursor,
        }

    def address_stats(self, filters, page_size=DEFAULT_PAGE_SIZE, cursor=None):
        """按参与地址分组的分页统计。

        每条匹配交易以 from_address、to_address 的原字符串分别记一次
        发送、接收参与；自转账（from == to）的 total_count 只计一次，
        send_count / receive_count 各加一，金额只累计一次。

        返回 {groups, total_groups, next_cursor}；每组含 address、
        send_count、receive_count、total_count、total_amount、avg_amount
        （金额为十进制整数字符串，平均值向下取整）。顺序：total_amount
        降序、total_count 降序、send_count 降序、receive_count 降序、
        address 的 Unicode 码点升序。
        """
        self._validate_page_size(page_size)

        totals = {}
        send_counts = {}
        receive_counts = {}
        total_counts = {}
        for record in self._records:
            if not _matches(record, filters):
                continue
            value = int(record["amount"])
            frm = record["from_address"]
            to = record["to_address"]

            totals[frm] = totals.get(frm, 0) + value
            send_counts[frm] = send_counts.get(frm, 0) + 1
            total_counts[frm] = total_counts.get(frm, 0) + 1
            receive_counts.setdefault(frm, 0)

            if to == frm:
                # 自转账：total_count 不重复计、金额不重复累计，
                # 但发送、接收两个身份各加一
                receive_counts[to] += 1
            else:
                totals[to] = totals.get(to, 0) + value
                receive_counts[to] = receive_counts.get(to, 0) + 1
                total_counts[to] = total_counts.get(to, 0) + 1
                send_counts.setdefault(to, 0)

        # (-total, -total_count, -send, -receive, address) 升序即各数值
        # 降序、address 升序，同时得到可直接 bisect 的单调递增键
        addresses = sorted(
            totals,
            key=lambda a: (
                -totals[a],
                -total_counts[a],
                -send_counts[a],
                -receive_counts[a],
                a,
            ),
        )
        total_groups = len(addresses)

        start = 0
        if cursor is not None:
            (
                after_total,
                after_total_count,
                after_send,
                after_receive,
                after_address,
            ) = decode_address_stats_cursor(cursor, filters)
            keys = [
                (
                    -totals[a],
                    -total_counts[a],
                    -send_counts[a],
                    -receive_counts[a],
                    a,
                )
                for a in addresses
            ]
            marker = (
                -after_total,
                -after_total_count,
                -after_send,
                -after_receive,
                after_address,
            )
            # keyset 续页：排序键严格大于 marker 的第一个位置。
            # marker 位于两键之间也安全（bisect 取下一键），不会跳过或重复。
            start = bisect.bisect_left(keys, marker)
            if start < total_groups and keys[start] == marker:
                # marker 命中现存分组本身：从其后一组开始
                start += 1

        end = start + page_size
        page_addresses = addresses[start:end]
        groups = [
            {
                "address": address,
                "send_count": send_counts[address],
                "receive_count": receive_counts[address],
                "total_count": total_counts[address],
                "total_amount": str(totals[address]),
                "avg_amount": str(totals[address] // total_counts[address]),
            }
            for address in page_addresses
        ]
        if end < total_groups:
            last = page_addresses[-1]
            next_cursor = encode_address_stats_cursor(
                filters,
                totals[last],
                total_counts[last],
                send_counts[last],
                receive_counts[last],
                last,
            )
        else:
            next_cursor = None

        return {
            "groups": groups,
            "total_groups": total_groups,
            "next_cursor": next_cursor,
        }

    def address_flow_stats(self, filters, page_size=DEFAULT_PAGE_SIZE,
                           cursor=None):
        """按参与地址拆分发送/接收资金流向的分页统计。

        每条匹配交易的 amount 以十进制整数数值分别计入 from_address 的
        sent_amount 与 to_address 的 received_amount；自转账
        （from == to）两方各计一次（sent_amount 与 received_amount
        各加一次 amount），send_count / receive_count 各加一，
        total_count 只计一次。net_amount = received_amount -
        sent_amount，可为负。

        返回 {groups, total_groups, next_cursor}；每组含 address、
        sent_amount、received_amount、net_amount（均为十进制整数字符串，
        net_amount 可为带负号字符串，零为 ``"0"``）、send_count、
        receive_count、total_count。顺序：net_amount 降序、sent_amount
        降序、received_amount 降序、total_count 降序、address 的 Unicode
        码点升序。游标绑定本命令与等价筛选，不绑定 page_size。
        """
        self._validate_page_size(page_size)

        sent_totals = {}
        received_totals = {}
        send_counts = {}
        receive_counts = {}
        total_counts = {}
        for record in self._records:
            if not _matches(record, filters):
                continue
            value = int(record["amount"])
            frm = record["from_address"]
            to = record["to_address"]

            sent_totals[frm] = sent_totals.get(frm, 0) + value
            send_counts[frm] = send_counts.get(frm, 0) + 1
            total_counts[frm] = total_counts.get(frm, 0) + 1
            receive_counts.setdefault(frm, 0)
            received_totals.setdefault(frm, 0)

            if to == frm:
                # 自转账：发送与接收两方各计一次金额与身份计数，
                # total_count 只计一次
                received_totals[to] += value
                receive_counts[to] += 1
            else:
                received_totals[to] = received_totals.get(to, 0) + value
                receive_counts[to] = receive_counts.get(to, 0) + 1
                total_counts[to] = total_counts.get(to, 0) + 1
                send_counts.setdefault(to, 0)
                sent_totals.setdefault(to, 0)

        def _net(address):
            return received_totals[address] - sent_totals[address]

        # (-net, -sent, -received, -total_count, address) 升序即各数值
        # 降序、address 升序，同时得到可直接 bisect 的单调递增键
        addresses = sorted(
            sent_totals,
            key=lambda a: (
                -_net(a),
                -sent_totals[a],
                -received_totals[a],
                -total_counts[a],
                a,
            ),
        )
        total_groups = len(addresses)

        start = 0
        if cursor is not None:
            (
                after_net,
                after_sent,
                after_received,
                after_total_count,
                after_address,
            ) = decode_address_flow_stats_cursor(cursor, filters)
            keys = [
                (
                    -_net(a),
                    -sent_totals[a],
                    -received_totals[a],
                    -total_counts[a],
                    a,
                )
                for a in addresses
            ]
            marker = (
                -after_net,
                -after_sent,
                -after_received,
                -after_total_count,
                after_address,
            )
            # keyset 续页：排序键严格大于 marker 的第一个位置。
            # marker 位于两键之间也安全（bisect 取下一键），不会跳过或重复。
            start = bisect.bisect_left(keys, marker)
            if start < total_groups and keys[start] == marker:
                # marker 命中现存分组本身：从其后一组开始
                start += 1

        end = start + page_size
        page_addresses = addresses[start:end]
        groups = []
        for address in page_addresses:
            sent = sent_totals[address]
            received = received_totals[address]
            groups.append({
                "address": address,
                "sent_amount": str(sent),
                "received_amount": str(received),
                "net_amount": str(received - sent),
                "send_count": send_counts[address],
                "receive_count": receive_counts[address],
                "total_count": total_counts[address],
            })
        if end < total_groups:
            last = page_addresses[-1]
            next_cursor = encode_address_flow_stats_cursor(
                filters,
                received_totals[last] - sent_totals[last],
                sent_totals[last],
                received_totals[last],
                total_counts[last],
                last,
            )
        else:
            next_cursor = None

        return {
            "groups": groups,
            "total_groups": total_groups,
            "next_cursor": next_cursor,
        }

    def counterparty_stats(self, filters, page_size=DEFAULT_PAGE_SIZE,
                           cursor=None):
        """按交易对手分页汇总（必须给定 address 筛选作为观察地址）。

        只统计发送方或接收方为观察地址的匹配交易，且每笔交易只归一个
        对手：发送方为观察地址时归入 to_address，接收方为观察地址时归
        入 from_address；自转账以观察地址自身为对手，total_count 只计
        一次、send_count / receive_count 各加一、金额只累计一次。
        各分组的 send_count / receive_count / total_count / 金额口径与
        address-stats 相同（从对手视角计发送/接收）。

        返回 {address, groups, total_groups, next_cursor}；每组含
        counterparty、send_count、receive_count、total_count、
        total_amount、avg_amount（金额为十进制整数字符串，平均值向下
        取整）。顺序：total_amount 降序、total_count 降序、send_count
        降序、receive_count 降序、counterparty 的 Unicode 码点升序。
        """
        self._validate_page_size(page_size)
        address = filters["address"]
        if address is None:
            raise InvalidFilterError(
                "counterparty-stats 必须给定 address 筛选", None
            )

        totals = {}
        send_counts = {}
        receive_counts = {}
        total_counts = {}
        for record in self._records:
            if not _matches(record, filters):
                continue
            value = int(record["amount"])
            frm = record["from_address"]
            to = record["to_address"]

            if frm == to:
                # 自转账：对手即观察地址本身，两个身份各加一，
                # total_count 与金额只计一次
                counterparty = frm
                send_counts[counterparty] = (
                    send_counts.get(counterparty, 0) + 1
                )
                receive_counts[counterparty] = (
                    receive_counts.get(counterparty, 0) + 1
                )
            elif frm == address:
                # 观察地址发出：对手为接收方，记一次接收
                counterparty = to
                send_counts.setdefault(counterparty, 0)
                receive_counts[counterparty] = (
                    receive_counts.get(counterparty, 0) + 1
                )
            else:
                # 观察地址接收：对手为发送方，记一次发送
                counterparty = frm
                send_counts[counterparty] = (
                    send_counts.get(counterparty, 0) + 1
                )
                receive_counts.setdefault(counterparty, 0)

            totals[counterparty] = totals.get(counterparty, 0) + value
            total_counts[counterparty] = total_counts.get(counterparty, 0) + 1

        # (-total, -total_count, -send, -receive, counterparty) 升序即各
        # 数值降序、counterparty 升序，同时得到可直接 bisect 的单调递增键
        counterparties = sorted(
            totals,
            key=lambda c: (
                -totals[c],
                -total_counts[c],
                -send_counts[c],
                -receive_counts[c],
                c,
            ),
        )
        total_groups = len(counterparties)

        start = 0
        if cursor is not None:
            (
                after_total,
                after_total_count,
                after_send,
                after_receive,
                after_counterparty,
            ) = decode_counterparty_stats_cursor(cursor, filters)
            keys = [
                (
                    -totals[c],
                    -total_counts[c],
                    -send_counts[c],
                    -receive_counts[c],
                    c,
                )
                for c in counterparties
            ]
            marker = (
                -after_total,
                -after_total_count,
                -after_send,
                -after_receive,
                after_counterparty,
            )
            # keyset 续页：排序键严格大于 marker 的第一个位置。
            # marker 位于两键之间也安全（bisect 取下一键），不会跳过或重复。
            start = bisect.bisect_left(keys, marker)
            if start < total_groups and keys[start] == marker:
                # marker 命中现存分组本身：从其后一组开始
                start += 1

        end = start + page_size
        page_counterparties = counterparties[start:end]
        groups = [
            {
                "counterparty": counterparty,
                "send_count": send_counts[counterparty],
                "receive_count": receive_counts[counterparty],
                "total_count": total_counts[counterparty],
                "total_amount": str(totals[counterparty]),
                "avg_amount": str(
                    totals[counterparty] // total_counts[counterparty]
                ),
            }
            for counterparty in page_counterparties
        ]
        if end < total_groups:
            last = page_counterparties[-1]
            next_cursor = encode_counterparty_stats_cursor(
                filters,
                totals[last],
                total_counts[last],
                send_counts[last],
                receive_counts[last],
                last,
            )
        else:
            next_cursor = None

        return {
            "address": address,
            "groups": groups,
            "total_groups": total_groups,
            "next_cursor": next_cursor,
        }

    @staticmethod
    def _validate_bucket_size(bucket_size):
        if (
            not isinstance(bucket_size, int)
            or isinstance(bucket_size, bool)
            or bucket_size < 1
        ):
            raise InvalidBucketSizeError(
                "bucket_size 必须为大于 0 的整数（秒）", None
            )

    def time_stats(self, filters, bucket_size, page_size=DEFAULT_PAGE_SIZE,
                   cursor=None):
        """按固定宽度时间区间分桶的分页统计。

        区间从 Unix 纪元对齐、左闭右开：``bucket_start = (timestamp //
        bucket_size) * bucket_size``，``bucket_end_exclusive =
        bucket_start + bucket_size``；每笔匹配交易恰好进入一个区间。
        只返回有交易的区间，按 bucket_start 升序分页。

        返回 {groups, total_groups, next_cursor}；每组含 bucket_start、
        bucket_end_exclusive（均为 UTC 秒整数）、total_count、
        total_amount、avg_amount（金额为十进制整数字符串，平均值向下
        取整）。游标绑定本命令、等价筛选与 bucket_size，不绑定
        page_size。
        """
        self._validate_page_size(page_size)
        self._validate_bucket_size(bucket_size)

        totals = {}
        counts = {}
        for record in self._records:
            if not _matches(record, filters):
                continue
            bucket_start = (record["timestamp"] // bucket_size) * bucket_size
            totals[bucket_start] = (
                totals.get(bucket_start, 0) + int(record["amount"])
            )
            counts[bucket_start] = counts.get(bucket_start, 0) + 1

        starts = sorted(totals)
        total_groups = len(starts)

        start = 0
        if cursor is not None:
            after_bucket_start = decode_time_stats_cursor(
                cursor, filters, bucket_size
            )
            # keyset 续页：bucket_start 严格大于 marker 的第一个区间。
            # marker 位于两键之间也安全，不会跳过或重复。
            start = bisect.bisect_right(starts, after_bucket_start)

        end = start + page_size
        page_starts = starts[start:end]
        groups = [
            {
                "bucket_start": bucket_start,
                "bucket_end_exclusive": bucket_start + bucket_size,
                "total_count": counts[bucket_start],
                "total_amount": str(totals[bucket_start]),
                "avg_amount": str(totals[bucket_start] // counts[bucket_start]),
            }
            for bucket_start in page_starts
        ]
        if end < total_groups:
            next_cursor = encode_time_stats_cursor(
                filters, bucket_size, page_starts[-1]
            )
        else:
            next_cursor = None

        return {
            "groups": groups,
            "total_groups": total_groups,
            "next_cursor": next_cursor,
        }

    def pair_stats(self, filters, page_size=DEFAULT_PAGE_SIZE, cursor=None):
        """按有向交易对（from_address → to_address）分组的分页统计。

        每条匹配交易按 from_address 到 to_address 的原字符串有向组合
        归入一组，自转账也累计一次。

        返回 {groups, total_groups, next_cursor}；每组含 from_address、
        to_address、total_count、total_amount、avg_amount（金额为十进制
        整数字符串，平均值向下取整）。顺序：total_amount 降序、
        total_count 降序、from_address 的 Unicode 码点升序、
        to_address 的 Unicode 码点升序。游标绑定本命令与等价筛选，
        不绑定 page_size。
        """
        self._validate_page_size(page_size)

        totals = {}
        counts = {}
        for record in self._records:
            if not _matches(record, filters):
                continue
            key = (record["from_address"], record["to_address"])
            totals[key] = totals.get(key, 0) + int(record["amount"])
            counts[key] = counts.get(key, 0) + 1

        # (-total, -count, from, to) 升序即 total/count 降序、from/to
        # 码点升序，同时得到可直接 bisect 的单调递增键
        pairs = sorted(
            totals, key=lambda p: (-totals[p], -counts[p], p[0], p[1])
        )
        total_groups = len(pairs)

        start = 0
        if cursor is not None:
            (
                after_total,
                after_count,
                after_from,
                after_to,
            ) = decode_pair_stats_cursor(cursor, filters)
            keys = [(-totals[p], -counts[p], p[0], p[1]) for p in pairs]
            marker = (-after_total, -after_count, after_from, after_to)
            # keyset 续页：排序键严格大于 marker 的第一个位置。
            # marker 位于两键之间也安全（bisect 取下一键），不会跳过或重复。
            start = bisect.bisect_left(keys, marker)
            if start < total_groups and keys[start] == marker:
                # marker 命中现存分组本身：从其后一组开始
                start += 1

        end = start + page_size
        page_pairs = pairs[start:end]
        groups = [
            {
                "from_address": frm,
                "to_address": to,
                "total_count": counts[(frm, to)],
                "total_amount": str(totals[(frm, to)]),
                "avg_amount": str(totals[(frm, to)] // counts[(frm, to)]),
            }
            for frm, to in page_pairs
        ]
        if end < total_groups:
            last_from, last_to = page_pairs[-1]
            next_cursor = encode_pair_stats_cursor(
                filters,
                totals[page_pairs[-1]],
                counts[page_pairs[-1]],
                last_from,
                last_to,
            )
        else:
            next_cursor = None

        return {
            "groups": groups,
            "total_groups": total_groups,
            "next_cursor": next_cursor,
        }

    def address_time_stats(self, filters, bucket_size,
                           page_size=DEFAULT_PAGE_SIZE, cursor=None):
        """按时间区间 × 参与地址分组的分页统计。

        区间从 Unix 纪元对齐、左闭右开：``bucket_start = (timestamp //
        bucket_size) * bucket_size``，``bucket_end_exclusive =
        bucket_start + bucket_size``；每笔匹配交易恰好进入一个区间。
        区间内按 from_address、to_address 的原字符串分别记一次发送、
        接收参与（口径与 :meth:`address_stats` 相同）：from/to 不同时
        发送方组 send_count 加一、接收方组 receive_count 加一，两组
        total_count 各加一、金额各累计一次；自转账只进一个组，
        send_count / receive_count / total_count 各加一，金额累计一次。
        只返回非空（区间, 地址）组。

        返回 {groups, total_groups, next_cursor}；每组含 address、
        bucket_start、bucket_end_exclusive、send_count、receive_count、
        total_count、total_amount、avg_amount（金额为十进制整数字符串，
        平均值向下取整）。顺序：bucket_start 升序、total_amount 降序、
        total_count 降序、send_count 降序、receive_count 降序、
        address 的 Unicode 码点升序。游标绑定本命令、等价筛选与
        bucket_size，不绑定 page_size。
        """
        self._validate_page_size(page_size)
        self._validate_bucket_size(bucket_size)

        # key = (bucket_start, address)
        totals = {}
        send_counts = {}
        receive_counts = {}
        total_counts = {}
        for record in self._records:
            if not _matches(record, filters):
                continue
            value = int(record["amount"])
            bucket_start = (record["timestamp"] // bucket_size) * bucket_size
            frm = record["from_address"]
            to = record["to_address"]
            sender_key = (bucket_start, frm)
            totals[sender_key] = totals.get(sender_key, 0) + value
            send_counts[sender_key] = (
                send_counts.get(sender_key, 0) + 1
            )
            total_counts[sender_key] = (
                total_counts.get(sender_key, 0) + 1
            )
            receive_counts.setdefault(sender_key, 0)

            if to == frm:
                # 自转账：只进一个组，total_count 与金额不重复累计，
                # 但发送、接收两个身份各加一
                receive_counts[sender_key] += 1
            else:
                receiver_key = (bucket_start, to)
                totals[receiver_key] = totals.get(receiver_key, 0) + value
                receive_counts[receiver_key] = (
                    receive_counts.get(receiver_key, 0) + 1
                )
                total_counts[receiver_key] = (
                    total_counts.get(receiver_key, 0) + 1
                )
                send_counts.setdefault(receiver_key, 0)

        # (bucket_start, -total, -total_count, -send, -receive, address)
        # 升序即 bucket_start 升序，其后各数值降序、address 升序，
        # 同时得到可直接 bisect 的单调递增键
        group_keys = sorted(
            totals,
            key=lambda k: (
                k[0],
                -totals[k],
                -total_counts[k],
                -send_counts[k],
                -receive_counts[k],
                k[1],
            ),
        )
        total_groups = len(group_keys)

        start = 0
        if cursor is not None:
            (
                after_bucket_start,
                after_total,
                after_total_count,
                after_send,
                after_receive,
                after_address,
            ) = decode_address_time_stats_cursor(cursor, filters, bucket_size)
            keys = [
                (
                    k[0],
                    -totals[k],
                    -total_counts[k],
                    -send_counts[k],
                    -receive_counts[k],
                    k[1],
                )
                for k in group_keys
            ]
            marker = (
                after_bucket_start,
                -after_total,
                -after_total_count,
                -after_send,
                -after_receive,
                after_address,
            )
            # keyset 续页：排序键严格大于 marker 的第一个位置。
            # marker 位于两键之间也安全（bisect 取下一键），不会跳过或重复。
            start = bisect.bisect_left(keys, marker)
            if start < total_groups and keys[start] == marker:
                # marker 命中现存分组本身：从其后一组开始
                start += 1

        end = start + page_size
        page_keys = group_keys[start:end]
        groups = []
        for bucket_start, address in page_keys:
            total_amount = totals[(bucket_start, address)]
            total_count = total_counts[(bucket_start, address)]
            groups.append({
                "address": address,
                "bucket_start": bucket_start,
                "bucket_end_exclusive": bucket_start + bucket_size,
                "send_count": send_counts[(bucket_start, address)],
                "receive_count": receive_counts[(bucket_start, address)],
                "total_count": total_count,
                "total_amount": str(total_amount),
                "avg_amount": str(total_amount // total_count),
            })
        if end < total_groups:
            last_key = page_keys[-1]
            last_bucket_start, last_address = last_key
            next_cursor = encode_address_time_stats_cursor(
                filters,
                bucket_size,
                last_bucket_start,
                totals[last_key],
                total_counts[last_key],
                send_counts[last_key],
                receive_counts[last_key],
                last_address,
            )
        else:
            next_cursor = None

        return {
            "groups": groups,
            "total_groups": total_groups,
            "next_cursor": next_cursor,
        }

    def time_bucket_aggregation(self, start_time, end_time, bucket=None,
                                address=None, method=None,
                                page_size=DEFAULT_PAGE_SIZE, cursor=None,
                                granularity=None):
        """按小时或自然日的时间分桶聚合（连续桶、含空桶、游标分页）。

        与既有查询/统计相互独立的新入口：接收可选 ``address``（精确匹配
        发送方或接收方）、可选 ``method``（单字符串或字符串集合，任一
        命中）、必填的 ``start_time`` / ``end_time``（UTC 秒，左闭右开：
        含开始时间、不含结束时间）以及必填的 ``bucket``（``"hour"`` 按
        整点、``"day"`` 按 UTC 自然日切分；同义词关键字参数
        ``granularity`` 等价）。

        返回 ``{buckets, total_buckets, next_cursor}``：``buckets`` 按
        桶起点升序返回查询范围内的**连续**桶，每桶含 ``bucket_start``、
        ``total_count``、``success_count``、``failure_count``；范围内没有
        交易的桶同样返回三个计数均为 0，不静默跳过。首桶按纪元整点/整日
        对齐（可早于 start_time），末桶只覆盖 end_time 之前的数据。
        记录的 ``success`` 为 False 计失败、其余（含未携带该字段的 JSONL
        记录）计成功。

        确定性错误（均在返回任何分页数据之前抛出）：

        - 起止时间缺失、类型非法、为负或 ``end_time <= start_time`` →
          InvalidAggregationRange
        - 桶粒度不是 hour/day → UnsupportedAggregationBucket
        - address/method 不符现有公开语义（空白等）→
          InvalidAggregationFilter
        - 游标格式错误、被篡改、与当前查询条件或桶粒度不一致 →
          InvalidAggregationCursor

        游标绑定完整查询条件（address、method、start_time、end_time）与
        桶粒度，不绑定 page_size；相同条件下翻页不重复、不遗漏。末页之后
        再翻页返回空 ``buckets`` 与 ``next_cursor=None``。
        """
        if granularity is not None:
            if bucket is not None:
                raise TypeError(
                    "bucket 与 granularity 是同义参数，不能同时指定"
                )
            bucket = granularity
        method_set, width = validate_time_bucket_aggregation_params(
            start_time, end_time, bucket, address=address, method=method
        )
        self._validate_page_size(page_size)

        agg_filters = {
            "address": address,
            "method": method_set,
            "start_time": start_time,
            "end_time": end_time,
        }

        # 连续桶边界：首桶按纪元整点/整日对齐（可早于 start_time），
        # 末桶为覆盖 end_time - 1 的那个桶
        first_start = (start_time // width) * width
        last_start = ((end_time - 1) // width) * width
        total_buckets = (last_start - first_start) // width + 1

        # 只记录非空桶的计数，连续序列在取页时零填充
        stats = {}
        for record in self._records:
            ts = record["timestamp"]
            if ts < start_time or ts >= end_time:
                continue
            if address is not None and (
                record["from_address"] != address
                and record["to_address"] != address
            ):
                continue
            if method_set is not None and record["method"] not in method_set:
                continue
            bucket_start = (ts // width) * width
            slot = stats.get(bucket_start)
            if slot is None:
                slot = [0, 0, 0]
                stats[bucket_start] = slot
            slot[0] += 1
            if record.get("success", True):
                slot[1] += 1
            else:
                slot[2] += 1

        # 4. 游标解码（绑定完整查询条件与桶粒度）：keyset 续页，
        #    marker 为上一页最后一桶起点，严格从下一桶开始
        if cursor is not None:
            after_bucket_start = decode_time_bucket_aggregation_cursor(
                cursor, agg_filters, bucket
            )
            start_index = (
                after_bucket_start - first_start
            ) // width + 1
        else:
            start_index = 0

        end_index = start_index + page_size
        page_end = min(end_index, total_buckets)
        buckets = []
        for index in range(start_index, page_end):
            bucket_start = first_start + index * width
            total_count, success_count, failure_count = stats.get(
                bucket_start, (0, 0, 0)
            )
            buckets.append({
                "bucket_start": bucket_start,
                "total_count": total_count,
                "success_count": success_count,
                "failure_count": failure_count,
            })

        if end_index < total_buckets:
            marker_start = first_start + (page_end - 1) * width
            next_cursor = encode_time_bucket_aggregation_cursor(
                agg_filters, bucket, marker_start
            )
        else:
            next_cursor = None

        return {
            "buckets": buckets,
            "total_buckets": total_buckets,
            "next_cursor": next_cursor,
        }
