"""Parent-held CSV differential oracle, never copied to the agent workspace."""

import csv
import io
import itertools
import sys
from pathlib import Path


def validate(root):
    sys.path.insert(0, str(root / "src"))
    from normalize import normalize_labels
    from parser import parse_row
    from pipeline import read_labels

    count = 0
    values = [" A ", "a", "B", "", " ", "x,y", 'a"b', "ß", "SS", "绿", "a\nb"]
    for size in range(4):
        for fields in itertools.product(values, repeat=size):
            buf = io.StringIO(newline="")
            csv.writer(buf, lineterminator="\r\n").writerow(fields)
            text = buf.getvalue().removesuffix("\r\n")
            expected_fields = list(next(csv.reader(io.StringIO(text, newline="")), []))
            expected, seen = [], set()
            for value in fields:
                cleaned = value.strip()
                if cleaned and cleaned.casefold() not in seen:
                    seen.add(cleaned.casefold())
                    expected.append(cleaned)
            assert parse_row(text) == expected_fields
            assert normalize_labels(iter(fields)) == expected
            assert read_labels(text) == expected
            count += 1
    for text in ['"unterminated', '"a"x,b', "a,b\nc,d"]:
        try:
            parse_row(text)
        except ValueError:
            pass
        else:
            raise AssertionError("MALFORMED_OR_MULTIRECORD_NOT_REJECTED")
    print(f"HIDDEN_VALIDATOR_PASS cases={count}")


if __name__ == "__main__":
    validate(Path(sys.argv[1]))
