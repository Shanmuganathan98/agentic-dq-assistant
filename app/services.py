"""Business logic shared by the API and tests: runs, approvals, memory, guarded SQL."""
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from app import db, llm, loader, memory
from app.observability import inc, log_event
from app.pipeline.graph import run_pipeline
from app.tools import rules as R


class ServiceError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _runs_dir() -> Path:
    return Path(os.getenv("RUNS_DIR", "data/runs"))


RUN_FILES = ("clean.csv", "changes.csv", "original.csv", "resolution_changes.csv", "final.csv", "quarantine.csv")


# ---- rules ----------------------------------------------------------------------------------

def parse_rules(raw) -> list | None:
    """Accepts None, a list, or a JSON string (as sent by the API/UI). Empty -> None (suggestions are used)."""
    if raw in (None, "", "null"):
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ServiceError(f"rules is not valid JSON: {e}", 400)
    return raw or None


def check_rules(df: pd.DataFrame, raw) -> list[dict]:
    try:
        return R.validate_rules(parse_rules(raw) or [], df.columns, df)
    except R.RuleError as e:
        raise ServiceError("Invalid rules: " + "; ".join(e.problems), 422)


def suggest(df: pd.DataFrame) -> dict:
    return {"columns": [{"name": c, "dtype": str(df[c].dtype), "nulls": int(df[c].isna().sum())} for c in df.columns],
            "rows": len(df), "suggested_rules": R.suggest_rules(df),
            "rule_types": {k: {"required": v["required"], "optional": list(v["optional"]), "actions": v["actions"],
                               "doc": v["doc"]} for k, v in R.SPEC.items()}}


# ---- runs -----------------------------------------------------------------------------------

def create_run(df: pd.DataFrame, filename: str, goal: str, rules=None, fault: str | None = None) -> dict:
    db.init_schema()
    rules = parse_rules(rules)
    if rules:
        check_rules(df, rules)                       # fail fast with every problem listed
    run_id = uuid.uuid4().hex[:12]
    s = run_pipeline(df, goal, rules, fault=fault)
    report = s.report
    report["run_id"], report["filename"] = run_id, filename
    report["default_table"] = loader.default_table_name(filename)
    mem = memory.suggestions()
    report["suggested_resolutions"] = {
        x["rule_id"]: mem[x["rule_key"]] for x in report["required_resolutions"]
        if mem.get(x["rule_key"]) in x["options"]}
    report["narrative"] = llm.narrate(report) or llm.summarize(report)
    report = json.loads(json.dumps(report, default=str))     # plain JSON types only

    folder = _runs_dir() / run_id
    folder.mkdir(parents=True, exist_ok=True)
    df.to_csv(folder / "original.csv", index=False)
    if report["safe_to_load"]:
        s.clean_df.to_csv(folder / "clean.csv", index=False)
        s.changes.to_csv(folder / "changes.csv", index=False)
    (folder / "report.json").write_text(json.dumps(report, indent=2))
    db.execute("INSERT INTO runs (id, created_at, filename, goal, status, rows_count, report_json) "
               "VALUES (?, ?, ?, ?, ?, ?, ?)",
               (run_id, _now(), filename, goal, report["status"], len(df), json.dumps(report)))
    inc("pipeline_runs_total", status=report["status"])
    if report["status"] == "failed_validation":
        inc("validation_failures_total")
    inc("validation_retries_total", max(report["attempts"] - 1, 0))
    log_event("run_created", run_id=run_id, status=report["status"], rows=len(df), attempts=report["attempts"],
              rules=len(report["rules"]), rules_source=report["rules_source"])
    return report


def get_run(run_id: str) -> dict:
    db.init_schema()
    rows = db.query("SELECT report_json FROM runs WHERE id = ?", (run_id,))
    if not rows:
        raise ServiceError(f"run {run_id} not found", 404)
    return json.loads(rows[0]["report_json"])


def list_runs(limit: int = 50) -> list[dict]:
    db.init_schema()
    return db.query("SELECT id, created_at, filename, goal, status, rows_count FROM runs "
                    f"ORDER BY created_at DESC LIMIT {int(limit)}")


def run_file(run_id: str, name: str) -> Path:
    if name not in RUN_FILES:
        raise ServiceError("unknown file", 404)
    get_run(run_id)
    path = _runs_dir() / run_id / name
    if not path.exists():
        raise ServiceError(f"{name} not available for this run", 404)
    return path


# ---- approvals ------------------------------------------------------------------------------

def _approval_row(r: dict) -> dict:
    r = dict(r)
    r["payload"] = json.loads(r.pop("payload_json") or "{}")
    r["result"] = json.loads(r.pop("result_json") or "null")
    return r


def get_approval(approval_id: str) -> dict:
    rows = db.query("SELECT * FROM approvals WHERE id = ?", (approval_id,))
    if not rows:
        raise ServiceError(f"approval {approval_id} not found", 404)
    return _approval_row(rows[0])


