"""
Builds QuickBooks Journal Entry lines from balance changes.

JE logic:
  Assets (Bank, Taxable, Retirement):
    balance increased  → DR asset account,  CR income account
    balance decreased  → CR asset account,  DR income account

  Liabilities (CreditCard, Liability):
    balance increased  → CR liability account, DR income account  (more owed)
    balance decreased  → DR liability account, CR income account  (less owed)

  For accounts with basis tracking (tracks_basis=true):
    The G/L sub-account holds the unrealized gain — only that changes monthly.
    The basis sub-account only changes on contributions (recorded separately).
    So we post the delta to the G/L sub-account vs. its income account.
"""

import json
from datetime import date
from db import get_prior_snapshot


ASSET_TYPES = {"Bank", "Taxable", "Retirement"}
LIABILITY_TYPES = {"CreditCard", "Liability"}


def build_je(period_date: date, snapshots: list[dict], account_map: list[dict]) -> list[dict]:
    """
    snapshots: list of {account_id, balance, basis, ...} for the period
    account_map: full list of account config dicts from account_map.json
    Returns: list of JE line dicts ready for QB API or CSV export
    """
    cfg_by_id = {a["id"]: a for a in account_map}
    lines = []

    for snap in snapshots:
        acc_id = snap["account_id"]
        cfg = cfg_by_id.get(acc_id)
        if not cfg:
            continue

        prior = get_prior_snapshot(acc_id, period_date)
        prior_balance = prior["balance"] if prior else 0.0
        delta = round(snap["balance"] - prior_balance, 2)

        if delta == 0:
            continue

        account_type = cfg["account_type"]
        is_asset = account_type in ASSET_TYPES
        is_liability = account_type in LIABILITY_TYPES
        tracks_basis = cfg.get("tracks_basis", False)

        if tracks_basis and snap.get("basis") is not None:
            # Post only the gain/loss delta; basis posts separately on contributions
            prior_basis = prior["basis"] if prior and prior.get("basis") is not None else 0.0
            prior_gl = (prior["balance"] - prior_basis) if prior else 0.0
            current_gl = snap["balance"] - snap["basis"]
            gl_delta = round(current_gl - prior_gl, 2)

            if gl_delta != 0:
                gl_account = cfg.get("qb_gl_sub_account") or cfg["qb_account"]
                income_account = cfg["qb_income_account"]
                lines.append(_make_line(gl_account, income_account, gl_delta, is_asset,
                                        acc_id, period_date, "gl"))
        else:
            if not is_asset and not is_liability:
                continue
            income_account = cfg.get("qb_income_account")
            if not income_account:
                continue
            lines.append(_make_line(cfg["qb_account"], income_account, delta, is_asset,
                                    acc_id, period_date, "balance"))

    return lines


def _make_line(balance_account: str, income_account: str, delta: float,
               is_asset: bool, account_id: str, period_date: date, line_type: str) -> dict:
    """
    Returns a dict with debit/credit amounts for each side of the JE line pair.
    Positive delta on an asset → DR asset, CR income.
    Negative delta on an asset → CR asset, DR income.
    For liabilities the logic is flipped.
    """
    if is_asset:
        if delta > 0:
            dr_account, cr_account, amount = balance_account, income_account, delta
        else:
            dr_account, cr_account, amount = income_account, balance_account, abs(delta)
    else:
        if delta > 0:
            dr_account, cr_account, amount = income_account, balance_account, delta
        else:
            dr_account, cr_account, amount = balance_account, income_account, abs(delta)

    return {
        "period_date": period_date.isoformat(),
        "account_id": account_id,
        "line_type": line_type,
        "debit_account": dr_account,
        "credit_account": cr_account,
        "amount": round(amount, 2),
        "memo": f"Monthly balance update - {period_date.strftime('%b %Y')}",
    }


def je_lines_to_csv(lines: list[dict]) -> str:
    """Formats JE lines as a simple two-column CSV for manual QB import review."""
    rows = ["Account,Debit,Credit,Memo"]
    for line in lines:
        rows.append(f'"{line["debit_account"]}",{line["amount"]:.2f},,'
                    f'"{line["memo"]}"')
        rows.append(f'"{line["credit_account"]}",,{line["amount"]:.2f},'
                    f'"{line["memo"]}"')
    return "\n".join(rows)
