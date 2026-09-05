# text2sql-bench

An execution-accuracy benchmark for text-to-SQL, over a real database built from
World Bank open data.

It does not measure whether a model writes the *same* SQL as a reference answer.
It runs both queries and compares the results — which moves the hard part rather
than removing it, because deciding whether two result sets mean the same thing
is itself a pile of judgement calls. Those calls are the project.

No API key needed to run the harness. No dependencies outside the standard
library unless you point it at Claude.

```bash
python run.py build                  # fetch World Bank data into SQLite
python run.py cases                  # validate the 40 gold queries
python run.py eval --model gold      # upper control: must score 100%
python run.py eval --model constant  # lower control: must score 0%
python run.py eval --model claude:claude-opus-5:high
python run.py eval --model claude:claude-opus-5:high --columns prefix
```

## Why not compare the SQL text?

There are many correct spellings of one query. `JOIN ... ON` versus `USING`,
a CTE versus a subquery, `COUNT(*)` versus `COUNT(1)`, columns in either order
in the `WHERE` clause. String similarity scores all of that as partially wrong,
so it measures conformity to one author's style, not correctness.

Executing both queries and comparing results asks the only question that
matters: did it answer correctly? The cost is that "the same result" needs
defining, and every definition is a decision someone can disagree with.

## The grader is the project

`bench/compare.py` makes six calls, each reported as its own verdict so a run
shows how often the grader had to make one.

**Row order.** Undefined unless the query says `ORDER BY`. Two correct answers
can come back in different orders, so rows are compared as a multiset — unless
the gold query actually asked for an ordering, which is detected by looking for
a top-level `ORDER BY` on a copy of the SQL with comments and string literals
stripped. An `ORDER BY` inside a subquery, a CTE, or a window function's
`OVER (...)` does not order the result the caller sees, and does not count.

**Floating point.** The same sum accumulated in a different join order differs
in the last bits, so exact equality would fail correct answers. The tolerance is
`1e-9` relative, and it is deliberately tight: GDP values here reach `1e12`, so
a more typical `1e-6` would silently accept an answer off by a million dollars.
A test pins that, and it caught the loose value during development.

**NULL is not zero.** A `LEFT JOIN` that should produce `NULL` and a query that
produces `0` are different answers. This is reported as its own verdict rather
than folded into generic mismatches, because it is a specific and fixable
mistake — usually an inner join where a left join was needed.

**Whitespace.** The World Bank stores region names like `"Latin America &
Caribbean "`, with a trailing space. Failing a model for not reproducing that
would measure the data, not the model.

**Duplicates.** Multiset, not set. If the gold result contains a row twice and a
candidate returns it once, it answered a different question — usually via a
stray `DISTINCT`, or a join that fanned rows out. Reported separately.

**Integers and floats are the same number.** Whether `COUNT(*)` arrives as `5`
or `5.0` is a query-plan artefact, not part of the answer.

**How many columns — reported both ways.** *"Which regions contain more than 30
countries?"* does not say whether the count comes back too, and a model that
returns it has arguably answered better. `--columns strict` requires exactly the
expected columns; `--columns prefix` accepts extra trailing ones. Which is right
is a genuine disagreement in text-to-SQL evaluation, so both are reported rather
than one being chosen quietly — and a relaxed match is labelled `match_prefix`,
never `match_exact`, so a headline figure can't silently include it. Prefix mode
only ever trims from the right: returning the columns in a different order is
still wrong.

## The two controls

Every run can include two models that exist only to check the benchmark:

| control | what it does | must score |
|---|---|---|
| `gold` | returns the reference SQL | **100%** |
| `constant` | returns `SELECT 1` for every question | **~0%** |

If `gold` scores below 100%, the grader is rejecting correct answers and every
other score in the run is understated. If `constant` scores above zero, some
case is handing out free points. `report.check_controls()` prints a warning in
both cases.

This is the part I would build first again. A benchmark without controls is a
number with no units, and both of these run instantly and for free.

Measured on the current case set:

```
model      gold                       model      constant
execution  40/40 correct (100%)       execution  0/40 correct (0%)
  match_exact  40  correct              diff_row_count  15  wrong
                                        diff_values     14  wrong
                                        diff_arity      11  wrong
```

## Does the grader name the mistake?

A taxonomy that cannot tell one error from another is decoration. Each row below
is a real mutation applied to a gold query and run against the live database:

| mistake introduced | verdict | what the report shows |
|---|---|---|
| drops the `is_country = 1` filter | `diff_values` | expected 7,830,279,316 — got 83,953,441,637 |
| drops the requested `ORDER BY` | `diff_order` | the query asked for an order and got another |
| inner join where a left join was needed | `diff_row_count` | 217 rows expected, 187 returned |
| `COALESCE(value, 0)` over missing data | `diff_null_vs_zero` | agrees except where NULL was expected |
| selects one column too many | `diff_arity` | expected 1 columns, got 2 |
| wrong year in the filter | `diff_values` | expected 43 — got 48 |

