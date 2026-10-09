"""Pure helpers for the UI's rule builder (no Streamlit import, so they are unit-tested)."""
import re


def _conv(x: str):
    try:
        return int(x)
    except ValueError:
        try:
            return float(x)
        except ValueError:
            return x


def parse_values(text: str) -> list:
    """'S, C, Q' -> ['S','C','Q'];  '0, 1' -> [0, 1]. Numbers only if every item is numeric."""
    items = [x.strip() for x in re.split(r"[,\n]", text or "") if x.strip()]
    converted = [_conv(x) for x in items]
    return converted if converted and all(not isinstance(v, str) for v in converted) else items


def parse_synonyms(text: str) -> dict:
    """One 'wrong=right' pair per line, e.g. 'Texas=TX'. Lines without '=' are ignored."""
    out = {}
    for line in (text or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            if k.strip() and v.strip():
                out[k.strip()] = _conv(v.strip())
    return out


def build_rule(rtype: str, columns: list, action: str = "flag", severity: str = "medium", *,
               values_text: str = "", synonyms_text: str = "", minimum=None, maximum=None,
               fix_with: str = "set_null", fill_with: str = "median", pattern: str = "", fmt: str = "", name: str = "") -> dict:
    """Assemble one rule dict from form fields. The backend validates it; this only drops empty parameters."""
    rule = {"type": rtype, "action": action, "severity": severity}
    if rtype == "unique":
        rule["columns" if len(columns) > 1 else "column"] = columns if len(columns) > 1 else (columns[0] if columns else "")
    else:
        rule["column"] = columns[0] if columns else ""
    if rtype == "allowed_values":
        rule["values"] = parse_values(values_text)
        syn = parse_synonyms(synonyms_text)
        if syn:
            rule["synonyms"] = syn
    elif rtype == "range":
        if minimum is not None:
            rule["min"] = minimum
        if maximum is not None:
            rule["max"] = maximum
        if action == "fix":
            rule["fix_with"] = fix_with
    elif rtype == "regex":
        rule["pattern"] = pattern
    elif rtype == "date_format":
        rule["format"] = fmt
    elif rtype == "not_null" and action == "fix":
        rule["fill_with"] = fill_with
    if name.strip():
        rule["name"] = name.strip()
    return rule


def describe(rule: dict) -> dict:
    cols = rule.get("columns") or [rule.get("column", "")]
    params = {k: v for k, v in rule.items() if k not in ("type", "column", "columns", "action", "severity", "name", "id", "rule_key")}
    return {"check": rule.get("name") or f'{rule["type"]} on {"+".join(cols)}', "type": rule["type"],
            "column(s)": ", ".join(cols), "action": rule.get("action", "flag"),
            "severity": rule.get("severity", "medium"), "parameters": str(params) if params else ""}
