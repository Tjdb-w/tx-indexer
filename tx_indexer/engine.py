"""查询、游标分页与聚合统计引擎。

筛选条件（不同条件之间取交集，均可省略）：
- ``address``：精确匹配 from_address 或 to_address（不可与
  ``from_address`` / ``to_address`` 并用）
- ``from_address`` / ``to_address`` / ``method``：值集合，集合内部
  任一命中即可；可重复给定，重复值等同一个条件
- ``start_time`` / ``end_time``：时间窗，左闭右闭（UTC 秒）

query 排序：block_number 升序，同高度按 tx_hash 升序。
method-stats 排序：total_amount 降序、total_count 降序、method 码点升序。
address-stats 排序：total_amount 降序、total_count 降序、send_count 降序、
receive_count 降序、address 码点升序。
counterparty-stats 排序键与 address-stats 相同，末级改为 counterparty
码点升序；每笔匹配交易只归入一个对手（发送方为观察地址时归
to_address，接收方为观察地址时归 from_address，自转账归观察地址本身）。
分页：不透明 keyset 游标（见 :mod:`tx_indexer.cursor`），相同命令与
筛选下不跳过、不重复、不乱序；``total`` / ``total_groups`` 始终为
全部匹配数 / 全部分组数。
"""

import bisect

from .cursor import (
    decode_address_stats_cursor,
    decode_counterparty_stats_cursor,
    decode_cursor,
    decode_method_stats_cursor,
    encode_address_stats_cursor,
    encode_counterparty_stats_cursor,
    encode_cursor,
    encode_method_stats_cursor,
)
from .errors import (
    InvalidFilterError,
    InvalidPageSizeError,
    InvalidTimeRangeError,
)

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


def normalize_filters(address=None, method=None, start_time=None, end_time=None,
                      from_address=None, to_address=None):
    """校验并归一化筛选条件。

    ``method`` / ``from_address`` / ``to_address`` 接受单个字符串或
    字符串迭代器，归一化为去重集合（frozenset）；不给或为空则为 None。
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
        "from_address": from_set,
        "to_address": to_set,
        "method": method_set,
        "start_time": start_time,
        "end_time": end_time,
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
