"""Public entry point for a single CSV record of labels."""

from normalize import normalize_labels
from parser import parse_row


def read_labels(text):
    return normalize_labels(parse_row(text))
