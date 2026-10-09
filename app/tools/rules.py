"""User-defined data-quality rules: validation, detection, safe fixes, suggestions.

Pure pandas, no LLM, no knowledge of any particular dataset. Everything the pipeline checks comes from here.

Rule (JSON):  {"type": "...", "column": "...", <params>, "severity": "low|medium|high", "action": "flag|fix|ask"}
  action  flag = report only, data untouched
          fix  = apply the rule's safe, logged fix automatically (set to NULL / normalise / clip)
          ask  = a human chooses the resolution at approval time, nothing is changed before that
"""
import re
from collections import defaultdict

import pandas as pd


class RuleError(ValueError):
    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


SEVERITIES = ("low", "medium", "high")
COMMON = {"type", "severity", "action", "name"}
ALL3 = ["flag", "fix", "ask"]
SPEC = {
    "not_null": {"required": ["column"], "optional": {"blank_is_null": True, "fill_with": None}, "actions": ALL3,
                 "doc": "Value must be present (blank text counts as missing unless blank_is_null=false). "
                        "fix = fill gaps with the column's median, mean or mode (set fill_with)."},
    "unique": {"required": [], "optional": {"column": None, "columns": None}, "actions": ["flag", "ask"],
               "doc": "No repeated values. Give 'column' or a 'columns' list for a composite key. NULLs are ignored."},
    "allowed_values": {"required": ["column", "values"], "optional": {"normalize": True, "synonyms": {}}, "actions": ALL3,
                       "doc": "Value must be exactly one of 'values'. fix = normalise case/whitespace or apply 'synonyms', else NULL."},
    "range": {"required": ["column"], "optional": {"min": None, "max": None, "fix_with": "set_null"}, "actions": ALL3,
              "doc": "Numeric value within [min, max]. fix_with: set_null | clip."},
    "regex": {"required": ["column", "pattern"], "optional": {}, "actions": ALL3,
              "doc": "Whole value must match the regular expression."},
    "date_format": {"required": ["column", "format"], "optional": {}, "actions": ALL3,
                    "doc": "Value must parse as a date with this strftime format, e.g. %Y-%m-%d."},
    "numeric": {"required": ["column"], "optional": {}, "actions": ALL3,
                "doc": "Value must be a number."},
}
ASK_OPTIONS = {"not_null": ["leave_null", "fill_median", "fill_mean", "fill_mode", "quarantine"],
               "unique": ["keep_first", "keep_last", "quarantine"]}
DEFAULT_ASK = ["leave", "set_null", "quarantine"]
FILL_METHODS = ("median", "mean", "mode")


def fill_value(series: pd.Series, how: str):
    """Median/mean/mode of the values that are present. Raises ValueError when it cannot be computed."""
    present = series[~_missing(series)]
    if present.empty:
        raise ValueError("cannot fill: the column has no values to learn from")
    if how == "mode":
        return present.mode().iloc[0]
    num = pd.to_numeric(present, errors="coerce").dropna()
    if num.empty:
        raise ValueError(f"cannot {how}: column is not numeric")
    return float(num.median() if how == "median" else num.mean())


def resolution_options(rule: dict, series: pd.Series | None = None) -> list[str]:
    if rule["action"] != "ask":
        return []
    opts = list(ASK_OPTIONS.get(rule["type"], DEFAULT_ASK))
    if rule["type"] == "not_null" and series is not None and not pd.api.types.is_numeric_dtype(series):
        opts = [o for o in opts if o not in ("fill_median", "fill_mean")]   # numeric-only fills
    return opts


def _canon(x) -> str:
    if isinstance(x, float) and x.is_integer():
        return str(int(x))
    return str(x)


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


# ---- validation -----------------------------------------------------------------------------

