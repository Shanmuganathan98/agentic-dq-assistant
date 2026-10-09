"""Run the pipeline on any CSV/TSV/Excel file from the command line.

  python scripts/run_pipeline.py data/customer.csv --rules examples/customer_rules.json
  python scripts/run_pipeline.py titanic.csv --suggest > titanic_rules.json     # starter rules to edit
  python scripts/run_pipeline.py titanic.csv --rules titanic_rules.json
  python scripts/run_pipeline.py data.xlsx --sheet Sheet1 "Just profile this"
  python scripts/run_pipeline.py data/customer.csv --rules examples/customer_rules.json --fault once

With no --rules, report-only rules are suggested from the data.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ingest
from app.tools import rules as R

p = argparse.ArgumentParser()
p.add_argument("file")
p.add_argument("goal", nargs="?", default="Analyze this dataset and prepare it for loading.")
p.add_argument("--rules", help="JSON file with the checks to run (see examples/)")
p.add_argument("--suggest", action="store_true", help="print starter rules inferred from the data and exit")
p.add_argument("--sheet", help="sheet name for Excel files")
p.add_argument("--fault", choices=["once", "always"], help="inject a cleaning bug to demo validation")
p.add_argument("--engine", choices=["python", "langgraph"], default="python")
a = p.parse_args()

src = Path(a.file)
try:
    df = ingest.read_table(src.name, src.read_bytes(), a.sheet)
except (ValueError, OSError) as e:
    sys.exit(f"error: {e}")

if a.suggest:
    print(json.dumps(R.suggest_rules(df), indent=2))
    sys.exit(0)

rules = json.loads(Path(a.rules).read_text()) if a.rules else None
if a.engine == "langgraph":
    from app.pipeline.graph_langgraph import run_pipeline_langgraph as run_pipeline
else:
    from app.pipeline.graph import run_pipeline
try:
    s = run_pipeline(df, a.goal, rules, fault=a.fault)
except R.RuleError as e:
    sys.exit("invalid rules:\n  " + "\n  ".join(e.problems))

for t in s.report["trace"]:
    print(f'{t["step"]:>2}  {t["node"]:<13} {t["summary"]}')
print()
for c in s.report["checks"]:
    print(f'   {c["count"]:>6}  {c["action"]:<5} {c["severity"]:<6} {c["name"]}')
print()
out = src.with_name(src.stem + "_report.json")
out.write_text(json.dumps(s.report, indent=2, default=str))
print(f'status: {s.report["status"]}   safe_to_load: {s.report["safe_to_load"]}   -> {out.name}')
if s.report["safe_to_load"]:
    s.clean_df.to_csv(src.with_name(src.stem + "_clean.csv"), index=False)
    s.changes.to_csv(src.with_name(src.stem + "_changes.csv"), index=False)
    print(f"wrote {src.stem}_clean.csv and {src.stem}_changes.csv")
else:
    print("clean file NOT written")
