"""Offline calibration implementation; never supplied to the external agent."""


def normalize_labels(labels):
    result = []
    seen = set()
    for label in labels:
        cleaned = label.strip()
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result
