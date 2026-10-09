"""Approved-resolution handling and the atomic, verified load into any target table.

The target table is created from the dataset's own columns (names sanitised, types inferred), so nothing here
knows about a particular dataset. Everything the reviewer can choose comes from the rules.
"""
import json
import re

import pandas as pd

from app import db
from app.db import quote_ident as qi
from app.tools import rules as R

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
OWN_COLUMNS = ("dq_flags", "load_run_id")


class LoadValidationError(Exception):
    def __init__(self, checks):
        super().__init__("post-load validation failed; transaction rolled back")
        self.checks = checks


# ---- names and types ------------------------------------------------------------------------

def validate_table_name(name: str) -> str:
    if not isinstance(name, str) or not _IDENT.match(name):
        raise ValueError("table name must be letters, digits and underscores, start with a letter or "
                         "underscore, and be at most 63 characters")
    low = name.lower()
    if low in db.INTERNAL_TABLES or low.startswith("sqlite_"):
        raise ValueError(f"'{name}' is reserved for the application")
    return low


def default_table_name(filename: str) -> str:
    stem = re.sub(r"\.[A-Za-z0-9]+$", "", filename or "")
    base = re.sub(r"[^0-9a-zA-Z]+", "_", stem).strip("_").lower() or "dataset"
    if base[0].isdigit():
        base = "t_" + base
    base = base[:60]
    return base if base not in db.INTERNAL_TABLES else base + "_data"


def safe_columns(cols) -> dict:
    """original column name -> safe lowercase SQL name (unique, never one of our own columns)."""
    mapping, used = {}, set(OWN_COLUMNS)
    for c in cols:
        base = re.sub(r"[^0-9a-zA-Z]+", "_", str(c).strip()).strip("_").lower() or "col"
        if base[0].isdigit():
            base = "c_" + base
        base, name, k = base[:60], base[:60], 2
        while name in used:
            name, k = f"{base}_{k}", k + 1
        used.add(name)
        mapping[c] = name
    return mapping