def list_approvals(status: str | None = None) -> list[dict]:
    db.init_schema()
    if status:
        rows = db.query("SELECT * FROM approvals WHERE status = ? ORDER BY created_at DESC", (status,))
    else:
        rows = db.query("SELECT * FROM approvals ORDER BY created_at DESC")
    return [_approval_row(r) for r in rows]


def request_load(run_id: str, table: str | None = None) -> dict:
    report = get_run(run_id)
    if not report.get("safe_to_load"):
        raise ServiceError("Run did not pass validation (or was analysis-only); refusing to request a load.", 409)
    try:
        table = loader.validate_table_name(table or report["default_table"])
    except ValueError as e:
        raise ServiceError(str(e), 400)
    for a in list_approvals():
        if a["run_id"] == run_id and a["status"] in ("pending", "executed"):
            if a["status"] == "executed":
                raise ServiceError("This run has already been loaded.", 409)
            return a
    approval_id = uuid.uuid4().hex[:12]
    payload = {"table": table, "rows": report["rows"], "required_resolutions": report["required_resolutions"],
               "suggested_resolutions": report["suggested_resolutions"]}
    db.execute("INSERT INTO approvals (id, run_id, action, status, payload_json, created_at) "
               "VALUES (?, ?, 'load_table', 'pending', ?, ?)", (approval_id, run_id, json.dumps(payload), _now()))
    inc("approvals_requested_total")
    log_event("approval_requested", approval_id=approval_id, run_id=run_id, table=table)
    return get_approval(approval_id)


def decide_approval(approval_id: str, approve: bool, resolutions: dict | None, decided_by: str) -> dict:
    ap = get_approval(approval_id)
    if ap["status"] != "pending":
        raise ServiceError(f"approval is already {ap['status']}", 409)
    resolutions = resolutions or {}

    if not approve:
        db.execute("UPDATE approvals SET status='rejected', decided_at=?, decided_by=? WHERE id=?",
                   (_now(), decided_by, approval_id))
        inc("approvals_decided_total", decision="rejected")
        log_event("approval_rejected", approval_id=approval_id, by=decided_by)
        return get_approval(approval_id)

    required = ap["payload"]["required_resolutions"]
    problems = loader.check_resolutions(required, resolutions)
    if problems:
        raise ServiceError("Resolution problem: " + "; ".join(problems), 400)

    report = get_run(ap["run_id"])
    clean = pd.read_csv(run_file(ap["run_id"], "clean.csv"))
    try:
        loadable, quarantined, res_changes, expect = loader.apply_resolutions(clean, report["rules"], resolutions)
        result = loader.load_to_db(loadable, quarantined, ap["run_id"], source_rows=len(clean),
                                   table=ap["payload"]["table"], rules=report["rules"], expect_clean=expect)
        status = "executed"
        if len(res_changes):
            res_changes.to_csv(_runs_dir() / ap["run_id"] / "resolution_changes.csv", index=False)
        result["resolution_changes"] = len(res_changes)
        folder = _runs_dir() / ap["run_id"]                     # the dataset a human can open: decisions applied
        loadable.to_csv(folder / "final.csv", index=False)
        quarantined.to_csv(folder / "quarantine.csv", index=False)
        memory.record({x["rule_key"]: resolutions[x["rule_id"]] for x in required}, decided_by)
    except loader.LoadValidationError as e:
        result, status = {"error": str(e), "checks": e.checks}, "failed"
        inc("load_validation_failures_total")
    except Exception as e:
        db.execute("UPDATE approvals SET status='failed', result_json=?, decided_at=?, decided_by=? WHERE id=?",
                   (json.dumps({"error": str(e)}), _now(), decided_by, approval_id))
        log_event("load_error", approval_id=approval_id, error=str(e))
        raise ServiceError(f"load failed: {e}", 400 if isinstance(e, ValueError) else 500)

    db.execute("UPDATE approvals SET status=?, result_json=?, decided_at=?, decided_by=? WHERE id=?",
               (status, json.dumps({"resolutions": resolutions, **result}, default=str), _now(), decided_by, approval_id))
    inc("approvals_decided_total", decision=status)
    if status == "executed":
        inc("rows_loaded_total", result["loaded"])
    log_event("approval_decided", approval_id=approval_id, status=status, by=decided_by)
    return get_approval(approval_id)


# ---- guarded SQL ----------------------------------------------------------------------------

def sql_query(sql: str, limit: int = 200) -> dict:
    db.init_schema()
    try:
        out = db.execute_select_query(sql, limit)
    except PermissionError as e:
        inc("sql_blocked_total")
        raise ServiceError(str(e), 403)
    except Exception as e:
        raise ServiceError(f"query error: {e}", 400)
    inc("sql_queries_total")
    return out
