from helpers import *
from app.tools.sql_guard import classify_sql
from app.tools import profiling


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


def test_profiling_is_generic():
    p = profiling.profile_table(TITANIC)
    assert p["row_count"] == 891 and p["column_count"] == 12
    assert {c["name"]: c["nulls"] for c in p["columns"] if c["nulls"]} == {"Age": 177, "Cabin": 687, "Embarked": 2}
    assert profiling.check_nulls(CUSTOMER) == {"email": 340}
    assert profiling.check_duplicates(CUSTOMER, "customer_id")["duplicate_rows"] == 119
    assert "error" in profiling.check_duplicates(CUSTOMER, "nope")
