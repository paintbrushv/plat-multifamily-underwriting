"""Tests for engine.excel_io._coerce_int (Wave 3 / Bug 1.8).

Standardizes the int-coercion policy across ingest entry points:
  - Floats whose fractional part is within ±0.01 of an integer are accepted
    (round-to-int).
  - Floats with a larger fractional part raise ValueError (no silent
    truncation, unlike the prior `rediq_bridge._to_int`).
"""
import unittest

from engine.excel_io import _coerce_int


class TestCoerceIntEpsilon(unittest.TestCase):
    def test_int_with_zero_fraction_returns_int(self):
        # Exact float-integer (e.g. openpyxl numeric cell) → int.
        self.assertEqual(_coerce_int(12.0, "field"), 12)

    def test_int_with_tiny_fraction_returns_int(self):
        # Float drift within epsilon → rounded to int (no error).
        self.assertEqual(_coerce_int(12.0001, "field"), 12)
        self.assertEqual(_coerce_int(11.9999, "field"), 12)

    def test_int_with_large_fraction_raises(self):
        # Genuinely non-integer float must fail loudly.
        with self.assertRaises(ValueError) as cm:
            _coerce_int(12.5, "renovation_programs.monthly_pace")
        self.assertIn("renovation_programs.monthly_pace", str(cm.exception))

    def test_int_negative_handled(self):
        # Negative floats with zero fraction also accepted.
        self.assertEqual(_coerce_int(-7.0, "field"), -7)
        self.assertEqual(_coerce_int(-7.0001, "field"), -7)

    def test_pure_int_passthrough(self):
        self.assertEqual(_coerce_int(42, "field"), 42)
        self.assertEqual(_coerce_int(0, "field"), 0)
        self.assertEqual(_coerce_int(-3, "field"), -3)

    def test_string_digits_accepted(self):
        self.assertEqual(_coerce_int("12", "field"), 12)
        self.assertEqual(_coerce_int("  42  ", "field"), 42)

    def test_string_float_within_epsilon(self):
        self.assertEqual(_coerce_int("12.0", "field"), 12)
        self.assertEqual(_coerce_int("-7.0", "field"), -7)

    def test_string_float_outside_epsilon_raises(self):
        with self.assertRaises(ValueError):
            _coerce_int("12.5", "field")

    def test_bool_rejected(self):
        # bool is a subclass of int in Python — must be rejected explicitly.
        with self.assertRaises(ValueError):
            _coerce_int(True, "field")
        with self.assertRaises(ValueError):
            _coerce_int(False, "field")

    def test_none_rejected(self):
        with self.assertRaises(ValueError):
            _coerce_int(None, "field")

    def test_garbage_string_rejected(self):
        with self.assertRaises(ValueError):
            _coerce_int("not_a_number", "field")


if __name__ == "__main__":
    unittest.main()
