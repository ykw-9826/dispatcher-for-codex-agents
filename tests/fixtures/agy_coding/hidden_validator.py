"""Parent-held synthetic exhaustive oracle; never placed in the agent repo."""

import importlib.util
import itertools
import sys


def expected(values):
    cleaned = [v.strip() for v in values if v.strip()]
    return [
        v
        for i, v in enumerate(cleaned)
        if v.casefold() not in {p.casefold() for p in cleaned[:i]}
    ]


def main():
    spec = importlib.util.spec_from_file_location("candidate_labels", sys.argv[1])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    alphabet = ["", " ", " A ", "a", "B", " b ", "ß", "SS", "\t猫", "\n猫 "]
    count = 0
    for size in range(4):
        for values in itertools.product(alphabet, repeat=size):
            wanted = expected(values)
            items = list(values)
            assert module.normalize_labels(items) == wanted, "HIDDEN_CASE_FAILED"
            assert items == list(values), "INPUT_MUTATED"
            assert module.normalize_labels(iter(values)) == wanted, "ITERATOR_FAILED"
            count += 1
    print(f"HIDDEN_VALIDATOR_PASS cases={count}")


if __name__ == "__main__":
    main()
