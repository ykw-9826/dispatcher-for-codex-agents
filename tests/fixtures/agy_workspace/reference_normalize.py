"""Parent-only reference; never provided to the external agent."""


def normalize_labels(labels):
    result, seen = [], set()
    for label in labels:
        cleaned = label.strip()
        folded = cleaned.casefold()
        if cleaned and folded not in seen:
            seen.add(folded)
            result.append(cleaned)
    return result
