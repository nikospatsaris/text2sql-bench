"""Tests for case validation and the end-to-end run.

The validator is what stops a broken question from quietly inflating a score, so
each way a case can be broken has a test. The runner tests use the two controls
on a tiny in-memory database: if `gold` does not score 100% and `constant` does
not score 0%, the harness is lying and nothing downstream is worth reading.
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bench import db, models, report, runner, sqlutil
from bench.cases import Case, tag_counts, validate
from bench.compare import Verdict


def _tiny_db():
    """Three countries, two aggregates, one indicator."""
    conn = db.init(db.connect(":memory:"))
    conn.execute("INSERT INTO regions VALUES ('EUR', 'Europe')")
    conn.execute("INSERT INTO income_levels VALUES ('HIC', 'High income')")
    rows = [
        ("GRC", "GR", "Greece", "EUR", "HIC", "Athens", 37.9, 23.7, 1),
        ("ESP", "ES", "Spain", "EUR", "HIC", "Madrid", 40.4, -3.7, 1),
        ("PRT", "PT", "Portugal", "EUR", "HIC", None, 38.7, -9.1, 1),
        ("WLD", "1W", "World", None, None, None, None, None, 0),
        ("EUU", "EU", "European Union", None, None, None, None, None, 0),
    ]
    conn.executemany("INSERT INTO economies VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn.execute("INSERT INTO indicators VALUES ('SP.POP.TOTL', 'Population', 'population')")
    # Portugal deliberately has no observation, so LEFT JOIN cases have something
    # to be wrong about.
    conn.executemany("INSERT INTO observations VALUES (?,'SP.POP.TOTL',2020,?)",
                     [("GRC", 10.7), ("ESP", 47.4), ("WLD", 7800.0)])
    conn.commit()
    return conn


class TestCaseValidation(unittest.TestCase):
    def setUp(self):
        self.conn = _tiny_db()

    def tearDown(self):
        self.conn.close()

    def _validate(self, case):
        usable, problems = validate(self.conn, [case])
        return usable, problems

    def test_a_good_case_is_accepted(self):
        usable, problems = self._validate(
            Case("ok", "How many countries?",
                 "SELECT COUNT(*) FROM economies WHERE is_country = 1", ["aggregate"]))
        self.assertEqual(len(usable), 1)
        self.assertEqual(problems, [])

    def test_a_case_whose_gold_returns_nothing_is_rejected(self):
        # Any wrong query that also returns nothing would score as correct.
        usable, problems = self._validate(
            Case("empty", "Countries on Mars?",
                 "SELECT name FROM economies WHERE name = 'Mars'", ["filter"]))
        self.assertEqual(usable, [])
        self.assertIn("no rows", problems[0].message)

    def test_limit_without_order_is_rejected(self):
        usable, problems = self._validate(
            Case("unstable", "Give me two countries",
                 "SELECT name FROM economies LIMIT 2", ["lookup"]))
        self.assertEqual(usable, [])
        self.assertIn("LIMIT", problems[0].message)

    def test_a_random_gold_query_is_rejected_by_reading_it_not_by_luck(self):
        # Running the query twice and comparing is not enough: on a small table
        # ORDER BY RANDOM() LIMIT 1 returns the same row by coincidence often
        # enough to pass. This must be caught by inspecting the SQL.
        usable, problems = self._validate(
            Case("random", "A random country",
                 "SELECT name FROM economies ORDER BY RANDOM() LIMIT 1", ["lookup"]))
        self.assertEqual(usable, [])
        self.assertIn("random", problems[0].message)
        self.assertIn("not fixed by the data", problems[0].message)

    def test_a_gold_query_that_depends_on_the_current_date_is_rejected(self):
        # This one would pass every check today and change the benchmark's
        # answers tomorrow, which is worse than failing outright.
        usable, problems = self._validate(
            Case("today", "Countries as of today",
                 "SELECT name FROM economies WHERE is_country = 1 "
                 "AND DATE('now') > '2000-01-01'", ["lookup"]))
        self.assertEqual(usable, [])
        self.assertIn("now", problems[0].message)

    def test_an_ordinary_query_is_not_flagged_as_nondeterministic(self):
        self.assertEqual(sqlutil.nondeterministic_parts(
            "SELECT name FROM economies WHERE is_country = 1"), [])

    def test_broken_sql_is_rejected(self):
        usable, problems = self._validate(
            Case("broken", "Nonsense", "SELECT nope FROM economies", ["lookup"]))
        self.assertEqual(usable, [])
        self.assertIn("failed", problems[0].message)

    def test_a_writing_gold_query_is_rejected(self):
        usable, problems = self._validate(
            Case("write", "Delete everything", "DROP TABLE economies", ["lookup"]))
        self.assertEqual(usable, [])

    def test_an_untagged_case_is_warned_about_but_kept(self):
        usable, problems = self._validate(
            Case("untagged", "How many regions?", "SELECT COUNT(*) FROM regions", []))
        self.assertEqual(len(usable), 1)
        self.assertEqual(problems[0].severity, "warn")

    def test_tag_counts_sorts_by_frequency(self):
        counts = tag_counts([
            Case("a", "q", "SELECT 1", ["join", "aggregate"]),
            Case("b", "q", "SELECT 1", ["join"]),
        ])
        self.assertEqual(list(counts), ["join", "aggregate"])


class TestReadOnlyExecution(unittest.TestCase):
    def test_a_write_cannot_reach_the_database(self):
        # The guard is a string check; this proves the connection itself refuses,
        # which is what actually protects the data.
        #
        # A fresh directory per run, not a fixed filename: an interrupted run
        # used to leave the file behind, and the next run inserted a second row
        # into it and failed on the count. A test that fails once and then
        # passes teaches you to ignore it.
        directory = tempfile.mkdtemp(prefix="t2s-ro-")
        path = os.path.join(directory, "probe.sqlite")
        try:
            writable = db.init(db.connect(path))
            writable.execute("INSERT INTO regions VALUES ('X', 'X')")
            writable.commit()
            writable.close()

            readonly = db.connect_readonly(path)
            self.assertEqual(readonly.execute("SELECT COUNT(*) FROM regions").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                readonly.execute("DELETE FROM regions")
            readonly.close()
        finally:
            shutil.rmtree(directory, ignore_errors=True)


class TestControls(unittest.TestCase):
    """The harness self-check, run on a tiny database."""

    def setUp(self):
        self.conn = _tiny_db()
        self.cases = [
            Case("c1", "How many countries?",
                 "SELECT COUNT(*) FROM economies WHERE is_country = 1", ["aggregate"]),
            Case("c2", "Population of Greece in 2020?",
                 "SELECT o.value FROM observations o JOIN economies e ON e.iso3 = o.iso3 "
                 "WHERE e.name = 'Greece' AND o.year = 2020", ["join"]),
            Case("c3", "List country names alphabetically.",
                 "SELECT name FROM economies WHERE is_country = 1 ORDER BY name", ["ordering"]),
        ]
        self.schema = db.schema_text(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_the_gold_control_scores_everything(self):
        attempts = runner.run(models.GoldModel(self.cases), self.conn, self.cases, self.schema)
        self.assertTrue(all(a.ok for a in attempts))
        self.assertEqual(report.summarise(attempts)["accuracy"], 1.0)

    def test_the_constant_control_scores_nothing(self):
        attempts = runner.run(models.ConstantModel(), self.conn, self.cases, self.schema)
        self.assertFalse(any(a.ok for a in attempts))

    def test_controls_that_misbehave_are_reported(self):
        good = runner.run(models.GoldModel(self.cases), self.conn, self.cases, self.schema)
        self.assertEqual(report.check_controls([("gold", good)]), [])

        # Simulate a grader that rejects a correct answer.
        broken = runner.run(models.GoldModel(self.cases), self.conn, self.cases, self.schema)
        broken[0].verdict = Verdict.DIFF_VALUES.value
        problems = report.check_controls([("gold", broken)])
        self.assertEqual(len(problems), 1)
        self.assertIn("grader", problems[0])

    def test_a_model_that_writes_bad_sql_is_recorded_not_raised(self):
        class Broken(models.Model):
            name = "broken"

            def generate(self, question, schema, indicators=""):
                return models.Generation(sql="SELECT nope FROM nowhere")

        attempts = runner.run(Broken(), self.conn, self.cases, self.schema)
        self.assertTrue(all(a.verdict == Verdict.ERROR.value for a in attempts))
        self.assertFalse(any(a.ok for a in attempts))

    def test_a_model_that_wraps_sql_in_markdown_is_still_graded(self):
        gold = {c.question: c.gold_sql for c in self.cases}

        class Fenced(models.Model):
            name = "fenced"

            def generate(self, question, schema, indicators=""):
                return models.Generation(
                    sql="Here is the query:\n```sql\n%s\n```" % gold[question])

        attempts = runner.run(Fenced(), self.conn, self.cases, self.schema)
        self.assertTrue(all(a.ok for a in attempts))

    def test_by_tag_breaks_results_down(self):
        attempts = runner.run(models.GoldModel(self.cases), self.conn, self.cases, self.schema)
        tags = report.by_tag(attempts)
        self.assertEqual(tags["aggregate"]["n"], 1)
        self.assertEqual(tags["aggregate"]["accuracy"], 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
