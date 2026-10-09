"""Shared test helpers (importable from any test module)."""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pandas as pd

CUSTOMER = pd.read_csv(ROOT / "data" / "customer.csv")
TITANIC = pd.read_csv(ROOT / "tests" / "fixtures" / "titanic.csv")
CUSTOMER_RULES = json.loads((ROOT / "examples" / "customer_rules.json").read_text())
TITANIC_RULES = json.loads((ROOT / "examples" / "titanic_rules.json").read_text())
GOAL = "Analyze and prepare for loading"


def orders() -> pd.DataFrame:
    """A third, unrelated dataset (different columns, messy header) to prove nothing is customer-specific."""
    return pd.DataFrame({
        "Order ID": [1, 2, 3, 4, 5, 5, 7, 8],
        "order_date": ["2024-01-05", "2024-13-01", "2024-02-30", "2024-03-10", "2024-03-11", "2024-03-12", "bad", "2024-04-01"],
        "country": ["US", "ca", "Canada", "MX", "XX", "us", "Mexico", None],
        "amount": [10.0, -5.0, 200000.0, 50.0, 0.0, 75.5, None, 20.0],
        "contact": ["a@x.com", "b@x", "c@x.com", None, "e@x.com", "f@x.com", "g@x.com", "h@x.com"],
    })


ORDERS_RULES = [
    {"type": "unique", "column": "Order ID", "action": "ask", "severity": "high"},
    {"type": "date_format", "column": "order_date", "format": "%Y-%m-%d", "action": "fix"},
    {"type": "allowed_values", "column": "country", "values": ["US", "CA", "MX"], "action": "fix",
     "synonyms": {"Canada": "CA", "Mexico": "MX"}},
    {"type": "range", "column": "amount", "min": 0, "max": 100000, "action": "fix", "fix_with": "clip"},
    {"type": "regex", "column": "contact", "pattern": "[^@\\s]+@[^@\\s]+\\.[^@\\s]+", "action": "ask"},
    {"type": "not_null", "column": "amount", "action": "flag"},
]


def fresh() -> str:
    """Isolated SQLite DB + run folder per test."""
    tmp = tempfile.mkdtemp()
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp}/t.db"
    os.environ["RUNS_DIR"] = f"{tmp}/runs"
    return tmp


def raises(fn, exc=Exception):
    try:
        fn()
    except exc as e:
        return e
    raise AssertionError("expected an exception")


def names(s):
    return [t["node"] for t in s.trace]
