# Agentic Data-Quality Assistant

Phase 1: one tool-using agent + CSV profiling + SQL safety guard.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/generate_dirty_customers.py      # creates data/customer.csv
python -m pytest -q                              # tools + guard tests
export ANTHROPIC_API_KEY=...                     # optional; without it you get the deterministic baseline
uvicorn app.main:app --reload
curl -F file=@data/customer.csv http://127.0.0.1:8000/analyze
```
Swagger UI: http://127.0.0.1:8000/docs

## Layout
- `app/tools/`    deterministic tools (profiling, DQ rules, SQL guard). No LLM here.
- `app/agents/`   agent loops. Phase 1 = profiling_agent.py
- `app/schemas.py` Pydantic models for tool args and the structured report
- `scripts/`      dirty-data generator with known defect counts (340/120/53/25)

## Roadmap
1. One agent + CSV profiling  (this repo)
2. Split into Profiling / DQ / Validation / Report agents + orchestrator (LangGraph)
3. Postgres + execute_select_query using sql_guard
4. Human approval queue + memory
5. React/Streamlit UI
6. Docker, AWS, tracing and monitoring