The first row is the aggregate trap costing a factor of **10.7**: summing
population without excluding *World* and the regional aggregates gives 84
billion people. That is the kind of wrong answer that looks like a number.

Two further mutations were scored **correct**, and both are informative rather
than broken. A stray `DISTINCT` after a `GROUP BY` is genuinely a no-op. And
changing `> 80` to `>= 80` changes the query's meaning but not its result, because
no country has a life expectancy of exactly 80 — see *Limitations*.

## Measured results

Claude Opus 5 at `high` effort, 40 cases, two runs per mode:

| scoring | run 1 | run 2 |
|---|---|---|
| `--columns strict` | 36/40 (90%) | 36/40 (90%) |
| `--columns prefix` | 39/40 (98%) | **40/40 (100%)** |

Roughly 2.7s and well under a cent per question, with the schema served from
prompt cache (~38k cached tokens per run).

**The gap between the two columns is the finding.** Every strict failure was the
model returning the right answer plus the number it had ranked by — asked
*"which regions contain more than 30 countries"*, it returned the region and the
count. That is arguably a better answer than my gold query's. Under strict
scoring it is wrong; under prefix it is right. Reporting one number would have
hidden the whole question.

**Two identical runs disagreed.** The prefix runs scored 39 and 40. The strict
runs both scored 36 but failed on *different cases* — one on `t2_15`, the other
on `t2_11`. So a single run carries roughly ±1 case of noise, and a difference
of one or two cases between two configurations means nothing without repeats.
Any comparison here — effort levels, prompt variants — needs several runs before
it says anything.

**A prompt change removed a real failure.** In the first run the model answered
the CO2 question with `EN.ATM.CO2E.PC`, an indicator the World Bank has since
retired — a plausible, formerly-correct identifier recalled from training. It
had no way to know better: the prompt sent the schema, which describes table
structure, not the values in `indicators`. Listing the eleven indicator ids in
the prompt eliminated that failure. Enumerable key values are schema context,
and leaving them out was my bug, not the model's.

**The last failure is my question's fault.** `t2_09` asks which indicator has
the fewest observations. My gold returns the id; the model returned the
indicator's name. Both identify the same row. It is kept unfixed and tagged
`ambiguous`, as a live example of the limitation below rather than a claim the
benchmark is clean.

For context, before these fixes the same model scored **30/40 (75%)** strict.
Six of those ten failures were my benchmark's problems, not the model's.

## The data

`https://api.worldbank.org/v2/` — no key, no registration. 217 countries,
78 aggregates, 11 indicators, 2000–2023, about 64,000 observations.

Two schema decisions exist to make the questions interesting:

**Aggregates share a table with countries.** The API returns 295 "economies",
of which 78 are things like *World*, *Euro area* and *Arab World*. They are kept
in `economies` with `is_country = 0`, because that is exactly the trap a model
should have to notice: *"what was the total population of all countries in
2020"* is wrong by roughly a factor of five if it sums the World row too. Five
cases are tagged `aggregate_trap`.

**A missing measurement is an absent row, not a NULL.** That makes *"which
countries have no education spending data for 2020"* a real `NOT EXISTS`
question rather than a scan for nulls. Indicator coverage was chosen to vary:
population is complete, education spending is missing for a third of
country-years.

## The cases

40 questions in `cases/cases.jsonl`, tagged so failures can be grouped:

| tier | what it exercises |
|---|---|
| `t1_*` | lookups, filters, `IS NULL`, text matching |
| `t2_*` | joins, grouping, `HAVING`, ranking, aggregates |
| `t3_*` | CTEs, window functions, correlated subqueries, self-joins, `LEFT JOIN` semantics |

Every case is validated before it can score anything. A case is **rejected** if
its gold query fails to execute, writes, returns zero rows, uses `LIMIT` without
`ORDER BY`, mentions a non-deterministic construct (`RANDOM()`, `CURRENT_DATE`,
`'now'`), or returns different results across repeated runs.

That non-determinism check reads the SQL rather than relying on repeated
execution, and it exists because the execution check alone is unsound: on a
small table `ORDER BY RANDOM() LIMIT 1` returns the same row by coincidence
often enough to pass. A flaky test surfaced that. Worse than `RANDOM()` is
`DATE('now')` — it would pass every check today and silently change the
benchmark's answers tomorrow.

The zero-rows rule matters most. A question whose gold answer is empty is passed
by any query that also returns nothing — including a model that misunderstood
the question in a different direction — so it inflates every score it appears
in. Rejecting those is why `constant` scores a true zero.

## What a run tells you

