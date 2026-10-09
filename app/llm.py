"""Optional LLM helpers. Everything degrades gracefully to deterministic behaviour.

The LLM may only (a) decide whether a goal asks for modification and (b) rewrite a summary.
It never executes SQL, never sets resolutions, and never produces numbers.
"""
import json
import os

MODEL = os.getenv("DQ_MODEL", "claude-sonnet-5-5")


def _client():
    if not os.getenv("ANTHROPIC_API_KEY"):
        return None
    import anthropic
    return anthropic.Anthropic()


def plan_fix(goal: str):
    """True = goal wants data modified, False = read-only, None = unknown (caller falls back to rules)."""
    try:
        c = _client()
        if c is None:
            return None
        r = c.messages.create(model=MODEL, max_tokens=5, messages=[{"role": "user", "content":
            "Does this request ask to modify/clean/fix/load data, or only analyse it? "
            f"Reply with exactly FIX or READONLY.\n\nRequest: {goal}"}])
        word = r.content[0].text.strip().upper()
        return True if word.startswith("FIX") else False if word.startswith("READ") else None
    except Exception:
        return None


def narrate(report: dict):
    try:
        c = _client()
        if c is None:
            return None
        facts = {k: v for k, v in report.items() if k not in ("trace", "narrative")}
        r = c.messages.create(model=MODEL, max_tokens=500, system=(
            "Write a concise 4-6 sentence summary for a data engineer. Use ONLY facts and numbers present "
            "in the JSON. State what was found, what was fixed, whether it is safe to load, and what needs "
            "human approval. No speculation."), messages=[{"role": "user", "content": json.dumps(facts, default=str)}])
        return r.content[0].text.strip()
    except Exception:
        return None


def summarize(report: dict) -> str:
    issues = report.get("issues_found", {})
    found = ", ".join(f"{v} {k}" for k, v in issues.items()) or "no issues"
    fixed = ", ".join(f"{v} in {k}" for k, v in report.get("fixes_applied", {}).items()) or "nothing"
    parts = [f"Analysed {report.get('rows', '?')} rows and found: {found}.",
             f"Safe fixes applied: {fixed}."]
    if report.get("status") == "failed_validation":
        parts.append("Validation FAILED after retries, so the cleaned data must not be loaded.")
    elif report.get("safe_to_load"):
        parts.append("Validation passed; the cleaned data is safe to load once approved.")
    if report.get("needs_human_approval"):
        parts.append(f"{len(report['needs_human_approval'])} issue type(s) need a human decision before loading.")
    return " ".join(parts)
