"""Rule-based DQ checks. Each issue says whether an automatic fix is *safe*."""
import re
import pandas as pd

US_STATES = set("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split())
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _issue(check, column, bad: pd.Series, severity, safe, fix):
    return {"check": check, "column": column, "count": int(bad.sum()),
            "severity": severity, "safe_to_autofix": safe, "proposed_fix": fix,
            "sample_row_indexes": [int(i) for i in bad[bad].index[:5]]}


def run_dq_checks(df: pd.DataFrame, key: str = "customer_id") -> list[dict]:
    issues = []
    if key in df:
        issues.append(_issue("duplicate_key", key, df.duplicated(subset=[key], keep="first"),
                             "high", False,
                             "Needs human review: keep-first could drop the 'right' record"))
    if "email" in df:
        issues.append(_issue("null_email", "email", df["email"].isna(), "medium", False,
                             "Cannot be invented. Load as NULL or send to steward"))
        present = df["email"].notna()
        bad_fmt = present & ~df["email"].astype(str).str.match(EMAIL_RE)
        issues.append(_issue("invalid_email_format", "email", bad_fmt, "medium", False,
                             "Quarantine rows for review"))
    if "signup_date" in df:
        parsed = pd.to_datetime(df["signup_date"], errors="coerce", format="%Y-%m-%d")
        issues.append(_issue("invalid_date", "signup_date", df["signup_date"].notna() & parsed.isna(),
                             "medium", True, "Set to NULL and flag (original value kept in quarantine)"))
    if "state" in df:
        s = df["state"].astype(str).str.strip().str.upper()
        issues.append(_issue("invalid_state_code", "state", df["state"].notna() & ~s.isin(US_STATES),
                             "low", True, "Set to NULL; map full names (e.g. 'Texas') where unambiguous"))
    return [i for i in issues if i["count"] > 0]
