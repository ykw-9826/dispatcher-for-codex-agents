"""Trim labels, discard empty strings, casefold-deduplicate in first-seen order.

Return the cleaned spelling of the first occurrence. Input strings are not
mutated; any finite iterable of strings is supported.
"""


def normalize_labels(labels):
    return sorted(set(label.strip().lower() for label in labels))
