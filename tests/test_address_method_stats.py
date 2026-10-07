"""address-method-stats：地址 × method 交叉分页统计测试。"""

import io
import json
import os
import sys
import tempfile
import unittest

from tx_indexer.cli import main
from tx_indexer.engine import TxIndexer, normalize_filters
from tx_indexer.errors import (
    InvalidCursorError,
    InvalidPageSizeError,
    InvalidStatusFilterError,
)


def rec(tx_hash, block_number, timestamp, frm, to, method, amount,
        success="__absent__"):
    record = {
        "tx_hash": tx_hash,
        "block_number": block_number,
        "timestamp": timestamp,
        "from_address": frm,
        "to_address": to,
        "method": method,
        "amount": amount,
    }
    # 缺省 success 时不写入该字段，语义为成功
    if success != "__absent__":
        record["success"] = success
    return record


class AddressMethodStatsTest(unittest.TestCase):
    def setUp(self):
        self.records = [
            # transfer：alice→bob 10（成功，缺省）
            rec("h1", 1, 10, "alice", "bob", "transfer", "10"),
            # approve：bob→alice 21（成功）
            rec("h2", 2, 20, "bob", "alice", "approve", "21",
                success=True),
            # transfer：alice→carol 5（失败）
            rec("h3", 2, 30, "alice", "carol", "transfer", "5",
                success=False),
            # transfer：bob→carol 7（失败）
            rec("h4", 3, 40, "bob", "carol", "transfer", "7",
                success=False),
        ]
        self.idx = TxIndexer(self.records)

    def _groups_by_key(self, result):
        return {(g["address"], g["method"]): g for g in result["groups"]}

    def test_aggregation_and_field_shape(self):
        result = self.idx.address_method_stats(normalize_filters())
        self.assertEqual(result["total_groups"], 5)
        self.assertIsNone(result["next_cursor"])
        groups = self._groups_by_key(result)

        # alice × transfer：发送 2 笔（10 成功 + 5 失败）= 15
        self.assertEqual(groups[("alice", "transfer")], {
            "address": "alice",
            "method": "transfer",
            "send_count": 2,
            "receive_count": 0,
            "total_count": 2,
            "total_amount": "15",
            "avg_amount": "7",
            "success_count": 1,
            "failure_count": 1,
        })
        # bob × transfer：发送 h4(7 失败)、接收 h1(10 成功)，2 笔 = 17
        self.assertEqual(groups[("bob", "transfer")], {
            "address": "bob",
            "method": "transfer",
            "send_count": 1,
            "receive_count": 1,
            "total_count": 2,
            "total_amount": "17",
            "avg_amount": "8",
            "success_count": 1,
            "failure_count": 1,
        })
        # carol × transfer：只接收，5 失败 + 7 失败 = 12
        self.assertEqual(groups[("carol", "transfer")], {
            "address": "carol",
            "method": "transfer",
            "send_count": 0,
            "receive_count": 2,
            "total_count": 2,
            "total_amount": "12",
            "avg_amount": "6",
            "success_count": 0,
            "failure_count": 2,
        })
        # approve 两个方向各成一组
        self.assertEqual(groups[("bob", "approve")]["send_count"], 1)
        self.assertEqual(groups[("bob", "approve")]["receive_count"], 0)
        self.assertEqual(groups[("alice", "approve")]["send_count"], 0)
        self.assertEqual(groups[("alice", "approve")]["receive_count"], 1)
        for key in (("bob", "approve"), ("alice", "approve")):
            g = groups[key]
            self.assertEqual(g["total_count"], 1)
            self.assertEqual(g["total_amount"], "21")
            self.assertEqual(g["avg_amount"], "21")
            self.assertEqual(g["success_count"], 1)
            self.assertEqual(g["failure_count"], 0)

    def test_sender_and_receiver_each_counted_once_by_method(self):
        # 同一笔交易在发送方组与接收方组都各计一次，但 method 必须一致；
        # 地址相同但 method 不同是两个独立分组
        result = self.idx.address_method_stats(normalize_filters())
        groups = self._groups_by_key(result)
        self.assertIn(("alice", "transfer"), groups)
        self.assertIn(("alice", "approve"), groups)
        # alice 的 transfer 接收数为 0（她在 transfer 上只发不收），
        # approve 接收数为 1（bob→alice）
        self.assertEqual(groups[("alice", "transfer")]["receive_count"], 0)
        self.assertEqual(groups[("alice", "approve")]["receive_count"], 1)

    def test_self_transfer_total_and_amount_once(self):
        records = [
            rec("s1", 1, 1, "eva", "eva", "transfer", "100",
                success=False),
            rec("s2", 2, 2, "eva", "eva", "transfer", "50",
                success=True),
        ]
        result = TxIndexer(records).address_method_stats(
            normalize_filters()
        )
        self.assertEqual(result["total_groups"], 1)
        g = result["groups"][0]
        self.assertEqual(g["address"], "eva")
        self.assertEqual(g["method"], "transfer")
        self.assertEqual(g["send_count"], 2)
        self.assertEqual(g["receive_count"], 2)
        # 自转账 total_count / total_amount 只计一次
        self.assertEqual(g["total_count"], 2)
        self.assertEqual(g["total_amount"], "150")
        self.assertEqual(g["avg_amount"], "75")
        self.assertEqual(g["success_count"], 1)
        self.assertEqual(g["failure_count"], 1)

    def test_self_transfer_single_tx(self):
        records = [rec("s1", 1, 1, "eva", "eva", "m", "100")]
        g = TxIndexer(records).address_method_stats(
            normalize_filters()
        )["groups"][0]
        self.assertEqual(g["send_count"], 1)
        self.assertEqual(g["receive_count"], 1)
        self.assertEqual(g["total_count"], 1)
        self.assertEqual(g["total_amount"], "100")
        self.assertEqual(g["success_count"], 1)
        self.assertEqual(g["failure_count"], 0)

    def test_amounts_no_leading_zero_and_floor_avg(self):
        records = [
            rec("a1", 1, 1, "x", "y", "m", "010", success=True),
            rec("a2", 2, 2, "x", "y", "m", "1", success=False),
        ]  # x × m 发送 2 笔合计 11 -> 11 // 2 = 5（向下取整）
        result = TxIndexer(records).address_method_stats(
            normalize_filters()
        )
        groups = self._groups_by_key(result)
        g = groups[("x", "m")]
        self.assertEqual(g["total_amount"], "11")
        self.assertEqual(g["avg_amount"], "5")
        for group in result["groups"]:
            self.assertIsInstance(group["total_amount"], str)
            self.assertIsInstance(group["avg_amount"], str)
            self.assertEqual(
                group["total_amount"], str(int(group["total_amount"]))
            )

    def test_count_invariant_success_plus_failure(self):
        for g in self.idx.address_method_stats(
            normalize_filters()
        )["groups"]:
            self.assertEqual(
                g["total_count"],
                g["success_count"] + g["failure_count"],
            )

    def test_ordering_tie_breakers(self):
        records = [
            # aa/bb × m：total=10,count=2,send=1,receive=1 全相同
            rec("aa1", 1, 1, "aa", "z", "m", "8", success=True),
            rec("aa2", 2, 2, "z", "aa", "m", "2", success=True),
            rec("bb1", 3, 3, "bb", "z", "m", "8", success=True),
            rec("bb2", 4, 4, "z", "bb", "m", "2", success=False),
            # cc × mm / nn：两键数值完全相同（5+0,count2,send1,recv1）
            rec("mm1", 5, 5, "cc", "z", "mm", "5", success=True),
            rec("mm2", 6, 6, "z", "cc", "mm", "0", success=False),
            rec("nn1", 7, 7, "cc", "z", "nn", "5", success=True),
            rec("nn2", 8, 8, "z", "cc", "nn", "0", success=False),
            # zz→y 与 y×m：total=20,count=1；zz send=1、y receive=1
            rec("zz1", 9, 9, "zz", "y", "m", "20", success=False),
        ]
        # 另有 (z,m) 组：收 8+8、发 2+2，total=20,count=4
        result = TxIndexer(records).address_method_stats(
            normalize_filters()
        )
        keys = [(g["address"], g["method"]) for g in result["groups"]]
        # total=20：(z,m) count4 居首；zz(send1) 先于 y(recv1)；
        # total=10：aa 先于 bb（address 码点）；
        # total=5：cc 先于 z（address），同地址 mm 先于 nn（method）
        self.assertEqual(keys, [
            ("z", "m"),
            ("zz", "m"),
            ("y", "m"),
            ("aa", "m"),
            ("bb", "m"),
            ("cc", "mm"),
            ("cc", "nn"),
            ("z", "mm"),
            ("z", "nn"),
        ])

    def test_send_count_tie_breaker(self):
        records = [
            # pp：send=2 receive=0；qq：send=1 receive=1；
            # total=10,total_count=2 相同 -> send 降序 pp 在前
            rec("p1", 1, 1, "pp", "z", "m", "9", success=True),
            rec("p2", 2, 2, "pp", "z", "m", "1", success=False),
            rec("q1", 3, 3, "qq", "z", "m", "9", success=True),
            rec("q2", 4, 4, "z", "qq", "m", "1", success=False),
        ]
        keys = [
            (g["address"], g["method"])
            for g in TxIndexer(records).address_method_stats(
                normalize_filters()
            )["groups"]
            if g["address"] in ("pp", "qq")
        ]
        self.assertEqual(keys, [("pp", "m"), ("qq", "m")])

    def test_empty_result(self):
        result = self.idx.address_method_stats(
            normalize_filters(method="missing")
        )
        self.assertEqual(
            result,
            {"groups": [], "total_groups": 0, "next_cursor": None},
        )

    def test_status_success_zeroes_failure_side(self):
        result = self.idx.address_method_stats(
            normalize_filters(status="success")
        )
        for g in result["groups"]:
            self.assertEqual(g["failure_count"], 0)
            self.assertEqual(g["total_count"], g["success_count"])
        keys = {(g["address"], g["method"]) for g in result["groups"]}
        # h1(alice→bob transfer)、h2(bob→alice approve) 成功
        self.assertEqual(keys, {
            ("alice", "transfer"),
            ("bob", "transfer"),
            ("bob", "approve"),
            ("alice", "approve"),
        })

    def test_status_failure_zeroes_success_side(self):
        result = self.idx.address_method_stats(
            normalize_filters(status="failure")
        )
        for g in result["groups"]:
            self.assertEqual(g["success_count"], 0)
            self.assertEqual(g["total_count"], g["failure_count"])
        groups = self._groups_by_key(result)
        # 失败：h3 alice→carol 5、h4 bob→carol 7
        self.assertEqual(groups[("alice", "transfer")]["total_amount"], "5")
        self.assertEqual(groups[("carol", "transfer")]["total_amount"], "12")
        self.assertNotIn(("bob", "approve"), groups)

    def test_invalid_status_filter(self):
        with self.assertRaises(InvalidStatusFilterError):
            normalize_filters(status="Success")

    def test_filters_intersect(self):
        # alice 参与 + method=transfer + 金额 [5,10]：
        # h1(alice→bob 10 成功)、h3(alice→carol 5 失败)
        result = self.idx.address_method_stats(
            normalize_filters(address="alice", method="transfer",
                              min_amount="5", max_amount="10")
        )
        self.assertEqual(result["total_groups"], 3)
        groups = self._groups_by_key(result)
        g = groups[("alice", "transfer")]
        self.assertEqual(g["total_count"], 2)
        self.assertEqual(g["total_amount"], "15")
        self.assertEqual(g["success_count"], 1)
        self.assertEqual(g["failure_count"], 1)
        # 对手组同样按筛选后的交易聚合
        self.assertEqual(groups[("bob", "transfer")]["total_amount"], "10")
        self.assertEqual(groups[("carol", "transfer")]["total_amount"], "5")

    def test_block_filter(self):
        result = self.idx.address_method_stats(
            normalize_filters(min_block=3)
        )
        # 仅 h4：bob→carol transfer 7
        self.assertEqual(result["total_groups"], 2)
        groups = self._groups_by_key(result)
        self.assertEqual(groups[("bob", "transfer")]["total_amount"], "7")
        self.assertEqual(groups[("carol", "transfer")]["total_amount"], "7")

    def test_from_to_filter(self):
        result = self.idx.address_method_stats(
            normalize_filters(from_address="alice")
        )
        # 只有 alice 发出的 h1/h3：alice、bob、carol 三组
        self.assertEqual(result["total_groups"], 3)
        groups = self._groups_by_key(result)
        self.assertEqual(groups[("alice", "transfer")]["send_count"], 2)

    def test_address_conflicts_with_from_to(self):
        from tx_indexer.errors import InvalidFilterError
        with self.assertRaises(InvalidFilterError):
            normalize_filters(address="alice", from_address="bob")

    def test_pagination_no_skip_no_duplicate(self):
        filters = normalize_filters()
        collected = []
        cursor = None
        pages = 0
        while True:
            page = self.idx.address_method_stats(
                filters, page_size=2, cursor=cursor
            )
            pages += 1
            self.assertEqual(page["total_groups"], 5)
            collected.extend(
                (g["address"], g["method"]) for g in page["groups"]
            )
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(pages, 3)
        # 与一次性返回的完整顺序一致
        full = [
            (g["address"], g["method"])
            for g in self.idx.address_method_stats(filters)["groups"]
        ]
        self.assertEqual(collected, full)
        self.assertEqual(len(collected), len(set(collected)))

    def test_last_page_has_null_cursor(self):
        filters = normalize_filters()
        cursor = None
        pages = 0
        while True:
            page = self.idx.address_method_stats(
                filters, page_size=2, cursor=cursor
            )
            pages += 1
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(pages, 3)

    def test_cursor_not_bound_to_page_size(self):
        filters = normalize_filters()
        page = self.idx.address_method_stats(filters, page_size=2)
        cursor = page["next_cursor"]
        resumed = self.idx.address_method_stats(
            filters, page_size=100, cursor=cursor
        )
        full = [
            (g["address"], g["method"])
            for g in self.idx.address_method_stats(filters)["groups"]
        ]
        self.assertEqual(
            [(g["address"], g["method"]) for g in resumed["groups"]],
            full[2:],
        )

    def test_cross_command_cursor_rejected(self):
        query_page = self.idx.query(normalize_filters(), page_size=1)
        with self.assertRaises(InvalidCursorError):
            self.idx.address_method_stats(
                normalize_filters(), cursor=query_page["next_cursor"]
            )
        for make in (
            lambda: self.idx.address_stats(
                normalize_filters(), page_size=1
            )["next_cursor"],
            lambda: self.idx.address_flow_stats(
                normalize_filters(), page_size=1
            )["next_cursor"],
            lambda: self.idx.method_status_stats(
                normalize_filters(), page_size=1
            )["next_cursor"],
        ):
            with self.assertRaises(InvalidCursorError):
                self.idx.address_method_stats(
                    normalize_filters(), cursor=make()
                )

    def test_address_method_cursor_rejected_by_other_commands(self):
        cursor = self.idx.address_method_stats(
            normalize_filters(), page_size=1
        )["next_cursor"]
        with self.assertRaises(InvalidCursorError):
            self.idx.address_stats(
                normalize_filters(), page_size=1, cursor=cursor
            )
        with self.assertRaises(InvalidCursorError):
            self.idx.address_flow_stats(
                normalize_filters(), page_size=1, cursor=cursor
            )
        with self.assertRaises(InvalidCursorError):
            self.idx.query(
                normalize_filters(), page_size=1, cursor=cursor
            )

    def test_cursor_bound_to_filters_and_status(self):
        cursor = self.idx.address_method_stats(
            normalize_filters(status="success"), page_size=1
        )["next_cursor"]
        with self.assertRaises(InvalidCursorError):
            self.idx.address_method_stats(
                normalize_filters(status="failure"), cursor=cursor
            )
        with self.assertRaises(InvalidCursorError):
            self.idx.address_method_stats(
                normalize_filters(), cursor=cursor
            )
        with self.assertRaises(InvalidCursorError):
            self.idx.address_method_stats(
                normalize_filters(status="success", method="approve"),
                cursor=cursor,
            )

    def test_cursor_amount_and_block_numeric_equivalence(self):
        page = self.idx.address_method_stats(
            normalize_filters(min_amount="05", min_block=1),
            page_size=1,
        )
        cursor = page["next_cursor"]
        # 仅前导零不同：等价筛选，可续翻
        resumed = self.idx.address_method_stats(
            normalize_filters(min_amount="5", min_block=1),
            page_size=2,
            cursor=cursor,
        )
        self.assertIn("groups", resumed)

    def test_tampered_or_undecodable_cursor(self):
        cursor = self.idx.address_method_stats(
            normalize_filters(), page_size=1
        )["next_cursor"]
        for bad in (cursor + "x", "!!!", "", "not-base64"):
            with self.assertRaises(InvalidCursorError):
                self.idx.address_method_stats(
                    normalize_filters(), cursor=bad
                )

    def test_invalid_page_size(self):
        for bad in (0, -1, 1001):
            with self.assertRaises(InvalidPageSizeError):
                self.idx.address_method_stats(
                    normalize_filters(), page_size=bad
                )
        with self.assertRaises(InvalidPageSizeError):
            self.idx.address_method_stats(
                normalize_filters(), page_size=True
            )


