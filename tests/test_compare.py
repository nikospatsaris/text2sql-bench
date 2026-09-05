"""Tests for the grader.

If the comparator is wrong, every number the benchmark produces is wrong, and
wrong in a way that looks fine. So each judgement call it makes is pinned here:
what counts as the same answer, what does not, and which kind of difference it
should be able to name.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bench import sqlutil
from bench.compare import Verdict, compare


class TestMatching(unittest.TestCase):
    def test_identical_results_match(self):
        rows = [("Greece", 10.0), ("Spain", 20.0)]
        self.assertIs(compare(rows, rows, ordered=True).verdict, Verdict.MATCH_EXACT)

    def test_row_order_is_ignored_when_the_query_did_not_ask_for_one(self):
        gold = [("Greece",), ("Spain",)]
        candidate = [("Spain",), ("Greece",)]
        result = compare(gold, candidate, ordered=False)
        self.assertIs(result.verdict, Verdict.MATCH_UNORDERED)
        self.assertTrue(result.is_match)

    def test_row_order_matters_when_the_query_asked_for_one(self):
        gold = [("Greece",), ("Spain",)]
        candidate = [("Spain",), ("Greece",)]
        result = compare(gold, candidate, ordered=True)
        self.assertIs(result.verdict, Verdict.DIFF_ORDER)
        self.assertFalse(result.is_match)

    def test_integers_and_floats_are_the_same_number(self):
        # COUNT(*) coming back as 5 or 5.0 is a query-plan artefact.
        self.assertTrue(compare([(5,)], [(5.0,)], ordered=True).is_match)

    def test_floating_point_noise_is_tolerated(self):
        gold = [("a", 1234.5678901)]
        candidate = [("a", 1234.5678902)]
        result = compare(gold, candidate, ordered=True)
        self.assertIs(result.verdict, Verdict.MATCH_ROUNDED)

    def test_a_real_difference_is_not_tolerated(self):
        result = compare([("a", 100.0)], [("a", 101.0)], ordered=True)
        self.assertIs(result.verdict, Verdict.DIFF_VALUES)

    def test_tolerance_is_relative_so_large_numbers_are_not_waved_through(self):
        # 1e12 vs 1e12 + 1e6 is a millionth apart in relative terms but is a
        # difference of a million dollars in the answer.
        result = compare([(1e12,)], [(1e12 + 1e6,)], ordered=True)
        self.assertFalse(result.is_match)

    def test_trailing_whitespace_in_the_source_data_is_not_the_models_fault(self):
        # The World Bank stores "Latin America & Caribbean " with a trailing space.
        gold = [("Latin America & Caribbean ",)]
        candidate = [("Latin America & Caribbean",)]
        self.assertTrue(compare(gold, candidate, ordered=True).is_match)

    def test_unordered_match_still_respects_float_tolerance(self):
        gold = [("a", 1.0000000001), ("b", 2.0)]
        candidate = [("b", 2.0), ("a", 1.0)]
        self.assertIs(compare(gold, candidate, ordered=False).verdict, Verdict.MATCH_ROUNDED)


class TestNullHandling(unittest.TestCase):
    def test_null_is_not_zero(self):
        result = compare([("a", None)], [("a", 0)], ordered=True)
        self.assertFalse(result.is_match)
        self.assertIs(result.verdict, Verdict.DIFF_NULL_VS_ZERO)

    def test_null_versus_zero_is_named_separately_from_other_mistakes(self):
        # A left join answered as an inner join produces exactly this, and
        # calling it a generic value mismatch would hide the pattern.
        gold = [("a", None), ("b", 5.0)]
        candidate = [("a", 0.0), ("b", 5.0)]
        self.assertIs(compare(gold, candidate, ordered=True).verdict, Verdict.DIFF_NULL_VS_ZERO)

    def test_null_equals_null(self):
        self.assertTrue(compare([("a", None)], [("a", None)], ordered=True).is_match)

    def test_null_against_a_nonzero_value_is_an_ordinary_mismatch(self):
        self.assertIs(compare([("a", None)], [("a", 7.0)], ordered=True).verdict,
                      Verdict.DIFF_VALUES)


class TestShapeDifferences(unittest.TestCase):
    def test_extra_columns_are_reported_as_arity(self):
        result = compare([("Greece",)], [("Greece", 10.0)], ordered=True)
        self.assertIs(result.verdict, Verdict.DIFF_ARITY)
        self.assertIn("1 columns", result.detail)

    def test_row_count_difference_is_reported(self):
        result = compare([("a",), ("b",)], [("a",)], ordered=True)
        self.assertIs(result.verdict, Verdict.DIFF_ROW_COUNT)

    def test_a_stray_distinct_is_reported_as_duplicates(self):
        # Same distinct rows, wrong multiplicity: the model answered a
        # different question, and this names which one.
        gold = [("a",), ("a",), ("b",)]
        candidate = [("a",), ("b",)]
        self.assertIs(compare(gold, candidate, ordered=False).verdict, Verdict.DIFF_DUPLICATES)

    def test_duplicates_matter_even_when_the_row_count_matches(self):
        gold = [("a",), ("a",), ("b",)]
        candidate = [("a",), ("b",), ("b",)]
        self.assertIs(compare(gold, candidate, ordered=False).verdict, Verdict.DIFF_DUPLICATES)

    def test_two_empty_results_match(self):
        self.assertTrue(compare([], [], ordered=True).is_match)

    def test_empty_against_nonempty_does_not_match(self):
        self.assertFalse(compare([], [("a",)], ordered=True).is_match)


class TestOrderByDetection(unittest.TestCase):
    def test_a_plain_order_by_is_found(self):
        self.assertTrue(sqlutil.has_top_level_order_by("SELECT name FROM economies ORDER BY name"))

    def test_no_order_by_is_reported(self):
        self.assertFalse(sqlutil.has_top_level_order_by("SELECT name FROM economies"))

    def test_an_order_by_inside_a_subquery_does_not_order_the_result(self):
        sql = ("SELECT name FROM (SELECT name FROM economies ORDER BY iso3) "
               "WHERE name LIKE 'G%'")
        self.assertFalse(sqlutil.has_top_level_order_by(sql))

    def test_a_window_functions_order_by_does_not_count(self):
        sql = ("SELECT name, ROW_NUMBER() OVER (ORDER BY value DESC) AS rank "
               "FROM observations JOIN economies USING (iso3)")
        self.assertFalse(sqlutil.has_top_level_order_by(sql))

    def test_an_order_by_in_a_string_literal_is_not_an_order_by(self):
        self.assertFalse(sqlutil.has_top_level_order_by(
            "SELECT 'order by name' AS note FROM economies"))

    def test_an_order_by_in_a_comment_is_not_an_order_by(self):
        self.assertFalse(sqlutil.has_top_level_order_by(
            "SELECT name FROM economies -- order by name\n"))

    def test_limit_without_order_is_flagged_as_unstable(self):
        self.assertTrue(sqlutil.has_limit_without_order("SELECT name FROM economies LIMIT 5"))
        self.assertFalse(sqlutil.has_limit_without_order(
            "SELECT name FROM economies ORDER BY name LIMIT 5"))


class TestQueryGuard(unittest.TestCase):
    def test_a_select_is_allowed(self):
        ok, _ = sqlutil.is_single_read_only_statement("SELECT 1")
        self.assertTrue(ok)

    def test_a_cte_is_allowed(self):
        ok, _ = sqlutil.is_single_read_only_statement(
            "WITH x AS (SELECT 1 AS n) SELECT n FROM x")
        self.assertTrue(ok)

    def test_a_write_is_rejected(self):
        for query in ("DROP TABLE economies", "DELETE FROM observations",
                      "UPDATE economies SET name='x'"):
            ok, reason = sqlutil.is_single_read_only_statement(query)
            self.assertFalse(ok, query)
            self.assertTrue(reason)

    def test_a_write_smuggled_after_a_select_is_rejected(self):
        ok, reason = sqlutil.is_single_read_only_statement("SELECT 1; DROP TABLE economies")
        self.assertFalse(ok)
        self.assertIn("statements", reason)

    def test_the_word_select_inside_a_string_is_still_one_statement(self):
        ok, _ = sqlutil.is_single_read_only_statement(
            "SELECT 'a; b' AS text FROM economies")
        self.assertTrue(ok)


class TestSqlExtraction(unittest.TestCase):
    def test_a_fenced_block_is_unwrapped(self):
        reply = "Here you go:\n```sql\nSELECT 1\n```\nHope that helps."
        self.assertEqual(sqlutil.extract_sql(reply), "SELECT 1")

    def test_an_unlabelled_fence_works_too(self):
        self.assertEqual(sqlutil.extract_sql("```\nSELECT 2\n```"), "SELECT 2")

    def test_commentary_before_bare_sql_is_dropped(self):
        reply = "Sure. SELECT name FROM economies"
        self.assertEqual(sqlutil.extract_sql(reply), "SELECT name FROM economies")

    def test_plain_sql_is_returned_unchanged(self):
        self.assertEqual(sqlutil.extract_sql("SELECT 3"), "SELECT 3")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestColumnMode(unittest.TestCase):
    """Strict vs prefix column matching.

    The seven failures that prompted this were all the model returning the
    right answer plus the number it had ranked by. Whether that is correct is a
    real disagreement, so both readings are available -- but a relaxed match is
    labelled as one, so a headline figure can never quietly include it.
    """

    def test_extra_column_fails_under_strict(self):
        result = compare([("Greece",)], [("Greece", 10.0)], ordered=True,
                         column_mode="strict")
        self.assertIs(result.verdict, Verdict.DIFF_ARITY)

    def test_extra_column_passes_under_prefix(self):
        result = compare([("Greece",)], [("Greece", 10.0)], ordered=True,
                         column_mode="prefix")
        self.assertIs(result.verdict, Verdict.MATCH_PREFIX)
        self.assertTrue(result.is_match)

    def test_a_relaxed_match_is_never_labelled_exact(self):
        # Otherwise a run in prefix mode would look identical to a clean one.
        result = compare([("a",)], [("a", 1.0)], ordered=True, column_mode="prefix")
        self.assertIsNot(result.verdict, Verdict.MATCH_EXACT)
        self.assertIn("extra", result.detail)

    def test_prefix_does_not_rescue_wrong_leading_columns(self):
        # Returning (value, name) where (name,) was asked is not an over-answer,
        # it is the wrong column. Trimming must not paper over that.
        result = compare([("Greece",)], [(10.0, "Greece")], ordered=True,
                         column_mode="prefix")
        self.assertFalse(result.is_match)

    def test_prefix_does_not_rescue_a_wrong_answer_with_extra_columns(self):
        result = compare([("Greece",)], [("Spain", 10.0)], ordered=True,
                         column_mode="prefix")
        self.assertFalse(result.is_match)

    def test_too_few_columns_is_still_wrong_under_prefix(self):
        result = compare([("Greece", 10.0)], [("Greece",)], ordered=True,
                         column_mode="prefix")
        self.assertIs(result.verdict, Verdict.DIFF_ARITY)

    def test_prefix_still_respects_row_ordering(self):
        gold = [("a",), ("b",)]
        candidate = [("b", 2.0), ("a", 1.0)]
        self.assertFalse(compare(gold, candidate, ordered=True, column_mode="prefix").is_match)
        self.assertTrue(compare(gold, candidate, ordered=False, column_mode="prefix").is_match)

    def test_an_unknown_column_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            compare([("a",)], [("a",)], ordered=True, column_mode="loose")
