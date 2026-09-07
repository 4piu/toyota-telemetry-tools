from __future__ import annotations

import unittest

from toyota_telemetry.decoding import evaluate_formula, extract_raw


class DecodingTests(unittest.TestCase):
    def test_bit_extraction_uses_catalog_msb_numbering(self) -> None:
        self.assertEqual(extract_raw(bytes.fromhex("12 34"), 0, 7), 0x12)
        self.assertEqual(extract_raw(bytes.fromhex("12 34"), 4, 11), 0x23)
        with self.assertRaises(ValueError):
            extract_raw(b"\x00", 0, 8)

    def test_formula_evaluator_is_bounded(self) -> None:
        self.assertEqual(evaluate_formula("signed(raw, 16) / 10", 0xFF9B), -10.1)
        with self.assertRaises(ValueError):
            evaluate_formula("__import__('os').getcwd()", 0)


if __name__ == "__main__":
    unittest.main()
