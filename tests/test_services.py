from helpers import *
from app import db, llm, loader, memory, observability, services
from app.services import ServiceError
from app.tools import rules as R

CUST_RES = {"r1": "keep_last", "r2": "leave_null"}
TIT_RES = {"r8": "fill_median", "r9": "fill_mode"}


def count(sql):
    return db.query(sql)[0]["n"]


def run_and_request(df, rules, table=None, name="data.csv", **kw):
    rep = services.create_run(df, name, GOAL, rules, **kw)
    return rep, services.request_load(rep["run_id"], table)


def test_customer_full_flow():
    fresh()
    rep, ap = run_and_request(CUSTOMER, CUSTOMER_RULES, name="customer.csv")
    assert ap["status"] == "pending" and ap["payload"]["table"] == "customer"
    assert [r["rule_id"] for r in ap["payload"]["required_resolutions"]] == ["r1", "r2"]
    assert "customer" not in db.list_tables()                                    # nothing loaded before approval
    e = raises(lambda: services.decide_approval(ap["id"], True, {}, "t"), ServiceError)
    assert e.status == 400 and "r1" in str(e)
    done = services.decide_approval(ap["id"], True, CUST_RES, "tester")
    r = done["result"]
    assert done["status"] == "executed" and r["loaded"] == 9881 and r["quarantined"] == 119
    assert all(c["passed"] for c in r["checks"])
    assert count("SELECT COUNT(*) AS n FROM customer") == 9881
    assert count("SELECT COUNT(*) - COUNT(DISTINCT customer_id) AS n FROM customer") == 0
    assert count("SELECT COUNT(*) AS n FROM dq_quarantine WHERE target_table = 'customer'") == 119
    assert count("SELECT COUNT(*) AS n FROM customer WHERE state NOT IN ('TX','CA','NY','FL','WA','IL','OH','GA','NC','CO')") == 0
    assert raises(lambda: services.decide_approval(ap["id"], True, CUST_RES, "t"), ServiceError).status == 409
    assert raises(lambda: services.request_load(rep["run_id"]), ServiceError).status == 409      # already loaded


def test_titanic_resolutions_fill_values():
    fresh()
    rep, ap = run_and_request(TITANIC, TITANIC_RULES, name="titanic.csv")
    assert ap["payload"]["table"] == "titanic"
    out = services.decide_approval(ap["id"], True, TIT_RES, "t")
    assert out["status"] == "executed" and out["result"]["loaded"] == 891 and out["result"]["resolution_changes"] == 179
    assert count("SELECT COUNT(*) AS n FROM titanic WHERE age IS NULL") == 0
    assert count("SELECT COUNT(*) AS n FROM titanic WHERE embarked IS NULL") == 0
    assert count("SELECT COUNT(*) AS n FROM titanic WHERE cabin IS NULL") == 687          # report-only rule: untouched
    med = float(TITANIC["Age"].median())
    assert count(f"SELECT COUNT(*) AS n FROM titanic WHERE age = {med}") >= 177
    assert services.run_file(rep["run_id"], "resolution_changes.csv").exists()


def test_leave_null_keeps_nulls():
    fresh()
    _, ap = run_and_request(TITANIC, TITANIC_RULES, name="titanic.csv")
    services.decide_approval(ap["id"], True, {"r8": "leave_null", "r9": "leave_null"}, "t")
    assert count("SELECT COUNT(*) AS n FROM titanic WHERE age IS NULL") == 177


def test_quarantine_resolutions_value_rules_apply_first():
    fresh()
    _, ap = run_and_request(CUSTOMER, CUSTOMER_RULES, name="customer.csv")
    out = services.decide_approval(ap["id"], True, {"r1": "quarantine", "r2": "quarantine"}, "t")
    no_email = CUSTOMER["email"].isna()
    rest = CUSTOMER[~no_email]
    expected_q = int(no_email.sum() + rest.duplicated("customer_id", keep=False).sum())
    assert out["result"]["quarantined"] == expected_q and out["result"]["loaded"] == 10000 - expected_q
    assert count("SELECT COUNT(*) AS n FROM customer WHERE email IS NULL") == 0


