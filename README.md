# Agentic Data-Quality Assistant

Upload any CSV or Excel file, **choose the checks you want**, and a pipeline of specialised agents runs them, fixes
what is safe, **proves** the fix worked, asks a human about everything risky, and only then loads the data into a
table it creates from your file's own columns.

```
Request -> Orchestrator -> Profile -> Your checks -> Clean (safe fixes only) -> Validate --fail--> retry / escalate
                                                                                  |pass
                                                              Report -> Approval queue (human) -> Atomic, verified load
```

Nothing in the code is specific to one dataset. The checks are input: see [docs/RULES.md](docs/RULES.md).

## What each phase gives you
| Phase | Delivered | Where |
|---|---|---|
| 1 | LLM tool-use agent with structured (Pydantic) output | `app/agents/profiling_agent.py` |
| 2 | Orchestrated pipeline, independent validation, retry, fault-injection demo | `app/pipeline/`, `app/tools/validation.py` |
| 3 | SQLite/PostgreSQL layer, guarded read-only SQL tool, schema tools | `app/db.py`, `app/tools/sql_guard.py` |
| 4 | Approval queue, quarantine, atomic load with post-load checks, memory of human decisions | `app/services.py`, `app/loader.py`, `app/memory.py` |
| 5 | Streamlit UI: rule builder, run, approve, SQL explorer, history | `ui/streamlit_app.py` |
| 6 | Docker, compose, CI, AWS guide, JSON logs, `/metrics` | `Dockerfile*`, `DEPLOY.md`, `app/observability.py` |
| 7 | User-defined checks, any CSV/Excel, dynamic target tables | `app/tools/rules.py`, `app/ingest.py`, `docs/RULES.md` |

## Quick start
```bash
conda create -n dqagent python=3.11 -y && conda activate dqagent
pip install -r requirements.txt -r requirements-ui.txt -r requirements-dev.txt
python scripts/generate_dirty_customers.py     # sample: data/customer.csv (340 null emails, 119 dup IDs, 53 bad dates, 25 bad states)
python -m pytest -q
uvicorn app.main:app --reload                  # terminal 1  -> http://127.0.0.1:8000/docs
streamlit run ui/streamlit_app.py              # terminal 2  -> http://localhost:8501
```

### Use your own file (CLI)
```bash
python scripts/run_pipeline.py mydata.xlsx --sheet Orders --suggest > rules.json   # starter checks inferred from the data
# edit rules.json: change "action" to fix/ask where you want changes, add your own checks
python scripts/run_pipeline.py mydata.xlsx --sheet Orders --rules rules.json
```
Other flags: `--fault once|always` (demo validation catching a bug), `--engine langgraph`, a goal as the last argument
(a goal that only asks to analyse or profile never modifies data).

### Use your own file (UI)
Upload a file, then under **Choose the checks** use *Use suggested checks*, *Import checks (JSON)*, or add checks one
at a time. *Preview violations* shows counts without changing anything.

### API
| Endpoint | Purpose |
|---|---|
| `GET /rule-types` | the check types and their parameters |
| `POST /suggest-rules` | columns plus starter checks for an uploaded file |
| `POST /validate-rules` | dry run: check your rules against a file, nothing changes |
| `POST /runs` | run the pipeline (`file`, `goal`, `rules` JSON, optional `sheet`, `fault`) |
| `POST /runs/{id}/request-load` | ask for approval to load (optional `table` name) |
| `POST /approvals/{id}/decide` | approve with resolutions `{rule_id: choice}`, or reject |
| `POST /sql/query` | read-only SELECT |

Environment (see `.env.example`): `ANTHROPIC_API_KEY` (optional), `DATABASE_URL` (default SQLite, or
`postgresql://user:pass@host:5432/db`), `RUNS_DIR`, `API_KEY`, `DQ_MODEL`, `MAX_ROWS`.

## Safety design
- Only the fixes you mark `fix` are automatic, and each is deterministic and logged. Anything else is `flag` (report)
  or `ask` (a human decides). Suggested checks are always report-only.
- Validation reconciles the cleaned data against the change log: an unlogged change is a failure and triggers a retry.
  If it still fails, the clean file is withheld and a load cannot be requested.
- Quarantined rows are never deleted: they go to `dq_quarantine` with the reason and the original row.
- SQL: guard (single SELECT/WITH only) **and** a read-only DB connection. Writes only happen through an approved load.
- Target table names are validated, application tables are protected, and an existing table with different columns
  is refused.
- The load runs in one transaction with post-load checks (row reconciliation, quarantine counts, unique keys,
  per-column NULL counts, your fix rules re-checked on the loaded rows); any failure rolls everything back.
- The LLM only decides read-only vs modify and rewrites the summary. It cannot run SQL, approve, or invent numbers.
  With no API key everything falls back to deterministic rules.
- Memory = the resolution a reviewer chose last time for the same check, shown as a *suggestion*; never applied
  automatically.

## Demo script (2 minutes)
1. Upload `titanic.csv`, click *Use suggested checks*, run. Show the trace and the missing-value findings.
2. Import `examples/titanic_rules.json`, run with "inject a cleaning bug: once". Validation fails with
   `no_unlogged_changes`, the orchestrator retries, passes.
3. Request approval, choose `fill_median` for Age, approve. Show the verified load.
4. SQL explorer: `SELECT embarked, AVG(age) FROM titanic GROUP BY embarked` works; `DELETE FROM titanic` is blocked.
5. Upload a different file (`customer.csv` with `examples/customer_rules.json`): same system, different checks.

## Honest limitations
- Types are inferred by pandas when the file is read (for example `02134` in a CSV becomes `2134`). Rules that must
  see the original text (leading zeros, exact formats) are best applied to text columns.
- Checks are per-column or composite-key. Cross-column and cross-table rules (for example "end date after start
  date", reference tables) are not built in yet.
- The LangGraph engine, PostgreSQL path, Docker files and CI workflow are written but were not executed in the
  build sandbox (no network). The Streamlit UI and the HTTP layer were not run there either. See `DEPLOY.md`.
- SQLite does not roll back `CREATE TABLE` with the rest of a failed load (PostgreSQL does), so a failed first load
  can leave an empty table behind.
