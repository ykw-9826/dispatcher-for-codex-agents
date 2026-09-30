"""Read-only public benchmark tests; executed by the parent, never model-graded."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from labels import normalize_labels  # noqa: E402


class LabelsTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(normalize_labels([]), [])
        self.assertEqual(normalize_labels([" ", "\t", "\n"]), [])

    def test_trim_and_preserve_spelling(self):
        self.assertEqual(normalize_labels(["  Alpha ", " Beta"]), ["Alpha", "Beta"])

    def test_order_is_not_sorted(self):
        self.assertEqual(normalize_labels(["Zebra", "Apple"]), ["Zebra", "Apple"])

    def test_first_case_insensitive_duplicate_wins(self):
        self.assertEqual(
            normalize_labels([" Beta ", "beta", "BETA", " Alpha"]), ["Beta", "Alpha"]
        )

    def test_unicode_casefold(self):
        self.assertEqual(
            normalize_labels(["Straße", "STRASSE", "猫", " 猫 "]), ["Straße", "猫"]
        )

    def test_input_unchanged(self):
        values = [" A ", "a", " B"]
        original = list(values)
        self.assertEqual(normalize_labels(values), ["A", "B"])
        self.assertEqual(values, original)

    def test_generator(self):
        self.assertEqual(normalize_labels(x for x in [" B ", "a", "b"]), ["B", "a"])


if __name__ == "__main__":
    unittest.main()
