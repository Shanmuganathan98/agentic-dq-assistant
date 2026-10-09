"""Orchestration graph. Nodes are agents; routes are the orchestrator's decisions.

Written as plain Python so it runs anywhere. The structure (nodes + conditional edges + shared state)
maps one-to-one onto LangGraph's StateGraph (see graph_langgraph.py).
"""
import time

import pandas as pd

from app.pipeline import nodes
from app.pipeline.state import PipelineState
from app.tools import rules as R

END = "__end__"
NODES = {"profile": nodes.profile_node, "dq": nodes.dq_node, "clean": nodes.clean_node,
         "validate": nodes.validate_node, "report": nodes.report_node}


def route(current: str, s: PipelineState) -> str:
    """The orchestrator's decisions."""
    if current == "profile":
        return "dq"
    if current == "dq":
        return "clean" if s.plan_fix else "report"
    if current == "clean":
        return "validate"
    if current == "validate":
        if s.validation["passed"] or s.attempts >= s.max_attempts:
            return "report"          # success, or out of retries: escalate via the report
        return "clean"               # validation failed: retry the fix
    return END


def init_state(df: pd.DataFrame, goal: str, rules: list | None = None,
               fault: str | None = None, max_attempts: int = 2) -> PipelineState:
    """Validate the user's rules against the dataset's columns (raises RuleError listing every problem).
    No rules given -> report-only rules suggested from the data, so any dataset works out of the box."""
    if rules:
        validated, source = R.validate_rules(rules, df.columns, df), "user"
    else:
        validated, source = R.validate_rules(R.suggest_rules(df), df.columns, df), "suggested"
    s = PipelineState(df=df, goal=goal, rules=validated, rules_source=source, fault=fault, max_attempts=max_attempts)
    s.trace.append({"step": 0, "node": "orchestrator", "summary": nodes.orchestrator_plan(s)})
    return s


def run_pipeline(df: pd.DataFrame, goal: str, rules: list | None = None,
                 fault: str | None = None, max_attempts: int = 2) -> PipelineState:
    s = init_state(df, goal, rules, fault, max_attempts)
    current, step = "profile", 0
    while current != END and step < 20:   # hard cap guards against routing loops
        step += 1
        t0 = time.perf_counter()
        summary = NODES[current](s)
        s.trace.append({"step": step, "node": current, "summary": summary,
                        "seconds": round(time.perf_counter() - t0, 3)})
        current = route(current, s)
    s.report["trace"] = s.trace
    return s
