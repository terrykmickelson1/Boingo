import json
import calendar
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import streamlit as st

import db
from je_builder import build_je, je_lines_to_csv
from parsers.schwab import parse_all_accounts, match_accounts
from parsers.manual import parse_manual
import qbo_client as qbo
import qbo_backfill as backfill

# ── Config ────────────────────────────────────────────────────────────────────
ACCOUNT_MAP_PATH = Path(__file__).parent / "config" / "account_map.json"
_raw = json.loads(ACCOUNT_MAP_PATH.read_text())
ACCOUNTS = [a for a in _raw["accounts"] if a.get("active", True)]
ACCOUNTS_BY_ID = {a["id"]: a for a in ACCOUNTS}

SCHWAB_IDS = {a["id"] for a in ACCOUNTS if a["institution"] == "Schwab"
              and a["account_type"] in ("Taxable", "Retirement")}

st.set_page_config(page_title="Boingo Finance", page_icon="💰", layout="wide")

# ── QB OAuth callback handler (runs on every page load) ───────────────────────
_params = st.query_params
if "code" in _params and "realmId" in _params and not qbo.is_connected():
    with st.spinner("Connecting to QuickBooks..."):
        ok = qbo.exchange_code(_params["code"], _params["realmId"])
    if ok:
        st.query_params.clear()
        st.success("QuickBooks connected! You can now post journal entries.")
        st.rerun()
    else:
        st.error("QuickBooks authorization failed. Please try again.")

# ── Helpers ───────────────────────────────────────────────────────────────────

def month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def fmt_dollar(val) -> str:
    if val is None:
        return "—"
    return f"${val:,.2f}" if val >= 0 else f"(${abs(val):,.2f})"


def get_prior_balance(account_id: str, period: date) -> float | None:
    snap = db.get_prior_snapshot(account_id, period)
    return snap["balance"] if snap else None


# ── Sidebar ───────────────────────────────────────────────────────────────────
st.sidebar.title("Boingo Finance")
tab_choice = st.sidebar.radio("Navigate", ["Monthly Update", "Dashboard", "Setup / Backfill"])

# QB connection status in sidebar
st.sidebar.divider()
if qbo.is_connected():
    st.sidebar.success("QuickBooks: Connected")
else:
    st.sidebar.warning("QuickBooks: Not connected")
    if st.sidebar.button("Connect to QuickBooks"):
        st.sidebar.markdown(f"[Click here to authorize]({qbo.auth_url()})",
                            unsafe_allow_html=True)

today = date.today()
default_year = today.year if today.month > 1 else today.year - 1
default_month = today.month - 1 if today.month > 1 else 12

# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — MONTHLY UPDATE
# ══════════════════════════════════════════════════════════════════════════════
if tab_choice == "Monthly Update":
    st.title("Monthly Balance Update")

    col1, col2 = st.columns(2)
    with col1:
        sel_year = st.selectbox("Year", list(range(2014, today.year + 1)),
                                index=list(range(2014, today.year + 1)).index(default_year))
    with col2:
        sel_month = st.selectbox("Month", list(range(1, 13)),
                                 format_func=lambda m: datetime(2000, m, 1).strftime("%B"),
                                 index=default_month - 1)

    period = month_end(sel_year, sel_month)
    st.caption(f"Posting period: **{period.strftime('%B %d, %Y')}** "
               f"(last day of selected month)")

    st.divider()

    # ── Schwab CSV Upload ─────────────────────────────────────────────────────
    st.subheader("Schwab — Upload Positions CSV")
    st.caption(
        "**How to export:** Schwab.com → Positions tab → select **All Accounts** "
        "in the account dropdown → Export icon (top right) → CSV. "
        "One file covers all Schwab accounts."
    )

    schwab_results: dict[str, dict] = {}

    uploaded_schwab = st.file_uploader(
        "Drop All-Accounts Positions CSV here",
        type=["csv"],
        key="upload_schwab_all",
    )

    if uploaded_schwab:
        try:
            parsed = parse_all_accounts(uploaded_schwab)
            matches, unmatched = match_accounts(parsed, ACCOUNTS)

            if matches:
                st.success(f"Parsed {len(matches)} Schwab accounts.")
                rows = []
                for m in matches:
                    prior = get_prior_balance(m["account_id"], period)
                    delta = (m["balance"] - prior) if prior is not None else None
                    rows.append({
                        "Account": m["label"],
                        "Balance": fmt_dollar(m["balance"]),
                        "Basis": fmt_dollar(m["basis"]) if m.get("basis") else "—",
                        "Unrealized G/L": fmt_dollar(m["balance"] - m["basis"])
                                          if m.get("basis") else "—",
                        "Change vs Prior": fmt_dollar(delta) if delta is not None else "no prior",
                    })
                    schwab_results[m["account_id"]] = {
                        "account_id": m["account_id"],
                        "balance": m["balance"],
                        "basis": m.get("basis"),
                    }
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

            if unmatched:
                with st.expander(f"⚠️ {len(unmatched)} account(s) in file not matched to config"):
                    for suffix, data in unmatched.items():
                        st.write(f"**...{suffix}** — {data['label']} — "
                                 f"Balance: {fmt_dollar(data['balance'])}")
                    st.caption("To add these accounts, update config/account_map.json.")

        except Exception as e:
            st.error(f"Parse error: {e}")

    st.divider()

    # ── Manual Entry ──────────────────────────────────────────────────────────
    st.subheader("All Other Accounts — Enter Balances")

    is_quarter_end = sel_month in (3, 6, 9, 12)
    if not is_quarter_end:
        st.info(
            "Non-quarter-end month: TIAA and NY Deferred Comp accounts update quarterly. "
            "Their prior balances will carry forward automatically — no entry needed."
        )
    else:
        st.caption("Quarter-end month: all accounts need updating.")

    st.caption("Leave any field blank to skip (prior balance carries forward).")

    manual_accounts = [a for a in ACCOUNTS if a["institution"] != "Schwab"]
    manual_results: dict[str, dict] = {}

    # Auto carry-forward quarterly accounts in non-quarter months
    for acc in manual_accounts:
        if acc.get("update_frequency") == "quarterly" and not is_quarter_end:
            prior = get_prior_balance(acc["id"], period)
            if prior is not None:
                manual_results[acc["id"]] = {
                    "account_id": acc["id"],
                    "balance": prior,
                    "basis": db.get_prior_snapshot(acc["id"], period).get("basis"),
                    "_carried_forward": True,
                }

    institution_groups = {}
    for acc in manual_accounts:
        institution_groups.setdefault(acc["institution"], []).append(acc)

    for institution, accs in institution_groups.items():
        st.markdown(f"**{institution}**")
        for acc in accs:
            is_quarterly = acc.get("update_frequency") == "quarterly"
            carried = manual_results.get(acc["id"], {}).get("_carried_forward", False)
            prior = get_prior_balance(acc["id"], period)

            cols = st.columns([3, 2, 2])
            with cols[0]:
                if carried:
                    st.markdown(f"{acc['label']}  *(carrying forward {fmt_dollar(prior)})*")
                else:
                    prior_str = f"  *(prior: {fmt_dollar(prior)})*" if prior is not None else ""
                    st.markdown(f"{acc['label']}{prior_str}")

            # Quarterly accounts are read-only in non-quarter months
            if is_quarterly and not is_quarter_end:
                with cols[1]:
                    st.caption("quarterly — auto-carried")
                continue

            with cols[1]:
                bal_str = st.text_input(
                    "Balance ($)", key=f"bal_{acc['id']}", label_visibility="collapsed",
                    placeholder="e.g. 12,345.67"
                )
            with cols[2]:
                if acc.get("tracks_basis"):
                    basis_str = st.text_input(
                        "Basis ($)", key=f"basis_{acc['id']}", label_visibility="collapsed",
                        placeholder="Basis (optional)"
                    )
                else:
                    basis_str = ""

            if bal_str.strip():
                try:
                    balance = float(bal_str.replace(",", "").replace("$", ""))
                    basis = (float(basis_str.replace(",", "").replace("$", ""))
                             if basis_str.strip() else None)
                    manual_results[acc["id"]] = {
                        "account_id": acc["id"],
                        "balance": balance,
                        "basis": basis,
                    }
                except ValueError:
                    st.warning(f"Invalid number for {acc['label']}: '{bal_str}'")

        st.markdown("")

    # ── Dividend reinvestment flag ─────────────────────────────────────────────
    reinvest_accounts = [a for a in ACCOUNTS if a.get("reinvests_dividends")]
    if reinvest_accounts and sel_month in (3, 6, 9, 12):
        st.divider()
        st.subheader("Quarterly Dividend Reinvestment")
        st.info(
            "It looks like this is a quarter-end month. The following accounts "
            "reinvest dividends, which increases their cost basis. "
            "If dividends were reinvested this quarter, enter the amount below "
            "to update the basis in QB."
        )
        for acc in reinvest_accounts:
            div_str = st.text_input(
                f"Dividends reinvested — {acc['label']} ($)",
                key=f"div_{acc['id']}",
                placeholder="0.00"
            )
            if div_str.strip():
                try:
                    div_amt = float(div_str.replace(",", "").replace("$", ""))
                    acc_data = manual_results.get(acc["id"]) or schwab_results.get(acc["id"])
                    if acc_data and div_amt > 0:
                        acc_data["basis"] = (acc_data.get("basis") or 0) + div_amt
                except ValueError:
                    pass

    st.divider()

    # ── Save & Generate JE ────────────────────────────────────────────────────
    all_results = {**schwab_results, **manual_results}

    if st.button("💾 Save Balances & Generate JE", type="primary",
                 disabled=not all_results):

        for acc_id, data in all_results.items():
            cfg = ACCOUNTS_BY_ID[acc_id]
            db.upsert_snapshot(
                period_date=period,
                account_id=acc_id,
                qb_account=cfg["qb_account"],
                balance=data["balance"],
                basis=data.get("basis"),
            )

        snapshots = db.get_snapshots_for_period(period)
        je_lines = build_je(period, snapshots, ACCOUNTS)
        je_id = db.log_je(period, je_lines, status="draft")

        st.success(f"Saved {len(all_results)} account balances. "
                   f"Generated {len(je_lines)} JE lines.")

        # Preview table
        if je_lines:
            st.subheader("Journal Entry Preview")
            df = pd.DataFrame(je_lines)[["debit_account", "credit_account", "amount", "memo"]]
            df.columns = ["Debit Account", "Credit Account", "Amount", "Memo"]
            df["Amount"] = df["Amount"].map(lambda x: f"${x:,.2f}")
            st.dataframe(df, use_container_width=True, hide_index=True)

            csv_str = je_lines_to_csv(je_lines)
            st.download_button(
                "⬇️  Download JE as CSV",
                data=csv_str,
                file_name=f"boingo_je_{period.strftime('%Y-%m')}.csv",
                mime="text/csv",
            )

            if qbo.is_connected():
                if st.button("🚀 Post JE to QuickBooks", type="primary"):
                    with st.spinner("Posting to QuickBooks..."):
                        try:
                            je_id = qbo.post_journal_entry(period, je_lines)
                            db.log_je(period, je_lines, status="posted", qbo_je_id=je_id)
                            st.success(f"Posted to QuickBooks — JE ID: {je_id}")
                        except Exception as e:
                            st.error(f"QB error: {e}")
            else:
                st.info("Connect QuickBooks (sidebar) to post directly, "
                        "or download the CSV above to import manually.")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════
