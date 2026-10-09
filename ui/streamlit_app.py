"""Streamlit front end. Talks to the FastAPI backend only (no direct DB access).
Run:  streamlit run ui/streamlit_app.py     (API_URL defaults to http://localhost:8000)
"""
import json
import os

import pandas as pd
import requests
import streamlit as st

from rule_builder import build_rule, describe

API = os.getenv("API_URL", "http://localhost:8000")
HEADERS = {"X-API-Key": os.getenv("API_KEY", "")} if os.getenv("API_KEY") else {}

st.set_page_config(page_title="Agentic DQ Assistant", layout="wide")


def call(method: str, path: str, **kw):
    r = requests.request(method, API + path, headers=HEADERS, timeout=300, **kw)
    if not r.ok:
        try:
            detail = r.json().get("detail", r.text)
        except ValueError:
            detail = r.text
        raise RuntimeError(f"{r.status_code}: {detail}")
    return r


def file_part(up):
    return {"file": (up.name, up.getvalue(), "application/octet-stream")}


STATUS_ICON = {"completed": "✅", "completed_needs_approval": "🟡", "failed_validation": "❌", "analysis_only": "🔍"}
ACTION_HELP = {"flag": "report only, data untouched", "fix": "apply the safe fix automatically (logged)",
               "ask": "a human chooses what to do at approval time"}

page = st.sidebar.radio("Page", ["Run pipeline", "Approvals", "SQL explorer", "History & memory"])
st.sidebar.caption(f"API: {API}")


def set_rules(new_rules):
    st.session_state["rules"] = new_rules
    st.session_state["ver"] = st.session_state.get("ver", 0) + 1
    st.rerun()


