"""
Smart price rounding for crypto tokens.
Preserves meaningful significant digits for any price magnitude —
from BTC ($67,000) down to micro-cap tokens ($0.000002871).
"""
import math


def smart_round(value: float, reference_price: float = None, sig_figs: int = 4) -> float:
    """
    Round a price/SL/TP to the appropriate number of decimal places
    based on the magnitude of the price, preserving `sig_figs` significant digits.

    Args:
        value: The value to round (e.g. stop-loss, take-profit, entry).
        reference_price: Optional reference price to determine precision from.
                         Useful when SL/TP should match the entry price's precision.
                         If None, uses `value` itself.
        sig_figs: Number of significant figures to preserve (default 4).

    Examples:
        smart_round(85000.0)          → 85000.0       (2 decimals)
        smart_round(1.2345)           → 1.235         (4 sig figs → 3 decimals)
        smart_round(0.01234)          → 0.01234       (4 sig figs → 5 decimals)
        smart_round(0.000002871)      → 0.000002871   (4 sig figs → 9 decimals)
        smart_round(0.00000000812)    → 0.00000000812 (4 sig figs → 11 decimals)
    """
    if value == 0:
        return 0.0

    ref = abs(reference_price) if reference_price is not None and reference_price != 0 else abs(value)
    if ref == 0:
        return 0.0

    # Calculate the number of decimal places needed for sig_figs significant digits
    magnitude = math.floor(math.log10(ref))
    decimals = max(-magnitude + (sig_figs - 1), 2)

    return round(value, decimals)
