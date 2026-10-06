"""命令行入口：``tx-indexer query`` / ``stats`` / ``method-stats`` /
``address-stats`` / ``address-flow-stats`` / ``counterparty-stats`` /
``time-stats`` / ``pair-stats`` / ``address-time-stats`` /
``time-bucket-aggregation``。

用法：
    tx-indexer query <data.jsonl> [筛选与分页选项]
    tx-indexer stats <data.jsonl> [筛选选项]
    tx-indexer status-stats <data.jsonl> [筛选选项]
    tx-indexer method-stats <data.jsonl> [筛选与分页选项]
    tx-indexer address-stats <data.jsonl> [筛选与分页选项]
    tx-indexer address-flow-stats <data.jsonl> [筛选与分页选项]
    tx-indexer counterparty-stats <data.jsonl> --address ADDR [筛选与分页选项]
    tx-indexer time-stats <data.jsonl> --bucket-size SECONDS [筛选与分页选项]
    tx-indexer pair-stats <data.jsonl> [筛选与分页选项]
    tx-indexer address-time-stats <data.jsonl> --bucket-size SECONDS [筛选与分页选项]
    tx-indexer time-bucket-aggregation <data.jsonl> --start-time TS --end-time TS --bucket hour|day [选项]

领域错误（invalid_transaction / duplicate_transaction / invalid_time_range /
invalid_page_size / invalid_cursor / invalid_filter / invalid_bucket_size /
invalid_amount_filter / invalid_amount_range / invalid_block_filter /
invalid_block_range / invalid_status_filter / invalid_aggregation_range /
unsupported_aggregation_bucket / invalid_aggregation_filter /
invalid_aggregation_cursor）
以 JSON 对象输出到 stderr，退出码 2：

    {"error": "...", "message": "...", "input_line": 12}
"""

import argparse
import json
import sys

from .engine import (
    DEFAULT_PAGE_SIZE,
    TxIndexer,
    normalize_filters,
    validate_time_bucket_aggregation_params,
)
from .errors import (
    InvalidAggregationFilter,
    InvalidAggregationRange,
    InvalidBlockFilterError,
    InvalidBucketSizeError,
    InvalidFilterError,
    InvalidPageSizeError,
    TxIndexerError,
    UnsupportedAggregationBucket,
)
from .loader import _AMOUNT_RE, load_file


def _add_filter_args(parser):
    parser.add_argument(
        "--address",
        help="精确匹配发送方或接收方地址（不可与 --from-address/--to-address 并用）",
    )
    parser.add_argument(
        "--from-address",
        action="append",
        help="精确匹配发送方地址，可重复（集合内任一命中）",
    )
    parser.add_argument(
        "--to-address",
        action="append",
        help="精确匹配接收方地址，可重复（集合内任一命中）",
    )
    parser.add_argument(
        "--method",
        action="append",
        help="精确匹配 method，可重复（集合内任一命中）",
    )
    parser.add_argument("--start-time", help="时间窗起点（UTC 秒，含）")
    parser.add_argument("--end-time", help="时间窗终点（UTC 秒，含）")
    parser.add_argument(
        "--min-amount",
        help="金额区间下界（非负十进制整数，含端点，按数值比较）",
    )
    parser.add_argument(
        "--max-amount",
        help="金额区间上界（非负十进制整数，含端点，按数值比较）",
    )
    parser.add_argument(
        "--min-block",
        help="区块高度下界（非负十进制整数，含端点）",
    )
    parser.add_argument(
        "--max-block",
        help="区块高度上界（非负十进制整数，含端点）",
    )
    parser.add_argument(
        "--status",
        help="按成功状态筛选：success（成功）或 failure（失败），缺省不筛",
    )


def _parse_time(value, flag, parser):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parser.error("%s 必须为非负 UTC 秒整数" % flag)
    if parsed < 0:
        parser.error("%s 必须为非负 UTC 秒整数" % flag)
    return parsed