def validate_rules(rules, columns=None, df=None) -> list[dict]:
    """Normalise user rules. Raises RuleError listing every problem at once."""
    if not isinstance(rules, list):
        raise RuleError(["rules must be a JSON list"])
    problems, out = [], []
    for i, raw in enumerate(rules, 1):
        w = f"rule {i}"
        if not isinstance(raw, dict):
            problems.append(f"{w}: must be an object")
            continue
        raw = {k: v for k, v in raw.items() if k not in ("id", "rule_key")}   # derived fields are ignored
        t = raw.get("type")
        spec = SPEC.get(t)
        if spec is None:
            problems.append(f"{w}: unknown type {t!r}; choose from {sorted(SPEC)}")
            continue
        allowed = COMMON | set(spec["required"]) | set(spec["optional"])
        local = []
        if set(raw) - allowed:
            local.append(f"unknown field(s) {sorted(set(raw) - allowed)}")
        missing = [k for k in spec["required"] if raw.get(k) in (None, "", [])]
        if missing:
            local.append(f"missing {missing}")
        if t == "unique":
            cols = raw.get("columns") or ([raw["column"]] if raw.get("column") else [])
            cols = [cols] if isinstance(cols, str) else list(cols)
            if not cols:
                local.append("give 'column' or 'columns'")
        else:
            cols = [raw["column"]] if raw.get("column") else []
        if cols and not all(isinstance(c, str) for c in cols):
            local.append("column names must be text")
        elif columns is not None and cols:
            bad = [c for c in cols if c not in columns]
            if bad:
                local.append(f"column(s) {bad} not in dataset (available: {list(columns)})")
        action, severity = raw.get("action", "flag"), raw.get("severity", "medium")
        if action not in spec["actions"]:
            local.append(f"action {action!r} not allowed for {t}; use one of {spec['actions']}")
        if severity not in SEVERITIES:
            local.append(f"severity must be one of {list(SEVERITIES)}")
        r = {**spec["optional"], **{k: v for k, v in raw.items() if k in allowed}}
        if not local:
            if t == "allowed_values":
                if not isinstance(r["values"], list) or not r["values"]:
                    local.append("'values' must be a non-empty list")
                elif not isinstance(r["synonyms"], dict):
                    local.append("'synonyms' must be an object like {\"Texas\": \"TX\"}")
                else:
                    canon = {_canon(v) for v in r["values"]}
                    bad = [v for v in r["synonyms"].values() if _canon(v) not in canon]
                    if bad:
                        local.append(f"synonym targets {bad} are not in 'values'")
            elif t == "range":
                if r["min"] is None and r["max"] is None:
                    local.append("give 'min' and/or 'max'")
                elif any(v is not None and not _num(v) for v in (r["min"], r["max"])):
                    local.append("'min' and 'max' must be numbers")
                elif r["fix_with"] not in ("set_null", "clip"):
                    local.append("fix_with must be set_null or clip")
            elif t == "regex":
                try:
                    re.compile(r["pattern"])
                except re.error as e:
                    local.append(f"invalid regular expression: {e}")
            elif t == "date_format" and not isinstance(r["format"], str):
                local.append("'format' must be text, e.g. %Y-%m-%d")
            elif t == "not_null" and (action == "fix" or r["fill_with"] is not None):
                if r["fill_with"] not in FILL_METHODS:
                    local.append(f"{'action fix needs ' if action == 'fix' else ''}'fill_with' set to one of {list(FILL_METHODS)}")
        if local:
            problems += [f"{w} ({t}): {p}" for p in local]
            continue
        label = "+".join(cols)
        r.update({"id": f"r{i}", "type": t, "columns": cols, "column": label, "severity": severity,
                  "action": action, "name": raw.get("name") or f"{t} on {label}", "rule_key": f"{t}:{label}"})
        out.append(r)
    if problems:
        raise RuleError(problems)
    seen = {}
    for r in out:
        seen[r["name"]] = seen.get(r["name"], 0) + 1
        if seen[r["name"]] > 1:
            r["name"] = f'{r["name"]} ({r["id"]})'
    if df is not None:          # median/mean need numbers: catch it before any work is done
        bad = [f'{r["id"]} ({r["name"]}): fill_with {r["fill_with"]} needs a numeric column, but {r["column"]!r} is not'
               for r in out if r["type"] == "not_null" and r["action"] == "fix"
               and r["fill_with"] in ("median", "mean") and not pd.api.types.is_numeric_dtype(df[r["column"]])]
        if bad:
            raise RuleError(bad)
    return out


# ---- detection ------------------------------------------------------------------------------

def _is_text(s):
    return s.dtype == object or pd.api.types.is_string_dtype(s)


def _missing(s: pd.Series, blank_is_null: bool = True) -> pd.Series:
    m = s.isna()
    if blank_is_null and _is_text(s):
        m = m | (s.astype(str).str.strip().eq("") & s.notna())
    return m


def unique_mask(df, cols, keep):
    sub = df[cols]
    return sub.duplicated(keep=keep) & ~sub.isna().any(axis=1)


