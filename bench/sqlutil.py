"""Small amount of SQL inspection, done on a stripped copy of the text.

Nothing here parses SQL properly. It answers two narrow questions -- does this
query request an ordering, and is it a single read-only statement -- and it
answers them after removing comments and string literals, so that an ORDER BY
inside a quoted string or a `-- comment` cannot be mistaken for the real thing.

The read-only check is defence in depth, not the defence. Candidate SQL runs on
a connection SQLite itself opened read-only (see db.connect_readonly); this
exists to reject obvious nonsense early with a clear message.
"""

import re

_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_STRING = re.compile(r"'(?:''|[^'])*'")
_DQUOTED = re.compile(r'"(?:""|[^"])*"')

_WRITE_KEYWORDS = {
    "insert", "update", "delete", "drop", "alter", "create", "replace",
    "attach", "detach", "pragma", "vacuum", "reindex", "truncate", "grant",
}


def strip_noise(sql):
    """Remove comments and quoted text, keeping the structural characters."""
    text = _BLOCK_COMMENT.sub(" ", sql)
    text = _LINE_COMMENT.sub(" ", text)
    text = _STRING.sub("''", text)
    text = _DQUOTED.sub('""', text)
    return text


def has_top_level_order_by(sql):
    """True when the query itself asks for an ordering.

    An ORDER BY inside a subquery, a CTE, or a window function's OVER (...) is
    nested and does not order the result the caller sees, so only depth zero
    counts.
    """
    text = strip_noise(sql)
    depth = 0
    for match in re.finditer(r"[()]|\border\s+by\b", text, re.IGNORECASE):
        token = match.group(0)
        if token == "(":
            depth += 1
        elif token == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            return True
    return False


def has_limit_without_order(sql):
    """LIMIT with no ordering picks arbitrary rows -- an unstable gold answer."""
    text = strip_noise(sql)
    return bool(re.search(r"\blimit\b", text, re.IGNORECASE)) and not has_top_level_order_by(sql)


#: Functions whose value is not fixed by the data. A gold answer built on one of
#: these is not a gold answer: RANDOM() changes between runs, and the 'now' family
#: changes between days -- so a benchmark using it would quietly start reporting
#: different scores for the same model.
_NONDETERMINISTIC = re.compile(
    r"\b(random|randomblob|current_time|current_date|current_timestamp)\b|"
    r"'now'", re.IGNORECASE)


def strip_comments(sql):
    """Remove comments but keep string literals, which matter here ('now')."""
    return _LINE_COMMENT.sub(" ", _BLOCK_COMMENT.sub(" ", sql))


def nondeterministic_parts(sql):
    """Names of non-deterministic constructs used by the query, if any.

    Cheaper and more reliable than running the query twice and hoping the
    difference shows: on a small table `ORDER BY RANDOM() LIMIT 1` returns the
    same row by coincidence often enough to pass a repeat check.
    """
    found = {m.group(0).lower() for m in _NONDETERMINISTIC.finditer(strip_comments(sql))}
    return sorted(found)


def statements(sql):
    """Split on top-level semicolons, discarding empties."""
    text = strip_noise(sql)
    parts, depth, start = [], 0, 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == ";" and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    parts.append(text[start:])
    return [p.strip() for p in parts if p.strip()]


def is_single_read_only_statement(sql):
    """(ok, reason). Rejects multiple statements and anything that writes."""
    if not sql or not sql.strip():
        return False, "empty query"

    parts = statements(sql)
    if len(parts) > 1:
        return False, "%d statements; expected one" % len(parts)

    first = re.match(r"\s*([a-zA-Z]+)", parts[0])
    if not first:
        return False, "no leading keyword"
    keyword = first.group(1).lower()
    if keyword not in {"select", "with"}:
        return False, "starts with %s; only SELECT or WITH is allowed" % keyword.upper()

    for word in re.findall(r"\b[a-zA-Z]+\b", parts[0]):
        if word.lower() in _WRITE_KEYWORDS:
            return False, "contains %s" % word.upper()
    return True, ""


def extract_sql(text):
    """Pull a query out of a model's reply.

    Models wrap SQL in ```sql fences, prefix it with commentary, or do both.
    Refusing to handle that would measure formatting compliance rather than
    whether the model can write the query.
    """
    if not text:
        return ""
    fenced = re.search(r"```(?:sql)?\s*(.+?)```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        return fenced.group(1).strip()

    # No fence: take from the first SELECT or WITH onwards.
    match = re.search(r"\b(select|with)\b", text, re.IGNORECASE)
    if match:
        return text[match.start():].strip()
    return text.strip()