def sql_type(s: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(s):
        return "BOOLEAN"
    if pd.api.types.is_integer_dtype(s):
        return "BIGINT"
    if pd.api.types.is_float_dtype(s):
        return "DOUBLE PRECISION"
    return "TEXT"


# ---- resolutions ----------------------------------------------------------------------------

def check_resolutions(required: list[dict], resolutions: dict) -> list[str]:
    """Problems with the reviewer's choices (empty list = fine)."""
    problems, known = [], {r["rule_id"] for r in required}
    for r in required:
        if resolutions.get(r["rule_id"]) not in r["options"]:
            problems.append(f'{r["rule_id"]} ({r["name"]}): choose one of {r["options"]}')
    problems += [f"unknown rule id {k!r}" for k in resolutions if k not in known]
    return problems


def _fill_value(series: pd.Series, how: str):
    return R.fill_value(series, how.removeprefix("fill_"))


def apply_resolutions(clean: pd.DataFrame, rules: list[dict], resolutions: dict):
    """Apply the reviewer's choices for 'ask' rules. Nothing is deleted: excluded rows go to quarantine.

    Order: value rules first (fill / set NULL / quarantine), then 'unique' rules among rows still in play.
    Returns (loadable, quarantined, changes_log, expect_clean_rule_ids).
    """
    df = clean.copy()
    reasons = pd.Series("", index=df.index)
    log, expect = [], []
    asks = [r for r in rules if r["action"] == "ask"]

    for r in (x for x in asks if x["type"] != "unique"):
        choice = resolutions.get(r["id"])
        if choice in (None, "leave", "leave_null"):
            continue
        m = R.violation_mask(df, r) & (reasons == "")
        if not m.any():
            continue
        col = r["columns"][0]
        if choice == "quarantine":
            reasons[m] = f'{r["rule_key"]}:quarantine'
        else:
            if choice == "set_null":
                new = {i: None for i in df.index[m]}
            elif choice.startswith("fill_"):
                v = _fill_value(df[col], choice)
                new = {i: v for i in df.index[m]}
            else:
                raise ValueError(f"unknown resolution {choice!r}")
            for i, v in new.items():
                log.append({"row_index": int(i), "column": col, "original": df.at[i, col], "new": v,
                            "rule_id": r["id"], "reason": f'{r["name"]}: resolution {choice}'})
            R.assign(df, col, new)
        expect.append(r["id"])

    for r in (x for x in asks if x["type"] == "unique"):
        choice = resolutions.get(r["id"])
        if choice is None:
            continue
        sub = df[reasons == ""]
        if choice == "quarantine":
            m = R.unique_mask(sub, r["columns"], False)
        elif choice in ("keep_first", "keep_last"):
            m = R.unique_mask(sub, r["columns"], "first" if choice == "keep_first" else "last")
        else:
            raise ValueError(f"unknown resolution {choice!r}")
        reasons[m.index[m]] = f'{r["rule_key"]}:{choice}'
        expect.append(r["id"])

    q = reasons != ""
    loadable = df[~q].copy()
    quarantined = clean[q].assign(quarantine_reason=reasons[q])
    changes = pd.DataFrame(log, columns=["row_index", "column", "original", "new", "rule_id", "reason"])
    return loadable, quarantined, changes, expect


# ---- load -----------------------------------------------------------------------------------

def _records(df: pd.DataFrame, text_cols=()) -> list:
    out = df.astype(object).where(df.notna(), None)
    for c in text_cols:     # TEXT columns must receive text (mixed-type Excel columns, timestamps)
        out[c] = out[c].map(lambda v: None if v is None else str(v))
    return out.values.tolist()


def _key_columns(rules: list[dict]) -> list[str]:
    for r in rules:
        if r["type"] == "unique":
            return list(r["columns"])
    return []


def load_to_db(loadable: pd.DataFrame, quarantined: pd.DataFrame, run_id: str, source_rows: int,
               table: str, rules: list[dict], expect_clean: list[str] = ()) -> dict:
    table = validate_table_name(table)
    src = [c for c in loadable.columns if c != "dq_flags"]
    if not src:
        raise ValueError("nothing to load: the dataset has no columns")
    cmap = safe_columns(src)
    db_cols = [cmap[c] for c in src] + list(OWN_COLUMNS)
    key_cols = [c for c in _key_columns(rules) if c in cmap]
    types = {c: sql_type(loadable[c]) for c in src}
    text_cols = [c for c in src if types[c] == "TEXT"]
    qt = qi(table)

    db.init_schema()
    conn = db.connect()
    try:
        q = lambda sql, p=(): db.conn_query(conn, sql, p)
        count = lambda where="", p=(): q(f"SELECT COUNT(*) AS n FROM {qt} {where}", p)[0]["n"]

        existing = db.conn_table_columns(conn, table)
        if existing:
            if set(existing) != set(db_cols):
                raise ValueError(
                    f"table '{table}' already exists with different columns "
                    f"(has {sorted(existing)}, dataset needs {sorted(db_cols)}). Choose another table name.")
        else:
            cols_ddl = ", ".join(f"{qi(cmap[c])} {types[c]}" for c in src)
            q(f'CREATE TABLE {qt} ({cols_ddl}, {qi("dq_flags")} TEXT, {qi("load_run_id")} TEXT)')
            if key_cols:
                q(f'CREATE UNIQUE INDEX {qi("ux_" + table + "_key")} ON {qt} '
                  f'({", ".join(qi(cmap[c]) for c in key_cols)})')

        q(f'DELETE FROM {qt} WHERE {qi("load_run_id")} = ?', (run_id,))      # makes re-loading a run idempotent
        base = count()
        data = [row + [run_id] for row in _records(loadable[src + ["dq_flags"]], text_cols)]
        placeholders = ", ".join("?" * len(db_cols))
        sql = (f'INSERT INTO {qt} ({", ".join(qi(c) for c in db_cols)}) VALUES ({placeholders}) '
               "ON CONFLICT DO NOTHING")
        cur = conn.cursor()
        cur.executemany(db.sql_for_driver(sql, (1,)), data)

        q("DELETE FROM dq_quarantine WHERE run_id = ?", (run_id,))
        qsrc = quarantined.drop(columns=["quarantine_reason", "dq_flags"], errors="ignore").astype(object)
        qsrc = qsrc.where(quarantined[qsrc.columns].notna(), None)
        qrows = [[run_id, table, int(i), quarantined.at[i, "quarantine_reason"], json.dumps(rec, default=str)]
                 for i, rec in zip(quarantined.index, qsrc.to_dict("records"))]
        if qrows:
            cur.executemany(db.sql_for_driver(
                "INSERT INTO dq_quarantine (run_id, target_table, row_index, reason, row_json) "
                "VALUES (?, ?, ?, ?, ?)", (1,)), qrows)

        after = count()
        inserted = after - base
        skipped = len(loadable) - inserted
        checks = []

        def add(name, ok, detail=""):
            checks.append({"name": name, "passed": bool(ok), "detail": detail})

        add("source_reconciliation", len(loadable) + len(quarantined) == source_rows,
            f"{len(loadable)} loadable + {len(quarantined)} quarantined vs {source_rows} source rows")
        qn = q("SELECT COUNT(*) AS n FROM dq_quarantine WHERE run_id = ?", (run_id,))[0]["n"]
        add("quarantine_reconciliation", qn == len(quarantined), f"{qn} in table vs {len(quarantined)} expected")
        here = count(f'WHERE {qi("load_run_id")} = ?', (run_id,))
        add("rows_present_in_target", here == inserted, f"{here} rows tagged with this run, {inserted} inserted")

        if key_cols:
            kc = ", ".join(qi(cmap[c]) for c in key_cols)
            notnull = " AND ".join(f"{qi(cmap[c])} IS NOT NULL" for c in key_cols)
            d = q(f"SELECT COUNT(*) AS n FROM (SELECT {kc} FROM {qt} WHERE {notnull} "
                  f"GROUP BY {kc} HAVING COUNT(*) > 1) AS d")[0]["n"]
            add("unique_keys_in_target", d == 0, f"{d} duplicated key value(s) on {key_cols}")

        if skipped == 0 and src:
            sums = ", ".join(f'SUM(CASE WHEN {qi(cmap[c])} IS NULL THEN 1 ELSE 0 END) AS n{i}'
                             for i, c in enumerate(src))
            row = q(f'SELECT {sums} FROM {qt} WHERE {qi("load_run_id")} = ?', (run_id,))[0]
            bad = {c: (int(row[f"n{i}"] or 0), int(loadable[c].isna().sum())) for i, c in enumerate(src)
                   if int(row[f"n{i}"] or 0) != int(loadable[c].isna().sum())}
            add("null_counts_match_source", not bad, f"(target, expected) differs: {bad}" if bad else
                f"{len(src)} columns reconciled")

        if len(loadable):
            sel = ", ".join(f'{qi(cmap[c])} AS {qi("c%d" % i)}' for i, c in enumerate(src))
            back = pd.DataFrame(q(f'SELECT {sel} FROM {qt} WHERE {qi("load_run_id")} = ?', (run_id,)))
            back = back.rename(columns={f"c{i}": c for i, c in enumerate(src)}) if len(back) else \
                pd.DataFrame(columns=src)
            must = set(expect_clean) | {r["id"] for r in rules if r["action"] == "fix"}
            viol = {x["rule_id"]: x["count"] for x in R.run_checks(back, [r for r in rules if r["id"] in must])
                    if x["count"]}
            add("rules_hold_in_target", not viol,
                f"still violated in target: {viol}" if viol else f"{len(must)} rule(s) verified on loaded rows")

        if not all(c["passed"] for c in checks):
            conn.rollback()
            raise LoadValidationError(checks)
        conn.commit()
        return {"loaded": inserted, "skipped_existing": skipped, "quarantined": len(quarantined),
                "target_table": table, "target_rows": after, "column_map": cmap, "checks": checks}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
