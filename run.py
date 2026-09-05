#!/usr/bin/env python3
"""Text-to-SQL execution benchmark over World Bank open data.

    python run.py build                  # fetch the data, build the database
    python run.py cases                  # validate the gold queries
    python run.py eval --model gold      # the upper control: must score 100%
    python run.py eval --model constant  # the lower control: must score ~0%
    python run.py eval --model claude:claude-opus-5:high
"""

import argparse
import logging
import sys

from bench import cases as case_module
from bench import db, load, models, report, runner
from bench.client import WorldBankClient


def _log(verbose):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s")


def _load_valid_cases(conn, path, strict=True):
    loaded = case_module.load(path)
    usable, problems = case_module.validate(conn, loaded)
    rejected = [p for p in problems if p.severity == "reject"]
    for problem in problems:
        line = "  %-9s %-6s %s" % (problem.case_id, problem.severity, problem.message)
        print(line, file=sys.stderr)
    if rejected and strict:
        raise SystemExit(
            "%d case(s) rejected. Fix them or pass --allow-bad; a benchmark with "
            "broken gold answers reports a number that means nothing." % len(rejected))
    return usable


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=db.DEFAULT_DB)
    parser.add_argument("--cases", default=case_module.DEFAULT_CASES)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("build", help="fetch World Bank data into the database")
    sub.add_parser("schema", help="print the schema the model is shown")

    validate = sub.add_parser("cases", help="validate the gold queries")
    validate.add_argument("--allow-bad", action="store_true")

    evaluate = sub.add_parser("eval", help="run a model over the cases")
    evaluate.add_argument("--model", default="gold",
                          help="gold | constant | claude[:model][:effort]")
    evaluate.add_argument("--limit", type=int, help="only the first N cases")
    evaluate.add_argument("--tag", help="only cases carrying this tag")
    evaluate.add_argument("--allow-bad", action="store_true")
    evaluate.add_argument("--no-save", action="store_true")
    evaluate.add_argument("--failures", type=int, default=8)
    evaluate.add_argument("--columns", choices=("strict", "prefix"), default="strict",
                          help="strict: the candidate must return exactly the expected "
                               "columns. prefix: extra trailing columns are allowed, "
                               "reported as match_prefix.")

    args = parser.parse_args(argv)
    _log(args.verbose)

    if args.command == "build":
        conn = db.init(db.connect(args.db))
        result = load.load_all(WorldBankClient(), conn)
        print(result)
        print(db.counts(conn))
        return 0

    if args.command == "schema":
        print(db.schema_text(db.connect_readonly(args.db)))
        return 0

    conn = db.connect_readonly(args.db)

    if args.command == "cases":
        usable = _load_valid_cases(conn, args.cases, strict=not args.allow_bad)
        print("%d usable cases" % len(usable))
        for tag, count in case_module.tag_counts(usable).items():
            print("  %-16s %d" % (tag, count))
        return 0

    if args.command == "eval":
        usable = _load_valid_cases(conn, args.cases, strict=not args.allow_bad)
        if args.tag:
            usable = [c for c in usable if args.tag in c.tags]
        if args.limit:
            usable = usable[:args.limit]
        if not usable:
            raise SystemExit("no cases selected")

        model = models.build(args.model, cases=usable)
        schema = db.schema_text(conn)
        indicators = db.reference_values(conn)

        def progress(index, total, attempt):
            mark = "ok  " if attempt.ok else "FAIL"
            print("  [%2d/%d] %s %-9s %s" % (
                index, total, mark, attempt.case_id, attempt.verdict), file=sys.stderr)

        attempts = runner.run(model, conn, usable, schema, on_progress=progress,
                              column_mode=args.columns, indicators=indicators)
        print()
        label = "%s [columns=%s]" % (model.name, args.columns)
        print(report.text(label, attempts, show_failures=args.failures))

        problems = report.check_controls([(model.name, attempts)])
        for problem in problems:
            print("\nWARNING: %s" % problem)

        if not args.no_save:
            path = runner.save("%s-%s" % (model.name, args.columns), attempts)
            print("\nsaved to %s" % path)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