# ------------------------------------------------------------------------------------------
if page == "Run pipeline":
    st.title("Agentic Data-Quality Assistant")
    up = st.file_uploader("1. Upload a dataset", type=["csv", "tsv", "txt", "xlsx", "xlsm"])

    sheet = ""
    if up is not None and up.name.lower().endswith((".xlsx", ".xlsm")):
        try:
            sheets = call("POST", "/sheets", files=file_part(up)).json()
            sheet = st.selectbox("Sheet", sheets) if len(sheets) > 1 else (sheets[0] if sheets else "")
        except Exception as e:
            st.error(str(e))

    if up is not None:
        key = (up.name, up.size, sheet)
        if st.session_state.get("file_key") != key:
            try:
                meta = call("POST", "/suggest-rules", files=file_part(up), data={"sheet": sheet}).json()
            except Exception as e:
                st.error(str(e))
                st.stop()
            st.session_state.update(file_key=key, meta=meta, rules=[], report=None, ver=0)
        meta = st.session_state["meta"]
        rules = st.session_state["rules"]
        ver = st.session_state.get("ver", 0)
        cols = [c["name"] for c in meta["columns"]]

        st.caption(f'{meta["rows"]} rows, {len(cols)} columns')
        st.dataframe(pd.DataFrame(meta["columns"]), hide_index=True)

        # ---- 2. choose the checks ---------------------------------------------------------
        st.subheader("2. Choose the checks")
        st.caption("Each check has an action: **flag** = report only · **fix** = apply the safe fix automatically · "
                   "**ask** = a human decides at approval time. With no checks chosen, report-only checks are "
                   "suggested from the data.")
        b1, b2, b3 = st.columns(3)
        if b1.button("Use suggested checks"):
            set_rules(json.loads(json.dumps(meta["suggested_rules"])))
        if b2.button("Clear checks"):
            set_rules([])
        imported = b3.file_uploader("Import checks (JSON)", type="json", key=f"import{ver}")
        if imported is not None:
            try:
                data = json.loads(imported.getvalue())
                set_rules(data if isinstance(data, list) else [])
            except ValueError:
                st.error("That file is not valid JSON.")

        if rules:
            table = pd.DataFrame([describe(r) for r in rules])
            table.index = range(1, len(table) + 1)
            st.dataframe(table)
            rm = st.selectbox("Remove a check", ["-"] + [f"{i}. {d}" for i, d in zip(table.index, table["check"])])
            if rm != "-" and st.button("Remove"):
                set_rules([r for i, r in enumerate(rules, 1) if i != int(rm.split(".")[0])])
        else:
            st.info("No checks chosen yet.")

        st.markdown("**Add a check**")
        rtype = st.selectbox("Check type", list(meta["rule_types"]),
                             format_func=lambda t: f'{t} — {meta["rule_types"][t]["doc"]}')
        spec = meta["rule_types"][rtype]
        with st.form(f"add{ver}"):
            if rtype == "unique":
                chosen = st.multiselect("Column(s) that must be unique together", cols)
            else:
                chosen = [st.selectbox("Column", cols)]
            f1, f2 = st.columns(2)
            action = f1.selectbox("Action", spec["actions"], format_func=lambda a: f"{a} — {ACTION_HELP[a]}")
            severity = f2.selectbox("Severity", ["medium", "high", "low"])
            values_text = syn_text = pattern = ""
            fmt = "%Y-%m-%d"
            minimum = maximum = None
            fix_with = "set_null"
            fill_with = "median"
            if rtype == "allowed_values":
                values_text = st.text_area("Allowed values (comma separated)", placeholder="S, C, Q")
                syn_text = st.text_area("Synonyms for fix (one 'wrong=right' per line)", placeholder="Texas=TX")
            elif rtype == "range":
                m1, m2 = st.columns(2)
                use_min, use_max = m1.checkbox("Set a minimum"), m2.checkbox("Set a maximum")
                lo, hi = m1.number_input("Minimum", value=0.0), m2.number_input("Maximum", value=100.0)
                minimum, maximum = (lo if use_min else None), (hi if use_max else None)
                fix_with = st.selectbox("When action is fix", ["set_null", "clip"])
            elif rtype == "regex":
                pattern = st.text_input("Pattern (the whole value must match)", r"[^@\s]+@[^@\s]+\.[^@\s]+")
            elif rtype == "date_format":
                fmt = st.text_input("Date format", "%Y-%m-%d")
            elif rtype == "not_null":
                fill_with = st.selectbox("Fill with (used when the action is fix)", ["median", "mean", "mode"],
                                         help="median and mean need a numeric column such as Age")
            name = st.text_input("Name (optional)")
            if st.form_submit_button("Add check"):
                if not chosen:
                    st.error("Pick at least one column.")
                else:
                    set_rules(rules + [build_rule(rtype, chosen, action, severity, values_text=values_text,
                                                  synonyms_text=syn_text, minimum=minimum, maximum=maximum,
                                                  fix_with=fix_with, fill_with=fill_with, pattern=pattern, fmt=fmt, name=name)])

        with st.expander("Edit all checks as JSON"):
            text = st.text_area("JSON", json.dumps(rules, indent=2), height=240, key=f"json{ver}")
            if st.button("Apply JSON"):
                try:
                    data = json.loads(text)
                    set_rules(data if isinstance(data, list) else [])
                except ValueError as e:
                    st.error(f"Invalid JSON: {e}")
            st.download_button("Download checks", json.dumps(rules, indent=2), "rules.json", "application/json")

        if rules and st.button("Preview violations (changes nothing)"):
            try:
                prev = call("POST", "/validate-rules", files=file_part(up),
                            data={"rules": json.dumps(rules), "sheet": sheet}).json()
                st.dataframe(pd.DataFrame(prev["checks"])[["name", "action", "severity", "count", "proposed_fix"]],
                             hide_index=True)
            except Exception as e:
                st.error(str(e))

        # ---- 3. run -----------------------------------------------------------------------
        st.subheader("3. Run")
        goal = st.text_input("Goal", "Analyze this dataset and prepare it for loading.",
                             help="A goal that only asks to analyse or profile never modifies data.")
        with st.expander("Demo options"):
            fault = st.selectbox("Inject a cleaning bug", ["none", "once", "always"],
                                 help="'once' shows validation catching a bug and the orchestrator retrying.")
        if st.button("Run", type="primary"):
            try:
                with st.spinner("Running agents..."):
                    st.session_state["report"] = call(
                        "POST", "/runs", files=file_part(up),
                        data={"goal": goal, "fault": "" if fault == "none" else fault,
                              "rules": json.dumps(rules) if rules else "", "sheet": sheet}).json()
            except Exception as e:
                st.error(str(e))

    rep = st.session_state.get("report")
    if rep:
        st.divider()
        st.subheader(f'{STATUS_ICON.get(rep["status"], "")} {rep["status"]}  ·  run {rep["run_id"]}')
        c = st.columns(5)
        c[0].metric("Rows", rep["rows"])
        c[1].metric("Rules", f'{len(rep["rules"])} ({rep["rules_source"]})')
        c[2].metric("Violated", len(rep["issues_found"]))
        c[3].metric("Attempts", rep["attempts"])
        c[4].metric("Safe to load", "yes" if rep["safe_to_load"] else "no")
        st.info(rep["narrative"])

        t1, t2, t3, t4, t5 = st.tabs(["Agent trace", "Checks", "Fixes", "Validation", "Needs approval"])
        with t1:
            st.dataframe(pd.DataFrame(rep["trace"]), hide_index=True)
        with t2:
            chk = pd.DataFrame(rep["checks"])
            if len(chk):
                st.dataframe(chk[["name", "action", "severity", "count", "proposed_fix"]], hide_index=True)
        with t3:
            st.write("Fixes applied automatically (per column)")
            st.json(rep["fixes_applied"])
            if rep["reported_only"]:
                st.write("Reported only (not changed)")
                for item in rep["reported_only"]:
                    st.write("• " + item)
        with t4:
            st.dataframe(pd.DataFrame(rep["validation"].get("checks", [])), hide_index=True)
        with t5:
            for item in rep["needs_human_approval"] or ["Nothing"]:
                st.write("• " + item)
            if rep["suggested_resolutions"]:
                st.caption(f'Remembered from earlier approvals: {rep["suggested_resolutions"]}')

        if rep["safe_to_load"]:
            if not rep["fixes_applied"]:
                need = sum(r["count"] for r in rep["required_resolutions"])
                st.warning(
                    "No automatic fixes were applied, so clean.csv matches your source (plus a dq_flags column marking "
                    "problem rows) and changes.csv is empty. That is expected when no check has the action 'fix'. "
                    + (f"{len(rep['required_resolutions'])} check(s) need a decision ({need} rows): request approval, choose "
                       "what to do (fill, quarantine, ...), and the final dataset with your decisions applied becomes "
                       "downloadable on the Approvals page." if rep["required_resolutions"] else
                       "Set a check's action to 'fix' or 'ask' if you want data changed."))
            d1, d2, d3 = st.columns([1, 1, 2])
            for col, name in ((d1, "clean.csv"), (d2, "changes.csv")):
                col.download_button(f"Download {name}", call("GET", f'/runs/{rep["run_id"]}/files/{name}').content,
                                    file_name=name, mime="text/csv")
            with d3:
                table = st.text_input("Target table", rep["default_table"],
                                      help="Created from the dataset's columns if it does not exist.")
                if st.button("Request load approval"):
                    try:
                        call("POST", f'/runs/{rep["run_id"]}/request-load', data={"table": table})
                        st.success("Approval requested. Open the Approvals page.")
                    except Exception as e:
                        st.error(str(e))
        elif rep["status"] == "analysis_only":
            st.caption("Analysis-only run: nothing was cleaned. Change the goal to ask for cleaning or loading.")
        else:
            st.warning("Cleaned data is not available for loading for this run.")

