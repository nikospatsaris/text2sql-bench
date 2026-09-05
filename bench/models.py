"""Things that turn a question into SQL.

Two of them need no network and no API key, and they exist to check the
benchmark rather than to compete in it:

  * `gold` returns the reference query. It must score 100%. If it does not, the
    grader is broken, and every other number in the run is meaningless.
  * `constant` returns `SELECT 1` for every question. It must score ~0%. If it
    scores higher, the case set is handing out free points -- usually a question
    whose gold answer is empty or trivially small.

Running both on every report costs nothing and pins the scale at each end. A
benchmark with no upper and lower control is a number with no units.

The Claude adapter uses the official SDK, imported inside the constructor so
that the harness itself keeps its zero-dependency install: you only need
`pip install anthropic` if you actually intend to call Claude.
"""

import os
import time

SYSTEM_PROMPT = """You translate questions into SQLite queries.

You will be given the schema of a database built from World Bank open data.
Answer with a single SQLite SELECT statement and nothing else. No explanation,
no commentary. Do not wrap the query in markdown fences.

Rules that matter for this schema:
- `economies` contains both countries and aggregates such as 'World' and
  'Euro area'. Rows where is_country = 0 are aggregates.
- `observations` holds only measured values. A missing measurement is an absent
  row, not a row with NULL.
- Return exactly the columns the question asks for, in the order asked.
- Add ORDER BY only when the question asks for an ordering.

Schema:
{schema}

Indicator ids present in this database (use only these):
{indicators}"""


class Generation:
    """One attempt at one question."""

    __slots__ = ("sql", "raw", "seconds", "input_tokens", "output_tokens",
                 "cached_tokens", "error")

    def __init__(self, sql="", raw="", seconds=0.0, input_tokens=0,
                 output_tokens=0, cached_tokens=0, error=None):
        self.sql = sql
        self.raw = raw
        self.seconds = seconds
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cached_tokens = cached_tokens
        self.error = error


class Model:
    name = "model"
    #: False for adapters that cannot be a fair measurement of anything.
    is_control = False

    def generate(self, question, schema, indicators=""):
        raise NotImplementedError


class GoldModel(Model):
    """Returns the reference SQL. The upper control: must score 100%."""

    name = "gold"
    is_control = True

    def __init__(self, cases):
        self._by_question = {c.question: c.gold_sql for c in cases}

    def generate(self, question, schema, indicators=""):
        sql = self._by_question.get(question)
        if sql is None:
            return Generation(error="no gold SQL for this question")
        return Generation(sql=sql, raw=sql)


class ConstantModel(Model):
    """Returns the same trivial query every time. The lower control: must score ~0%."""

    name = "constant"
    is_control = True

    def generate(self, question, schema, indicators=""):
        return Generation(sql="SELECT 1", raw="SELECT 1")


class ClaudeModel(Model):
    """Claude via the Anthropic SDK.

    Deliberately does not enable server-side refusal fallbacks. They are
    recommended for production, where transparently completing the request is
    the point -- but here a fallback would answer some questions on a different
    model and report the total under one name, which is exactly the thing a
    benchmark exists to prevent. A refusal is recorded as a failure of the model
    under test.
    """

    def __init__(self, model="claude-opus-5", effort="high", max_tokens=2000,
                 api_key=None, cache_schema=True):
        try:
            import anthropic
        except ImportError as exc:                      # pragma: no cover
            raise RuntimeError(
                "the Claude adapter needs the Anthropic SDK: pip install anthropic") from exc

        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
        self._anthropic = anthropic
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.cache_schema = cache_schema
        self.name = "%s/%s" % (model, effort)

    def generate(self, question, schema, indicators=""):
        system_text = SYSTEM_PROMPT.format(schema=schema, indicators=indicators or "  (none)")
        # The schema is identical for all 40 questions, so it is the obvious
        # thing to cache. Whether it actually caches depends on the model's
        # minimum cacheable prefix, so the runner records cache reads rather
        # than assuming a saving happened.
        system = [{"type": "text", "text": system_text}]
        if self.cache_schema:
            system[0]["cache_control"] = {"type": "ephemeral"}

        started = time.monotonic()
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                output_config={"effort": self.effort},
                messages=[{"role": "user", "content": question}],
            )
        except Exception as exc:                        # noqa: BLE001 - any failure is a result
            return Generation(seconds=time.monotonic() - started,
                              error="%s: %s" % (type(exc).__name__, exc))

        elapsed = time.monotonic() - started

        if response.stop_reason == "refusal":
            detail = getattr(response, "stop_details", None)
            return Generation(seconds=elapsed, error="refused (%s)" % (
                getattr(detail, "category", None) or "no category"))

        text = "".join(b.text for b in response.content if b.type == "text")
        usage = response.usage
        return Generation(
            sql=text.strip(),
            raw=text,
            seconds=elapsed,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cached_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        )


def build(spec, cases=None):
    """Build a model from a command-line spec.

    'gold', 'constant', or 'claude[:model][:effort]' -- for example
    claude:claude-opus-5:low
    """
    parts = spec.split(":")
    kind = parts[0].lower()

    if kind == "gold":
        if cases is None:
            raise ValueError("the gold control needs the case list")
        return GoldModel(cases)
    if kind == "constant":
        return ConstantModel()
    if kind == "claude":
        model = parts[1] if len(parts) > 1 and parts[1] else "claude-opus-5"
        effort = parts[2] if len(parts) > 2 and parts[2] else "high"
        return ClaudeModel(model=model, effort=effort)
    raise ValueError("unknown model spec %r (try gold, constant, claude)" % spec)
