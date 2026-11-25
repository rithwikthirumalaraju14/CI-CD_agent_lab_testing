"""Fixed local checks for bounded Python expression repairs."""

from dataclasses import dataclass
import random


@dataclass(frozen=True)
class Family:
    name: str
    contract: str
    choices: tuple[str, ...]  # The starting expression is listed first.
    checks: tuple[tuple[object, object], ...]


FAMILIES = (
    Family("first-occurrence", "Deduplicate a list of strings, preserving the first occurrence.",
           ("sorted(set(x))", "list(dict.fromkeys(x))", "x", "list(dict.fromkeys(reversed(x)))"),
           ((["b", "a", "b", "c", "a"], ["b", "a", "c"]), ([], []), (["c", "b", "c"], ["c", "b"]))),
    Family("stable-ties", "Sort pairs by ascending numeric second field, preserving input order for ties.",
           ("sorted(x, key=lambda p: (p[1], p[0]))", "sorted(x, key=lambda p: p[1], reverse=True)",
            "sorted(x, key=lambda p: p[1])", "sorted(x)"),
           (([("z", 1), ("a", 1), ("b", 0)], [("b", 0), ("z", 1), ("a", 1)]),
            ([], []), ([("x", 4), ("w", -2)], [("w", -2), ("x", 4)]))),
    Family("nullable-values", "Remove only None and preserve other falsy values and input order.",
           ("list(filter(None, x))", "[v for v in x if v != 0]", "[v for v in x if v is not None]",
            "[v for v in x if v is None]"),
           (([0, "", None, False, 7], [0, "", False, 7]), ([], []), ([None, "a", False], ["a", False]))),
    Family("last-delimiter", "Return the text before the last colon, or the whole string without a colon.",
           ("x.split(':', 1)[0]", "x.rsplit(':', 1)[0]", "x.partition(':')[2]", "x.rpartition(':')[2]"),
           (("a:b:c", "a:b"), ("name", "name"), (":", ""), ("a:b:", "a:b"))),
    Family("negative-buckets", "Map a number to the index of its half-open bucket of width k, starting at zero.",
           ("int(x / k)", "round(x / k)", "x // k", "abs(x) // k"),
           ((-1, -1), (0, 0), (1, 0))),
    Family("reverse-slice", "Return the last k elements in original order. For k=0 return an empty list.",
           ("x[-k:]", "x[:k]", "x[-k:] if k else []", "x[-k:][::-1] if k else []"),
           (([1, 2, 3, 4], [2, 3, 4]), ([1], [1]), ([], []))),
    Family("default-count", "Count each string exactly as written, preserving first-seen key order.",
           ("{v: 1 for v in x}", "dict.fromkeys(x, len(x))", "{v: x.count(v) for v in dict.fromkeys(x)}",
            "{v.lower(): x.count(v) for v in x}"),
           ((["A", "b", "A", "a"], {"A": 2, "b": 1, "a": 1}), ([], {}), (["x", "x"], {"x": 2}))),
    Family("ascii-whitespace", "Trim ASCII spaces only, preserving tabs and other whitespace.",
           ("x.strip()", "x.strip(' ')", "x.lstrip(' ')", "x.replace(' ', '')"),
           ((" \t a b \t ", "\t a b \t"), ("  name  ", "name"), ("   ", ""))),
    Family("merge-priority", "Merge two dictionaries. The right dictionary wins on duplicate keys.",
           ("x[1] | x[0]", "x[0] | x[1]", "x[0]", "x[1]"),
           ((({"n": 1, "a": 3}, {"n": 0, "b": 2}), {"n": 0, "a": 3, "b": 2}),
            (({}, {"b": 0}), {"b": 0}), (({"a": 0}, {}), {"a": 0}))),
    Family("integer-ceiling", "Return the number of width-k groups needed for x nonnegative items.",
           ("x // k", "x // k + 1", "(x + k - 1) // k", "round(x / k)"),
           ((1, 1), (0, 0))),
)


def fixtures():
    """One visible failing example, plus fixed private checks for each problem."""
    for family in FAMILIES:
        for variant in range(1, 6):
            k = variant + 2
            checks = [(x, k, expected) for x, expected in family.checks]
            if family.name == "negative-buckets":
                checks += [(-2 * k - 1, k, -3), (2 * k, k, 2), (k - 1, k, 0)]
            elif family.name == "reverse-slice":
                checks = [([variant, variant + 1], 0, [])] + [
                    (list(range(k + 1)), k, list(range(1, k + 1))), ([variant], k, [variant]), ([], k, []),
                ]
            elif family.name == "integer-ceiling":
                checks += [(k, k, 1), (2 * k + 1, k, 3), (k - 1, k, 1)]
            else:
                # Each variant uses a different visible/hidden split where possible.
                checks = checks[variant % len(checks):] + checks[:variant % len(checks)]
            options = list(family.choices)
            random.Random(f"{family.name}/{variant}").shuffle(options)
            yield {
                "id": f"local-{family.name}-{variant:02d}", "family": family.name, "split": "dev",
                "contract": family.contract, "original": family.choices[0],
                "options": dict(zip("ABCD", options)), "checks": checks,
            }


def evaluate(expression, x, k):
    """Evaluate only expressions from the source-controlled fixture catalogue."""
    return eval(expression, {"__builtins__": {}, "abs": abs, "dict": dict, "filter": filter, "int": int,
                             "len": len, "list": list, "reversed": reversed,
                             "round": round, "set": set, "sorted": sorted, "x": x, "k": k})


def verify(case, choice):
    expression = case["options"].get(choice)
    if expression is None:
        return False
    for x, k, expected in case["checks"]:
        result = evaluate(expression, x, k)
        if type(result) is not type(expected) or result != expected:
            return False
        if isinstance(result, dict) and list(result) != list(expected):
            return False
        if isinstance(result, list) and [type(v) for v in result] != [type(v) for v in expected]:
            return False
    return True
