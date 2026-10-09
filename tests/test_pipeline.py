from helpers import *
from app.pipeline.graph import run_pipeline
from app.tools import rules as R


def test_happy_path_customer():
    s = run_pipeline(CUSTOMER, GOAL, CUSTOMER_RULES)
    assert names(s) == ["orchestrator", "profile", "dq", "clean", "validate", "report"]
    r = s.report
    assert r["status"] == "completed_needs_approval" and r["safe_to_load"] and r["rules_source"] == "user"
    assert r["fixes_applied"] == {"signup_date": 53, "state": 25} and s.attempts == 1
    assert [x["rule_id"] for x in r["required_resolutions"]] == ["r1", "r2"]


def test_titanic_with_user_rules():
    r = run_pipeline(TITANIC, GOAL, TITANIC_RULES).report
    assert r["status"] == "completed_needs_approval" and r["safe_to_load"]
    assert r["issues_found"] == {"not_null on Age": 177, "not_null on Embarked": 2, "not_null on Cabin": 687}
    assert r["reported_only"] == ["not_null on Cabin (687)"]


def test_no_rules_falls_back_to_suggestions_and_does_not_fail():
    """Regression: a dataset with no auto-fixable issues used to be reported as failed_validation."""
    for df in (TITANIC, orders()):
        r = run_pipeline(df, GOAL).report
        assert r["rules_source"] == "suggested" and r["status"] in ("completed", "completed_needs_approval")
        assert r["safe_to_load"] and r["validation"]["passed"]


def test_clean_dataset_completes():
    df = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})
    r = run_pipeline(df, GOAL, [{"type": "not_null", "column": "a"}]).report
    assert r["status"] == "completed" and r["issues_found"] == {} and r["safe_to_load"]


def test_read_only_goal_never_cleans():
    s = run_pipeline(TITANIC, "Just profile this dataset", TITANIC_RULES)
    assert "clean" not in names(s) and s.report["status"] == "analysis_only" and not s.report["safe_to_load"]
    assert s.report["required_resolutions"] == []


def test_fault_once_is_caught_and_retried_on_any_dataset():
    for df, rules in ((CUSTOMER, CUSTOMER_RULES), (TITANIC, TITANIC_RULES), (orders(), ORDERS_RULES)):
        s = run_pipeline(df, GOAL, rules, fault="once")
        assert names(s).count("clean") == 2 and names(s).count("validate") == 2
        assert s.attempts == 2 and s.report["safe_to_load"]
        assert any("FAILED" in t["summary"] and "no_unlogged_changes" in t["summary"] for t in s.trace)


def test_persistent_fault_escalates_and_blocks_load():
    for df, rules in ((TITANIC, TITANIC_RULES), (orders(), ORDERS_RULES)):
        r = run_pipeline(df, GOAL, rules, fault="always").report
        assert r["status"] == "failed_validation" and r["safe_to_load"] is False and r["attempts"] == 2
        assert r["required_resolutions"] == [] and r["fixes_applied"] == {}


def test_original_data_never_mutated():
    for df, rules in ((CUSTOMER, CUSTOMER_RULES), (TITANIC, None), (orders(), ORDERS_RULES)):
        before = df.copy()
        run_pipeline(df, GOAL, rules)
        assert df.equals(before)


def test_invalid_rules_are_rejected_before_any_work():
    e = raises(lambda: run_pipeline(TITANIC, GOAL, [{"type": "range", "column": "Nope", "min": 0}]), R.RuleError)
    assert "not in dataset" in str(e)


def test_validation_detects_a_silent_change_by_a_user_rule_interaction():
    """A fix that NULLs values legitimately raises a not_null count on that column; that must not fail validation."""
    df = pd.DataFrame({"s": ["TX", "Texas", "ZZ"]})
    rules = [{"type": "allowed_values", "column": "s", "values": ["TX"], "action": "fix", "synonyms": {"Texas": "TX"}},
             {"type": "not_null", "column": "s", "action": "flag"}]
    assert run_pipeline(df, GOAL, rules).report["status"] == "completed"


def test_titanic_autofill_rules_fill_age_in_the_cleaned_file_and_pass_validation():
    rules = json.loads((ROOT / "examples" / "titanic_rules_autofill.json").read_text())
    s = run_pipeline(TITANIC, GOAL, rules)
    r = s.report
    assert r["status"] == "completed" and r["safe_to_load"] and s.attempts == 1
    assert r["fixes_applied"] == {"Age": 177, "Embarked": 2}
    assert s.clean_df["Age"].isna().sum() == 0 and len(s.changes) == 179
    assert r["required_resolutions"] == [] and r["reported_only"] == ["not_null on Cabin (687)"]
    f = run_pipeline(TITANIC, GOAL, rules, fault="once")                 # validation still catches an unlogged change
    assert f.attempts == 2 and f.report["safe_to_load"]
