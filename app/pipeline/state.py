from __future__ import annotations
from dataclasses import dataclass, field
import pandas as pd


@dataclass
class PipelineState:
    """Shared state passed between agents. Each node reads from and writes to this."""
    df: pd.DataFrame
    goal: str
    rules: list = field(default_factory=list)      # validated rules (app.tools.rules)
    rules_source: str = "user"                     # "user" | "suggested"
    fault: str | None = None                       # None | "once" | "always": inject a cleaning bug to demo validation
    max_attempts: int = 2
    plan_fix: bool = True                          # set by the orchestrator from the goal
    profile: dict = field(default_factory=dict)
    checks: list = field(default_factory=list)     # one result per rule
    issues: list = field(default_factory=list)     # checks with count > 0
    clean_df: pd.DataFrame | None = None
    changes: pd.DataFrame | None = None
    validation: dict = field(default_factory=dict)
    attempts: int = 0
    report: dict = field(default_factory=dict)
    trace: list = field(default_factory=list)
