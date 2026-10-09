from helpers import *
from app.tools import rules as R


def V(rules, df):
    return R.validate_rules(rules, df.columns)


def counts(df, rules):
    return {x["rule_id"]: x["count"] for x in R.run_checks(df, V(rules, df))}


def test_validation_lists_every_problem():
    e = raises(lambda: V([{"type": "range", "column": "Nope"}, {"type": "foo"},
                          {"type": "regex", "column": "Sex", "pattern": "("},
                          {"type": "unique", "column": "PassengerId", "action": "fix"},
                          {"type": "range", "column": "Age"},
                          {"type": "allowed_values", "column": "Sex", "values": ["m"], "synonyms": {"x": "zz"}},
                          {"type": "unique"}, "oops", {"type": "numeric", "column": "Age", "bogus": 1}], TITANIC), R.RuleError)
    text = "\n".join(e.problems)
    for needle in ("not in dataset", "unknown type", "invalid regular expression", "action 'fix' not allowed",
                   "give 'min' and/or 'max'", "synonym targets", "give 'column' or 'columns'", "must be an object",
                   "unknown field"):
        assert needle in text, needle
    assert raises(lambda: R.validate_rules({"a": 1}), R.RuleError)


def test_customer_counts_match_injected_defects():
    c = {x["name"]: x["count"] for x in R.run_checks(CUSTOMER, V(CUSTOMER_RULES, CUSTOMER))}
    assert c["unique on customer_id"] == 119          # 120 injected, one random collision
    assert c["not_null on email"] == 340
    assert c["date_format on signup_date"] == 53
    assert c["allowed_values on state"] == 25
    assert c["regex on email"] == 0


def test_titanic_counts():
    c = {x["name"]: x["count"] for x in R.run_checks(TITANIC, V(TITANIC_RULES, TITANIC))}
    assert c["not_null on Age"] == 177 and c["not_null on Cabin"] == 687 and c["not_null on Embarked"] == 2
    assert c["unique on PassengerId"] == 0 and c["allowed_values on Survived"] == 0


def test_each_rule_type_detects():
    df = pd.DataFrame({"a": [1, 2, 2, None], "t": ["x", " ", "x", "y"], "n": ["1", "2.5", "abc", None],
                       "d": ["2020-01-01", "2020-13-01", None, "2020-02-02"], "r": [5, -1, 11, None]})
    got = counts(df, [{"type": "unique", "column": "a"},                       # r1: NULLs ignored
                      {"type": "not_null", "column": "t"},                     # r2: blank text counts as missing
                      {"type": "not_null", "column": "t", "blank_is_null": False},  # r3
                      {"type": "numeric", "column": "n"},                      # r4
                      {"type": "date_format", "column": "d", "format": "%Y-%m-%d"},  # r5
                      {"type": "range", "column": "r", "min": 0, "max": 10},   # r6
                      {"type": "allowed_values", "column": "t", "values": ["x", "y"]}])  # r7 (blank is "missing", not wrong)
    assert got == {"r1": 1, "r2": 1, "r3": 0, "r4": 1, "r5": 1, "r6": 2, "r7": 0}


def test_composite_unique_and_flags():
    df = pd.DataFrame({"a": [1, 1, 1, 2], "b": ["x", "x", "y", "x"]})
    rules = V([{"type": "unique", "columns": ["a", "b"], "action": "flag"}], df)
    assert R.run_checks(df, rules)[0]["count"] == 1
    clean, log = R.apply_fixes(df, rules)
    assert len(log) == 0 and clean["dq_flags"].tolist() == ["unique:a+b", "unique:a+b", "", ""]


def test_fixes_are_logged_and_never_drop_rows():
    df = orders()
    rules = V(ORDERS_RULES, df)
    clean, log = R.apply_fixes(df, rules)
    assert len(clean) == len(df)
    norm = lambda col: [None if pd.isna(v) else v for v in clean[col]]     # pandas 3 stores missing text as nan
    assert norm("country") == ["US", "CA", "CA", "MX", None, "US", "MX", None]
    assert norm("order_date") == ["2024-01-05", None, None, "2024-03-10", "2024-03-11", "2024-03-12", None, "2024-04-01"]
    assert clean["amount"].tolist()[:3] == [10.0, 0.0, 100000.0]
    assert log.groupby("column").size().to_dict() == {"order_date": 3, "country": 5, "amount": 2}
    assert set(log.columns) == {"row_index", "column", "original", "new", "rule_id", "reason"}
    after = {x["rule_id"]: x["count"] for x in R.run_checks(clean.drop(columns="dq_flags"), rules) if x["action"] == "fix"}
    assert after == {"r2": 0, "r3": 0, "r4": 0}


