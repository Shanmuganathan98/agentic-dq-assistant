import io
import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from app.agents.profiling_agent import run_agent
from app.schemas import AgentResult
from app.tools.sql_guard import classify_sql

app = FastAPI(title="Agentic DQ Assistant", version="0.1.0")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/analyze", response_model=AgentResult)
async def analyze(file: UploadFile = File(...),
                  goal: str = Form("Analyze this dataset and identify data-quality problems.")):
    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(400, "Only CSV supported in Phase 1")
    df = pd.read_csv(io.BytesIO(await file.read()))
    return run_agent(df, goal)


@app.post("/sql/check")
def sql_check(sql: str = Form(...)):
    """Preview what the guard would do with a statement."""
    return classify_sql(sql)