def test_any_dataset_end_to_end_with_messy_headers():
    fresh()
    rep, ap = run_and_request(orders(), ORDERS_RULES, table="orders")
    assert rep["status"] == "completed_needs_approval"
    assert rep["issues_found"] == {"unique on Order ID": 1, "date_format on order_date": 3,
                                   "allowed_values on country": 5, "range on amount": 2,
                                   "regex on contact": 1, "not_null on amount": 1}
    out = services.decide_approval(ap["id"], True, {"r1": "keep_first", "r5": "set_null"}, "t")
    assert out["status"] == "executed", out["result"]
    assert out["result"]["loaded"] == 7 and out["result"]["quarantined"] == 1 and out["result"]["resolution_changes"] == 1
    assert out["result"]["column_map"]["Order ID"] == "order_id"                 # sanitised header
    rows = db.query("SELECT * FROM orders ORDER BY order_id")
    assert [r["order_id"] for r in rows] == [1, 2, 3, 4, 5, 7, 8]
    assert [r["country"] for r in rows] == ["US", "CA", "CA", "MX", None, "MX", None]
    assert [r["amount"] for r in rows] == [10.0, 0.0, 100000.0, 50.0, 0.0, None, 20.0]
    assert rows[1]["contact"] is None and rows[0]["contact"] == "a@x.com"        # b@x set to NULL by reviewer
    assert [c["name"] for c in db.get_table_schema("orders")][:3] == ["order_id", "order_date", "country"]


def test_rejection_loads_nothing():
    fresh()
    _, ap = run_and_request(TITANIC, TITANIC_RULES, name="titanic.csv")
    assert services.decide_approval(ap["id"], False, None, "t")["status"] == "rejected"
    assert "titanic" not in db.list_tables()


def test_bad_resolution_choices_rejected():
    fresh()
    _, ap = run_and_request(TITANIC, TITANIC_RULES, name="titanic.csv")
    assert "choose one of" in str(raises(lambda: services.decide_approval(ap["id"], True, {"r8": "delete_all", "r9": "fill_mode"}, "t"), ServiceError))
    assert "unknown rule id" in str(raises(lambda: services.decide_approval(ap["id"], True, {**TIT_RES, "r99": "x"}, "t"), ServiceError))
    assert get_status(ap["id"]) == "pending"                                      # rejected choices leave it pending
    # text column offers no numeric fills
    txt = next(r for r in ap["payload"]["required_resolutions"] if r["column"] == "Embarked")
    assert "fill_median" not in txt["options"]


def get_status(aid):
    return services.get_approval(aid)["status"]


def test_memory_suggests_previous_decision_by_check():
    fresh()
    first = services.create_run(CUSTOMER, "customer.csv", GOAL, CUSTOMER_RULES)
    assert first["suggested_resolutions"] == {}
    ap = services.request_load(first["run_id"])
    services.decide_approval(ap["id"], True, CUST_RES, "tester")
    second = services.create_run(CUSTOMER, "customer.csv", GOAL, CUSTOMER_RULES)
    assert second["suggested_resolutions"] == CUST_RES
    assert {d["check_name"] for d in memory.all_decisions()} == {"unique:customer_id", "not_null:email"}
    other = services.create_run(TITANIC, "t.csv", GOAL, TITANIC_RULES)             # different checks: no suggestion
    assert other["suggested_resolutions"] == {}


def test_failed_validation_blocks_load_request():
    fresh()
    rep = services.create_run(TITANIC, "t.csv", GOAL, TITANIC_RULES, fault="always")
    assert rep["status"] == "failed_validation"
    assert raises(lambda: services.request_load(rep["run_id"]), ServiceError).status == 409
    assert raises(lambda: services.run_file(rep["run_id"], "clean.csv"), ServiceError).status == 404


def test_analysis_only_run_cannot_be_loaded():
    fresh()
    rep = services.create_run(TITANIC, "t.csv", "Just profile this", TITANIC_RULES)
    assert rep["status"] == "analysis_only"
    assert raises(lambda: services.request_load(rep["run_id"]), ServiceError).status == 409


