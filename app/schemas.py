from pydantic import BaseModel, Field


class NoArgs(BaseModel):
    pass


class KeyArgs(BaseModel):
    key: str = Field(description="Column to check for duplicate values")


class Finding(BaseModel):
    check: str
    column: str
    count: int
    severity: str = Field(description="low | medium | high")


class Report(BaseModel):
    """Structured final output. The agent must produce this via the submit_report tool."""
    summary: str
    findings: list[Finding]
    safe_fixes: list[str] = Field(description="Fixes that are deterministic and reversible")
    needs_human_approval: list[str] = Field(description="Anything lossy, ambiguous or destructive")


class AgentResult(BaseModel):
    report: Report
    tool_trace: list[dict]
    mode: str  # "llm" or "deterministic"
