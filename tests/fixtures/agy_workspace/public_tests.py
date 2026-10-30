"""Visible immutable specifications, runnable with the standard library."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from normalize import normalize_labels  # noqa: E402
from parser import parse_row  # noqa: E402
from pipeline import read_labels  # noqa: E402


class ParserTests(unittest.TestCase):
    def test_quoted_commas(self):
        self.assertEqual(parse_row('A,"B,C",D'), ["A", "B,C", "D"])

    def test_escaped_quote(self):
        self.assertEqual(parse_row('"a""b",c'), ['a"b', "c"])

    def test_quoted_newline(self):
        self.assertEqual(parse_row('"a\nb",c'), ["a\nb", "c"])

    def test_malformed(self):
        with self.assertRaises(ValueError):
            parse_row('"unterminated')

    def test_multiple_records(self):
        with self.assertRaises(ValueError):
            parse_row("a,b\nc,d")


class NormalizeTests(unittest.TestCase):
    def test_spelling_order_empty(self):
        self.assertEqual(normalize_labels([" B ", "a", "b", " "]), ["B", "a"])

    def test_unicode_iterable(self):
        self.assertEqual(normalize_labels(iter(["Straße", "STRASSE"])), ["Straße"])

    def test_input_unchanged(self):
        source = [" A ", "a"]
        self.assertEqual(normalize_labels(source), ["A"])
        self.assertEqual(source, [" A ", "a"])


class PipelineTests(unittest.TestCase):
    def test_composition(self):
        self.assertEqual(
            read_labels('" Red,Blue ",RED,"red,blue",,绿'), ["Red,Blue", "RED", "绿"]
        )


if __name__ == "__main__":
    unittest.main()
