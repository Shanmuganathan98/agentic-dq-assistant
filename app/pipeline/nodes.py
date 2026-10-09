"""One function per agent. Each takes the state, mutates it, and returns a one-line summary for the trace."""
from app import llm
from app.pipeline.state import PipelineState
from app.tools import profiling
from app.tools import rules as R
from app.tools.validation import validate_cleaning

FIX_WORDS = ("clean", "fix", "prepare", "load", "transform", "correct", "standardi")


def orchestrator_plan(state: PipelineState) -> str:
    """Decide whether modification is wanted. LLM if available, else keyword rules.
    Either way a read-only goal never reaches the cleaning step, and writes still need approval."""
    decision = llm.plan_fix(state.goal)
    source = "llm" if decision is not None else "rules"
    state.plan_fix = decision if decision is not None else any(w in state.goal.lower() for w in FIX_WORDS)
    return (f"plan ({source}): profile -> dq -> " + ("clean -> validate -> " if state.plan_fix else "") +
            f"report  [{len(state.rules)} {state.rules_source} rules]")


def profile_node(state: PipelineState) -> str:
    state.profile = profiling.profile_table(state.df)
    return f"{state.profile['row_count']} rows, {state.profile['column_count']} columns"


def dq_node(state: PipelineState) -> str:
    state.checks = R.run_checks(state.df, state.rules)
    state.issues = [c for c in state.checks if c["count"] > 0]
    n = lambda a: sum(1 for i in state.issues if i["action"] == a)
    return (f"{len(state.rules)} rules checked, {len(state.issues)} violated "
            f"({n('fix')} auto-fix, {n('ask')} need a decision, {n('flag')} report-only)")


def clean_node(state: PipelineState) -> str:
    state.attempts += 1
    clean, changes = R.apply_fixes(state.df, state.rules)
    inject = state.fault == "always" or (state.fault == "once" and state.attempts == 1)
    victim_col = None
    if inject:   # simulated bug: silently blanks 5 values in the first column no rule touches, with no log entry
        fixed_cols = {r["columns"][0] for r in state.rules if r["action"] == "fix"}
        for c in state.df.columns:
            if c not in fixed_cols and state.df[c].notna().any():
                victim_col = c
                break
        if victim_col:
            victims = clean.index[clean[victim_col].notna()][:5]
            R.assign(clean, victim_col, {i: None for i in victims})
    state.clean_df, state.changes = clean, changes
    return (f"attempt {state.attempts}: {len(changes)} logged changes" +
            (f" (fault injected into '{victim_col}')" if victim_col else ""))


def validate_node(state: PipelineState) -> str:
    before = {c["rule_id"]: c["count"] for c in state.checks}
    state.validation = validate_cleaning(state.df, state.clean_df, state.changes, state.rules, before)
    failed = [c["name"] for c in state.validation["checks"] if not c["passed"]]
    return "PASSED" if state.validation["passed"] else "FAILED: " + ", ".join(failed)


def report_node(state: PipelineState) -> str:
    validated = state.plan_fix and state.validation.get("passed", False)
    asks = [i for i in state.issues if i["action"] == "ask"]
    flags = [i for i in state.issues if i["action"] == "flag"]
    if not state.plan_fix:
        status = "analysis_only"
    elif not validated:
        status = "failed_validation"
    else:
        status = "completed_needs_approval" if asks else "completed"
    fixes = {}
    if validated and len(state.changes):
        fixes = {str(k): int(v) for k, v in state.changes.groupby("column").size().items()}
    state.report = {
        "status": status,
        "safe_to_load": bool(validated),
        "goal": state.goal,
        "rows": len(state.df),
        "columns": list(state.df.columns),
        "rules": state.rules,
        "rules_source": state.rules_source,
        "checks": state.checks,
        "issues_found": {i["name"]: i["count"] for i in state.issues},
        "fixes_applied": fixes,
        "validation": state.validation,
        "attempts": state.attempts,
        "needs_human_approval": [f'{i["name"]} ({i["count"]}): {i["proposed_fix"]}' for i in asks],
        "reported_only": [f'{i["name"]} ({i["count"]})' for i in flags],
        "required_resolutions": ([{"rule_id": i["rule_id"], "rule_key": i["rule_key"], "name": i["name"],
                                   "column": i["column"], "type": i["type"], "count": i["count"],
                                   "options": i["resolution_options"]} for i in asks] if validated else []),
        "profile": state.profile,
        "trace": state.trace,
    }
    return f"status={status}"
