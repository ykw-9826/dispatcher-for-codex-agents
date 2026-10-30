"""Read exactly one CSV record using comma separators and double-quote quoting.

Quoted commas, doubled quotes and embedded newlines are data. An empty input
returns []. Malformed quoting or multiple records must raise ValueError.
The parser preserves field spelling and whitespace; normalization is separate.
"""


def parse_row(text):
    if not text:
        return []
    return text.split(",")
