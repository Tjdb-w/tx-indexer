"""加载与校验测试。"""

import unittest

from tx_indexer.errors import (
    DuplicateTransactionError,
    InvalidTransactionError,
)
from tx_indexer.loader import load_lines

VALID = (
    '{"tx_hash": "0xaaa", "block_number": 10, "timestamp": 1000, '
    '"from_address": "alice", "to_address": "bob", "method": "transfer", '
    '"amount": "100"}'
)


class LoaderTest(unittest.TestCase):
    def test_valid(self):
        records = load_lines([VALID])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["amount"], "100")

    def test_bad_json_reports_line_no(self):
        with self.assertRaises(InvalidTransactionError) as ctx:
            load_lines([VALID, "{not json"])
        self.assertEqual(ctx.exception.error, "invalid_transaction")
        self.assertEqual(ctx.exception.input_line, 2)

    def test_blank_line(self):
        with self.assertRaises(InvalidTransactionError) as ctx:
            load_lines(["   "])
        self.assertEqual(ctx.exception.input_line, 1)

    def test_missing_field(self):
        line = '{"tx_hash": "x", "block_number": 1, "timestamp": 2, ' \
               '"from_address": "a", "to_address": "b", "method": "m"}'
        with self.assertRaises(InvalidTransactionError) as ctx:
            load_lines([line])
        self.assertEqual(ctx.exception.error, "invalid_transaction")
        self.assertIn("amount", ctx.exception.message)

    def test_extra_field(self):
        line = VALID[:-1] + ', "extra": 1}'
        with self.assertRaises(InvalidTransactionError):
            load_lines([line])

    def test_empty_text_fields(self):
        for field in ("tx_hash", "from_address", "to_address", "method"):
            line = (
                '{"tx_hash": "x", "block_number": 1, "timestamp": 2, '
                '"from_address": "a", "to_address": "b", "method": "m", '
                '"amount": "1"}'
            )
            import json
            obj = json.loads(line)
            obj[field] = "  "
            with self.assertRaises(InvalidTransactionError):
                load_lines([json.dumps(obj)])

    def test_negative_and_non_integer_numbers(self):
        import json

        base = {
            "tx_hash": "x", "block_number": 1, "timestamp": 2,
            "from_address": "a", "to_address": "b", "method": "m",
            "amount": "1",
        }
        for field, bad in (
            ("block_number", -1),
            ("block_number", 1.5),
            ("block_number", True),
            ("timestamp", -1),
            ("timestamp", "2"),
        ):
            obj = dict(base)
            obj[field] = bad
            with self.assertRaises(InvalidTransactionError):
                load_lines([json.dumps(obj)])

    def test_amount_must_be_integer_string(self):
        import json

        base = {
            "tx_hash": "x", "block_number": 1, "timestamp": 2,
            "from_address": "a", "to_address": "b", "method": "m",
        }
        for bad in (100, "-1", "1.5", "", "0x10", " 5", None):
            obj = dict(base, amount=bad)
            with self.assertRaises(InvalidTransactionError):
                load_lines([json.dumps(obj)])

    def test_zero_amount_ok(self):
        import json

        obj = {
            "tx_hash": "x", "block_number": 0, "timestamp": 0,
            "from_address": "a", "to_address": "b", "method": "m",
            "amount": "0",
        }
        self.assertEqual(load_lines([json.dumps(obj)])[0]["amount"], "0")

    def test_duplicate_tx_hash(self):
        second = VALID.replace('"100"', '"200"')
        with self.assertRaises(DuplicateTransactionError) as ctx:
            load_lines([VALID, second])
        self.assertEqual(ctx.exception.error, "duplicate_transaction")
        self.assertEqual(ctx.exception.input_line, 2)

    def test_success_explicit_true_false(self):
        import json

        base = {
            "tx_hash": "x", "block_number": 1, "timestamp": 2,
            "from_address": "a", "to_address": "b", "method": "m",
            "amount": "1",
        }
        ok = dict(base, success=True)
        bad = dict(base, tx_hash="y", success=False)
        records = load_lines([json.dumps(ok), json.dumps(bad)])
        self.assertIs(records[0]["success"], True)
        self.assertIs(records[1]["success"], False)

    def test_success_defaults_to_true(self):
        records = load_lines([VALID])
        self.assertIs(records[0]["success"], True)

    def test_success_non_bool_rejected_with_line_no(self):
        import json

        base = {
            "tx_hash": "x", "block_number": 1, "timestamp": 2,
            "from_address": "a", "to_address": "b", "method": "m",
            "amount": "1",
        }
        for index, bad in enumerate((0, 1, "true", "false", "", None), start=1):
            line = json.dumps(dict(base, success=bad))
            with self.assertRaises(InvalidTransactionError) as ctx:
                load_lines([VALID, line])
            self.assertEqual(ctx.exception.error, "invalid_transaction")
            # 非法记录在物理第 2 行，必须保留行号
            self.assertEqual(ctx.exception.input_line, 2)

    def test_success_plus_unknown_extra_rejected(self):
        # success 合法，但其余额外字段仍然非法
        import json

        line = json.dumps({
            "tx_hash": "x", "block_number": 1, "timestamp": 2,
            "from_address": "a", "to_address": "b", "method": "m",
            "amount": "1", "success": True, "extra": 9,
        })
        with self.assertRaises(InvalidTransactionError) as ctx:
            load_lines([line])
        self.assertIn("extra", ctx.exception.message)


if __name__ == "__main__":
    unittest.main()
