"""address-method-stats（地址 × method 交叉统计）测试。"""

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
    InvalidFilterError,
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
            rec("a", 1, 10, "alice", "bob", "transfer", "100"),
            rec("b", 2, 20, "bob", "alice", "transfer", "30",
                success=False),
            rec("c", 3, 30, "carol", "dave", "approve", "200"),
            rec("d", 4, 40, "alice", "carol", "mint", "15"),
        ]
        self.idx = TxIndexer(self.records)

    def _groups_by_key(self, **kwargs):
        result = self.idx.address_method_stats(normalize_filters(**kwargs))
        return {(g["address"], g["method"]): g for g in result["groups"]}

    def test_sender_and_receiver_counted_per_method(self):
        groups = self._groups_by_key()
        # alice 作为 transfer 发送方（a）与接收方（b）
        at = groups[("alice", "transfer")]
        self.assertEqual(
            (at["send_count"], at["receive_count"], at["total_count"]),
            (1, 1, 2),
        )
        self.assertEqual(at["total_amount"], "130")
        self.assertEqual(at["avg_amount"], "65")
        self.assertEqual((at["success_count"], at["failure_count"]), (1, 1))
        # bob 的 transfer：发出 b（失败）、接收 a（成功）
        bt = groups[("bob", "transfer")]
        self.assertEqual(
            (bt["send_count"], bt["receive_count"], bt["total_count"]),
            (1, 1, 2),
        )
        self.assertEqual(bt["total_amount"], "130")
        self.assertEqual((bt["success_count"], bt["failure_count"]), (1, 1))
        # carol：approve 发送、mint 接收，按 method 分成两组
        ca = groups[("carol", "approve")]
        self.assertEqual(
            (ca["send_count"], ca["receive_count"], ca["total_count"]),
            (1, 0, 1),
        )
        cm = groups[("carol", "mint")]
        self.assertEqual(
            (cm["send_count"], cm["receive_count"], cm["total_count"]),
            (0, 1, 1),
        )
        # dave 只作为 approve 接收方
        da = groups[("dave", "approve")]
        self.assertEqual(
            (da["send_count"], da["receive_count"], da["total_count"]),
            (0, 1, 1),
        )
        self.assertEqual(da["success_count"], 1)
        self.assertEqual(da["failure_count"], 0)
        # alice 的 mint 发送是独立组，不与 transfer 合并
        am = groups[("alice", "mint")]
        self.assertEqual(
            (am["send_count"], am["receive_count"], am["total_count"]),
            (1, 0, 1),
        )
        self.assertEqual(am["total_amount"], "15")
        self.assertEqual(len(groups), 6)

    def test_field_shape_and_order(self):
        group = self.idx.address_method_stats(normalize_filters())["groups"][0]
        self.assertEqual(
            list(group.keys()),
            [
                "address", "method", "send_count", "receive_count",
                "total_count", "total_amount", "success_count",
                "failure_count", "avg_amount",
            ],
        )
        self.assertIsInstance(group["total_amount"], str)
        self.assertIsInstance(group["avg_amount"], str)

    def test_success_plus_failure_equals_total(self):
        for group in self.idx.address_method_stats(normalize_filters())["groups"]:
            self.assertEqual(
                group["total_count"],
                group["success_count"] + group["failure_count"],
            )

    def test_avg_floors(self):
        records = [
            rec("h1", 1, 10, "a", "b", "m", "10"),
            rec("h2", 2, 20, "b", "a", "m", "11"),
            rec("h3", 3, 30, "c", "a", "m", "0"),
        ]
        groups = {
            (g["address"], g["method"]): g
            for g in TxIndexer(records).address_method_stats(
                normalize_filters()
            )["groups"]
        }
        # a：收 10、收 0、发 11 → total_amount 21, total_count 3 → avg 7
        self.assertEqual(groups[("a", "m")]["total_amount"], "21")
        self.assertEqual(groups[("a", "m")]["total_count"], 3)
        self.assertEqual(groups[("a", "m")]["avg_amount"], "7")

    def test_no_leading_zeros_in_amounts(self):
        for group in self.idx.address_method_stats(normalize_filters())["groups"]:
            for key in ("total_amount", "avg_amount"):
                value = group[key]
                self.assertIsInstance(value, str)
                self.assertNotRegex(value, r"^0[0-9]")

    def test_self_transfer_total_and_amount_once(self):
        records = [
            rec("s1", 1, 1, "eva", "eva", "m", "100"),
            rec("s2", 2, 2, "eva", "eva", "m", "50", success=False),
            rec("o1", 3, 3, "fin", "gus", "m", "7"),
        ]
        groups = {
            (g["address"], g["method"]): g
            for g in TxIndexer(records).address_method_stats(
                normalize_filters()
            )["groups"]
        }
        eva = groups[("eva", "m")]
        # 自转账：total/金额各一次，send/receive 各一，成功失败各一次
        self.assertEqual(eva["total_amount"], "150")
        self.assertEqual(eva["total_count"], 2)
        self.assertEqual(eva["send_count"], 2)
        self.assertEqual(eva["receive_count"], 2)
        self.assertEqual(eva["success_count"], 1)
        self.assertEqual(eva["failure_count"], 1)
        self.assertEqual(eva["avg_amount"], "75")

    def test_self_transfer_mixed_with_normal_same_method(self):
        records = [
            rec("s1", 1, 1, "a", "a", "m", "10"),
            rec("o1", 2, 2, "a", "b", "m", "5"),
            rec("i1", 3, 3, "b", "a", "m", "7"),
        ]
        groups = {
            (g["address"], g["method"]): g
            for g in TxIndexer(records).address_method_stats(
                normalize_filters()
            )["groups"]
        }
        a = groups[("a", "m")]
        # 自转账 10 一次 + 发出 5 + 接收 7
        self.assertEqual(a["total_amount"], "22")
        self.assertEqual(a["total_count"], 3)
        self.assertEqual((a["send_count"], a["receive_count"]), (2, 2))
        b = groups[("b", "m")]
        self.assertEqual(b["total_amount"], "12")
        self.assertEqual(b["total_count"], 2)
        self.assertEqual((b["send_count"], b["receive_count"]), (1, 1))

    def test_missing_success_counts_as_success(self):
        records = [
            # 回放标准化记录：没有 success 字段
            {
                "tx_hash": "h1", "block_number": 1, "timestamp": 1,
                "from_address": "a", "to_address": "b", "method": "m",
                "amount": "3",
            },
        ]
        groups = TxIndexer(records).address_method_stats(
            normalize_filters()
        )["groups"]
        self.assertEqual(groups[0]["success_count"], 1)
        self.assertEqual(groups[0]["failure_count"], 0)

    def test_status_success_zeroes_failure(self):
        groups = self.idx.address_method_stats(
            normalize_filters(status="success")
        )["groups"]
        for group in groups:
            self.assertEqual(group["failure_count"], 0)
            self.assertEqual(group["total_count"], group["success_count"])
        # 失败的 b 被过滤：alice/bob 的 transfer 各只剩一笔 100
        by_key = {(g["address"], g["method"]): g for g in groups}
        self.assertEqual(by_key[("alice", "transfer")]["total_amount"], "100")
        self.assertEqual(by_key[("bob", "transfer")]["total_amount"], "100")

    def test_status_failure_zeroes_success(self):
        groups = self.idx.address_method_stats(
            normalize_filters(status="failure")
        )["groups"]
        for group in groups:
            self.assertEqual(group["success_count"], 0)
            self.assertEqual(group["total_count"], group["failure_count"])
        by_key = {(g["address"], g["method"]): g for g in groups}
        self.assertEqual(set(by_key), {
            ("bob", "transfer"), ("alice", "transfer"),
        })
        self.assertEqual(by_key[("bob", "transfer")]["send_count"], 1)
        self.assertEqual(by_key[("bob", "transfer")]["receive_count"], 0)
        self.assertEqual(by_key[("alice", "transfer")]["send_count"], 0)
        self.assertEqual(by_key[("alice", "transfer")]["receive_count"], 1)

    def test_sort_total_amount_then_counts_then_address_method(self):
        result = self.idx.address_method_stats(normalize_filters())
        self.assertEqual(
            [(g["address"], g["method"]) for g in result["groups"]],
            [
                ("carol", "approve"),   # 200
                ("dave", "approve"),    # 200, total_count 相同、send 1 > 0
                ("alice", "transfer"),  # 130, total_count 2
                ("bob", "transfer"),    # 130, total_count 2, send 相同
                ("alice", "mint"),      # 15
                ("carol", "mint"),      # 15, address 码点 alice < carol
            ],
        )
        self.assertEqual(result["total_groups"], 6)

    def test_sort_total_amount_then_count(self):
        records = [
            # x/m：作为接收方出现 2 次，总额 20、count 2
            rec("h1", 1, 1, "q", "x", "m", "10"),
            rec("h2", 2, 2, "r", "x", "m", "10"),
            # s→y 20：s/m 与 y/m 各一笔 20（count 1）
            rec("h3", 3, 3, "s", "y", "m", "20"),
        ]
        keys = [
            (g["address"], g["method"])
            for g in TxIndexer(records).address_method_stats(
                normalize_filters()
            )["groups"]
        ]
        # 三个总额 20 的组：x（count 2）在前；s 与 y count 同为 1，
        # send 降序 s（send=1）在 y（receive=1）之前；
        # 两个总额 10 的发送方最后按地址码点 q、r
        self.assertEqual(
            keys,
            [("x", "m"), ("s", "m"), ("y", "m"), ("q", "m"), ("r", "m")],
        )

    def test_sort_send_then_receive_tiebreak(self):
        records = [
            # 同总额(20)、同 count(1)：s 是发送方（send=1），
            # y 是接收方（receive=1），send 降序 s 在前
            rec("h1", 1, 1, "s", "y", "m", "20"),
        ]
        keys = [
            (g["address"], g["method"])
            for g in TxIndexer(records).address_method_stats(
                normalize_filters()
            )["groups"]
        ]
        self.assertEqual(keys, [("s", "m"), ("y", "m")])

    def test_tiebreak_by_method_codepoint(self):
        records = [
            rec("h1", 1, 1, "a", "b", "z", "10"),
            rec("h2", 2, 2, "a", "b", "a", "10"),
        ]
        result = TxIndexer(records).address_method_stats(normalize_filters())
        # 发送方身份（send=1）排在接收方身份之前；同为发送方或接收方时
        # 按 address、再按 method 码点升序
        keys = [(g["address"], g["method"]) for g in result["groups"]]
        self.assertEqual(keys, [
            ("a", "a"),
            ("a", "z"),
            ("b", "a"),
            ("b", "z"),
        ])

    def test_pagination_no_skip_no_dup(self):
        filters = normalize_filters()
        all_keys = [
            (g["address"], g["method"])
            for g in self.idx.address_method_stats(
                filters, page_size=100
            )["groups"]
        ]
        collected = []
        cursor = None
        pages = 0
        while True:
            page = self.idx.address_method_stats(
                filters, page_size=2, cursor=cursor
            )
            pages += 1
            collected.extend(
                (g["address"], g["method"]) for g in page["groups"]
            )
            self.assertEqual(page["total_groups"], 6)
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(pages, 3)
        self.assertEqual(collected, all_keys)

    def test_page_size_not_bound_to_cursor(self):
        page1 = self.idx.address_method_stats(
            normalize_filters(), page_size=1
        )
        page2 = self.idx.address_method_stats(
            normalize_filters(), page_size=100,
            cursor=page1["next_cursor"],
        )
        self.assertEqual(len(page2["groups"]), 5)
        self.assertIsNone(page2["next_cursor"])

    def test_cursor_replay_same_page(self):
        page1 = self.idx.address_method_stats(
            normalize_filters(), page_size=2
        )
        again = self.idx.address_method_stats(
            normalize_filters(), page_size=3,
            cursor=page1["next_cursor"],
        )
        once_more = self.idx.address_method_stats(
            normalize_filters(), page_size=3,
            cursor=page1["next_cursor"],
        )
        self.assertEqual(
            [(g["address"], g["method"]) for g in again["groups"]],
            [(g["address"], g["method"]) for g in once_more["groups"]],
        )

    def test_no_match(self):
        result = self.idx.address_method_stats(
            normalize_filters(method="nope")
        )
        self.assertEqual(result, {
            "groups": [],
            "total_groups": 0,
            "next_cursor": None,
        })

    def test_filters_intersect_before_grouping(self):
        result = self.idx.address_method_stats(
            normalize_filters(start_time=15, end_time=30, method=["transfer"])
        )
        # 仅 b：bob→alice 30（失败）命中
        self.assertEqual(
            {(g["address"], g["method"]) for g in result["groups"]},
            {("bob", "transfer"), ("alice", "transfer")},
        )
        by_key = {(g["address"], g["method"]): g for g in result["groups"]}
        self.assertEqual(by_key[("bob", "transfer")]["send_count"], 1)
        self.assertEqual(by_key[("alice", "transfer")]["receive_count"], 1)

    def test_from_to_filters(self):
        result = self.idx.address_method_stats(
            normalize_filters(from_address=["alice"])
        )
        # 只含 alice 发出的 a（100）、d（15）：参与地址为 alice/bob/carol
        keys = {(g["address"], g["method"]) for g in result["groups"]}
        self.assertEqual(keys, {
            ("alice", "transfer"), ("bob", "transfer"),
            ("alice", "mint"), ("carol", "mint"),
        })

    def test_amount_and_block_filters(self):
        result = self.idx.address_method_stats(
            normalize_filters(min_amount="100", min_block=3)
        )
        # 仅 c：carol→dave approve 200（a 区块 1、b/d 金额不足）
        self.assertEqual(
            {(g["address"], g["method"]) for g in result["groups"]},
            {("carol", "approve"), ("dave", "approve")},
        )

    def test_page_size_bounds(self):
        with self.assertRaises(InvalidPageSizeError):
            self.idx.address_method_stats(normalize_filters(), page_size=0)
        with self.assertRaises(InvalidPageSizeError):
            self.idx.address_method_stats(
                normalize_filters(), page_size=1001
            )
        with self.assertRaises(InvalidPageSizeError):
            self.idx.address_method_stats(
                normalize_filters(), page_size="10"
            )
        with self.assertRaises(InvalidPageSizeError):
            self.idx.address_method_stats(
                normalize_filters(), page_size=True
            )

    def test_default_page_size_is_100(self):
        records = [
            rec("h%d" % i, i, i, "a%d" % i, "b%d" % i, "m%d" % i, "1")
            for i in range(60)
        ]
        page = TxIndexer(records).address_method_stats(normalize_filters())
        self.assertEqual(len(page["groups"]), 100)
        self.assertIsNotNone(page["next_cursor"])
        self.assertEqual(page["total_groups"], 120)

    def test_invalid_status_filter(self):
        with self.assertRaises(InvalidStatusFilterError):
            normalize_filters(status="Success")
        with self.assertRaises(InvalidStatusFilterError):
            normalize_filters(status=True)

    def test_address_conflicts_with_from_to(self):
        with self.assertRaises(InvalidFilterError):
            normalize_filters(address="alice", from_address=["bob"])

    def test_cursor_filter_mismatch(self):
        cursor = self.idx.address_method_stats(
            normalize_filters(), page_size=1
        )["next_cursor"]
        with self.assertRaises(InvalidCursorError):
            self.idx.address_method_stats(
                normalize_filters(method="mint"), page_size=1,
                cursor=cursor,
            )

    def test_cursor_status_mismatch(self):
        cursor = self.idx.address_method_stats(
            normalize_filters(status="failure"), page_size=1
        )["next_cursor"]
        with self.assertRaises(InvalidCursorError):
            self.idx.address_method_stats(
                normalize_filters(), page_size=1, cursor=cursor
            )

    def test_cursor_leading_zero_equivalent_amount(self):
        page1 = self.idx.address_method_stats(
            normalize_filters(min_amount="0100", min_block=1),
            page_size=1,
        )
        cursor = page1["next_cursor"]
        # Python 侧区块边界本身是非负整数；金额边界只调整前导零可续页
        page2 = self.idx.address_method_stats(
            normalize_filters(min_amount="100", min_block=1),
            page_size=1, cursor=cursor,
        )
        # 命中 a（100）、c（200）
        first = page1["groups"][0]
        self.assertEqual((first["address"], first["method"]),
                         ("carol", "approve"))
        second = page2["groups"][0]
        self.assertEqual((second["address"], second["method"]),
                         ("dave", "approve"))

    def test_cursor_cross_command_rejected(self):
        from tx_indexer.cursor import (
            decode_address_method_stats_cursor,
            decode_address_stats_cursor,
            decode_method_stats_cursor,
            encode_address_method_stats_cursor,
            encode_address_stats_cursor,
            encode_method_stats_cursor,
        )

        filters = normalize_filters()
        am_cursor = encode_address_method_stats_cursor(
            filters, 15, 1, 1, 0, "a", "m"
        )
        address_cursor = encode_address_stats_cursor(
            filters, 15, 1, 1, 0, "a"
        )
        method_cursor = encode_method_stats_cursor(filters, 15, 1, "m")
        with self.assertRaises(InvalidCursorError):
            decode_address_stats_cursor(am_cursor, filters)
        with self.assertRaises(InvalidCursorError):
            decode_method_stats_cursor(am_cursor, filters)
        with self.assertRaises(InvalidCursorError):
            decode_address_method_stats_cursor(address_cursor, filters)
        with self.assertRaises(InvalidCursorError):
            decode_address_method_stats_cursor(method_cursor, filters)

    def test_cursor_garbage(self):
        for bad in ("", "not-base64!!!", "bm9wZQ", "%%%"):
            with self.assertRaises(InvalidCursorError):
                self.idx.address_method_stats(
                    normalize_filters(), cursor=bad
                )

    def test_cursor_tampered_payload_rejected(self):
        import base64

        cursor = self.idx.address_method_stats(
            normalize_filters(), page_size=1
        )["next_cursor"]
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode(cursor + padding).decode("utf-8")
        )
        payload["after"] = "not-a-list"
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        tampered = base64.urlsafe_b64encode(raw.encode("utf-8")).rstrip(
            b"="
        ).decode("ascii")
        with self.assertRaises(InvalidCursorError):
            self.idx.address_method_stats(
                normalize_filters(), page_size=1, cursor=tampered
            )

    def test_big_amounts_exact_decimal_no_float(self):
        big = "123456789012345678901234567890"
        bigger = "123456789012345678901234567891"
        idx = TxIndexer([
            rec("x", 1, 1, "a", "b", "m", big),
            rec("y", 2, 2, "b", "a", "m", bigger),
        ])
        groups = {
            (g["address"], g["method"]): g
            for g in idx.address_method_stats(normalize_filters())["groups"]
        }
        expected_sum = str(int(big) + int(bigger))
        # a、b 都是发出一笔、收到一笔、同一 method，归入同一组
        for key in (("a", "m"), ("b", "m")):
            self.assertEqual(groups[key]["total_count"], 2)
            self.assertEqual(groups[key]["total_amount"], expected_sum)
            # (big + bigger) // 2 = big，整除下取整、无浮点误差
            self.assertEqual(groups[key]["avg_amount"], big)


class AddressMethodStatsCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".jsonl", delete=False, encoding="utf-8"
        )
        self.tmp.write(
            '{"tx_hash":"a","block_number":1,"timestamp":10,'
            '"from_address":"alice","to_address":"bob","method":"transfer",'
            '"amount":"100"}\n'
            '{"tx_hash":"b","block_number":2,"timestamp":20,'
            '"from_address":"bob","to_address":"alice","method":"transfer",'
            '"amount":"030","success":false}\n'
        )
        self.tmp.close()
        self.path = self.tmp.name
        self._stdout = sys.stdout

    def tearDown(self):
        os.unlink(self.path)
        sys.stdout = self._stdout

    def _run(self, *argv):
        out = io.StringIO()
        sys.stdout = out
        code = main(["address-method-stats", self.path] + list(argv))
        sys.stdout = self._stdout
        return code, out.getvalue()

    def test_cli_success(self):
        code, text = self._run("--page-size", "10")
        self.assertEqual(code, 0)
        result = json.loads(text)
        self.assertEqual(result["total_groups"], 2)
        self.assertEqual(len(result["groups"]), 2)
        # 金额输入前导零不影响输出
        for group in result["groups"]:
            self.assertNotRegex(group["total_amount"], r"^0[0-9]")

    def test_cli_leading_zero_bounds_page_through(self):
        code, text = self._run(
            "--min-amount", "0100", "--min-block", "001",
            "--page-size", "1",
        )
        self.assertEqual(code, 0)
        cursor = json.loads(text)["next_cursor"]
        out = io.StringIO()
        sys.stdout = out
        code = main([
            "address-method-stats", self.path,
            "--min-amount", "100", "--min-block", "1",
            "--page-size", "1", "--cursor", cursor,
        ])
        sys.stdout = self._stdout
        self.assertEqual(code, 0)
        page2 = json.loads(out.getvalue())
        self.assertEqual(page2["total_groups"], 2)
        self.assertEqual(len(page2["groups"]), 1)

    def test_cli_invalid_page_size(self):
        code, _ = self._run("--page-size", "0")
        self.assertEqual(code, 2)

    def test_cli_invalid_status(self):
        code, _ = self._run("--status", "Failure")
        self.assertEqual(code, 2)

    def test_cli_invalid_filter_conflict(self):
        code, _ = self._run("--address", "alice", "--from-address", "bob")
        self.assertEqual(code, 2)

    def test_cli_invalid_cursor(self):
        code, _ = self._run("--cursor", "garbage!!")
        self.assertEqual(code, 2)

    def test_cli_invalid_data_has_input_line(self):
        path = self.path + ".bad"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(
                '{"tx_hash":"a","block_number":1,"timestamp":10,'
                '"from_address":"alice","to_address":"bob",'
                '"method":"transfer","amount":"100"}\n'
                '{"tx_hash":"x","block_number":1,"timestamp":10,'
                '"from_address":"alice","to_address":"bob",'
                '"method":"transfer","amount":"notnum"}\n'
            )
        try:
            out = io.StringIO()
            sys.stdout = out
            old_err = sys.stderr
            err = io.StringIO()
            sys.stderr = err
            code = main(["address-method-stats", path])
            sys.stderr = old_err
            sys.stdout = self._stdout
            self.assertEqual(code, 2)
            payload = json.loads(err.getvalue())
            self.assertEqual(payload["error"], "invalid_transaction")
            self.assertEqual(payload["input_line"], 2)
        finally:
            os.unlink(path)

    def test_cli_duplicate_has_input_line(self):
        path = self.path + ".dup"
        with open(path, "w", encoding="utf-8") as fh:
            line = (
                '{"tx_hash":"a","block_number":1,"timestamp":10,'
                '"from_address":"alice","to_address":"bob",'
                '"method":"transfer","amount":"100"}\n'
            )
            fh.write(line)
            fh.write(line)
        try:
            old_err = sys.stderr
            err = io.StringIO()
            sys.stderr = err
            code = main(["address-method-stats", path])
            sys.stderr = old_err
            sys.stdout = self._stdout
            self.assertEqual(code, 2)
            payload = json.loads(err.getvalue())
            self.assertEqual(payload["error"], "duplicate_transaction")
            self.assertEqual(payload["input_line"], 2)
        finally:
            os.unlink(path)

    def test_cli_missing_file_is_io_error(self):
        old_err = sys.stderr
        err = io.StringIO()
        sys.stderr = err
        code = main(["address-method-stats", self.path + ".missing"])
        sys.stderr = old_err
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
