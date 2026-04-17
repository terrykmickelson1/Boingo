"""
QuickBooks Online OAuth 2.0 client and API helpers.

Auth flow (one-time setup):
  1. Call auth_url() to get the Intuit authorization URL
  2. User visits that URL, approves access, and is redirected back to
     http://localhost:8501?code=...&realmId=...
  3. Streamlit reads st.query_params and calls exchange_code(code, realm_id)
  4. Tokens are saved to .streamlit/secrets.toml for reuse

After setup, tokens auto-refresh on every API call.
"""

import json
import requests
import streamlit as st
from urllib.parse import urlencode
from base64 import b64encode
from datetime import datetime, timedelta

AUTH_ENDPOINT  = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_ENDPOINT = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
REVOKE_ENDPOINT = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"
API_BASE       = "https://quickbooks.api.intuit.com/v3/company"
SCOPE          = "com.intuit.quickbooks.accounting"

def _save_token(access_token: str, refresh_token: str, realm_id: str, expires_in: int):
    from db import set_config
    expiry = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()
    set_config("QBO_ACCESS_TOKEN",  access_token)
    set_config("QBO_REFRESH_TOKEN", refresh_token)
    set_config("QBO_REALM_ID",      realm_id)
    set_config("QBO_TOKEN_EXPIRY",  expiry)
    st.session_state["_qbo_access_token"]  = access_token
    st.session_state["_qbo_refresh_token"] = refresh_token
    st.session_state["_qbo_realm_id"]      = realm_id
    st.session_state["_qbo_token_expiry"]  = expiry


def _get(key: str, default: str = "") -> str:
    from db import get_config
    return (st.session_state.get(f"_qbo_{key.lower()}")
            or get_config(key)
            or st.secrets.get(key, default))


# ── Auth flow ─────────────────────────────────────────────────────────────────

def auth_url() -> str:
    params = {
        "client_id":     st.secrets["QBO_CLIENT_ID"],
        "response_type": "code",
        "scope":         SCOPE,
        "redirect_uri":  st.secrets["QBO_REDIRECT_URI"],
        "state":         "boingo",
        "prompt":        "select_account",  # force company picker, don't reuse cached
    }
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


def exchange_code(code: str, realm_id: str) -> bool:
    """Exchange authorization code for tokens. Returns True on success."""
    creds = b64encode(
        f"{st.secrets['QBO_CLIENT_ID']}:{st.secrets['QBO_CLIENT_SECRET']}".encode()
    ).decode()
    resp = requests.post(TOKEN_ENDPOINT, headers={
        "Authorization": f"Basic {creds}",
        "Content-Type":  "application/x-www-form-urlencoded",
    }, data={
        "grant_type":   "authorization_code",
        "code":          code,
        "redirect_uri":  st.secrets["QBO_REDIRECT_URI"],
    })
    if resp.ok:
        data = resp.json()
        _save_token(data["access_token"], data["refresh_token"],
                    realm_id, data.get("expires_in", 3600))
        return True
    return False


def _refresh() -> bool:
    refresh_token = _get("QBO_REFRESH_TOKEN")
    if not refresh_token:
        return False
    creds = b64encode(
        f"{st.secrets['QBO_CLIENT_ID']}:{st.secrets['QBO_CLIENT_SECRET']}".encode()
    ).decode()
    resp = requests.post(TOKEN_ENDPOINT, headers={
        "Authorization": f"Basic {creds}",
        "Content-Type":  "application/x-www-form-urlencoded",
    }, data={
        "grant_type":    "refresh_token",
        "refresh_token":  refresh_token,
    })
    if resp.ok:
        data = resp.json()
        realm_id = _get("QBO_REALM_ID")
        _save_token(data["access_token"], data["refresh_token"],
                    realm_id, data.get("expires_in", 3600))
        return True
    return False


def is_connected() -> bool:
    return bool(_get("QBO_ACCESS_TOKEN"))


def _ensure_fresh_token():
    expiry_str = _get("QBO_TOKEN_EXPIRY")
    if expiry_str:
        try:
            expiry = datetime.fromisoformat(expiry_str)
            if datetime.utcnow() > expiry - timedelta(minutes=5):
                _refresh()
        except ValueError:
            _refresh()


# ── API helpers ───────────────────────────────────────────────────────────────

def _headers() -> dict:
    _ensure_fresh_token()
    return {
        "Authorization": f"Bearer {_get('QBO_ACCESS_TOKEN')}",
        "Accept":        "application/json",
        "Content-Type":  "application/json",
    }


def _company_url(path: str) -> str:
    realm = _get("QBO_REALM_ID")
    return f"{API_BASE}/{realm}/{path}"


def get_company_info() -> dict:
    """Quick connectivity test — returns QB company info."""
    resp = requests.get(_company_url("companyinfo/" + _get("QBO_REALM_ID")),
                        headers=_headers())
    resp.raise_for_status()
    return resp.json()


# ── Journal Entry posting ─────────────────────────────────────────────────────

def post_journal_entry(period_date, je_lines: list[dict]) -> str:
    """
    Post a list of JE lines (from je_builder.build_je) to QuickBooks.

    Each line in je_lines has: debit_account, credit_account, amount, memo.
    Returns the QB JournalEntry ID on success.
    """
    # Build QB JE line items — each je_line becomes two JournalEntry lines
    qb_lines = []
    line_num = 1
    for line in je_lines:
        for side, account_name, posting_type in [
            ("debit",  line["debit_account"],  "Debit"),
            ("credit", line["credit_account"], "Credit"),
        ]:
            qb_lines.append({
                "Id": str(line_num),
                "Description": line["memo"],
                "Amount": line["amount"],
                "DetailType": "JournalEntryLineDetail",
                "JournalEntryLineDetail": {
                    "PostingType": posting_type,
                    "AccountRef": {"name": account_name},
                },
            })
            line_num += 1

    payload = {
        "TxnDate": period_date.isoformat(),
        "PrivateNote": f"Boingo monthly balance update — {period_date.strftime('%B %Y')}",
        "Line": qb_lines,
    }

    resp = requests.post(
        _company_url("journalentry"),
        headers=_headers(),
        data=json.dumps({"JournalEntry": payload}),
    )

    if not resp.ok:
        raise RuntimeError(
            f"QB API error {resp.status_code}: {resp.text[:500]}"
        )

    return resp.json()["JournalEntry"]["Id"]


def query_balance_sheet(as_of_date: str) -> dict:
    """
    Fetch a Balance Sheet report from QB as of a given date (YYYY-MM-DD).
    Used for historical backfill.
    """
    resp = requests.get(
        _company_url("reports/BalanceSheet"),
        headers=_headers(),
        params={
            "start_date": as_of_date,
            "end_date":   as_of_date,
            "accounting_method": "Accrual",
        },
    )
    resp.raise_for_status()
    return resp.json()
