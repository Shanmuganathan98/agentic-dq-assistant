"""Phase 1: one agent, real tools, structured output.

The LLM chooses which tools to call and writes the report. The tools do all counting,
so numbers in the report come from code, not from the model's imagination.
Rules come from the user; with none given, report-only rules are suggested from the data.
"""
import json
import os

import pandas as pd

from app.schemas import AgentResult, Finding, KeyArgs, NoArgs, Report
from app.tools import profiling
from app.tools import rules as R

MODEL = os.getenv("DQ_MODEL", "claude-sonnet-5-5")
MAX_STEPS = 8

SYSTEM = (
    "You are a data-quality analyst. Use the tools to profile the dataset and run the data-quality rules. "
    "Never state a number you did not get from a tool. Classify each fix as safe (deterministic, "
    "reversible) or needs_human_approval (lossy, ambiguous, or destructive). "
    "When finished, call submit_report exactly once."
)


def _rules_for(df: pd.DataFrame, rules):
    return R.validate_rules(rules or R.suggest_rules(df), df.columns, df)


def _tools(df: pd.DataFrame, rules: list[dict]) -> dict:
    """name -> (description, args model, callable(**args))"""
    return {
        "profile_table": ("Schema, dtypes, null counts, distinct counts, row count.", NoArgs,
                          lambda: profiling.profile_table(df)),
        "check_nulls": ("Null counts per column (only columns with nulls).", NoArgs,
                        lambda: profiling.check_nulls(df)),
        "check_duplicates": ("Duplicate rows for a key column.", KeyArgs,
                             lambda key: profiling.check_duplicates(df, key)),
        "run_dq_rules": ("Run the data-quality rules; returns one result per rule with counts and proposed fixes.",
                         NoArgs, lambda: R.run_checks(df, rules)),
    }


def _specs(tools: dict) -> list[dict]:
    specs = [{"name": n, "description": d, "input_schema": m.model_json_schema()} for n, (d, m, _) in tools.items()]
    specs.append({"name": "submit_report", "description": "Submit the final structured report.",
                  "input_schema": Report.model_json_schema()})
    return specs


def run_deterministic(df: pd.DataFrame, rules=None) -> AgentResult:
    """No-API-key fallback, and a useful baseline to compare the LLM agent against."""
    rules = _rules_for(df, rules)
    issues = [c for c in R.run_checks(df, rules) if c["count"] > 0]
    report = Report(
        summary=f"{len(df)} rows analysed against {len(rules)} rules; {len(issues)} violated.",
        findings=[Finding(check=i["name"], column=i["column"], count=i["count"], severity=i["severity"]) for i in issues],
        safe_fixes=[f'{i["name"]} ({i["count"]}): {i["proposed_fix"]}' for i in issues if i["safe_to_autofix"]],
        needs_human_approval=[f'{i["name"]} ({i["count"]}): {i["proposed_fix"]}' for i in issues if not i["safe_to_autofix"]],
    )
    return AgentResult(report=report, tool_trace=[{"tool": "run_dq_rules", "args": {}}], mode="deterministic")


def run_agent(df: pd.DataFrame, goal: str, rules=None) -> AgentResult:
    if not os.getenv("ANTHROPIC_API_KEY"):
        return run_deterministic(df, rules)

    import anthropic  # imported lazily so the tools work without it
    rules = _rules_for(df, rules)
    tools = _tools(df, rules)
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": goal}]
    trace: list[dict] = []

    for _ in range(MAX_STEPS):
        resp = client.messages.create(model=MODEL, max_tokens=2000, system=SYSTEM,
                                      tools=_specs(tools), messages=messages)
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            if block.name == "submit_report":
                return AgentResult(report=Report.model_validate(block.input), tool_trace=trace, mode="llm")
            try:
                _, args_model, fn = tools[block.name]
                out = fn(**args_model.model_validate(block.input).model_dump())
            except Exception as e:  # feed errors back so the model can recover
                out = {"error": str(e)}
            trace.append({"tool": block.name, "args": block.input})
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": json.dumps(out, default=str)})
        if not results:
            break
        messages.append({"role": "user", "content": results})
    raise RuntimeError("Agent did not submit a report within the step limit")
