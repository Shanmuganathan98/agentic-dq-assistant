# Writing checks (rules)

You decide what gets checked. A rule is one JSON object; a rule set is a JSON list. Give the list to the UI
(**Choose the checks**), the API (`rules` field on `POST /runs`) or the CLI (`--rules rules.json`).
With no rules, report-only rules are suggested from the data (`--suggest` prints them so you can edit them).

```json
{"type": "range", "column": "Age", "min": 0, "max": 120, "severity": "medium", "action": "fix"}
```

## Fields every rule has

| Field | Values | Meaning |
|---|---|---|
| `type` | see below | which check |
| `column` | a column name | must exist in the file (`unique` can use `columns`: a list) |
| `severity` | `low` / `medium` / `high` (default `medium`) | for reporting only |
| `action` | `flag` / `fix` / `ask` (default `flag`) | what happens to violations |
| `name` | text (optional) | label in reports |

**Actions**
- `flag`: report only. Data is never changed.
- `fix`: apply the rule's safe fix automatically. Every change is written to the change log and re-checked by validation.
- `ask`: nothing changes until a human picks a resolution at approval time (see *Resolutions*).

## Check types

| `type` | Parameters | Violation | Allowed actions | What `fix` does |
|---|---|---|---|---|
| `not_null` | `blank_is_null` (default true), `fill_with`: `median`, `mean` or `mode` | value missing (blank text counts as missing) | flag, fix, ask | with `fill_with` set: fill gaps with the median, mean or mode of the existing values (median/mean need a numeric column) |
| `unique` | `column` or `columns` | repeated value(s); NULLs ignored; the first occurrence is not counted | flag, ask | n/a |
| `allowed_values` | `values` (list), `synonyms` (`{"Texas": "TX"}`), `normalize` (default true) | present value not in `values` | flag, fix, ask | match ignoring case/whitespace, or via `synonyms`; otherwise set NULL |
| `range` | `min` and/or `max`, `fix_with`: `set_null` (default) or `clip` | numeric value outside the range | flag, fix, ask | set NULL, or clip to the nearest bound |
| `regex` | `pattern` (whole value must match) | present value does not match | flag, fix, ask | set NULL |
| `date_format` | `format` (strftime, e.g. `%Y-%m-%d`) | present value does not parse | flag, fix, ask | set NULL |
| `numeric` | none | present value is not a number | flag, fix, ask | set NULL |

"Present" means not missing, so missing values are only ever reported by `not_null`. Add a `not_null` rule too when
you also care about gaps.

Filling is opt-in: `fix` on `not_null` does nothing until you say how (`fill_with`), and every filled cell is written to
the change log. Example: `{"type": "not_null", "column": "Age", "action": "fix", "fill_with": "median"}`.

## Resolutions (for `ask` rules)

Chosen by a reviewer on the Approvals page (or in the API body), one choice per violated `ask` rule:

| Rule type | Options |
|---|---|
| `unique` | `keep_first`, `keep_last`, `quarantine` (every row of each duplicate group) |
| `not_null` | `leave_null`, `fill_median`, `fill_mean` (numeric columns only), `fill_mode`, `quarantine` |
| anything else | `leave`, `set_null`, `quarantine` |

Order: value rules (fill / set NULL / quarantine) are applied first, then `unique` rules among the rows still in play.
Quarantined rows are never deleted: they go to the `dq_quarantine` table with the reason and the original row.
Fills are written to `resolution_changes.csv` for the run.

The system remembers each choice by check (for example `not_null:Age`) and suggests it next time. It never applies a
remembered choice by itself.

## Examples

Two complete rule sets live in `examples/`: `customer_rules.json` and `titanic_rules.json`.

```json
[
  {"type": "unique", "columns": ["order_id", "line_no"], "action": "ask", "severity": "high"},
  {"type": "allowed_values", "column": "country", "values": ["US", "CA", "MX"],
   "synonyms": {"Canada": "CA", "Mexico": "MX"}, "action": "fix"},
  {"type": "range", "column": "amount", "min": 0, "max": 100000, "fix_with": "clip", "action": "fix"},
  {"type": "regex", "column": "contact", "pattern": "[^@\\s]+@[^@\\s]+\\.[^@\\s]+", "action": "ask"},
  {"type": "date_format", "column": "order_date", "format": "%Y-%m-%d", "action": "fix"}
]
```

Mistakes are reported all at once, for example:
`rule 1 (range): column(s) ['Nope'] not in dataset (available: [...])`.

## What happens at load

The target table is created from the file's own columns: names become lowercase with underscores
(`Order ID` becomes `order_id`), types are inferred (whole numbers, decimals, text), and two columns are added:
`dq_flags` (which flag/ask rules a row violated) and `load_run_id`. The first `unique` rule becomes a unique key.
Dates stay text. If the table already exists with different columns the load is refused.
The load runs in one transaction and is rolled back if any post-load check fails (row reconciliation, quarantine
counts, unique keys, per-column NULL counts, and your `fix` rules re-checked on the loaded rows).
