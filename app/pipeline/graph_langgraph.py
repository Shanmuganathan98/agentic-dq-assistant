"""The same pipeline expressed as a LangGraph StateGraph. Requires: pip install langgraph

Nodes and routing are reused from graph.py, so behaviour is identical to run_pipeline().
NOTE: written against the LangGraph StateGraph API but not executed in the build sandbox (no network).
Run `python scripts/run_pipeline.py data/customer.csv --engine langgraph` to verify on your machine.
"""
import time
from typing import TypedDict

import pandas as pd
from langgraph.graph import END as LG_END
from langgraph.graph import StateGraph

from app.pipeline.graph import END, NODES, init_state, route
from app.pipeline.state import PipelineState


class GraphState(TypedDict):
    s: PipelineState


def _wrap(name, fn):
    def node(state: GraphState):
        s = state["s"]
        t0 = time.perf_counter()
        summary = fn(s)
        s.trace.append({"step": len(s.trace), "node": name, "summary": summary,
                        "seconds": round(time.perf_counter() - t0, 3)})
        return {"s": s}
    return node


def _router(name):
    return lambda state: route(name, state["s"])


def build_graph():
    g = StateGraph(GraphState)
    for name, fn in NODES.items():
        g.add_node(name, _wrap(name, fn))
    g.set_entry_point("profile")
    targets = {n: n for n in NODES} | {END: LG_END}
    for name in ("profile", "dq", "clean", "validate"):
        g.add_conditional_edges(name, _router(name), targets)
    g.add_edge("report", LG_END)
    return g.compile()


def run_pipeline_langgraph(df: pd.DataFrame, goal: str, rules: list | None = None,
                           fault: str | None = None, max_attempts: int = 2) -> PipelineState:
    s = init_state(df, goal, rules, fault, max_attempts)
    final = build_graph().invoke({"s": s}, {"recursion_limit": 25})["s"]
    final.report["trace"] = final.trace
    return final
