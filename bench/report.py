"""Turn a run into something you can act on.

The headline number is the least useful thing here. What tells you where to
spend effort is the breakdown: which kinds of question fail, and which kind of
wrong the failures are.
"""

from collections import Counter, defaultdict

from .compare import Verdict


def summarise(attempts):
    correct = sum(1 for a in attempts if a.ok)
    verdicts = Counter(a.verdict for a in attempts)
    return {
        "cases": len(attempts),
        "correct": correct,
        "accuracy": correct / len(attempts) if attempts else 0.0,
        "verdicts": dict(verdicts.most_common()),
        "seconds": sum(a.seconds or 0 for a in attempts),
        "input_tokens": sum(a.input_tokens or 0 for a in attempts),
        "output_tokens": sum(a.output_tokens or 0 for a in attempts),
        "cached_tokens": sum(a.cached_tokens or 0 for a in attempts),
    }


def by_tag(attempts):
    """Accuracy per tag. A case counts once for each tag it carries."""
    totals, correct = defaultdict(int), defaultdict(int)
    for attempt in attempts:
        for tag in attempt.tags or ():
            totals[tag] += 1
            correct[tag] += 1 if attempt.ok else 0
    return {
        tag: {"n": totals[tag], "correct": correct[tag], "accuracy": correct[tag] / totals[tag]}
        for tag in sorted(totals, key=lambda t: (correct[t] / totals[t], -totals[t]))
    }


def text(model_name, attempts, show_failures=8):
    summary = summarise(attempts)
    lines = []
    add = lines.append

    add("model      %s" % model_name)
    add("execution  %d/%d correct (%.0f%%)" % (
        summary["correct"], summary["cases"], 100 * summary["accuracy"]))
    if summary["seconds"]:
        add("time       %.1fs total, %.2fs per question" % (
            summary["seconds"], summary["seconds"] / max(1, summary["cases"])))
    if summary["input_tokens"] or summary["output_tokens"]:
        add("tokens     %d in, %d out, %d read from cache" % (
            summary["input_tokens"], summary["output_tokens"], summary["cached_tokens"]))
    add("")

    add("How the grader decided")
    add("-" * 66)
    for verdict, count in summary["verdicts"].items():
        marker = "correct" if Verdict(verdict).is_match else "wrong"
        add("  %-20s %4d   %s" % (verdict, count, marker))
    add("")

    tags = by_tag(attempts)
    if tags:
        add("Accuracy by question type (weakest first)")
        add("-" * 66)
        for tag, stat in tags.items():
            bar = "#" * int(round(stat["accuracy"] * 20))
            add("  %-16s %2d/%-2d %4.0f%%  %s" % (
                tag, stat["correct"], stat["n"], 100 * stat["accuracy"], bar))
        add("")

    failures = [a for a in attempts if not a.ok]
    if failures:
        add("Failures (%d)" % len(failures))
        add("-" * 66)
        for attempt in failures[:show_failures]:
            add("  %-8s %s" % (attempt.case_id, attempt.question[:60]))
            add("           %s -- %s" % (attempt.verdict, (attempt.detail or "")[:90]))
            if attempt.sql:
                add("           %s" % attempt.sql[:110])
            add("")
        if len(failures) > show_failures:
            add("  ... and %d more (see the saved JSON)" % (len(failures) - show_failures))

    return "\n".join(lines)


def compare_runs(runs):
    """A table across several runs: model, accuracy, and the failure profile."""
    lines = []
    add = lines.append
    add("%-26s %6s %8s %9s %9s %8s" % (
        "model", "cases", "correct", "accuracy", "errors", "time/q"))
    add("-" * 74)
    for name, attempts in runs:
        summary = summarise(attempts)
        errors = summary["verdicts"].get(Verdict.ERROR.value, 0)
        per_question = summary["seconds"] / max(1, summary["cases"])
        add("%-26s %6d %8d %8.0f%% %9d %7.2fs" % (
            name[:26], summary["cases"], summary["correct"],
            100 * summary["accuracy"], errors, per_question))
    return "\n".join(lines)


def check_controls(runs):
    """Warn if the controls did not behave. Bad controls invalidate the run."""
    problems = []
    for name, attempts in runs:
        summary = summarise(attempts)
        if name == "gold" and summary["accuracy"] < 1.0:
            problems.append(
                "the gold control scored %.0f%%, not 100%% -- the grader is rejecting "
                "correct answers, so every other score in this run is understated"
                % (100 * summary["accuracy"]))
        if name == "constant" and summary["accuracy"] > 0.05:
            problems.append(
                "the constant control scored %.0f%% by answering 'SELECT 1' to "
                "everything -- some cases are giving away free points"
                % (100 * summary["accuracy"]))
    return problems