def test_invalid_rules_rejected_with_all_problems():
    fresh()
    e = raises(lambda: services.create_run(TITANIC, "t.csv", GOAL, '[{"type":"range","column":"Nope"},{"type":"foo"}]'), ServiceError)
    assert e.status == 422 and "Nope" in str(e) and "foo" in str(e)
    assert raises(lambda: services.create_run(TITANIC, "t.csv", GOAL, "[{oops"), ServiceError).status == 400
    assert services.list_runs() == []                                              # nothing recorded


def test_rules_accepted_as_json_string():
    fresh()
    rep = services.create_run(TITANIC, "t.csv", GOAL, json.dumps(TITANIC_RULES))
    assert rep["rules_source"] == "user" and len(rep["rules"]) == 10


def test_target_table_name_safety():
    fresh()
    rep = services.create_run(TITANIC, "t.csv", GOAL, TITANIC_RULES)
    for bad in ("runs", "approvals", "dq_quarantine", "bad name", "x; DROP TABLE runs", "1abc", "a" * 70):
        assert raises(lambda: services.request_load(rep["run_id"], bad), ServiceError).status == 400, bad
    assert services.request_load(rep["run_id"], "My_Table")["payload"]["table"] == "my_table"
    assert loader.default_table_name("Sales Q1 (final).xlsx") == "sales_q1_final"
    assert loader.default_table_name("2024.csv") == "t_2024"


def test_existing_table_with_different_columns_is_refused():
    fresh()
    _, a1 = run_and_request(CUSTOMER, CUSTOMER_RULES, table="shared")
    services.decide_approval(a1["id"], True, CUST_RES, "t")
    rep = services.create_run(TITANIC, "t.csv", GOAL, TITANIC_RULES)
    a2 = services.request_load(rep["run_id"], "shared")
    e = raises(lambda: services.decide_approval(a2["id"], True, TIT_RES, "t"), ServiceError)
    assert "different columns" in str(e) and get_status(a2["id"]) == "failed"
    assert count("SELECT COUNT(*) AS n FROM shared") == 9881                        # existing data untouched


def test_second_run_appends_to_same_table_without_duplicating_keys():
    fresh()
    for _ in range(2):
        _, ap = run_and_request(CUSTOMER, CUSTOMER_RULES, table="customer")
        out = services.decide_approval(ap["id"], True, CUST_RES, "t")
    assert out["result"]["loaded"] == 0 and out["result"]["skipped_existing"] == 9881
    assert count("SELECT COUNT(*) AS n FROM customer") == 9881 and all(c["passed"] for c in out["result"]["checks"])


def test_reloading_the_same_run_is_idempotent():
    fresh()
    rules = R.validate_rules(CUSTOMER_RULES, CUSTOMER.columns)
    clean, _ = R.apply_fixes(CUSTOMER, rules)
    loadable, quarantined, _, expect = loader.apply_resolutions(clean, rules, CUST_RES)
    a = loader.load_to_db(loadable, quarantined, "runA", len(clean), "customer", rules, expect)
    b = loader.load_to_db(loadable, quarantined, "runA", len(clean), "customer", rules, expect)
    assert a["loaded"] == b["loaded"] == 9881 and a["target_rows"] == b["target_rows"] == 9881
    assert count("SELECT COUNT(*) AS n FROM dq_quarantine") == 119                  # not doubled


def test_load_rolls_back_when_post_load_check_fails():
    fresh()
    rules = R.validate_rules(CUSTOMER_RULES, CUSTOMER.columns)
    clean, _ = R.apply_fixes(CUSTOMER, rules)
    loadable, quarantined, _, expect = loader.apply_resolutions(clean, rules, CUST_RES)
    loadable = loadable.copy()
    loadable.loc[loadable.index[0], "state"] = "ZZ"                                 # corrupt a row after the fact
    e = raises(lambda: loader.load_to_db(loadable, quarantined, "runB", len(clean), "customer", rules, expect), loader.LoadValidationError)
    assert any(c["name"] == "rules_hold_in_target" and not c["passed"] for c in e.checks)
    assert count("SELECT COUNT(*) AS n FROM customer") == 0
    assert count("SELECT COUNT(*) AS n FROM dq_quarantine") == 0


