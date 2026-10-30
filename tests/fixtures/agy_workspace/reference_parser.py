"""Parent-only reference; never provided to the external agent."""

import csv
import io


def parse_row(text):
    if not text:
        return []
    try:
        rows = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except csv.Error as exc:
        raise ValueError("INVALID_CSV") from exc
    if len(rows) != 1:
        raise ValueError("EXPECTED_ONE_RECORD")
    return rows[0]
