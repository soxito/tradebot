"""
AI Market Analyst — Risk Policy Engine

Validates every AI-proposed trade against hard safety limits
BEFORE it reaches any order gateway. Runs server-side — cannot be bypassed by prompt injection.
"""
from datetime import datetime, timezone
from typing import Dict, List, Optional
from loguru import logger

from plugins.AiMarketAnalyst.backend.config import ai_analyst_config


def validate_decision(
    *,
    action: str = "analyze",
    allowed_actions: str = "analyze",
    trade_mode: str = "both",
    trading_hours: Optional[List[Dict]] = None,
    now: Optional[datetime] = None,
    direction: str,
    entry: Optional[float],
    sl: Optional[float],
    tp: Optional[float],
    lot_size: Optional[float],
    lot_min: float = ai_analyst_config.min_lot_size,
    lot_max: float = ai_analyst_config.max_lot_size,
    min_sl_pct: float = ai_analyst_config.min_sl_distance_pct,
    max_risk_pct: float = ai_analyst_config.max_risk_per_trade_pct,
    paper_mode: bool = True,
    auto_place: bool = False,
    # ── Risk management quality checks ────────────────────────────────────────
    min_rr: float = 2.0,          # minimum reward:risk ratio (warn below this)
    require_min_rr: bool = False,  # if True, block trades below min_rr
    atr: Optional[float] = None,  # used for minimum SL distance quality check
) -> Dict:
    """
    Validate a proposed trade against risk policy.

    Returns: {"allowed": bool, "blocked_reasons": [...], "warnings": [...]}
    """
    reasons: List[str] = []
    warnings: List[str] = []

    normalized_action = action or "analyze"
    normalized_allowed = allowed_actions or "analyze"
    normalized_trade_mode = trade_mode or "both"

    if normalized_action not in ("analyze", "propose_limit", "place"):
        reasons.append(f"Invalid action: {normalized_action}")

    if normalized_allowed not in ("analyze", "propose", "auto-place"):
        reasons.append(f"Invalid allowed_actions: {normalized_allowed}")
    elif normalized_allowed == "analyze" and normalized_action in ("propose_limit", "place"):
        reasons.append("Agent is restricted to analyze-only")
    elif normalized_allowed == "propose" and normalized_action == "place":
        reasons.append("Agent is not allowed to place orders")

    if normalized_trade_mode not in ("buy_only", "sell_only", "both"):
        reasons.append(f"Invalid trade mode: {normalized_trade_mode}")

    requires_trade = normalized_action in ("propose_limit", "place")

    # Direction check
    if direction not in ("buy", "sell"):
        if requires_trade:
            reasons.append(f"Invalid direction: {direction}")
    elif normalized_trade_mode == "buy_only" and direction == "sell":
        reasons.append("Trade mode is buy_only")
    elif normalized_trade_mode == "sell_only" and direction == "buy":
        reasons.append("Trade mode is sell_only")

    if requires_trade and direction == "none":
        reasons.append("Trade action requires a buy/sell direction")

    # Trading hours check (UTC)
    if requires_trade and trading_hours:
        now_ts = now or datetime.now(timezone.utc)
        current = now_ts.time()
        within_window = False
        for window in trading_hours:
            start = window.get("from") if isinstance(window, dict) else None
            end = window.get("to") if isinstance(window, dict) else None
            if not start or not end:
                continue
            try:
                start_time = datetime.strptime(start, "%H:%M").time()
                end_time = datetime.strptime(end, "%H:%M").time()
            except ValueError:
                reasons.append("Invalid trading hours format; expected HH:MM")
                within_window = False
                break
            if start_time <= end_time:
                if start_time <= current <= end_time:
                    within_window = True
                    break
            else:
                if current >= start_time or current <= end_time:
                    within_window = True
                    break
        if not within_window:
            reasons.append("Outside allowed trading hours")

    # Entry must be positive
    if entry is not None and entry <= 0:
        reasons.append("Entry price must be positive")

    if requires_trade and entry is None:
        reasons.append("Entry price is required for trade actions")

    # Stop-loss checks
    if requires_trade and sl is None:
        reasons.append("Stop-loss is required for trade actions")
    elif requires_trade and entry is not None and entry > 0 and sl is not None:
        sl_distance_pct = abs(entry - sl) / entry * 100
        if sl_distance_pct < min_sl_pct:
            reasons.append(
                f"SL distance {sl_distance_pct:.2f}% < minimum {min_sl_pct}%"
            )
        # SL direction check
        if direction == "buy" and sl >= entry:
            reasons.append("Buy SL must be below entry")
        elif direction == "sell" and sl <= entry:
            reasons.append("Sell SL must be above entry")

        # ── ATR-based SL quality check ────────────────────────────────────────
        # SL must be at least 0.5 × ATR from entry to be meaningful
        if atr and atr > 0:
            sl_dist = abs(entry - sl)
            if sl_dist < atr * 0.5:
                warnings.append(
                    f"SL is very tight ({sl_dist:.4f} < 0.5×ATR={atr*0.5:.4f}); "
                    "risk of premature stop-out. SL should be beyond swing structure."
                )

    # Take-profit direction check
    if tp is not None and entry is not None:
        if direction == "buy" and tp <= entry:
            reasons.append("Buy TP must be above entry")
        elif direction == "sell" and tp >= entry:
            reasons.append("Sell TP must be below entry")

    # ── Reward:Risk ratio quality checks ──────────────────────────────────────
    if entry is not None and sl is not None and tp is not None and entry > 0:
        risk = abs(entry - sl)
        reward = abs(tp - entry)
        if risk > 0:
            actual_rr = reward / risk
            if actual_rr < 1.0:
                # Hard block: reward must exceed risk (RR < 1 is always wrong)
                reasons.append(
                    f"RR {actual_rr:.2f} < 1.0 — reward must exceed risk. "
                    "Move TP to a real liquidity target."
                )
            elif actual_rr < min_rr:
                msg = (
                    f"RR {actual_rr:.2f} is below recommended minimum of {min_rr:.1f}. "
                    "Consider extending TP to the next liquidity pool."
                )
                if require_min_rr:
                    reasons.append(msg)
                else:
                    warnings.append(msg)
            else:
                # Good RR — log it
                logger.debug(f"[RiskPolicy] RR={actual_rr:.2f} ✓ (min={min_rr:.1f})")

    # Lot size bounds
    if requires_trade and lot_size is None:
        reasons.append("Lot size is required for trade actions")
    if lot_size is not None:
        if lot_size < lot_min:
            reasons.append(f"Lot size {lot_size} < minimum {lot_min}")
        if lot_size > lot_max:
            reasons.append(f"Lot size {lot_size} > maximum {lot_max}")

    # Auto-place requires paper mode disabled explicitly if live
    if auto_place and not paper_mode:
        warnings.append("Auto-place is ON with paper_mode OFF — live orders will be placed")

    if normalized_action == "place" and not paper_mode and not auto_place:
        reasons.append("Live placement requires auto_place to be enabled")

    return {
        "allowed": len(reasons) == 0,
        "blocked_reasons": reasons,
        "warnings": warnings,
    }