elif tab_choice == "Dashboard":
    st.title("Boingo Dashboard")

    all_data = db.get_all_history()
    if not all_data:
        st.info("No data yet. Use the Monthly Update tab to enter your first period.")
        st.stop()

    df = pd.DataFrame(all_data)
    df["period_date"] = pd.to_datetime(df["period_date"])
    df["person"] = df["account_id"].map(
        lambda i: ACCOUNTS_BY_ID[i]["person"] if i in ACCOUNTS_BY_ID else "Unknown"
    )
    df["account_type"] = df["account_id"].map(
        lambda i: ACCOUNTS_BY_ID[i]["account_type"] if i in ACCOUNTS_BY_ID else "Unknown"
    )
    df["label"] = df["account_id"].map(
        lambda i: ACCOUNTS_BY_ID[i]["label"] if i in ACCOUNTS_BY_ID else i
    )

    # ── KPI row ───────────────────────────────────────────────────────────────
    latest_period = df["period_date"].max()
    latest = df[df["period_date"] == latest_period]

    assets = latest[latest["account_type"].isin(["Bank", "Taxable", "Retirement"])]["balance"].sum()
    liabilities = latest[latest["account_type"].isin(["CreditCard", "Liability"])]["balance"].sum()
    net_worth = assets - liabilities

    prev_periods = sorted(df["period_date"].unique())
    if len(prev_periods) > 1:
        prev = df[df["period_date"] == prev_periods[-2]]
        prev_assets = prev[prev["account_type"].isin(["Bank", "Taxable", "Retirement"])]["balance"].sum()
        prev_liabilities = prev[prev["account_type"].isin(["CreditCard", "Liability"])]["balance"].sum()
        prev_nw = prev_assets - prev_liabilities
        nw_delta = net_worth - prev_nw
    else:
        nw_delta = None

    k1, k2, k3 = st.columns(3)
    k1.metric("Net Worth", fmt_dollar(net_worth),
              delta=f"{nw_delta:+,.2f}" if nw_delta is not None else None)
    k2.metric("Total Assets", fmt_dollar(assets))
    k3.metric("Total Liabilities", fmt_dollar(liabilities))

    st.caption(f"As of {latest_period.strftime('%B %d, %Y')}")
    st.divider()

    # ── Net worth over time ───────────────────────────────────────────────────
    st.subheader("Net Worth Over Time")
    nw_by_period = (
        df[df["account_type"].isin(["Bank", "Taxable", "Retirement"])]
        .groupby("period_date")["balance"].sum()
        .reset_index()
        .rename(columns={"balance": "Assets"})
    )
    liab_by_period = (
        df[df["account_type"].isin(["CreditCard", "Liability"])]
        .groupby("period_date")["balance"].sum()
        .reset_index()
        .rename(columns={"balance": "Liabilities"})
    )
    nw_chart = nw_by_period.merge(liab_by_period, on="period_date", how="left").fillna(0)
    nw_chart["Net Worth"] = nw_chart["Assets"] - nw_chart["Liabilities"]
    nw_chart = nw_chart.set_index("period_date")
    st.line_chart(nw_chart[["Net Worth", "Assets"]])

    st.divider()

    # ── Balances by person ────────────────────────────────────────────────────
    st.subheader("Balances by Person")

    person_filter = st.multiselect(
        "Filter by person",
        options=df["person"].unique().tolist(),
        default=df["person"].unique().tolist(),
    )

    person_df = (
        df[df["person"].isin(person_filter) &
           df["account_type"].isin(["Bank", "Taxable", "Retirement"])]
        .groupby(["period_date", "person"])["balance"].sum()
        .reset_index()
        .pivot(index="period_date", columns="person", values="balance")
        .fillna(0)
    )
    st.area_chart(person_df)

    st.divider()

    # ── Basis vs Gain/Loss ────────────────────────────────────────────────────
    basis_accounts = df[(df["account_id"].isin(
        {a["id"] for a in ACCOUNTS if a.get("tracks_basis")}
    )) & df["basis"].notna()]

    if not basis_accounts.empty:
        st.subheader("Basis vs. Unrealized Gain/Loss")
        bg = (basis_accounts
              .groupby("period_date")[["basis", "gain_loss"]].sum()
              .reset_index()
              .set_index("period_date"))
        bg.columns = ["Total Basis", "Unrealized G/L"]
        st.bar_chart(bg)

    st.divider()

    # ── Account detail table ──────────────────────────────────────────────────
    st.subheader(f"Account Balances — {latest_period.strftime('%B %Y')}")
    detail = latest[latest["account_type"].isin(["Bank", "Taxable", "Retirement"])].copy()
    detail = detail[["label", "person", "account_type", "balance", "basis", "gain_loss"]]
    detail.columns = ["Account", "Person", "Type", "Balance", "Basis", "Unrealized G/L"]
    for col in ["Balance", "Basis", "Unrealized G/L"]:
        detail[col] = detail[col].map(lambda x: fmt_dollar(x) if pd.notna(x) else "—")
    st.dataframe(detail.sort_values(["Type", "Person"]),
                 use_container_width=True, hide_index=True)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — SETUP / BACKFILL
