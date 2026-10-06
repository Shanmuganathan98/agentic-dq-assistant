"""Phase 1: one agent, real tools, structured output.

The LLM chooses which tools to call and writes the report. The tools do all counting,
so numbers in the report come from code, not from the model's imagination.
"""
import json
import os
import pandas as pd
from app.schemas import AgentResult, KeyArgs, NoArgs, Report, Finding
from app.tools import dq_checks, profiling

MODEL = os.getenv("DQ_MODEL", "claude-sonnet-5-5")
MAX_STEPS = 8

SYSTEM = (
    "You are a data-quality analyst. Use the tools to profile the dataset and find problems. "
    "Never state a number you did not get from a tool. Classify each fix as safe (deterministic, "
    "reversible) or needs_human_approval (lossy, ambiguous, or destructive). "
    "When finished, call submit_report exactly once."
)

# name -> (description, args model, callable(df, **args))
TOOLS = {
    "profile_table": ("Schema, dtypes, null counts, distinct counts, row count.", NoArgs,
                      lambda df: profiling.profile_table(df)),
    "check_nulls": ("Null counts per column (only columns with nulls).", NoArgs,
                    lambda df: profiling.check_nulls(df)),
    "check_duplicates": ("Duplicate rows for a key column.", KeyArgs,
                         lambda df, key: profiling.check_duplicates(df, key)),
    "run_dq_checks": ("Run format, validity and key checks; returns issues with proposed fixes.", KeyArgs,
                      lambda df, key: dq_checks.run_dq_checks(df, key)),
}


def _specs() -> list[dict]:
    specs = [{"name": n, "description": d, "input_schema": m.model_json_schema()} for n, (d, m, _) in TOOLS.items()]
    specs.append({"name": "submit_report", "description": "Submit the final structured report.",
                  "input_schema": Report.model_json_schema()})
    return specs


def run_deterministic(df: pd.DataFrame) -> AgentResult:
    """No-API-key fallback, and a useful baseline to compare the LLM agent against."""
    issues = dq_checks.run_dq_checks(df)
    report = Report(
        summary=f"{len(df)} rows analysed; {len(issues)} issue types found.",
        findings=[Finding(check=i["check"], column=i["column"], count=i["count"], severity=i["severity"]) for i in issues],
        safe_fixes=[f'{i["check"]} ({i["count"]}): {i["proposed_fix"]}' for i in issues if i["safe_to_autofix"]],
        needs_human_approval=[f'{i["check"]} ({i["count"]}): {i["proposed_fix"]}' for i in issues if not i["safe_to_autofix"]],
    )
    return AgentResult(report=report, tool_trace=[{"tool": "run_dq_checks", "args": {}}], mode="deterministic")


def run_agent(df: pd.DataFrame, goal: str) -> AgentResult:
    if not os.getenv("ANTHROPIC_API_KEY"):
        return run_deterministic(df)

    import anthropic  # imported lazily so the tools work without it
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": goal}]
    trace: list[dict] = []

    for _ in range(MAX_STEPS):
        resp = client.messages.create(model=MODEL, max_tokens=2000, system=SYSTEM,
                                      tools=_specs(), messages=messages)
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            if block.name == "submit_report":
                return AgentResult(report=Report.model_validate(block.input), tool_trace=trace, mode="llm")
            desc, args_model, fn = TOOLS[block.name]
            try:
                out = fn(df, **args_model.model_validate(block.input).model_dump())
            except Exception as e:  # feed errors back so the model can recover
                out = {"error": str(e)}
            trace.append({"tool": block.name, "args": block.input})
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": json.dumps(out, default=str)})
        if not results:
            break
        messages.append({"role": "user", "content": results})
    raise RuntimeError("Agent did not submit a report within the step limit")
