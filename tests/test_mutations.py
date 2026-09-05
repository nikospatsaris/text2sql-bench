"""Mutation tests for the grader.

The other tests check the comparator on hand-built row lists. These check it end
to end on real SQL: take a correct query, introduce one specific mistake of the
kind a model actually makes, and require the grader to name that mistake rather
than shrug and say "wrong".

A verdict taxonomy that cannot distinguish these is decoration. The point of
`diff_null_vs_zero` existing at all is that someone reading a report can tell a
left-join mistake from an arbitrary wrong answer without opening the SQL.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bench import db, sqlutil
from bench.compare import Verdict, compare


def _tiny_db():
    conn = db.init(db.connect(":memory:"))
    conn.execute("INSERT INTO regions VALUES ('EUR', 'Europe')")
    conn.execute("INSERT INTO income_levels VALUES ('HIC', 'High income')")
    conn.executemany("INSERT INTO economies VALUES (?,?,?,?,?,?,?,?,?)", [
        ("GRC", "GR", "Greece", "EUR", "HIC", "Athens", 37.9, 23.7, 1),
        ("ESP", "ES", "Spain", "EUR", "HIC", "Madrid", 40.4, -3.7, 1),
        ("PRT", "PT", "Portugal", "EUR", "HIC", None, 38.7, -9.1, 1),
        ("WLD", "1W", "World", None, None, None, None, None, 0),
    ])
    conn.execute("INSERT INTO indicators VALUES ('SP.POP.TOTL', 'Population', 'population')")
    # Portugal has no observation, so left-join mistakes have something to hit.
    conn.executemany("INSERT INTO observations VALUES (?,'SP.POP.TOTL',2020,?)",
                     [("GRC", 10.7), ("ESP", 47.4), ("WLD", 7800.0)])
    conn.commit()
    return conn


class TestMutations(unittest.TestCase):
    def setUp(self):
        self.conn = _tiny_db()

    def tearDown(self):
        self.conn.close()

    def _verdict(self, gold_sql, mutated_sql):
        self.assertNotEqual(gold_sql, mutated_sql, "the mutation changed nothing")
        gold = [tuple(r) for r in self.conn.execute(gold_sql)]
        candidate = [tuple(r) for r in self.conn.execute(mutated_sql)]
        return compare(gold, candidate,
                       ordered=sqlutil.has_top_level_order_by(gold_sql)).verdict

    def test_counting_the_aggregates_is_caught(self):
        # The signature mistake on this schema: summing 'World' along with the
        # countries. Here it turns 58 into 7858.
        gold = ("SELECT SUM(o.value) FROM observations o JOIN economies e ON e.iso3 = o.iso3 "
                "WHERE e.is_country = 1 AND o.indicator_id = 'SP.POP.TOTL' AND o.year = 2020")
        mutated = gold.replace("e.is_country = 1 AND ", "")
        self.assertIs(self._verdict(gold, mutated), Verdict.DIFF_VALUES)

    def test_dropping_a_requested_ordering_is_caught(self):
        gold = "SELECT name FROM economies WHERE is_country = 1 ORDER BY name DESC"
        mutated = "SELECT name FROM economies WHERE is_country = 1 ORDER BY name ASC"
        self.assertIs(self._verdict(gold, mutated), Verdict.DIFF_ORDER)

    def test_an_inner_join_where_a_left_join_was_needed_is_caught(self):
        gold = ("SELECT e.name, o.value FROM economies e LEFT JOIN observations o "
                "ON o.iso3 = e.iso3 AND o.year = 2020 WHERE e.is_country = 1 ORDER BY e.name")
        mutated = gold.replace("LEFT JOIN", "JOIN")
        # Portugal disappears entirely.
        self.assertIs(self._verdict(gold, mutated), Verdict.DIFF_ROW_COUNT)

    def test_filling_missing_data_with_zero_is_named_specifically(self):
        # The row count is right and only the NULL is wrong, so this must be
        # distinguishable from an arbitrary bad answer.
        gold = ("SELECT e.name, o.value FROM economies e LEFT JOIN observations o "
                "ON o.iso3 = e.iso3 AND o.year = 2020 WHERE e.is_country = 1 ORDER BY e.name")
        mutated = gold.replace("SELECT e.name, o.value", "SELECT e.name, COALESCE(o.value, 0)")
        self.assertIs(self._verdict(gold, mutated), Verdict.DIFF_NULL_VS_ZERO)

    def test_an_extra_column_is_caught(self):
        gold = "SELECT name FROM economies WHERE is_country = 1 ORDER BY name"
        mutated = "SELECT name, iso3 FROM economies WHERE is_country = 1 ORDER BY name"
        self.assertIs(self._verdict(gold, mutated), Verdict.DIFF_ARITY)

    def test_a_stray_distinct_that_collapses_rows_is_caught(self):
        gold = "SELECT region_id FROM economies WHERE is_country = 1"
        mutated = "SELECT DISTINCT region_id FROM economies WHERE is_country = 1"
        self.assertIs(self._verdict(gold, mutated), Verdict.DIFF_DUPLICATES)

    def test_a_wrong_filter_value_is_caught(self):
        gold = ("SELECT COUNT(*) FROM observations o JOIN economies e ON e.iso3 = o.iso3 "
                "WHERE e.is_country = 1 AND o.value > 20")
        mutated = gold.replace("o.value > 20", "o.value > 5")
        self.assertIs(self._verdict(gold, mutated), Verdict.DIFF_VALUES)


class TestKnownLimitOfExecutionAccuracy(unittest.TestCase):
    """Documents where this grading method cannot help, with a live example.

    Execution accuracy asks whether the answer came out right on this data. A
    query that is wrong in general can still be right here, and no amount of
    care in the comparator changes that -- it is a property of the method. The
    honest response is to know where the blind spot is, not to claim it away.
    """

    def setUp(self):
        self.conn = _tiny_db()

    def tearDown(self):
        self.conn.close()

    def test_a_boundary_error_is_invisible_when_no_row_sits_on_the_boundary(self):
        # `> 10.7` and `>= 10.7` differ in meaning. On data where nothing equals
        # the boundary they differ in nothing, and both are scored correct.
        gold = "SELECT COUNT(*) FROM observations WHERE value > 47.4"
        mutated = "SELECT COUNT(*) FROM observations WHERE value >= 47.5"
        gold_rows = [tuple(r) for r in self.conn.execute(gold)]
        rows = [tuple(r) for r in self.conn.execute(mutated)]
        self.assertTrue(compare(gold_rows, rows, ordered=False).is_match)

    def test_a_boundary_error_is_caught_when_a_row_does_sit_on_it(self):
        # Which is the argument for choosing thresholds that land on real values
        # when writing cases.
        gold = "SELECT COUNT(*) FROM observations WHERE value > 47.4"
        mutated = "SELECT COUNT(*) FROM observations WHERE value >= 47.4"
        gold_rows = [tuple(r) for r in self.conn.execute(gold)]
        rows = [tuple(r) for r in self.conn.execute(mutated)]
        self.assertFalse(compare(gold_rows, rows, ordered=False).is_match)


if __name__ == "__main__":
    unittest.main(verbosity=2)