# ══════════════════════════════════════════════════════════════════════════════
elif tab_choice == "Setup / Backfill":
    st.title("Setup & Historical Backfill")

    if not qbo.is_connected():
        st.warning("Connect QuickBooks first (sidebar) to use the backfill tool.")
        st.stop()

    st.markdown("""
    Pull balance snapshots directly from QuickBooks for any month-end date.
    Start with **December 31, 2025** to seed the opening balance, then add
    January, February, and March 2026 to build your test set.
    """)

    st.subheader("Pull Balance Sheet from QuickBooks")
    col1, col2 = st.columns(2)
    with col1:
        bf_year  = st.selectbox("Year",  list(range(2014, today.year + 1)),
                                index=list(range(2014, today.year + 1)).index(2025),
                                key="bf_year")
    with col2:
        bf_month = st.selectbox("Month", list(range(1, 13)),
                                format_func=lambda m: datetime(2000, m, 1).strftime("%B"),
                                index=11, key="bf_month")   # default December

    bf_period = month_end(bf_year, bf_month)
    st.caption(f"Will pull balances as of **{bf_period.strftime('%B %d, %Y')}**")

    if st.button("Fetch from QuickBooks", type="primary"):
        with st.spinner(f"Pulling balance sheet from QB for {bf_period}..."):
            try:
                report   = backfill.fetch_balance_sheet(bf_period)
                qb_bals  = backfill.parse_balances(report)
                matched, unmatched_ids = backfill.match_to_accounts(qb_bals, ACCOUNTS)

                st.session_state["bf_matched"]      = matched
                st.session_state["bf_unmatched_ids"] = unmatched_ids
                st.session_state["bf_period"]        = bf_period
                st.session_state["bf_qb_bals"]       = qb_bals
                st.success(f"Fetched {len(qb_bals)} QB accounts. "
                           f"Matched {len(matched)} to our account map.")
            except Exception as e:
                st.error(f"QB error: {e}")

    if "bf_matched" in st.session_state and st.session_state.get("bf_period") == bf_period:
        matched   = st.session_state["bf_matched"]
        unmatched = st.session_state["bf_unmatched_ids"]
        qb_bals   = st.session_state["bf_qb_bals"]

        if matched:
            st.subheader("Matched Accounts — Review Before Saving")
            preview_rows = []
            for m in matched:
                cfg = ACCOUNTS_BY_ID.get(m["account_id"], {})
                preview_rows.append({
                    "Account":      cfg.get("label", m["account_id"]),
                    "Person":       cfg.get("person", ""),
                    "QB Balance":   fmt_dollar(m["balance"]),
                    "QB Name":      m["matched_name"],
                })
            st.dataframe(pd.DataFrame(preview_rows),
                         use_container_width=True, hide_index=True)

        if unmatched:
            with st.expander(f"⚠️ {len(unmatched)} config accounts not found in QB report"):
                for acc_id in unmatched:
                    cfg = ACCOUNTS_BY_ID.get(acc_id, {})
                    st.write(f"- **{cfg.get('label', acc_id)}** "
                             f"(QB name: `{cfg.get('qb_account', '')}`) — "
                             f"balance in QB: {fmt_dollar(qb_bals.get(cfg.get('qb_account',''), 0))}")
                st.caption("These may have $0 balances or slightly different names in QB. "
                           "You can enter them manually in the Monthly Update tab.")

        if matched and st.button("Save These Balances to Database", type="primary"):
            saved = 0
            for m in matched:
                cfg = ACCOUNTS_BY_ID.get(m["account_id"])
                if cfg:
                    db.upsert_snapshot(
                        period_date=bf_period,
                        account_id=m["account_id"],
                        qb_account=cfg["qb_account"],
                        balance=m["balance"],
                        basis=None,
                        source="qbo_backfill",
                    )
                    saved += 1
            st.success(f"Saved {saved} account balances for {bf_period.strftime('%B %Y')}.")
            st.caption("Repeat for January, February, and March 2026 to build your test set.")
