"""Trim fields, remove empty labels and casefold duplicates.

Keep the first cleaned spelling and original order. Accept a finite iterable
of strings without modifying the caller's collection.
"""


def normalize_labels(labels):
    return sorted({label.strip().lower() for label in labels})
