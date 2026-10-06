"""Deterministic profiling tools. No LLM in here: tools do the counting, agents do the reasoning."""
import pandas as pd


def profile_table(df: pd.DataFrame) -> dict:
    cols = []
    for c in df.columns:
        nulls = int(df[c].isna().sum())
        cols.append({
            "name": c,
            "dtype": str(df[c].dtype),
            "nulls": nulls,
            "null_pct": round(100 * nulls / max(len(df), 1), 2),
            "distinct": int(df[c].nunique(dropna=True)),
            "sample": [str(v) for v in df[c].dropna().head(3)],
        })
    return {"row_count": len(df), "column_count": df.shape[1],
            "fully_duplicated_rows": int(df.duplicated().sum()), "columns": cols}


def check_nulls(df: pd.DataFrame) -> dict:
    return {c: int(n) for c, n in df.isna().sum().items() if n > 0}


def check_duplicates(df: pd.DataFrame, key: str) -> dict:
    if key not in df.columns:
        return {"error": f"column '{key}' not found", "available": list(df.columns)}
    dup_mask = df.duplicated(subset=[key], keep="first")
    return {"key": key, "duplicate_rows": int(dup_mask.sum()),
            "distinct_keys_affected": int(df.loc[df.duplicated(subset=[key], keep=False), key].nunique())}


def compare_row_counts(before: pd.DataFrame, after: pd.DataFrame) -> dict:
    return {"before": len(before), "after": len(after), "delta": len(after) - len(before)}
