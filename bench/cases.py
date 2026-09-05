"""Load and validate benchmark cases.

A benchmark is only as good as its gold answers, and a bad case is worse than no
case: it produces a number that looks like a measurement. Every case is checked
before it is allowed to score anything.

The check that matters most is triviality. A question whose gold query returns
zero rows is passed by any query that also returns nothing -- including
`SELECT 1 WHERE 0`, and including a model that misunderstood the question in a
different direction. Such a case inflates every score it appears in, so it is
rejected rather than counted.
"""

import json
import logging
import os

from . import sqlutil

log = logging.getLogger("bench.cases")

DEFAULT_CASES = os.path.join(os.path.dirname(os.path.dirname(__file__)), "cases", "cases.jsonl")

REQUIRED_FIELDS = ("id", "question", "gold_sql")


class Case:
    __slots__ = ("id", "question", "gold_sql", "tags", "note", "ordered")

    def __init__(self, id, question, gold_sql, tags=(), note=None, ordered=None):
        self.id = id
        self.question = question
        self.gold_sql = gold_sql.strip()
        self.tags = tuple(tags)
        self.note = note
        # Order matters only if the gold query asked for it, unless a case
        # deliberately overrides that.
        self.ordered = sqlutil.has_top_level_order_by(self.gold_sql) if ordered is None else ordered

    def __repr__(self):
        return "Case(%s, %r)" % (self.id, self.question[:48])


def load(path=DEFAULT_CASES):
    cases = []
    seen = set()
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError("%s line %d: %s" % (path, number, exc)) from exc
            missing = [f for f in REQUIRED_FIELDS if not raw.get(f)]
            if missing:
                raise ValueError("%s line %d: missing %s" % (path, number, ", ".join(missing)))
            if raw["id"] in seen:
                raise ValueError("%s line %d: duplicate id %s" % (path, number, raw["id"]))
            seen.add(raw["id"])
            cases.append(Case(**raw))
    return cases


class CaseProblem:
    __slots__ = ("case_id", "severity", "message")

    def __init__(self, case_id, severity, message):
        self.case_id = case_id
        self.severity = severity      # 'reject' or 'warn'
        self.message = message

    def __repr__(self):
        return "%s [%s] %s: %s" % (self.case_id, self.severity, "", self.message)


def validate(conn, cases):
    """Run every gold query and report what is wrong with the set.

    Returns (usable_cases, problems).
    """
    problems = []
    usable = []

    for case in cases:
        ok, reason = sqlutil.is_single_read_only_statement(case.gold_sql)
        if not ok:
            problems.append(CaseProblem(case.id, "reject", "gold SQL rejected: %s" % reason))
            continue

        unstable = sqlutil.nondeterministic_parts(case.gold_sql)
        if unstable:
            problems.append(CaseProblem(
                case.id, "reject",
                "gold SQL uses %s, so its answer is not fixed by the data"
                % ", ".join(unstable)))
            continue

        try:
            rows = conn.execute(case.gold_sql).fetchall()
        except Exception as exc:                       # noqa: BLE001 - report anything
            problems.append(CaseProblem(case.id, "reject", "gold SQL failed: %s" % exc))
            continue

        if not rows:
            problems.append(CaseProblem(
                case.id, "reject",
                "gold returns no rows, so any empty result would score as correct"))
            continue

        if sqlutil.has_limit_without_order(case.gold_sql):
            problems.append(CaseProblem(
                case.id, "reject",
                "LIMIT without ORDER BY: which rows come back is arbitrary"))
            continue

        # A single scalar of 0 or NULL is nearly as weak as an empty result.
        if len(rows) == 1 and len(rows[0]) == 1 and rows[0][0] in (0, None):
            problems.append(CaseProblem(
                case.id, "warn", "gold answer is a single %s" % (
                    "NULL" if rows[0][0] is None else "zero")))

        # Backstop for instability the syntactic check cannot see, such as an
        # unstable tie in an ORDER BY. Repeated because a single re-run can
        # agree by chance; this narrows the window rather than closing it,
        # which is why the syntactic check above exists.
        first = [tuple(r) for r in rows]
        if any(first != [tuple(r) for r in conn.execute(case.gold_sql).fetchall()]
               for _ in range(3)):
            problems.append(CaseProblem(
                case.id, "reject", "gold query is not stable across repeated runs"))
            continue

        if not case.tags:
            problems.append(CaseProblem(case.id, "warn", "no tags, so it cannot be grouped"))

        usable.append(case)

    return usable, problems


def tag_counts(cases):
    counts = {}
    for case in cases:
        for tag in case.tags:
            counts[tag] = counts.get(tag, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
