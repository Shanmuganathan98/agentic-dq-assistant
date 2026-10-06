import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
from app.tools.sql_guard import classify_sql
from app.tools.dq_checks import run_dq_checks
from app.tools.profiling import check_nulls, check_duplicates

def test_guard_allows_select():
    assert classify_sql("SELECT * FROM customers WHERE state = 'TX'")["allowed"]
    assert classify_sql("WITH t AS (SELECT 1) SELECT * FROM t;")["allowed"]

def test_guard_blocks_writes_and_tricks():
    for sql in ["DELETE FROM customers", "SELECT 1; DROP TABLE customers",
                "SELECT * INTO backup FROM customers", "UPDATE customers SET x=1",
                "/* hi */ DROP TABLE x"]:
        assert not classify_sql(sql)["allowed"], sql

def test_guard_ignores_keywords_in_strings():
    assert classify_sql("SELECT * FROM t WHERE note = 'please delete; later'")["allowed"]

def test_injected_defects_are_found():
    df = pd.read_csv(Path(__file__).resolve().parents[1] / "data" / "customer.csv")
    found = {i["check"]: i["count"] for i in run_dq_checks(df)}
    assert found["null_email"] == 340
    assert found["invalid_date"] == 53
    assert found["invalid_state_code"] == 25
    assert 100 <= found["duplicate_key"] <= 120  # random collisions can merge a few
    assert check_nulls(df)["email"] == 340
    assert check_duplicates(df, "customer_id")["duplicate_rows"] == found["duplicate_key"]
