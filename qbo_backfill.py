"""
Pulls a Balance Sheet from the QB API for any given date and maps account
balances back to our account_map IDs for seeding the local database.
"""

import re
import requests
import streamlit as st
from datetime import date

API_BASE = "https://quickbooks.api.intuit.com/v3/company"


def _headers() -> dict:
    from qbo_client import _get, _ensure_fresh_token
    _ensure_fresh_token()
    return {
        "Authorization": f"Bearer {_get('QBO_ACCESS_TOKEN')}",
        "Accept": "application/json",
    }


def _realm() -> str:
    from qbo_client import _get
    return _get("QBO_REALM_ID")


def fetch_balance_sheet(as_of: date) -> dict:
    """Returns raw QB BalanceSheet JSON for the given date."""
    resp = requests.get(
        f"{API_BASE}/{_realm()}/reports/BalanceSheet",
        headers=_headers(),
        params={
            "date_macro":        "custom",
            "start_date":        as_of.isoformat(),
            "end_date":          as_of.isoformat(),
            "accounting_method": "Accrual",
        },
    )
    resp.raise_for_status()
    return resp.json()


def _walk_rows(rows: list, results: dict):
    """Recursively walk QB report rows, collecting {account_name: value}."""
    for row in rows:
        row_type = row.get("type")
        col_data = row.get("ColData", [])

        if row_type == "Data" and len(col_data) >= 2:
            name = col_data[0].get("value", "").strip()
            raw  = col_data[1].get("value", "").strip()
            if name and raw not in ("", "0.00", None):
                try:
                    results[name] = float(raw.replace(",", ""))
                except ValueError:
                    pass

        # Recurse into sub-sections and summary rows
        if "Rows" in row:
            sub = row["Rows"].get("Row", [])
            _walk_rows(sub, results)

        if "Summary" in row:
            summary_cols = row["Summary"].get("ColData", [])
            if len(summary_cols) >= 2:
                name = summary_cols[0].get("value", "").strip()
                raw  = summary_cols[1].get("value", "").strip()
                if name and raw not in ("", "0.00", None):
                    try:
                        results[f"__total__{name}"] = float(raw.replace(",", ""))
                    except ValueError:
                        pass


def parse_balances(report: dict) -> dict[str, float]:
    """Flatten a QB BalanceSheet report into {account_name: balance}."""
    results: dict[str, float] = {}
    top_rows = report.get("Rows", {}).get("Row", [])
    _walk_rows(top_rows, results)
    return results


def match_to_accounts(qb_balances: dict[str, float],
                      account_configs: list[dict]) -> list[dict]:
    """
    Match QB account names to our account_map entries.

    QB names are the exact strings from the chart of accounts.
    We match on qb_account (exact) and also try the last sub-account name
    for cases where QB returns just the leaf name.

    Returns list of {account_id, qb_account, balance, matched_name}.
    """
    matched = []
    unmatched_cfg = []

    for cfg in account_configs:
        if not cfg.get("active", True):
            continue

        qb_name  = cfg["qb_account"]
        balance  = qb_balances.get(qb_name)

        if balance is None:
            # Try matching just the leaf account name (after last ":")
            leaf = qb_name.split(":")[-1].strip()
            balance = qb_balances.get(leaf)

        if balance is not None:
            # Liabilities in QB are stored as positive numbers representing amounts owed
            matched.append({
                "account_id":    cfg["id"],
                "qb_account":    qb_name,
                "balance":       abs(balance),
                "matched_name":  qb_name,
            })
        else:
            unmatched_cfg.append(cfg["id"])

    return matched, unmatched_cfg