def test_sql_tool_is_read_only_in_two_layers():
    fresh()
    db.init_schema()
    assert raises(lambda: db.execute_select_query("DELETE FROM runs"), PermissionError)
    assert raises(lambda: db.execute_select_query("SELECT 1; DROP TABLE runs"), PermissionError)
    raises(lambda: db.query("DELETE FROM runs", read_only=True))                    # guard bypassed: engine still refuses
    assert raises(lambda: services.sql_query("UPDATE runs SET status='x'"), ServiceError).status == 403


def test_select_tool_limit_cte_and_schema():
    fresh()
    _, ap = run_and_request(CUSTOMER, CUSTOMER_RULES, table="customer")
    services.decide_approval(ap["id"], True, CUST_RES, "t")
    out = db.execute_select_query("SELECT * FROM customer", limit=5)
    assert len(out["rows"]) == 5 and out["truncated"]
    cte = db.execute_select_query("WITH t AS (SELECT state, COUNT(*) AS n FROM customer GROUP BY state) SELECT * FROM t")
    assert cte["rows"] and not cte["truncated"]
    assert "email" in [c["name"] for c in db.get_table_schema("customer")]
    assert raises(lambda: db.get_table_schema("x; drop table y"), ValueError)
    assert "customer" in db.list_tables()


def test_metrics_and_narrative():
    observability.inc("unit_test_total", 2, kind="a")
    assert 'unit_test_total{kind="a"} 2.0' in observability.render()
    text = llm.summarize({"rows": 10, "issues_found": {"null_email": 3}, "fixes_applied": {"state": 2},
                          "safe_to_load": True, "needs_human_approval": ["x"]})
    assert "3 null_email" in text and "2 in state" in text


def test_suggest_endpoint_payload():
    out = services.suggest(TITANIC)
    assert out["rows"] == 891 and len(out["columns"]) == 12 and out["suggested_rules"]
    assert set(out["rule_types"]) == set(R.SPEC)


def test_final_and_quarantine_files_are_downloadable_after_approval():
    fresh()
    rep, ap = run_and_request(TITANIC, TITANIC_RULES, name="titanic.csv")
    clean = pd.read_csv(services.run_file(rep["run_id"], "clean.csv"))
    assert clean["Age"].isna().sum() == 177 and len(pd.read_csv(services.run_file(rep["run_id"], "changes.csv"))) == 0
    assert raises(lambda: services.run_file(rep["run_id"], "final.csv"), ServiceError).status == 404   # not before approval
    services.decide_approval(ap["id"], True, TIT_RES, "t")
    final = pd.read_csv(services.run_file(rep["run_id"], "final.csv"))
    assert len(final) == 891 and final["Age"].isna().sum() == 0 and final["Embarked"].isna().sum() == 0
    assert final["Cabin"].isna().sum() == 687                                                         # report-only: untouched
    assert (final["Age"].round(1) == float(TITANIC["Age"].median())).sum() >= 177
    assert len(pd.read_csv(services.run_file(rep["run_id"], "resolution_changes.csv"))) == 179
    rep2, ap2 = run_and_request(CUSTOMER, CUSTOMER_RULES, name="customer.csv")
    services.decide_approval(ap2["id"], True, {"r1": "quarantine", "r2": "leave_null"}, "t")
    q = pd.read_csv(services.run_file(rep2["run_id"], "quarantine.csv"))
    assert len(q) > 119 and "quarantine_reason" in q.columns


def test_autofilled_age_loads_without_any_decision():
    fresh()
    rules = json.loads((ROOT / "examples" / "titanic_rules_autofill.json").read_text())
    rep, ap = run_and_request(TITANIC, rules, name="titanic.csv")
    assert ap["payload"]["required_resolutions"] == []                    # nothing ambiguous: no decisions to make
    clean = pd.read_csv(services.run_file(rep["run_id"], "clean.csv"))
    assert clean["Age"].isna().sum() == 0 and len(pd.read_csv(services.run_file(rep["run_id"], "changes.csv"))) == 179
    out = services.decide_approval(ap["id"], True, {}, "t")
    assert out["status"] == "executed" and out["result"]["loaded"] == 891
    assert count("SELECT COUNT(*) AS n FROM titanic WHERE age IS NULL") == 0