def test_integer_column_survives_null_fix():
    df = pd.DataFrame({"q": [1, 2, 99, 3]})
    clean, log = R.apply_fixes(df, V([{"type": "range", "column": "q", "min": 0, "max": 10, "action": "fix"}], df))
    assert clean["q"].isna().sum() == 1 and clean["q"].tolist()[:2] == [1, 2] and len(log) == 1


def test_inputs_never_mutated():
    for df, rules in ((CUSTOMER, CUSTOMER_RULES), (orders(), ORDERS_RULES)):
        before = df.copy()
        R.apply_fixes(df, V(rules, df))
        assert df.equals(before)


def test_suggestions_work_on_any_data_and_are_report_only():
    for df in (CUSTOMER, TITANIC, orders()):
        sug = R.suggest_rules(df)
        assert sug and all(s["action"] == "flag" for s in sug)
        V(sug, df)                                           # every suggestion must itself be valid
    t = {(s["type"], s["column"]) for s in R.suggest_rules(TITANIC)}
    assert ("unique", "PassengerId") in t and ("not_null", "Age") in t and ("range", "Age") in t
    assert ("allowed_values", "first_name") not in {(s["type"], s["column"]) for s in R.suggest_rules(CUSTOMER)}


def test_resolution_options_depend_on_column_type():
    df = pd.DataFrame({"num": [1.0, None], "txt": ["a", None]})
    res = {x["column"]: x["resolution_options"] for x in
           R.run_checks(df, V([{"type": "not_null", "column": c, "action": "ask"} for c in ("num", "txt")], df))}
    assert "fill_median" in res["num"] and "fill_median" not in res["txt"] and "fill_mode" in res["txt"]


def test_duplicate_names_are_made_unique():
    df = pd.DataFrame({"a": [1]})
    assert len({r["name"] for r in V([{"type": "not_null", "column": "a"}] * 3, df)}) == 3


def test_docs_describe_every_rule_type_and_action():
    text = (ROOT / "docs" / "RULES.md").read_text()
    for t, spec in R.SPEC.items():
        assert f"`{t}`" in text, t
        for p in list(spec["optional"]) + spec["required"]:
            assert f"`{p}`" in text or p == "column", (t, p)
    for opts in list(R.ASK_OPTIONS.values()) + [R.DEFAULT_ASK]:
        for o in opts:
            assert f"`{o}`" in text, o


def test_not_null_fix_fills_with_median_mean_mode_and_logs_it():
    rules = V([{"type": "not_null", "column": "Age", "action": "fix", "fill_with": "median"},
               {"type": "not_null", "column": "Embarked", "action": "fix", "fill_with": "mode"}], TITANIC)
    clean, log = R.apply_fixes(TITANIC, rules)
    assert clean["Age"].isna().sum() == 0 and clean["Embarked"].isna().sum() == 0
    med = float(TITANIC["Age"].median())
    assert (clean.loc[TITANIC["Age"].isna(), "Age"] == med).all()
    assert (clean.loc[TITANIC["Embarked"].isna(), "Embarked"] == "S").all()
    assert log.groupby("column").size().to_dict() == {"Age": 177, "Embarked": 2}
    assert log["reason"].str.contains("filled with median").any()
    mean_rule = V([{"type": "not_null", "column": "Age", "action": "fix", "fill_with": "mean"}], TITANIC)
    c2, _ = R.apply_fixes(TITANIC, mean_rule)
    assert abs(c2.loc[TITANIC["Age"].isna(), "Age"].iloc[0] - TITANIC["Age"].mean()) < 1e-9


def test_not_null_fix_needs_fill_with_and_a_numeric_column_for_median():
    e = raises(lambda: V([{"type": "not_null", "column": "Age", "action": "fix"}], TITANIC), R.RuleError)
    assert "fill_with" in str(e)
    assert "fill_with" in str(raises(lambda: V([{"type": "not_null", "column": "Age", "fill_with": "max"}], TITANIC), R.RuleError))
    e = raises(lambda: R.validate_rules([{"type": "not_null", "column": "Embarked", "action": "fix", "fill_with": "median"}],
                                        TITANIC.columns, TITANIC), R.RuleError)
    assert "numeric" in str(e)


def test_fill_with_empty_column_is_left_alone_and_caught_by_validation():
    from app.pipeline.graph import run_pipeline
    df = pd.DataFrame({"a": [None, None, None], "b": [1, 2, 3]})
    r = run_pipeline(df, "prepare for loading", [{"type": "not_null", "column": "a", "action": "fix", "fill_with": "mode"}]).report
    assert r["status"] == "failed_validation" and not r["safe_to_load"]
