"""HTTP-level tests with FastAPI's TestClient. Skipped automatically when fastapi/httpx are not installed.

NOTE: these were written without being able to run them in the build sandbox (no fastapi there);
the route *bodies* were exercised separately. If one fails on your machine, paste the output.
"""
from helpers import *

try:
    import pytest
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
except ImportError:                      # plain runner without pytest: tests below become no-ops
    pytest = None

TITANIC_CSV = (ROOT / "tests" / "fixtures" / "titanic.csv").read_bytes()
TITANIC_JSON = json.dumps(TITANIC_RULES)


def client(api_key=None):
    fresh()
    if api_key:
        os.environ["API_KEY"] = api_key
    else:
        os.environ.pop("API_KEY", None)
    from fastapi.testclient import TestClient
    import app.main as m
    return TestClient(m.app)


def files(name="titanic.csv", content=TITANIC_CSV):
    return {"file": (name, content, "application/octet-stream")}


def test_health_and_rule_types():
    if pytest is None:
        return
    c = client()
    assert c.get("/health").json()["status"] == "ok"
    assert "metrics" not in c.get("/rule-types").text and set(c.get("/rule-types").json()) == set(R.SPEC)
    assert "pipeline_runs_total" in c.get("/metrics").text or c.get("/metrics").status_code == 200


def test_suggest_validate_and_reject_bad_rules():
    if pytest is None:
        return
    c = client()
    s = c.post("/suggest-rules", files=files()).json()
    assert s["rows"] == 891 and s["suggested_rules"]
    v = c.post("/validate-rules", files=files(), data={"rules": TITANIC_JSON}).json()
    assert {x["rule_id"]: x["count"] for x in v["checks"] if x["count"]} == {"r8": 177, "r9": 2, "r10": 687}
    bad = c.post("/validate-rules", files=files(), data={"rules": '[{"type":"range","column":"Nope"}]'})
    assert bad.status_code == 422 and "Nope" in bad.json()["detail"]
    assert c.post("/runs", files=files("x.pdf", b"hi"), data={}).status_code == 400


def test_full_flow_over_http():
    if pytest is None:
        return
    c = client()
    rep = c.post("/runs", files=files(), data={"goal": GOAL, "rules": TITANIC_JSON}).json()
    assert rep["status"] == "completed_needs_approval" and rep["rules_source"] == "user"
    assert c.get(f'/runs/{rep["run_id"]}/files/clean.csv').status_code == 200
    ap = c.post(f'/runs/{rep["run_id"]}/request-load', data={"table": "pax"}).json()
    assert ap["status"] == "pending" and ap["payload"]["table"] == "pax"
    assert c.post(f'/approvals/{ap["id"]}/decide', json={"approve": True, "resolutions": {}}).status_code == 400
    out = c.post(f'/approvals/{ap["id"]}/decide', json={
        "approve": True, "resolutions": {"r8": "fill_median", "r9": "fill_mode"}, "decided_by": "me"}).json()
    assert out["status"] == "executed" and out["result"]["loaded"] == 891
    q = c.post("/sql/query", data={"sql": "SELECT COUNT(*) AS n FROM pax"}).json()
    assert q["rows"] == [{"n": 891}]
    assert c.post("/sql/query", data={"sql": "DROP TABLE pax"}).status_code == 403
    assert "pax" in c.get("/schema").json()


def test_excel_upload_and_sheets():
    if pytest is None:
        return
    import io
    buf = io.BytesIO()
    TITANIC.head(40).to_excel(buf, index=False, sheet_name="Pax")
    c = client()
    assert c.post("/sheets", files=files("t.xlsx", buf.getvalue())).json() == ["Pax"]
    rep = c.post("/runs", files=files("t.xlsx", buf.getvalue()), data={"goal": "Just profile this", "sheet": "Pax"}).json()
    assert rep["rows"] == 40 and rep["status"] == "analysis_only"


def test_api_key_is_enforced_when_configured():
    if pytest is None:
        return
    c = client(api_key="secret")
    assert c.get("/health").status_code == 200                     # health stays public
    assert c.get("/runs").status_code == 401
    assert c.get("/runs", headers={"X-API-Key": "secret"}).status_code == 200
    os.environ.pop("API_KEY", None)
