"""Run a model over the case set and record what happened.

Every attempt is stored, not just the score: the generated SQL, the verdict, the
timing and the tokens. A benchmark that keeps only the total tells you a model
got 62% and nothing about why, which is the half worth having.
"""

import json
import logging
import os
import time

from . import sqlutil
from .compare import Verdict, compare
from .models import Generation

log = logging.getLogger("bench.runner")

RESULTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "results")


class Attempt:
    __slots__ = ("case_id", "question", "tags", "gold_sql", "sql", "verdict",
                 "detail", "seconds", "input_tokens", "output_tokens",
                 "cached_tokens", "gold_rows", "candidate_rows")

    def __init__(self, **kwargs):
        for slot in self.__slots__:
            setattr(self, slot, kwargs.get(slot))

    @property
    def ok(self):
        return Verdict(self.verdict).is_match

    def as_dict(self):
        return {slot: getattr(self, slot) for slot in self.__slots__}


def _execute(conn, sql, limit=10000):
    """Run a query, returning (rows, error). Rows are plain tuples."""
    ok, reason = sqlutil.is_single_read_only_statement(sql)
    if not ok:
        return None, "rejected: %s" % reason
    try:
        cursor = conn.execute(sql)
        rows = [tuple(r) for r in cursor.fetchmany(limit)]
    except Exception as exc:                            # noqa: BLE001
        return None, "%s: %s" % (type(exc).__name__, exc)
    return rows, None


def run(model, conn, cases, schema, on_progress=None, column_mode="strict",
        indicators=""):
    attempts = []
    for index, case in enumerate(cases, start=1):
        gold_rows, gold_error = _execute(conn, case.gold_sql)
        if gold_error:
            # validate() should have caught this; if it did not, say so loudly
            # rather than scoring the case as a model failure.
            raise RuntimeError("gold SQL for %s failed at run time: %s"
                               % (case.id, gold_error))

        generation = model.generate(case.question, schema, indicators)
        if generation.error:
            verdict, detail, candidate_rows = Verdict.ERROR, generation.error, None
        else:
            sql = sqlutil.extract_sql(generation.sql or generation.raw)
            candidate_rows, run_error = _execute(conn, sql)
            if run_error:
                verdict, detail = Verdict.ERROR, run_error
            else:
                result = compare(gold_rows, candidate_rows, ordered=case.ordered,
                                 column_mode=column_mode)
                verdict, detail = result.verdict, result.detail
            generation = Generation(
                sql=sql, raw=generation.raw, seconds=generation.seconds,
                input_tokens=generation.input_tokens,
                output_tokens=generation.output_tokens,
                cached_tokens=generation.cached_tokens)

        attempt = Attempt(
            case_id=case.id, question=case.question, tags=list(case.tags),
            gold_sql=case.gold_sql, sql=generation.sql, verdict=verdict.value,
            detail=detail, seconds=round(generation.seconds, 3),
            input_tokens=generation.input_tokens, output_tokens=generation.output_tokens,
            cached_tokens=generation.cached_tokens,
            gold_rows=len(gold_rows), candidate_rows=(
                len(candidate_rows) if candidate_rows is not None else None))
        attempts.append(attempt)

        if on_progress:
            on_progress(index, len(cases), attempt)

    return attempts


def save(model_name, attempts, directory=RESULTS_DIR):
    os.makedirs(directory, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = model_name.replace("/", "_").replace(":", "_")
    path = os.path.join(directory, "%s-%s.json" % (safe, stamp))
    payload = {
        "model": model_name,
        "created_at": time.time(),
        "cases": len(attempts),
        "correct": sum(1 for a in attempts if a.ok),
        "attempts": [a.as_dict() for a in attempts],
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1, ensure_ascii=False)
    return path


def load(path):
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    payload["attempts"] = [Attempt(**a) for a in payload["attempts"]]
    return payload
