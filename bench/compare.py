"""Decide whether two query results mean the same thing.

This is the whole benchmark. Grading text-to-SQL by string similarity is
meaningless -- there are many correct spellings of one query -- so both queries
are executed and their results compared. That moves the difficulty rather than
removing it, because "the same result" is not obvious:

  * Row order is undefined unless the query says ORDER BY. Two correct answers
    can come back in different orders, so rows are compared as a multiset unless
    the gold query actually asked for an ordering.

  * Floating point. AVG over the same rows in a different join order can differ
    in the last bits. Exact equality would fail correct answers.

  * NULL is not zero. A LEFT JOIN that should produce NULL and a query that
    produces 0 are different answers to the question, and conflating them hides
    a real class of mistake -- so this is detected and reported separately
    rather than tolerated or lumped in with generic value mismatches.

  * Whitespace. The World Bank returns region names like "Latin America &
    Caribbean " with a trailing space. Failing a model for reproducing the data
    exactly as stored would be measuring the data, not the model.

  * Duplicates. If the gold result contains a row twice, a candidate that
    returns it once answered a different question. Multiset, not set.

  * How many columns. A question like "which regions have more than 30
    countries" does not say whether the count comes back too, and a model that
    returns it has arguably answered better. `column_mode="prefix"` accepts a
    result whose leading columns are the expected ones; `"strict"` does not.
    Which is correct is a real disagreement in text-to-SQL evaluation, so this
    reports both rather than quietly picking a side -- and a run relaxed this
    way is labelled `match_prefix`, never `match_exact`.

Every decision above is a judgement call, and each is reported as its own
verdict so the results table shows how often the grader had to make one.
"""

from collections import Counter
from enum import Enum

# Tight on purpose. The tolerance exists to absorb float non-determinism -- the
# same sum accumulated in a different order -- which lands around 1e-12 relative
# in double precision. It is not there to be forgiving. GDP values here reach
# 1e12, so a relative tolerance of 1e-6 would have silently accepted an answer
# off by a million dollars; a test pins that.
REL_TOLERANCE = 1e-9
ABS_TOLERANCE = 1e-12
MAX_FUZZY_ROWS = 2000     # beyond this, fall back to exact matching to stay fast


class Verdict(str, Enum):
    """Outcome of one comparison. The MATCH_* values all count as correct."""

    MATCH_EXACT = "match_exact"                  # identical, including order
    MATCH_UNORDERED = "match_unordered"          # same rows, order not requested
    MATCH_ROUNDED = "match_rounded"              # equal within float tolerance
    MATCH_PREFIX = "match_prefix"                # right answer plus extra trailing columns
    DIFF_ARITY = "diff_arity"                    # different number of columns
    DIFF_ROW_COUNT = "diff_row_count"            # different number of rows
    DIFF_ORDER = "diff_order"                    # right rows, wrong order, ORDER BY asked
    DIFF_DUPLICATES = "diff_duplicates"          # same distinct rows, different multiplicity
    DIFF_NULL_VS_ZERO = "diff_null_vs_zero"      # every difference is NULL against 0
    DIFF_VALUES = "diff_values"                  # genuinely different content
    ERROR = "error"                              # the candidate query did not run

    @property
    def is_match(self):
        return self.value.startswith("match_")


class Comparison:
    __slots__ = ("verdict", "detail")

    def __init__(self, verdict, detail=""):
        self.verdict = verdict
        self.detail = detail

    @property
    def is_match(self):
        return self.verdict.is_match

    def __repr__(self):
        return "Comparison(%s, %r)" % (self.verdict.value, self.detail)


_NULL = object()


def normalise(value, strip_strings=True):
    """Reduce a SQLite value to something comparable across drivers and types.

    Integers and floats are unified, because whether COUNT(*) arrives as 5 or
    5.0 is an artefact of the query plan and not part of the answer. NULL is
    kept deliberately distinct from everything else.
    """
    if value is None:
        return _NULL
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return value
    if isinstance(value, str):
        return value.strip() if strip_strings else value
    return value


def _row(row, strip_strings=True):
    return tuple(normalise(v, strip_strings) for v in row)


def _values_close(left, right):
    if left is _NULL or right is _NULL:
        return left is right          # NULL only ever equals NULL
    if isinstance(left, float) and isinstance(right, float):
        if left == right:
            return True
        return abs(left - right) <= max(ABS_TOLERANCE, REL_TOLERANCE * max(abs(left), abs(right)))
    return left == right


def _rows_close(left, right):
    return len(left) == len(right) and all(_values_close(a, b) for a, b in zip(left, right))


def _multiset_matches_fuzzily(gold, candidate):
    """Greedy pairing under float tolerance. Exact matching is tried first."""
    if len(gold) > MAX_FUZZY_ROWS:
        return False
    remaining = list(candidate)
    for gold_row in gold:
        for index, other in enumerate(remaining):
            if _rows_close(gold_row, other):
                remaining.pop(index)
                break
        else:
            return False
    return not remaining


