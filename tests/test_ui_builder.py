from helpers import *
sys.path.insert(0, str(ROOT / "ui"))
from rule_builder import build_rule, describe, parse_synonyms, parse_values
from app.tools import rules as R


def test_parse_values():
    assert parse_values("S, C, Q") == ["S", "C", "Q"]
    assert parse_values("0, 1") == [0, 1] and parse_values("1.5, 2") == [1.5, 2]
    assert parse_values("1, a") == ["1", "a"]            # mixed stays text
    assert parse_values("") == [] and parse_values("a\nb") == ["a", "b"]


def test_parse_synonyms():
    assert parse_synonyms("Texas=TX\nbad line\n New York = NY ") == {"Texas": "TX", "New York": "NY"}


def test_built_rules_are_accepted_by_the_backend():
    df = orders()
    built = [
        build_rule("unique", ["Order ID"], "ask", "high"),
        build_rule("unique", ["Order ID", "country"], "flag"),
        build_rule("allowed_values", ["country"], "fix", values_text="US, CA, MX", synonyms_text="Canada=CA"),
        build_rule("range", ["amount"], "fix", minimum=0, maximum=100000, fix_with="clip"),
        build_rule("range", ["amount"], "flag", minimum=0),
        build_rule("regex", ["contact"], "ask", pattern=r"[^@\s]+@[^@\s]+\.[^@\s]+"),
        build_rule("date_format", ["order_date"], "fix", fmt="%Y-%m-%d", name="dates ok"),
        build_rule("not_null", ["contact"], "flag"),
        build_rule("numeric", ["amount"], "flag"),
    ]
    assert len(R.validate_rules(built, df.columns)) == 9
    assert "fix_with" not in build_rule("range", ["amount"], "flag", minimum=0)
    assert describe(built[1])["column(s)"] == "Order ID, country"


def test_fill_rule_built_by_the_form_is_valid():
    rule = build_rule("not_null", ["Age"], "fix", fill_with="median")
    assert rule["fill_with"] == "median" and "fill_with" not in build_rule("not_null", ["Age"], "flag", fill_with="median")
    assert R.validate_rules([rule], TITANIC.columns, TITANIC)[0]["action"] == "fix"
