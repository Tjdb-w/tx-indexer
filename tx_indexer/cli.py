"""命令行入口：``tx-indexer query`` / ``stats`` / ``method-stats`` /
``address-stats`` / ``counterparty-stats``。

用法：
    tx-indexer query <data.jsonl> [筛选与分页选项]
    tx-indexer stats <data.jsonl> [筛选选项]
    tx-indexer method-stats <data.jsonl> [筛选与分页选项]
    tx-indexer address-stats <data.jsonl> [筛选与分页选项]
    tx-indexer counterparty-stats <data.jsonl> --address ADDR [筛选与分页选项]

领域错误（invalid_transaction / duplicate_transaction / invalid_time_range /
invalid_page_size / invalid_cursor / invalid_filter）以 JSON 对象输出到
stderr，退出码 2：

    {"error": "...", "message": "...", "input_line": 12}
"""

import argparse
import json
import sys

from .engine import DEFAULT_PAGE_SIZE, TxIndexer, normalize_filters
from .errors import InvalidFilterError, InvalidPageSizeError, TxIndexerError
from .loader import load_file


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

    counterparty_stats_parser = subparsers.add_parser(
        "counterparty-stats",
        help="从观察地址按交易对手分页汇总"
        "（返回 address/groups/total_groups/next_cursor）",
    )
    counterparty_stats_parser.add_argument(
        "file", help="JSON Lines 数据文件路径"
    )
    _add_filter_args(counterparty_stats_parser)
    counterparty_stats_parser.add_argument(
        "--page-size",
        default=str(DEFAULT_PAGE_SIZE),
        help="每页分组数，1 到 1000，默认 100",
    )
    counterparty_stats_parser.add_argument(
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
    # 空白值与 address/from/to 冲突也在此抛 InvalidFilterError，
    # 均发生在读取数据文件之前
    return normalize_filters(
        address=args.address,
        method=args.method,
        start_time=start_time,
        end_time=end_time,
        from_address=args.from_address,
        to_address=args.to_address,
    )


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        filters = _filters_from_args(args, parser)
        page_size = None
        if args.command in (
            "query",
            "method-stats",
            "address-stats",
            "counterparty-stats",
        ):
            page_size = _parse_page_size(args.page_size)
        # counterparty-stats 必须显式指定观察地址；在读取数据文件前报错
        if args.command == "counterparty-stats" and filters["address"] is None:
            raise InvalidFilterError(
                "counterparty-stats 必须通过 --address 指定观察地址", None
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
        elif args.command == "counterparty-stats":
            result = indexer.counterparty_stats(
                filters, page_size=page_size, cursor=args.cursor
            )
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