def _null_zero_only(gold, candidate):
    """True when the results differ only by NULL appearing where 0 does.

    Worth separating because it is a specific, fixable modelling mistake --
    usually an inner join where a left join was needed, or SUM over an empty
    group -- rather than an arbitrary wrong answer.
    """
    if len(gold) != len(candidate):
        return False
    saw_null_zero = False
    for gold_row, cand_row in zip(gold, candidate):
        if len(gold_row) != len(cand_row):
            return False
        for a, b in zip(gold_row, cand_row):
            if _values_close(a, b):
                continue
            is_null_zero = (
                (a is _NULL and isinstance(b, float) and b == 0.0)
                or (b is _NULL and isinstance(a, float) and a == 0.0)
            )
            if not is_null_zero:
                return False
            saw_null_zero = True
    return saw_null_zero


def compare(gold_rows, candidate_rows, *, ordered, strip_strings=True,
            column_mode="strict"):
    """Compare two result sets.

    `ordered` says whether row order is part of the answer -- normally derived
    from whether the gold query contains a top-level ORDER BY.

    `column_mode` is "strict" (the candidate must return exactly the expected
    columns) or "prefix" (extra columns after the expected ones are allowed).
    Prefix mode only ever trims from the right, so returning the columns in a
    different order is still wrong -- column order is part of the answer.
    """
    if column_mode not in ("strict", "prefix"):
        raise ValueError("column_mode must be 'strict' or 'prefix'")

    gold = [_row(r, strip_strings) for r in gold_rows]
    candidate = [_row(r, strip_strings) for r in candidate_rows]

    if gold == candidate:
        return Comparison(Verdict.MATCH_EXACT)

    gold_arity = {len(r) for r in gold}
    cand_arity = {len(r) for r in candidate}
    if gold and candidate and gold_arity != cand_arity:
        trimmed = _trim_to_prefix(gold, candidate, gold_arity, cand_arity)             if column_mode == "prefix" else None
        if trimmed is not None:
            inner = compare(gold, trimmed, ordered=ordered,
                            strip_strings=False, column_mode="strict")
            if inner.is_match:
                # Never reported as an exact match: the run should show that the
                # grader had to relax something to accept this.
                return Comparison(
                    Verdict.MATCH_PREFIX,
                    "correct in the first %d column(s); %d extra returned" % (
                        min(gold_arity), min(cand_arity) - min(gold_arity)))
            candidate = trimmed
        else:
            return Comparison(
                Verdict.DIFF_ARITY,
                "expected %s columns, got %s" % (
                    "/".join(map(str, sorted(gold_arity))),
                    "/".join(map(str, sorted(cand_arity)))))

    if len(gold) != len(candidate):
        # Same distinct content but different multiplicity is its own mistake:
        # a stray DISTINCT, or a join that fanned rows out.
        if set(gold) == set(candidate):
            return Comparison(
                Verdict.DIFF_DUPLICATES,
                "%d rows expected, %d returned, same distinct rows" % (len(gold), len(candidate)))
        return Comparison(
            Verdict.DIFF_ROW_COUNT, "%d rows expected, %d returned" % (len(gold), len(candidate)))

    gold_counts, cand_counts = Counter(gold), Counter(candidate)
    same_multiset = gold_counts == cand_counts

    if same_multiset:
        if ordered:
            return Comparison(Verdict.DIFF_ORDER, "the query asked for an order and got another")
        return Comparison(Verdict.MATCH_UNORDERED)

    # Not equal exactly. Retry allowing float tolerance before calling it wrong.
    if ordered:
        if _rows_close_sequence(gold, candidate):
            return Comparison(Verdict.MATCH_ROUNDED, "equal within floating-point tolerance")
    elif _multiset_matches_fuzzily(gold, candidate):
        return Comparison(Verdict.MATCH_ROUNDED, "equal within floating-point tolerance")

    if _null_zero_only(gold, candidate):
        return Comparison(Verdict.DIFF_NULL_VS_ZERO,
                          "results agree except where NULL was expected and 0 returned")

    if not ordered and set(gold) == set(candidate):
        return Comparison(Verdict.DIFF_DUPLICATES, "same distinct rows, different multiplicity")

    return Comparison(Verdict.DIFF_VALUES, _first_difference(gold, candidate))


def _trim_to_prefix(gold, candidate, gold_arity, cand_arity):
    """Drop trailing columns from the candidate so both have the gold's width.

    Returns None when that cannot apply -- ragged results, or a candidate with
    fewer columns than expected, which is a genuine shortfall rather than an
    over-answer.
    """
    if len(gold_arity) != 1 or len(cand_arity) != 1:
        return None
    width, got = min(gold_arity), min(cand_arity)
    if got <= width:
        return None
    return [row[:width] for row in candidate]


def _rows_close_sequence(gold, candidate):
    return all(_rows_close(a, b) for a, b in zip(gold, candidate))


def _first_difference(gold, candidate):
    for index, (gold_row, cand_row) in enumerate(zip(gold, candidate)):
        if not _rows_close(gold_row, cand_row):
            return "row %d: expected %s, got %s" % (
                index, _render(gold_row), _render(cand_row))
    return "results differ"


def _render(row):
    return "(" + ", ".join("NULL" if v is _NULL else repr(v) for v in row) + ")"
