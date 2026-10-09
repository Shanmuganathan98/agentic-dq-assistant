"""Independent validation of a cleaning run, for any dataset and any rule set.

Unlike re-running the rules that found the problems, this reconciles the cleaned data against the change log:
every cell that differs from the original must be logged, and every logged change must match the data.
"""
import pandas as pd

from app.tools import rules as R


def _same(a, b) -> bool:
    return (pd.isna(a) and pd.isna(b)) or a == b


def changed_cells(before: pd.DataFrame, after: pd.DataFrame) -> set:
    out = set()
    for c in before.columns:
        neq = ~((before[c] == after[c]) | (before[c].isna() & after[c].isna()))
        out.update((int(i), c) for i in before.index[neq])
    return out


def validate_cleaning(before: pd.DataFrame, after: pd.DataFrame, changes: pd.DataFrame,
                      rules: list[dict], before_counts: dict) -> dict:
    """before_counts: {rule_id: violation count on the original data}."""
    checks = []

    def add(name, passed, detail=""):
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    add("row_count_preserved", len(before) == len(after) and before.index.equals(after.index),
        f"{len(before)} -> {len(after)}")
    if not checks[0]["passed"]:
        return {"passed": False, "checks": checks}

    a = after[list(before.columns)]
    changed = changed_cells(before, a)
    logged = {(int(r.row_index), r.column) for r in changes.itertuples()}

    unlogged = changed - logged
    add("no_unlogged_changes", not unlogged,
        f"{len(unlogged)} cells changed without a log entry, e.g. {sorted(unlogged)[:3]}" if unlogged else
        f"{len(changed)} changed cells, all logged")
    phantom = logged - changed
    add("no_phantom_log_entries", not phantom,
        f"{len(phantom)} logged changes not applied" if phantom else "")
    mismatched = [r for r in changes.itertuples() if not _same(after.at[r.row_index, r.column], r.new)]
    add("log_matches_data", not mismatched,
        f"{len(mismatched)} log rows disagree with data" if mismatched else "")

    now = {x["rule_id"]: x["count"] for x in R.run_checks(a, rules)}
    fix_ids = [r["id"] for r in rules if r["action"] == "fix"]
    left = {i: now[i] for i in fix_ids if now[i]}
    add("fix_rules_resolved", not left,
        f"still violated: {left}" if left else f"{len(fix_ids)} auto-fix rule(s) now clean")

    touched = {c for _, c in logged}
    worse = {}
    for r in rules:
        if r["action"] == "fix" or touched & set(r["columns"]):
            continue                     # fixes legitimately change these columns (e.g. NULLs raise not_null counts)
        if now[r["id"]] > before_counts.get(r["id"], 0):
            worse[r["id"]] = (before_counts.get(r["id"], 0), now[r["id"]])
    add("untouched_rules_not_worsened", not worse, f"increased: {worse}" if worse else "")
    return {"passed": all(c["passed"] for c in checks), "checks": checks}