# ------------------------------------------------------------------------------------------
elif page == "Approvals":
    st.title("Approvals")
    try:
        pending = call("GET", "/approvals", params={"status": "pending"}).json()
    except Exception as e:
        st.error(str(e))
        pending = []
    if not pending:
        st.success("No pending approvals.")
    for ap in pending:
        p = ap["payload"]
        with st.expander(f'Load run {ap["run_id"]} into {p["table"]} ({p["rows"]} rows)', expanded=True):
            chosen = {}
            for req in p["required_resolutions"]:
                sugg = p["suggested_resolutions"].get(req["rule_id"])
                opts = req["options"]
                label = f'{req["name"]} — {req["count"]} row(s)' + (f" (last time: {sugg})" if sugg else "")
                chosen[req["rule_id"]] = st.selectbox(label, opts, index=opts.index(sugg) if sugg in opts else 0,
                                                      key=f'{ap["id"]}-{req["rule_id"]}')
            if not p["required_resolutions"]:
                st.caption("No decisions needed; this run only needs your approval.")
            who = st.text_input("Your name", "reviewer", key=f'{ap["id"]}-who')
            a, b = st.columns(2)
            if a.button("Approve & load", key=f'{ap["id"]}-yes', type="primary"):
                try:
                    out = call("POST", f'/approvals/{ap["id"]}/decide',
                               json={"approve": True, "resolutions": chosen, "decided_by": who}).json()
                    (st.success if out["status"] == "executed" else st.error)(out["status"])
                    st.dataframe(pd.DataFrame(out["result"].get("checks", [])), hide_index=True)
                    if out["status"] == "executed":
                        st.caption("Your decisions are applied. Download the final dataset below, or find it any time under Decided.")
                        for fname in ("final.csv", "quarantine.csv"):
                            st.download_button(f"Download {fname}", call("GET", f'/runs/{ap["run_id"]}/files/{fname}').content,
                                               file_name=fname, mime="text/csv", key=f'{ap["id"]}-dl-{fname}')
                except Exception as e:
                    st.error(str(e))
            if b.button("Reject", key=f'{ap["id"]}-no'):
                call("POST", f'/approvals/{ap["id"]}/decide', json={"approve": False, "decided_by": who})
                st.warning("Rejected. Nothing was loaded.")
    st.subheader("Decided")
    done = [a for a in call("GET", "/approvals").json() if a["status"] != "pending"]
    if done:
        st.dataframe(pd.DataFrame([{k: a[k] for k in ("id", "run_id", "status", "decided_by", "decided_at")} for a in done]),
                     hide_index=True)
        executed = [a for a in done if a["status"] == "executed"]
        if executed:
            pick = st.selectbox("Download results of a loaded run", [f'{a["run_id"]}  ({a["decided_at"]})' for a in executed])
            rid = pick.split()[0]
            for fname in ("final.csv", "quarantine.csv", "resolution_changes.csv"):
                try:
                    st.download_button(f"Download {fname}", call("GET", f"/runs/{rid}/files/{fname}").content,
                                       file_name=f"{rid}_{fname}", mime="text/csv", key=f"hist-{rid}-{fname}")
                except RuntimeError:
                    st.caption(f"{fname}: not produced for this run")