class AddressMethodStatsCliTest(unittest.TestCase):
    DATA_LINES = [
        {"tx_hash": "h1", "block_number": 1, "timestamp": 10,
         "from_address": "alice", "to_address": "bob",
         "method": "transfer", "amount": "10"},
        {"tx_hash": "h2", "block_number": 2, "timestamp": 20,
         "from_address": "bob", "to_address": "alice",
         "method": "approve", "amount": "21", "success": True},
        {"tx_hash": "h3", "block_number": 2, "timestamp": 30,
         "from_address": "alice", "to_address": "carol",
         "method": "transfer", "amount": "5", "success": False},
        {"tx_hash": "h4", "block_number": 3, "timestamp": 40,
         "from_address": "bob", "to_address": "carol",
         "method": "transfer", "amount": "7", "success": False},
    ]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "data.jsonl")
        with open(self.path, "w", encoding="utf-8") as fh:
            for obj in self.DATA_LINES:
                fh.write(json.dumps(obj) + "\n")
        self._stdout, self._stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()

    def tearDown(self):
        sys.stdout, sys.stderr = self._stdout, self._stderr
        self.tmp.cleanup()

    def _run(self, argv):
        sys.stdout.seek(0)
        sys.stdout.truncate(0)
        sys.stderr.seek(0)
        sys.stderr.truncate(0)
        code = main(argv)
        out = sys.stdout.getvalue()
        err = sys.stderr.getvalue()
        return (
            code,
            json.loads(out) if out.strip() else None,
            json.loads(err.strip().splitlines()[-1]) if err.strip() else None,
        )

    def test_cli_basic(self):
        code, result, err = self._run(
            ["address-method-stats", self.path]
        )
        self.assertEqual(code, 0)
        self.assertIsNone(err)
        self.assertEqual(result["total_groups"], 5)
        # total_amount 降序：approve=21 的两组在前
        self.assertEqual(result["groups"][0]["total_amount"], "21")

    def test_cli_pagination_and_cross_command(self):
        code, page1, err = self._run(
            ["address-method-stats", self.path, "--page-size", "2"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(page1["groups"]), 2)
        cursor = page1["next_cursor"]

        # 游标不绑定 page_size：改用 3 续翻，剩余 3 组恰好末页
        code, page2, err = self._run(
            ["address-method-stats", self.path, "--page-size", "3",
             "--cursor", cursor]
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(page2["groups"]), 3)
        self.assertEqual(page2["total_groups"], 5)
        self.assertIsNone(page2["next_cursor"])

        # 跨命令复用
        code, result, err = self._run(
            ["address-stats", self.path, "--page-size", "1",
             "--cursor", cursor]
        )
        self.assertEqual(code, 2)
        self.assertEqual(err["error"], "invalid_cursor")

    def test_cli_status_filter(self):
        code, result, err = self._run(
            ["address-method-stats", self.path, "--status", "failure"]
        )
        self.assertEqual(code, 0)
        for g in result["groups"]:
            self.assertEqual(g["success_count"], 0)
            self.assertEqual(g["failure_count"], g["total_count"])

    def test_cli_invalid_filter_codes(self):
        for argv, error in (
            (["address-method-stats", self.path, "--status", "nope"],
             "invalid_status_filter"),
            (["address-method-stats", self.path, "--min-amount", "-1"],
             "invalid_amount_filter"),
            (["address-method-stats", self.path,
              "--min-amount", "9", "--max-amount", "1"],
             "invalid_amount_range"),
            (["address-method-stats", self.path, "--min-block", "x"],
             "invalid_block_filter"),
            (["address-method-stats", self.path,
              "--min-block", "9", "--max-block", "1"],
             "invalid_block_range"),
            (["address-method-stats", self.path,
              "--start-time", "9", "--end-time", "1"],
             "invalid_time_range"),
            (["address-method-stats", self.path, "--address", "  "],
             "invalid_filter"),
            (["address-method-stats", self.path, "--page-size", "0"],
             "invalid_page_size"),
            (["address-method-stats", self.path, "--page-size", "1001"],
             "invalid_page_size"),
        ):
            code, result, err = self._run(argv)
            self.assertEqual(code, 2, argv)
            self.assertIsNone(result)
            self.assertEqual(err["error"], error, argv)
            self.assertIsNone(err["input_line"])

    def test_cli_invalid_data_carry_line(self):
        bad_path = os.path.join(self.tmp.name, "bad.jsonl")
        with open(bad_path, "w", encoding="utf-8") as fh:
            fh.write("\n")
        code, result, err = self._run(
            ["address-method-stats", bad_path]
        )
        self.assertEqual(code, 2)
        self.assertEqual(err["error"], "invalid_transaction")
        self.assertEqual(err["input_line"], 1)

    def test_cli_bad_success_field(self):
        bad_path = os.path.join(self.tmp.name, "bad.jsonl")
        with open(bad_path, "w", encoding="utf-8") as fh:
            fh.write(
                '{"tx_hash":"x","block_number":1,"timestamp":1,'
                '"from_address":"a","to_address":"b","method":"m",'
                '"amount":"1","success":1}\n'
            )
        code, result, err = self._run(
            ["address-method-stats", bad_path]
        )
        self.assertEqual(code, 2)
        self.assertEqual(err["error"], "invalid_transaction")
        self.assertEqual(err["input_line"], 1)

    def test_cli_bad_amount(self):
        bad_path = os.path.join(self.tmp.name, "bad.jsonl")
        with open(bad_path, "w", encoding="utf-8") as fh:
            fh.write(
                '{"tx_hash":"x","block_number":1,"timestamp":1,'
                '"from_address":"a","to_address":"b","method":"m",'
                '"amount":1}\n'
            )
        code, result, err = self._run(
            ["address-method-stats", bad_path]
        )
        self.assertEqual(code, 2)
        self.assertEqual(err["error"], "invalid_transaction")
        self.assertEqual(err["input_line"], 1)

    def test_cli_duplicate_hash(self):
        bad_path = os.path.join(self.tmp.name, "dup.jsonl")
        with open(bad_path, "w", encoding="utf-8") as fh:
            line = (
                '{"tx_hash":"x","block_number":1,"timestamp":1,'
                '"from_address":"a","to_address":"b","method":"m",'
                '"amount":"1"}\n'
            )
            fh.write(line)
            fh.write(line)
        code, result, err = self._run(
            ["address-method-stats", bad_path]
        )
        self.assertEqual(code, 2)
        self.assertEqual(err["error"], "duplicate_transaction")
        self.assertEqual(err["input_line"], 2)

    def test_cli_missing_file_is_io_error(self):
        # 文件打不开不属于领域错误：stderr 为纯文本、退出码 2
        missing = os.path.join(self.tmp.name, "missing.jsonl")
        sys.stdout.seek(0)
        sys.stdout.truncate(0)
        sys.stderr.seek(0)
        sys.stderr.truncate(0)
        code = main(["address-method-stats", missing])
        self.assertEqual(code, 2)
        self.assertEqual(sys.stdout.getvalue(), "")
        self.assertIn("无法读取数据文件", sys.stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
