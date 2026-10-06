"""Gatekeeper for SQL an agent wants to run. Auto-approve read-only only; everything else needs a human."""
import re

BLOCKED = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|create|grant|revoke|merge|call|copy|exec|execute|into)\b",
    re.IGNORECASE)


def _strip_comments_and_strings(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return re.sub(r"'(?:[^']|'')*'", "''", sql)  # string literals can't hide keywords or semicolons


def classify_sql(sql: str) -> dict:
    cleaned = _strip_comments_and_strings(sql).strip().rstrip(";").strip()
    if ";" in cleaned:
        return {"allowed": False, "requires_approval": True, "reason": "multiple statements"}
    if not re.match(r"^(select|with)\b", cleaned, re.IGNORECASE):
        return {"allowed": False, "requires_approval": True, "reason": "not a SELECT/WITH query"}
    hit = BLOCKED.search(cleaned)
    if hit:
        return {"allowed": False, "requires_approval": True, "reason": f"contains '{hit.group(0).upper()}'"}
    return {"allowed": True, "requires_approval": False, "reason": "read-only SELECT"}