The headline number is the least useful part. The report breaks results down by
tag, weakest first, and by verdict — so a run says *which kinds of question* the
model fails and *what kind of wrong* the failures are. A model at 70% that fails
mostly on `aggregate_trap` needs a schema description that explains the
aggregates; one that fails on `diff_order` needs a prompt that mentions ordering.
Those are different fixes, and the total on its own points at neither.

Every attempt is saved to `results/` with the generated SQL, so a failure can be
read rather than guessed at.

## Running against Claude

```bash
pip install anthropic
export ANTHROPIC_API_KEY=...
python run.py eval --model claude:claude-opus-5:high
python run.py eval --model claude:claude-opus-5:high --columns prefix
```

Effort is part of the model spec (`low`, `medium`, `high`, `xhigh`, `max`), which
makes "is higher effort worth it for this task" a question the harness can
answer by running the same 40 cases at each level.

Two deliberate choices in the adapter:

- **Server-side refusal fallbacks are off.** They are the right default in
  production, where completing the request is the point. Here a fallback would
  answer some questions on a different model and report the total under one
  name, which is the exact thing a benchmark exists to prevent. A refusal is
  recorded as a failure of the model under test.
- **The schema is sent as a cached system prompt**, since it is identical across
  all 40 questions — but the runner reports `cache_read_input_tokens` rather
  than assuming a saving, because whether a prefix that size actually caches
  depends on the model's minimum.

## Safety of executing generated SQL

Candidate SQL is written by a language model, so it runs on a connection SQLite
itself opened read-only (`file:...?mode=ro`), with a progress handler that
aborts runaway queries. There is also a text-level check that rejects multiple
statements and anything that is not a single `SELECT`/`WITH` — but that is
defence in depth, not the defence. A string check can always be phrased around;
a read-only connection cannot.

## Two bugs worth recording

**The loader silently discarded every row.** The country endpoint puts ISO-3 in
`country.id`; the observation endpoint puts **ISO-2** there and ISO-3 in
`countryiso3code`. My fallback chain preferred the wrong field, so all 70,000
observations were dropped as "unknown economy" and the load reported success
with an empty table. The fix was one line; the lesson was the other change —
`load.py` now raises when an indicator returns rows and stores none, because
that is a bug in the loader, not a fact about the world.

**The float tolerance was three orders of magnitude too loose.** `1e-6` relative
sounds strict until the data reaches `1e12`. A test written to express the
intent — "a million dollars apart is not the same answer" — failed against the
implementation, which is the only reason it was caught.

Both failures produced plausible-looking output rather than an error. That is
the recurring shape of bugs in this kind of work, and it is why the controls and
the case validator exist at all.

## Limitations

- **40 cases is small.** Per-tag numbers with `n = 1` are anecdotes. The
  structure supports more; the set does not have them yet.
- **One database, one dialect.** Results say nothing about schemas with
  hundreds of tables, or about dialects other than SQLite.
- **Gold answers encode one reading of each question.** Where a question is
  ambiguous, the gold query resolves it, and a model that read it the other way
  is scored wrong. The `note` field on a case records that where it matters.
- **Execution accuracy can reward a wrong query.** Changing `> 80` to `>= 80`
  changes what a query means, but not what it returns when no row sits exactly
  on the boundary — so the grader scores it correct. This is a property of
  execution-based grading, not something the comparator can fix. Two tests
  document it directly: one showing the blind spot, one showing the same
  mutation being caught once a row does land on the boundary. The practical
  response is to choose thresholds in cases that land on real values.

## Tests

```bash
python -m unittest discover -s tests -v
```

69 tests, all offline and hermetic — they build their own synthetic database and
never touch the network. They cover every judgement the grader makes, the
`ORDER BY` detection (including inside subqueries, window functions, string
literals and comments), the read-only guard, SQL extraction from fenced replies,
each way a case can be rejected, that the read-only connection genuinely refuses
writes, and that the two controls score 100% and 0%.

Eight cover the two column modes, including that prefix mode never rescues a
wrong answer, wrongly-ordered columns, or a result with too few columns. Nine
are mutation tests: they take a correct query, introduce one
specific realistic mistake, and assert the grader returns the matching verdict
rather than a generic mismatch. Those are the tests that would catch a
comparator change that quietly stopped distinguishing a left-join error from a
wrong answer.

## Layout

```
run.py                 CLI: build | schema | cases | eval
cases/cases.jsonl      the benchmark
bench/client.py        World Bank API client
bench/load.py          fetch and load
bench/schema.sql       the database
bench/db.py            connections, including the read-only one
bench/compare.py       result-set equivalence -- the core
bench/sqlutil.py       ORDER BY detection, query guard, SQL extraction
bench/cases.py         loading and validation
bench/models.py        gold, constant, and Claude adapters
bench/runner.py        execute, compare, record
bench/report.py        accuracy by tag and verdict
tests/                 offline test suite
```
