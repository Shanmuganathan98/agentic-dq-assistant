import os
import time

from fastapi import APIRouter, Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from app import db, ingest, memory, observability, services
from app.agents.profiling_agent import run_agent
from app.schemas import AgentResult
from app.services import ServiceError
from app.tools import rules as R
from app.tools.sql_guard import classify_sql

observability.configure_logging()
app = FastAPI(title="Agentic DQ Assistant", version="2.0.0")

DEFAULT_GOAL = "Analyze this dataset and prepare it for loading."


def require_key(x_api_key: str | None = Header(default=None)):
    expected = os.getenv("API_KEY")
    if expected and x_api_key != expected:
        raise HTTPException(401, "invalid or missing X-API-Key")


public = APIRouter()
api = APIRouter(dependencies=[Depends(require_key)])


@app.exception_handler(ServiceError)
async def service_error_handler(_, exc: ServiceError):
    return JSONResponse(status_code=exc.status, content={"detail": str(exc)})


@app.middleware("http")
async def observe(request, call_next):
    t0 = time.perf_counter()
    response = await call_next(request)
    route = request.scope.get("route")
    path = getattr(route, "path", request.url.path)
    elapsed = time.perf_counter() - t0
    observability.inc("http_requests_total", method=request.method, path=path, status=response.status_code)
    observability.inc("http_request_duration_seconds_sum", elapsed, path=path)
    observability.log_event("http_request", method=request.method, path=path,
                            status=response.status_code, ms=round(elapsed * 1000, 1))
    return response


async def read_upload(file: UploadFile, sheet: str = ""):
    """Any CSV/TSV/Excel upload -> DataFrame (400 with a readable message if it cannot be read)."""
    try:
        return ingest.read_table(file.filename, await file.read(), sheet or None)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---- public -------------------------------------------------------------------------------

@public.get("/health")
def health():
    try:
        db.init_schema()
        return {"status": "ok", "database": "postgres" if db.is_postgres() else "sqlite"}
    except Exception as e:
        return JSONResponse(status_code=503, content={"status": "degraded", "error": str(e)})


@public.get("/metrics", response_class=PlainTextResponse)
def metrics():
    return observability.render()


# ---- rules: what can be checked, and suggestions for a dataset ----------------------------

@api.get("/rule-types")
def rule_types():
    """The check types a user can choose from, with their parameters."""
    return {k: {"required": v["required"], "optional": v["optional"], "actions": v["actions"], "doc": v["doc"]}
            for k, v in R.SPEC.items()}


@api.post("/sheets")
async def sheets(file: UploadFile = File(...)):
    if not ingest.is_excel(file.filename):
        return []
    try:
        return ingest.sheet_names(await file.read())
    except ValueError as e:
        raise HTTPException(400, str(e))


@api.post("/suggest-rules")
async def suggest_rules(file: UploadFile = File(...), sheet: str = Form("")):
    """Columns + report-only starter rules inferred from the data, to edit and send back with /runs."""
    return services.suggest(await read_upload(file, sheet))


@api.post("/validate-rules")
async def validate_rules(file: UploadFile = File(...), rules: str = Form(...), sheet: str = Form("")):
    """Dry-run: check the rules against the dataset's columns and show the violation counts. Changes nothing."""
    df = await read_upload(file, sheet)
    validated = services.check_rules(df, rules)
    return {"rules": validated, "checks": R.run_checks(df, validated)}


# ---- Phase 1: single LLM agent ------------------------------------------------------------

@api.post("/analyze", response_model=AgentResult)
async def analyze(file: UploadFile = File(...),
                  goal: str = Form("Analyze this dataset and identify data-quality problems."),
                  rules: str = Form(""), sheet: str = Form("")):
    df = await read_upload(file, sheet)
    raw = services.parse_rules(rules)
    if raw:
        services.check_rules(df, raw)        # 422 with every problem listed; run_agent validates again itself
    return run_agent(df, goal, raw)


# ---- Phase 2/4: pipeline runs, approvals, memory ------------------------------------------

@api.post("/runs")
async def create_run(file: UploadFile = File(...), goal: str = Form(DEFAULT_GOAL),
                     rules: str = Form(""), fault: str = Form(""), sheet: str = Form("")):
    """rules = JSON list of checks (see /rule-types). Empty -> report-only rules suggested from the data."""
    if fault not in ("", "once", "always"):
        raise HTTPException(400, "fault must be '', 'once' or 'always'")
    df = await read_upload(file, sheet)
    return services.create_run(df, file.filename, goal, rules, fault or None)


@api.get("/runs")
def list_runs():
    return services.list_runs()


@api.get("/runs/{run_id}")
def get_run(run_id: str):
    return services.get_run(run_id)


@api.get("/runs/{run_id}/files/{name}")
def download(run_id: str, name: str):
    return FileResponse(services.run_file(run_id, name), media_type="text/csv", filename=f"{run_id}_{name}")


@api.post("/runs/{run_id}/request-load")
def request_load(run_id: str, table: str = Form("")):
    return services.request_load(run_id, table or None)


class Decision(BaseModel):
    approve: bool
    resolutions: dict[str, str] = Field(default_factory=dict, description="rule_id -> chosen option")
    decided_by: str = "reviewer"


@api.get("/approvals")
def approvals(status: str | None = None):
    return services.list_approvals(status)


@api.post("/approvals/{approval_id}/decide")
def decide(approval_id: str, body: Decision):
    return services.decide_approval(approval_id, body.approve, body.resolutions, body.decided_by)


@api.get("/memory/decisions")
def decisions():
    return memory.all_decisions()


# ---- Phase 3: guarded SQL tools -----------------------------------------------------------

@api.post("/sql/check")
def sql_check(sql: str = Form(...)):
    return classify_sql(sql)


@api.post("/sql/query")
def sql_query(sql: str = Form(...), limit: int = Form(200)):
    return services.sql_query(sql, min(max(limit, 1), 1000))


@api.get("/schema")
def tables():
    db.init_schema()
    return db.list_tables()


@api.get("/schema/{table}")
def schema(table: str):
    try:
        return db.get_table_schema(table)
    except ValueError as e:
        raise HTTPException(400, str(e))


app.include_router(public)
app.include_router(api)
