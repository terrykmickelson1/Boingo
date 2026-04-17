"""
Fallback for institutions where we enter the balance directly in the UI
(TIAA, Empower, NY Deferred Comp, TSP, banks, credit cards, etc.)
"""


def parse_manual(balance: float, basis: float | None = None) -> dict:
    return {
        "balance": round(float(balance), 2),
        "basis": round(float(basis), 2) if basis is not None else None,
    }