def _parse_page_size(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise InvalidPageSizeError(
            "page_size 必须为 1 到 1000 之间的整数（默认 100）", None
        )
    # 范围校验交给引擎统一抛出 InvalidPageSizeError
    return parsed


def _parse_bucket_size(value):
    """解析 --bucket-size；缺失、非整数或不大于 0 都报 invalid_bucket_size。"""
    if value is None:
        raise InvalidBucketSizeError(
            "必须指定 --bucket-size（秒）", None
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise InvalidBucketSizeError(
            "bucket_size 必须为大于 0 的整数（秒）", None
        )
    if parsed < 1:
        raise InvalidBucketSizeError(
            "bucket_size 必须为大于 0 的整数（秒）", None
        )
    return parsed


def _parse_block_bound(value, flag):
    """解析 --min-block / --max-block：空值、空白、非十进制文本、正负号
    或小数点都在读取数据文件前报 invalid_block_filter；前导零合法。"""
    if not isinstance(value, str) or _AMOUNT_RE.fullmatch(value) is None:
        raise InvalidBlockFilterError(
            "%s 必须为非负十进制整数区块高度" % flag, None
        )
    return int(value)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="tx-indexer",
        description="链上交易索引与查询引擎：读取 JSON Lines 数据。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    query_parser = subparsers.add_parser(
        "query", help="分页查询交易（返回 transactions/total/next_cursor）"
    )
    query_parser.add_argument("file", help="JSON Lines 数据文件路径")
    _add_filter_args(query_parser)
    query_parser.add_argument(
        "--page-size",
        default=str(DEFAULT_PAGE_SIZE),
        help="每页条数，1 到 1000，默认 100",
    )
    query_parser.add_argument("--cursor", help="上一页返回的 next_cursor")

    stats_parser = subparsers.add_parser(
        "stats", help="聚合统计（返回 total_count/total_amount/min/max/avg）"
    )
    stats_parser.add_argument("file", help="JSON Lines 数据文件路径")
    _add_filter_args(stats_parser)

    status_stats_parser = subparsers.add_parser(
        "status-stats",
        help="按成功/失败状态聚合统计"
             "（返回 total/success/failure 的计数与金额）",
    )
    status_stats_parser.add_argument("file", help="JSON Lines 数据文件路径")
    _add_filter_args(status_stats_parser)

    method_stats_parser = subparsers.add_parser(
        "method-stats",
        help="按 method 分页汇总（返回 groups/total_groups/next_cursor）",
    )
    method_stats_parser.add_argument("file", help="JSON Lines 数据文件路径")
    _add_filter_args(method_stats_parser)
    method_stats_parser.add_argument(
        "--page-size",
        default=str(DEFAULT_PAGE_SIZE),
        help="每页分组数，1 到 1000，默认 100",
    )
    method_stats_parser.add_argument(
        "--cursor", help="上一页返回的 next_cursor"
    )

    address_stats_parser = subparsers.add_parser(
        "address-stats",
        help="按参与地址分页汇总（返回 groups/total_groups/next_cursor）",
    )
    address_stats_parser.add_argument("file", help="JSON Lines 数据文件路径")
    _add_filter_args(address_stats_parser)
    address_stats_parser.add_argument(
        "--page-size",
        default=str(DEFAULT_PAGE_SIZE),
        help="每页分组数，1 到 1000，默认 100",
    )
    address_stats_parser.add_argument(
        "--cursor", help="上一页返回的 next_cursor"
    )

    address_flow_stats_parser = subparsers.add_parser(
        "address-flow-stats",
        help="按地址拆分发送/接收资金流向分页汇总"
             "（返回 groups/total_groups/next_cursor）",
    )
    address_flow_stats_parser.add_argument(
        "file", help="JSON Lines 数据文件路径"
    )
    _add_filter_args(address_flow_stats_parser)
    address_flow_stats_parser.add_argument(
        "--page-size",
        default=str(DEFAULT_PAGE_SIZE),
        help="每页分组数，1 到 1000，默认 100",
    )
    address_flow_stats_parser.add_argument(
        "--cursor", help="上一页返回的 next_cursor"
    )

    counterparty_stats_parser = subparsers.add_parser(
        "counterparty-stats",
        help="按交易对手分页汇总（返回 address/groups/total_groups/next_cursor）",
    )
    counterparty_stats_parser.add_argument("file", help="JSON Lines 数据文件路径")
    _add_filter_args(counterparty_stats_parser)
    counterparty_stats_parser.add_argument(
        "--page-size",
        default=str(DEFAULT_PAGE_SIZE),
        help="每页分组数，1 到 1000，默认 100",
    )
    counterparty_stats_parser.add_argument(
        "--cursor", help="上一页返回的 next_cursor"
    )

    time_stats_parser = subparsers.add_parser(
        "time-stats",
        help="按固定宽度时间区间分页汇总（返回 groups/total_groups/next_cursor）",
    )
    time_stats_parser.add_argument("file", help="JSON Lines 数据文件路径")
    _add_filter_args(time_stats_parser)
    time_stats_parser.add_argument(
        "--bucket-size",
        help="区间宽度（秒），大于 0 的整数；区间从 Unix 纪元对齐、左闭右开",
    )
    time_stats_parser.add_argument(
        "--page-size",
        default=str(DEFAULT_PAGE_SIZE),
        help="每页区间数，1 到 1000，默认 100",
    )
    time_stats_parser.add_argument(
        "--cursor", help="上一页返回的 next_cursor"
    )

    pair_stats_parser = subparsers.add_parser(
        "pair-stats",
        help="按有向交易对分页汇总（返回 groups/total_groups/next_cursor）",
    )
    pair_stats_parser.add_argument("file", help="JSON Lines 数据文件路径")
    _add_filter_args(pair_stats_parser)
    pair_stats_parser.add_argument(
        "--page-size",
        default=str(DEFAULT_PAGE_SIZE),
        help="每页分组数，1 到 1000，默认 100",
    )
    pair_stats_parser.add_argument(
        "--cursor", help="上一页返回的 next_cursor"
    )

    address_time_stats_parser = subparsers.add_parser(
        "address-time-stats",
        help="按时间区间 × 参与地址分页汇总"
             "（返回 groups/total_groups/next_cursor）",
    )
    address_time_stats_parser.add_argument(
        "file", help="JSON Lines 数据文件路径"
    )
    _add_filter_args(address_time_stats_parser)
    address_time_stats_parser.add_argument(
        "--bucket-size",
        help="区间宽度（秒），大于 0 的整数；区间从 Unix 纪元对齐、左闭右开",
    )
    address_time_stats_parser.add_argument(
        "--page-size",
        default=str(DEFAULT_PAGE_SIZE),
        help="每页分组数，1 到 1000，默认 100",
    )
    address_time_stats_parser.add_argument(
        "--cursor", help="上一页返回的 next_cursor"
    )

    time_bucket_parser = subparsers.add_parser(
        "time-bucket-aggregation",
        help="按小时/自然日连续分桶聚合"
             "（返回 buckets/total_buckets/next_cursor，含空桶）",
    )
    time_bucket_parser.add_argument(
        "file", help="JSON Lines 数据文件路径"
    )
    time_bucket_parser.add_argument(
        "--address",
        help="精确匹配发送方或接收方地址（可选）",
    )
    time_bucket_parser.add_argument(
        "--method",
        action="append",
        help="精确匹配 method，可重复（集合内任一命中，可选）",
    )
    time_bucket_parser.add_argument(
        "--start-time",
        help="时间窗起点（UTC 秒，含）；必填，非负整数",
    )
    time_bucket_parser.add_argument(
        "--end-time",
        help="时间窗终点（UTC 秒，不含）；必填，非负整数且晚于起点",
    )
    time_bucket_parser.add_argument(
        "--bucket",
        help="桶粒度：hour（整点小时）或 day（UTC 自然日）；必填",
    )
    time_bucket_parser.add_argument(
        "--page-size",
        default=str(DEFAULT_PAGE_SIZE),
        help="每页桶数，1 到 1000，默认 100",
    )
    time_bucket_parser.add_argument(
        "--cursor", help="上一页返回的 next_cursor"
    )

    return parser


def _filters_from_args(args, parser):
    start_time = (
        _parse_time(args.start_time, "--start-time", parser)
        if args.start_time is not None
        else None
    )
    end_time = (
        _parse_time(args.end_time, "--end-time", parser)
        if args.end_time is not None
        else None
    )
    # 倒置校验集中在 normalize_filters，抛 InvalidTimeRangeError；
    # 空白值与 address/from/to 冲突也在此抛 InvalidFilterError；
    # 金额格式非法抛 InvalidAmountFilterError、区间倒置抛
    # InvalidAmountRangeError；区块边界非十进制文本抛
    # InvalidBlockFilterError、区间倒置抛 InvalidBlockRangeError，
    # 均发生在读取数据文件之前
    min_block = (
        _parse_block_bound(args.min_block, "--min-block")
        if args.min_block is not None
        else None
    )
    max_block = (
        _parse_block_bound(args.max_block, "--max-block")
        if args.max_block is not None
        else None
    )
    return normalize_filters(
        address=args.address,
        method=args.method,
        start_time=start_time,
        end_time=end_time,
        from_address=args.from_address,
        to_address=args.to_address,
        min_amount=args.min_amount,
        max_amount=args.max_amount,
        min_block=min_block,
        max_block=max_block,
        status=args.status,
    )


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "time-bucket-aggregation":
            # 独立的时间分桶聚合入口：左闭右开、连续桶、含空桶，不与
            # 既有命令共用筛选解析（既有时间窗为左闭右闭）。范围、桶粒度
            # 与筛选值的校验全部在读取数据文件前完成。
            if args.start_time is None or args.end_time is None:
                raise InvalidAggregationRange(
                    "time-bucket-aggregation 必须指定 --start-time 与"
                    " --end-time（UTC 秒，左闭右开）",
                    None,
                )
            try:
                agg_start = int(args.start_time)
                agg_end = int(args.end_time)
            except (TypeError, ValueError):
                raise InvalidAggregationRange(
                    "--start-time 与 --end-time 必须为非负 UTC 秒整数",
                    None,
                )
            if args.bucket is None:
                raise UnsupportedAggregationBucket(
                    "time-bucket-aggregation 必须指定 --bucket hour|day",
                    None,
                )
            agg_method = tuple(args.method) if args.method else None
            # 范围倒置、非法桶粒度、空白筛选均在此抛出对应聚合错误
            _, _ = validate_time_bucket_aggregation_params(
                agg_start,
                agg_end,
                args.bucket,
                address=args.address,
                method=agg_method,
            )
            page_size = _parse_page_size(args.page_size)

            records = load_file(args.file)
            indexer = TxIndexer(records)
            result = indexer.time_bucket_aggregation(
                agg_start,
                agg_end,
                args.bucket,
                address=args.address,
                method=agg_method,
                page_size=page_size,
                cursor=args.cursor,
            )
        else:
            filters = _filters_from_args(args, parser)
            page_size = None
            if args.command in (
                "query",
                "method-stats",
                "address-stats",
                "address-flow-stats",
                "counterparty-stats",
                "time-stats",
                "pair-stats",
                "address-time-stats",
            ):
                page_size = _parse_page_size(args.page_size)
            bucket_size = None
            if args.command in ("time-stats", "address-time-stats"):
                # 缺失、非整数或不大于 0 都在读取数据文件前报 invalid_bucket_size
                bucket_size = _parse_bucket_size(args.bucket_size)
            if args.command == "counterparty-stats" and filters["address"] is None:
                # 缺少 --address 与空白值、address/from/to 冲突一样，
                # 都在读取数据文件前报 invalid_filter
                raise InvalidFilterError(
                    "counterparty-stats 必须指定 --address", None
                )

            records = load_file(args.file)
            indexer = TxIndexer(records)

            if args.command == "query":
                result = indexer.query(
                    filters, page_size=page_size, cursor=args.cursor
                )
            elif args.command == "method-stats":
                result = indexer.method_stats(
                    filters, page_size=page_size, cursor=args.cursor
                )
            elif args.command == "address-stats":
                result = indexer.address_stats(
                    filters, page_size=page_size, cursor=args.cursor
                )
            elif args.command == "address-flow-stats":
                result = indexer.address_flow_stats(
                    filters, page_size=page_size, cursor=args.cursor
                )
            elif args.command == "counterparty-stats":
                result = indexer.counterparty_stats(
                    filters, page_size=page_size, cursor=args.cursor
                )
            elif args.command == "time-stats":
                result = indexer.time_stats(
                    filters, bucket_size, page_size=page_size, cursor=args.cursor
                )
            elif args.command == "pair-stats":
                result = indexer.pair_stats(
                    filters, page_size=page_size, cursor=args.cursor
                )
            elif args.command == "address-time-stats":
                result = indexer.address_time_stats(
                    filters, bucket_size, page_size=page_size, cursor=args.cursor
                )
            elif args.command == "status-stats":
                # 复用与 query 相同的筛选（含 --status），但不使用游标分页
                result = indexer.status_stats(filters)
            else:
                result = indexer.stats(filters)
    except TxIndexerError as exc:
        json.dump(exc.to_dict(), sys.stderr, ensure_ascii=False)
        sys.stderr.write("\n")
        return 2
    except OSError as exc:
        # 文件读取失败不属于五类领域错误，按普通 I/O 错误处理
        sys.stderr.write("无法读取数据文件 %s：%s\n" % (args.file, exc))
        return 2

    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