# ------------------------------------------------------------------------------------------
elif page == "SQL explorer":
    st.title("SQL explorer (read-only)")
    st.caption("Only a single SELECT/WITH runs. Anything else is blocked and must go through approvals.")
    sql = "SELECT 1"
    try:
        tables = call("GET", "/schema").json()
        t = st.selectbox("Table", tables, index=0 if tables else None)
        if t:
            st.dataframe(pd.DataFrame(call("GET", f"/schema/{t}").json()), hide_index=True)
            sql = f"SELECT * FROM {t} LIMIT 20"
    except Exception as e:
        st.error(str(e))
    sql = st.text_area("Query", sql, key=f"sql-{sql}")
    if st.button("Run query"):
        try:
            out = call("POST", "/sql/query", data={"sql": sql}).json()
            st.dataframe(pd.DataFrame(out["rows"]))
            if out["truncated"]:
                st.caption("Result truncated.")
        except Exception as e:
            st.error(str(e))
    st.caption("Quarantined rows are in the dq_quarantine table (filter by run_id or target_table).")

# ------------------------------------------------------------------------------------------
else:
    st.title("History & memory")
    st.subheader("Past runs")
    st.dataframe(pd.DataFrame(call("GET", "/runs").json()), hide_index=True)
    st.subheader("Remembered human decisions")
    st.caption("Keyed by check (e.g. unique:customer_id). Proposed next time as suggestions, never applied automatically.")
    st.dataframe(pd.DataFrame(call("GET", "/memory/decisions").json()), hide_index=True)
