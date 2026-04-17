"""
Parses Schwab "All Accounts" Positions CSV export.

To get the file:
  Schwab.com → Positions tab → select "All Accounts" in the account dropdown
  → Export (top-right icon) → CSV

The file contains multiple account sections separated by blank lines.
Each section has a header line like:
    Account_Name_Label ...XXX
followed by column headers and position rows, ending with a "Positions Total" row.

We parse each section and return a dict keyed by the last 3–4 digits of the account number.
"""

import io
import re
import pandas as pd


def _parse_dollar(val) -> float | None:
    if pd.isna(val):
        return None
    s = str(val).strip().replace("$", "").replace(",", "").replace("+", "")
    if s in ("--", "", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_all_accounts(file_obj) -> dict[str, dict]:
    """
    Parse a Schwab All-Accounts Positions CSV.

    Returns a dict keyed by last-4-digits account suffix, e.g.:
        {
          "2746": {"balance": 224353.15, "basis": 187626.70, "label": "Taxable_-_PT_(NW)"},
          "3700": {"balance": 93013.00,  "basis": 82212.52,  "label": "Roth_IRA_-_Terry_NW"},
          ...
        }
    """
    raw = file_obj.read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")

    results = {}
    lines = raw.splitlines()

    # Split file into per-account blocks.
    # Each block starts with a line matching "AccountName ...NNN"
    # (no leading quote, ends with last digits after "...")
    block_starts = []
    for i, line in enumerate(lines):
        m = re.match(r'^([A-Za-z0-9_().\-]+)\s+\.\.\.(\d+)\s*$', line.strip())
        if m:
            block_starts.append((i, m.group(1), m.group(2)))

    if not block_starts:
        raise ValueError(
            "No account sections found. Make sure you're uploading the "
            "Schwab All-Accounts Positions export (not the Transactions export). "
            "Select 'All Accounts' in the account dropdown before exporting."
        )

    # For each block, extract lines up to the next block start
    for idx, (start_line, label, suffix) in enumerate(block_starts):
        end_line = block_starts[idx + 1][0] if idx + 1 < len(block_starts) else len(lines)
        block = lines[start_line + 1: end_line]

        # Find the column header row
        header_idx = None
        for j, line in enumerate(block):
            if re.search(r'"Symbol".*"Mkt Val|Market Value"', line, re.IGNORECASE):
                header_idx = j
                break
        if header_idx is None:
            continue

        # Collect data rows up to (and including) Positions Total
        data_lines = [block[header_idx]]
        for line in block[header_idx + 1:]:
            stripped = line.strip()
            if not stripped:
                continue
            data_lines.append(line)
            if stripped.startswith('"Positions Total"'):
                break

        try:
            df = pd.read_csv(io.StringIO("\n".join(data_lines)))
        except Exception:
            continue

        df.columns = [c.strip() for c in df.columns]

        # Normalize column names
        col_map = {}
        for c in df.columns:
            key = c.lower().replace(" ", "").replace("(", "").replace(")", "")
            if "mktval" in key or "marketvalue" in key:
                col_map[c] = "market_value"
            elif "costbasis" in key:
                col_map[c] = "cost_basis"
            elif c.strip().lower() == "symbol":
                col_map[c] = "symbol"
        df.rename(columns=col_map, inplace=True)

        # Use the Positions Total row for the final figures — it's pre-summed by Schwab
        total_row = df[df.get("symbol", pd.Series(dtype=str)).astype(str)
                       .str.startswith("Positions Total", na=False)]

        if not total_row.empty and "market_value" in df.columns:
            balance = _parse_dollar(total_row["market_value"].iloc[0])
            basis = _parse_dollar(total_row["cost_basis"].iloc[0]) \
                if "cost_basis" in df.columns else None
        else:
            # Fall back to summing rows (exclude Total and header-like rows)
            data_only = df[~df.get("symbol", pd.Series(dtype=str))
                           .astype(str).str.startswith("Positions Total", na=False)]
            if "market_value" in df.columns:
                balance = round(data_only["market_value"]
                                .apply(_parse_dollar).dropna().sum(), 2)
            else:
                balance = None
            basis = None

        if balance is not None:
            results[suffix] = {
                "label": label,
                "balance": round(balance, 2),
                "basis": round(basis, 2) if basis is not None and basis > 0 else None,
            }

    return results


def match_accounts(parsed: dict[str, dict], account_configs: list[dict]) -> list[dict]:
    """
    Match parsed Schwab account sections to account config entries by suffix.

    Returns a list of dicts with keys:
        account_id, label, balance, basis, suffix, matched
    """
    matches = []
    for cfg in account_configs:
        if cfg.get("institution") != "Schwab":
            continue
        suffix = str(cfg.get("institution_account_id", ""))
        # Try exact match, then last-N-digits match
        data = parsed.get(suffix)
        if data is None:
            # try matching the last digits of any key against the suffix
            for key, val in parsed.items():
                if suffix.endswith(key) or key.endswith(suffix):
                    data = val
                    suffix = key
                    break
        if data:
            matches.append({
                "account_id": cfg["id"],
                "label": cfg["label"],
                "balance": data["balance"],
                "basis": data.get("basis") if cfg.get("tracks_basis") else None,
                "suffix": suffix,
                "matched": True,
            })

    matched_suffixes = {m["suffix"] for m in matches}
    unmatched = {k: v for k, v in parsed.items()
                 if k not in matched_suffixes and v["balance"] != 0}

    return matches, unmatched
