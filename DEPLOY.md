# Deployment & monitoring (Phase 6)

> Status: the Dockerfiles, compose file and CI workflow were written but NOT executed in the build
> environment (no Docker/network there). Test locally first with `docker compose up --build`.

## 1. Run everything locally with Docker
```bash
cp .env.example .env            # add ANTHROPIC_API_KEY (optional) and API_KEY
docker compose up --build
# API  http://localhost:8000/docs     UI  http://localhost:8501     Postgres on the compose network
```

## 2. AWS reference architecture
| Piece | AWS service |
|---|---|
| API container | ECS Fargate service behind an Application Load Balancer (or App Runner for the simplest path) |
| UI container | second ECS service (or run Streamlit locally against the API) |
| Database | RDS PostgreSQL (private subnet; security group allows only the API tasks) |
| Secrets | Secrets Manager: `DATABASE_URL`, `ANTHROPIC_API_KEY`, `API_KEY`, injected as task-definition secrets |
| Images | ECR |
| Logs | CloudWatch Logs via the `awslogs` driver (app logs are JSON, one event per line) |

```bash
aws ecr create-repository --repository-name dq-agent
aws ecr get-login-password | docker login --username AWS --password-stdin <acct>.dkr.ecr.<region>.amazonaws.com
docker build -t dq-agent . && docker tag dq-agent <acct>.dkr.ecr.<region>.amazonaws.com/dq-agent:latest
docker push <acct>.dkr.ecr.<region>.amazonaws.com/dq-agent:latest
```
Use `DATABASE_URL=postgresql://user:pass@<rds-endpoint>:5432/dq` (plain `postgresql://`, not the SQLAlchemy form).

### Known gaps before this is production-grade
- **Run artifacts live on local disk** (`RUNS_DIR`). Fargate disk is ephemeral and not shared between tasks.
  Either mount an EFS volume at `RUNS_DIR`, or replace `services.py` file I/O with S3 reads/writes.
- **Use a dedicated read-only Postgres role** for the SQL explorer. The SQL guard and read-only transaction
  are two layers, but a DB role limited to SELECT is the layer that cannot be bypassed by an application bug.
  (Needs a second connection string, e.g. `READONLY_DATABASE_URL`; not wired up yet.)
- Authentication is a single shared `X-API-Key`. Put real SSO (Cognito/ALB OIDC) in front for multi-user use;
  `decided_by` is currently a free-text name, not an authenticated identity.
- The schema is created with `CREATE TABLE IF NOT EXISTS`; add Alembic migrations before changing tables.

## 3. Monitoring
- **Health:** `GET /health` (checks the DB). Use it for the ALB target group and the container healthcheck.
- **Metrics:** `GET /metrics` serves Prometheus text. Key series:
  `pipeline_runs_total{status}`, `validation_failures_total`, `validation_retries_total`,
  `load_validation_failures_total`, `approvals_requested_total`, `approvals_decided_total{decision}`,
  `rows_loaded_total`, `sql_blocked_total`, `http_requests_total{method,path,status}`.
  Counters are per-process and reset on restart; scrape with Prometheus/ADOT, or use CloudWatch metric filters.
- **Metric filters on JSON logs (no extra infra):** filter pattern `{ $.event = "run_created" && $.status = "failed_validation" }`
  -> metric `ValidationFailures`.
- **Suggested alarms:** any `failed_validation` run; `load_validation_failures_total` > 0; ALB 5xx > 1% for 5 min;
  `sql_blocked_total` spike (someone probing the guard); `/health` failing.