def violation_mask(df: pd.DataFrame, rule: dict) -> pd.Series:
    """True for each row violating the rule. For 'unique' the first occurrence is NOT counted."""
    t, cols = rule["type"], rule["columns"]
    if t == "not_null":
        return _missing(df[cols[0]], rule["blank_is_null"])
    if t == "unique":
        return unique_mask(df, cols, "first")
    s = df[cols[0]]
    present = ~_missing(s)
    if t == "allowed_values":
        allowed = {_canon(v) for v in rule["values"]}
        return present & ~s.map(_canon).isin(allowed)
    if t == "range":
        num = pd.to_numeric(s, errors="coerce")
        bad = pd.Series(False, index=s.index)
        if rule["min"] is not None:
            bad |= num < rule["min"]
        if rule["max"] is not None:
            bad |= num > rule["max"]
        return present & bad
    if t == "regex":
        pat = re.compile(rule["pattern"])
        ok = s.map(lambda x: isinstance(x, str) and bool(pat.fullmatch(x)) or
                   (not isinstance(x, str) and not pd.isna(x) and bool(pat.fullmatch(_canon(x)))))
        return present & ~ok.astype(bool)
    if t == "date_format":
        return present & pd.to_datetime(s.astype(str), errors="coerce", format=rule["format"]).isna()
    if t == "numeric":
        return present & pd.to_numeric(s, errors="coerce").isna()
    raise RuleError([f"unknown type {t}"])


def group_mask(df, rule):
    """Like violation_mask, but for 'unique' flags every member of a duplicate group."""
    return unique_mask(df, rule["columns"], False) if rule["type"] == "unique" else violation_mask(df, rule)


def describe_fix(rule: dict) -> str:
    a, t = rule["action"], rule["type"]
    if a == "flag":
        return "Reported only; data is not changed."
    if a == "fix" and t == "not_null":
        return f"Fill missing values with the column's {rule['fill_with']} (of the existing values). Logged."
    if a == "ask":
        return "Needs a human decision at approval: " + ", ".join(resolution_options(rule))
    if t == "allowed_values":
        return "Normalise case/whitespace (and apply synonyms) to a listed value; anything else becomes NULL. Logged."
    if t == "range" and rule["fix_with"] == "clip":
        return "Clip to the allowed range. Logged."
    return "Set the invalid value to NULL. Logged."


def run_checks(df: pd.DataFrame, rules: list[dict]) -> list[dict]:
    results = []
    for r in rules:
        m = violation_mask(df, r)
        results.append({
            "rule_id": r["id"], "rule_key": r["rule_key"], "name": r["name"], "type": r["type"],
            "column": r["column"], "severity": r["severity"], "action": r["action"],
            "count": int(m.sum()), "sample_row_indexes": [int(i) for i in m.index[m][:5]],
            "proposed_fix": describe_fix(r), "safe_to_autofix": r["action"] == "fix",
            "resolution_options": resolution_options(r, df[r["columns"][0]]),
        })
    return results


# ---- fixes ----------------------------------------------------------------------------------

def assign(df: pd.DataFrame, col: str, mapping: dict) -> None:
    """Set df.loc[idx, col] = value for many cells, upcasting integer columns when needed."""
    if not mapping:
        return
    s = df[col]
    if pd.api.types.is_integer_dtype(s) and any(
            v is None or (isinstance(v, float) and not v.is_integer()) for v in mapping.values()):
        df[col] = s.astype("float64")
    groups = defaultdict(list)
    for idx, v in mapping.items():
        groups[(None if v is None else v)].append(idx)
    for v, idxs in groups.items():
        df.loc[idxs, col] = v


def _fold(x, normalize):
    s = _canon(x)
    return s.strip().lower() if normalize else s


def apply_fixes(df: pd.DataFrame, rules: list[dict]):
    """Apply rules with action 'fix'. Returns (clean_df_with_dq_flags, change_log_df). Rows are never dropped."""
    clean, log = df.copy(), []
    for r in rules:
        if r["action"] != "fix":
            continue
        col, t = r["columns"][0], r["type"]
        mask = violation_mask(clean, r)
        if not mask.any():
            continue
        s = clean[col]
        new = {}
        if t == "not_null":
            try:
                v = fill_value(s, r["fill_with"])
            except ValueError:
                continue                 # nothing to learn from: leave it, validation will flag the unresolved rule
            new = {i: v for i in clean.index[mask]}
            reason = f'filled with {r["fill_with"]} ({round(v, 4) if isinstance(v, float) else v})'
        elif t == "allowed_values":
            lookup = {_fold(v, r["normalize"]): v for v in r["values"]}
            lookup.update({_fold(k, r["normalize"]): v for k, v in r["synonyms"].items()})
            new = {i: lookup.get(_fold(s.at[i], r["normalize"])) for i in clean.index[mask]}
            reason = "normalised to allowed value, else NULL"
        elif t == "range" and r["fix_with"] == "clip":
            num = pd.to_numeric(s, errors="coerce")
            for i in clean.index[mask]:
                lo = r["min"] is not None and num.at[i] < r["min"]
                new[i] = r["min"] if lo else r["max"]
            reason = "clipped to range"
        else:
            new = {i: None for i in clean.index[mask]}
            reason = "invalid value -> NULL"
        for i, v in new.items():
            log.append({"row_index": int(i), "column": col, "original": s.at[i], "new": v,
                        "rule_id": r["id"], "reason": f'{r["name"]}: {reason}'})
        assign(clean, col, new)
    flags = pd.Series("", index=clean.index)
    for r in rules:
        if r["action"] in ("flag", "ask"):
            m = group_mask(clean, r)
            flags[m] = flags[m] + r["rule_key"] + ";"
    clean["dq_flags"] = flags.str.rstrip(";")
    return clean, pd.DataFrame(log, columns=["row_index", "column", "original", "new", "rule_id", "reason"])


# ---- suggestions ----------------------------------------------------------------------------

DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d", "%d-%m-%Y", "%Y-%m-%d %H:%M:%S"]
RANGE_HINTS = [(r"(^|_)age$", 0, 120), (r"fare|price|amount|cost|salary|revenue|qty|quantity|count$", 0, None)]
EMAIL = r"[^@\s]+@[^@\s]+\.[^@\s]+"


def suggest_rules(df: pd.DataFrame) -> list[dict]:
    """Starter rules inferred from the data. Everything is 'flag' (report-only): a human must opt in to changes."""
    out, n = [], len(df)
    for c in df.columns:
        s, low = df[c], str(c).lower()
        present = s[~_missing(s)]
        if re.search(r"(id|key)$", low) and len(present) and present.nunique() / len(present) >= 0.9:
            out.append({"type": "unique", "column": c, "severity": "high", "action": "flag",
                        "name": f"{c} should be unique ({present.nunique()} distinct of {len(present)})"})
        nulls = int(_missing(s).sum())
        if nulls:
            out.append({"type": "not_null", "column": c, "severity": "medium", "action": "flag",
                        "name": f"{c} has {nulls} missing ({100 * nulls / n:.1f}%)"})
        if "mail" in low and _is_text(s):
            out.append({"type": "regex", "column": c, "pattern": EMAIL, "severity": "medium", "action": "flag",
                        "name": f"{c} should look like an email"})
        if _is_text(s) and len(present) >= 5:
            vals = present.astype(str).head(2000)
            best = None
            for f in DATE_FORMATS:
                ok = pd.to_datetime(vals, errors="coerce", format=f).notna().mean()
                if ok >= 0.9 and (best is None or ok > best[1]):
                    best = (f, ok)
            if best:
                out.append({"type": "date_format", "column": c, "format": best[0], "severity": "medium",
                            "action": "flag", "name": f"{c} should be a date ({best[0]}, {best[1]:.1%} already are)"})
            elif 0.95 <= pd.to_numeric(vals, errors="coerce").notna().mean() < 1:
                out.append({"type": "numeric", "column": c, "severity": "medium", "action": "flag",
                            "name": f"{c} is mostly numeric but has non-numeric values"})
            elif 2 <= present.nunique() <= 20 and "name" not in low:
                freq = present.astype(str).str.strip().value_counts(normalize=True)
                common = [v for v, f in freq.items() if f >= 0.02]
                if len(common) >= 2:
                    out.append({"type": "allowed_values", "column": c, "values": sorted(common), "severity": "low",
                                "action": "flag", "name": f"{c} should be one of its {len(common)} common values"})
        elif pd.api.types.is_integer_dtype(s) and 2 <= present.nunique() <= 4:
            out.append({"type": "allowed_values", "column": c, "values": sorted(int(v) for v in present.unique()),
                        "severity": "low", "action": "flag", "name": f"{c} should be one of its observed values"})
        if pd.api.types.is_numeric_dtype(s) and not pd.api.types.is_bool_dtype(s):
            for pat, lo, hi in RANGE_HINTS:
                if re.search(pat, low):
                    rule = {"type": "range", "column": c, "severity": "medium", "action": "flag",
                            "name": f"{c} should be within a plausible range"}
                    rule.update({k: v for k, v in (("min", lo), ("max", hi)) if v is not None})
                    out.append(rule)
                    break
    return out
