"""
Agent Orchestrator — coordinates multiple AI agents for trade decisions.

Memory-aware pipeline:
  1. Gather market context (OHLCV, indicators, sentiment, positions)
  2. For each agent, check decision memory first:
     a. If enough historical data → try local decision (no OpenAI call)
     b. Otherwise call OpenAI with memory context injected
  3. Run the 4-phase pipeline (Market+Sentiment → Signal → Risk → Executor)
  4. Store every decision with ai_called flag for learning
  5. Gracefully skip if ENABLE_AI_AGENTS is False or OpenAI is unavailable
"""
import json
import time
import uuid
import asyncio
from typing import Dict, Any, List, Optional, Sequence
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.database import Agent, AgentDecision, Signal, SignalAction, SignalSource, SignalStatus, Trade, SimPosition, SimAccount
from app.core.database import AsyncSessionLocal, safe_rollback
from app.agents.specialists import agent_from_db
from app.agents.memory import get_past_decisions, build_memory_prompt, try_local_decision
from app.agents import room
from app.signals.technical import analyze as technical_analyze
from app.exchanges.manager import exchange_manager, SupportedExchange
from app.exchanges.forex_provider import is_forex_symbol, fetch_ohlcv as forex_fetch_ohlcv
from app.services import market_data


def _is_quota_error_decision(decision: Dict[str, Any]) -> bool:
    """Check if a decision is a quota/billing error that should NOT be stored."""
    if not decision.get("error"):
        return False
    reasoning = str(decision.get("reasoning", "")).lower()
    _quota_phrases = ["insufficient_quota", "exceeded your current quota", "quota", "billing", "429", "rate limit"]
    return any(phrase in reasoning for phrase in _quota_phrases)
from app.core.timezone import now_sast
from app.core.config import settings


#: Triggers that came from a person and are being waited on. These runs skip
#: every AI shortcut — the memory decision and the per-symbol answer cache —
#: because both exist to keep the background scanner cheap, and neither is what
#: someone typing /room is asking for.
LIVE_TRIGGERS = frozenset({"telegram", "manual", "api", "user"})

# Symbols with a room meeting in flight. Stops the worker (or a second trigger
# such as a fresh signal) convening a duplicate session on the same pair before
# the first finishes. Discarded in a finally so a crash never wedges a symbol.
_inflight_symbols: set[str] = set()


def _norm_symbol(symbol: str) -> str:
    return (symbol or "").replace("/", "").upper()


def _analysis_window() -> int:
    """Closed candles agents study for market movement.

    Reads ``AGENT_ANALYSIS_CANDLES`` but never drops below 28 — every agent
    compares the current candle against at least the last ~28 closed candles so
    signals reflect real movement, not a 24h snapshot. No hard upper cap.
    """
    try:
        return max(28, int(getattr(settings, "AGENT_ANALYSIS_CANDLES", 120) or 120))
    except Exception:
        return 120


def reasoning_text(value: Any) -> str:
    """Coerce an agent's ``reasoning`` into the string the column expects.

    Models do not reliably honour "reasoning is a string": some return a nested
    object of sub-analyses instead. Handing that straight to a Text column
    raises DataError on INSERT, which aborts the *whole* session — every other
    agent's work in the same flush is lost with it. Flattening keeps the
    content and the meeting.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        parts = []
        for key, val in value.items():
            label = str(key).replace("_", " ").strip().capitalize()
            body = reasoning_text(val) if isinstance(val, (dict, list)) else str(val)
            parts.append(f"{label}: {body}")
        return "\n".join(parts)
    if isinstance(value, (list, tuple)):
        return "\n".join(reasoning_text(v) for v in value)
    return str(value)


#: One step up the ladder for the multi-horizon Kronos ensemble — the entry
#: timeframe's read cross-checked against the swing view above it.
_HIGHER_TF: Dict[str, str] = {
    "1m": "5m",
    "3m": "15m",
    "5m": "15m",
    "15m": "1h",
    "30m": "2h",
    "1h": "4h",
    "2h": "6h",
    "4h": "1d",
    "1d": "1w",
}


class AgentOrchestrator:
    """Coordinate multiple AI agents for collaborative trade decisions."""

    @staticmethod
    def _as_dict(value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                return {}
        return {}

    @staticmethod
    def _as_optional_bool(value: Any) -> Optional[bool]:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "y"}:
                return True
            if normalized in {"false", "0", "no", "n"}:
                return False
        return None

    @staticmethod
    def _normalize_trade_action(value: Any) -> str:
        if value is None:
            return ""
        normalized = str(value).strip().lower()
        action_map = {
            "long": "buy",
            "short": "sell",
            "bull": "buy",
            "bullish": "buy",
            "bear": "sell",
            "bearish": "sell",
            "neutral": "hold",
            "approve": "buy",
            "approved": "buy",
            "cancel": "reject",
            "rejected": "reject",
            "deny": "reject",
        }
        if normalized in action_map:
            return action_map[normalized]
        if normalized in {"buy", "sell", "hold", "wait", "reject"}:
            return normalized
        if "buy" in normalized or "long" in normalized:
            return "buy"
        if "sell" in normalized or "short" in normalized:
            return "sell"
        if "reject" in normalized or "cancel" in normalized or "deny" in normalized:
            return "reject"
        if "hold" in normalized or "wait" in normalized:
            return "hold"
        return normalized

    @staticmethod
    def _parse_trade_validation_decision(
        signal_action: Any,
        decision_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        approved_flag = decision_payload.get("approved")
        requested_action = AgentOrchestrator._normalize_trade_action(signal_action)
        decision_action = AgentOrchestrator._normalize_trade_action(
            decision_payload.get("action")
            or decision_payload.get("decision")
            or decision_payload.get("final_decision")
        )

        if isinstance(approved_flag, bool):
            approved = approved_flag
        elif decision_action == "reject":
            approved = False
        elif decision_action in {"hold", "wait"}:
            approved = False
        elif requested_action in {"buy", "sell"} and decision_action in {"buy", "sell"}:
            approved = decision_action == requested_action
        else:
            approved = True

        reasoning = (
            decision_payload.get("reasoning")
            or decision_payload.get("summary")
            or decision_payload.get("rationale")
            or "TradingAgents validation completed"
        )

        confidence = decision_payload.get("confidence")
        try:
            confidence_val = float(confidence) if confidence is not None else 0.0
        except (TypeError, ValueError):
            confidence_val = 0.0

        if not approved and requested_action in {"buy", "sell"} and decision_action in {"buy", "sell"} and decision_action != requested_action:
            reasoning = f"Direction mismatch: requested {requested_action}, validator suggested {decision_action}. {reasoning}"

        return {
            "approved": approved,
            "decision_action": decision_action or "reject",
            "reasoning": str(reasoning),
            "confidence": max(0.0, min(1.0, confidence_val)),
        }

    @staticmethod
    def _extract_learning_features(payload: Dict[str, Any]) -> Dict[str, Any]:
        container = AgentOrchestrator._as_dict(payload)
        pipeline_signal = AgentOrchestrator._as_dict(container.get("pipeline_signal"))
        signal_payload = AgentOrchestrator._as_dict(container.get("signal"))
        raw_data = AgentOrchestrator._as_dict(pipeline_signal.get("raw_data"))
        if not raw_data:
            raw_data = AgentOrchestrator._as_dict(signal_payload.get("raw_data"))

        volume_context = AgentOrchestrator._as_dict(container.get("volume_context"))
        if not volume_context:
            volume_context = AgentOrchestrator._as_dict(pipeline_signal.get("volume_context"))
        if not volume_context:
            volume_context = AgentOrchestrator._as_dict(signal_payload.get("volume_context"))
        if not volume_context:
            volume_context = AgentOrchestrator._as_dict(raw_data.get("volume_context"))

        btc_news_context = AgentOrchestrator._as_dict(container.get("btc_news_context"))
        if not btc_news_context:
            btc_news_context = AgentOrchestrator._as_dict(pipeline_signal.get("btc_news_context"))
        if not btc_news_context:
            btc_news_context = AgentOrchestrator._as_dict(signal_payload.get("btc_news_context"))
        if not btc_news_context:
            btc_news_context = AgentOrchestrator._as_dict(raw_data.get("btc_news_context"))

        sentiment = AgentOrchestrator._as_dict(container.get("sentiment"))
        if not sentiment:
            sentiment = AgentOrchestrator._as_dict(pipeline_signal.get("sentiment"))
        if not sentiment:
            sentiment = AgentOrchestrator._as_dict(signal_payload.get("sentiment"))

        order_flow_confirmed = AgentOrchestrator._as_optional_bool(container.get("order_flow_confirmed"))
        if order_flow_confirmed is None:
            order_flow_confirmed = AgentOrchestrator._as_optional_bool(pipeline_signal.get("order_flow_confirmed"))
        if order_flow_confirmed is None:
            order_flow_confirmed = AgentOrchestrator._as_optional_bool(signal_payload.get("order_flow_confirmed"))
        if order_flow_confirmed is None:
            order_flow_confirmed = AgentOrchestrator._as_optional_bool(raw_data.get("order_flow_confirmed"))
        if order_flow_confirmed is None:
            order_flow_confirmed = AgentOrchestrator._as_optional_bool(volume_context.get("directional_confirmed"))

        btc_news_confirms = AgentOrchestrator._as_optional_bool(container.get("btc_sentiment_confirms"))
        if btc_news_confirms is None:
            btc_news_confirms = AgentOrchestrator._as_optional_bool(container.get("btc_news_confirms"))
        if btc_news_confirms is None:
            btc_news_confirms = AgentOrchestrator._as_optional_bool(sentiment.get("btc_confirms"))
        if btc_news_confirms is None:
            btc_news_confirms = AgentOrchestrator._as_optional_bool(pipeline_signal.get("btc_sentiment_confirms"))
        if btc_news_confirms is None:
            btc_news_confirms = AgentOrchestrator._as_optional_bool(signal_payload.get("btc_sentiment_confirms"))
        if btc_news_confirms is None:
            btc_news_confirms = AgentOrchestrator._as_optional_bool(raw_data.get("btc_sentiment_confirms"))
        if btc_news_confirms is None:
            btc_news_confirms = AgentOrchestrator._as_optional_bool(btc_news_context.get("confirms"))

        btc_score = btc_news_context.get("score")
        if btc_score is None:
            btc_score = sentiment.get("btc_score")
        if btc_score is None:
            btc_score = container.get("btc_sentiment_score")
        if btc_score is None:
            btc_score = pipeline_signal.get("btc_sentiment_score")
        if btc_score is None:
            btc_score = signal_payload.get("btc_sentiment_score")

        btc_label = btc_news_context.get("label")
        if not btc_label:
            btc_label = sentiment.get("btc_label")
        if not btc_label:
            btc_label = container.get("btc_sentiment_label")
        if not btc_label:
            btc_label = pipeline_signal.get("btc_sentiment_label")
        if not btc_label:
            btc_label = signal_payload.get("btc_sentiment_label")

        volume_ratio = volume_context.get("volume_ratio")

        return {
            "order_flow_confirmed": order_flow_confirmed,
            "btc_news_confirms": btc_news_confirms,
            "btc_score": btc_score,
            "btc_label": btc_label,
            "volume_ratio": volume_ratio,
        }

    @staticmethod
    def _augment_market_data(base_data: Dict[str, Any], source_payload: Dict[str, Any]) -> Dict[str, Any]:
        enriched = dict(base_data)
        learning_features = AgentOrchestrator._extract_learning_features(source_payload)
        if learning_features:
            enriched["learning_features"] = learning_features
            for key, value in learning_features.items():
                if value is not None:
                    enriched[key] = value
        return enriched

    @staticmethod
    def _add_candles(
        context: Dict[str, Any],
        ohlcv: Sequence[Sequence[Any]],
        timeframe: str,
        window: int,
    ) -> None:
        """Closed candles, the forming one, and the movement they describe.

        The two are kept apart on purpose. An agent asked to weigh the current
        candle against the ones before it cannot do that if the bar still being
        printed is sitting in the same list as the completed ones — its high,
        low and close are provisional, and treating them as settled is how a
        "breakout" gets called on a candle that closes back inside the range.
        """
        from app.signals.candle_window import movement_summary, split_closed

        closed, forming = split_closed(ohlcv, timeframe)
        studied = closed[-window:] if window > 0 else closed

        context["recent_candles"] = [
            {"time": c[0], "open": c[1], "high": c[2], "low": c[3],
             "close": c[4], "volume": c[5] if len(c) > 5 else 0}
            for c in studied
        ]
        context["candles_analysed"] = len(studied)
        context["candle_movement"] = movement_summary(studied, forming)
        if forming is not None:
            context["forming_candle"] = {
                "time": forming[0], "open": forming[1], "high": forming[2],
                "low": forming[3], "close": forming[4],
                "volume": forming[5] if len(forming) > 5 else 0,
                "note": "still forming — not a closed candle",
            }

    @staticmethod
    async def _add_structure(
        context: Dict[str, Any],
        symbol: str,
        ohlcv: Sequence[Sequence[float]],
        db: Optional[AsyncSession] = None,
    ) -> None:
        """Fib, SMC zones, released data and our own open plans, in place.

        The board was reading indicator values with no sense of where price sat
        in the structure, which is why its levels read as arbitrary numbers.
        These are the same computations the charts draw, so what an agent
        argues and what the user sees are the same analysis. Every block is
        independently guarded: a missing one is silence, never a failed
        meeting.
        """
        from app.signals.technical import auto_fib_retracement, ohlcv_to_dataframe

        try:
            df = ohlcv_to_dataframe(ohlcv)
            fib = auto_fib_retracement(df)
            if fib.get("swing"):
                context["fib"] = {
                    "swing": fib["swing"],
                    "golden_zone": fib["golden_zone"],
                    "levels": [
                        {"ratio": lv["ratio"], "price": lv["price"]}
                        for lv in fib["levels"]
                        if lv["ratio"] in (0.382, 0.5, 0.618, 0.786)
                    ],
                }
        except Exception as exc:  # noqa: BLE001 — enrichment only
            logger.debug(f"[Orchestrator] fib context skipped for {symbol}: {exc}")

        # ── Supply/demand + channel read (the same one the charts draw) ────
        # The seats argue structure; this hands them the bases and rails the
        # user sees on the dashboard so both describe the same map.
        try:
            from app.signals.zones import compact_payload as zones_compact
            from app.signals.technical import ohlcv_to_dataframe as _zones_df

            zdf = _zones_df(ohlcv)
            if not zdf.empty:
                context["sd_channels"] = zones_compact(zdf)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[Orchestrator] sd/channel context skipped for {symbol}: {exc}")

        try:
            from app.signals.zone_narrative import zones_ahead
            from plugins.MT5TradingPlugin.backend.services.smc_strategy import (
                Candle, SMCStrategyEngine, contract_size_for_symbol,
            )

            candles = [
                Candle(time=int(c[0]) // 1000, open=float(c[1]), high=float(c[2]),
                       low=float(c[3]), close=float(c[4]),
                       volume=float(c[5]) if len(c) > 5 else 0.0)
                for c in ohlcv
            ]
            analysis = SMCStrategyEngine(
                symbol=symbol, contract_size=contract_size_for_symbol(symbol)
            ).analyze(candles)
            if not analysis.get("error"):
                last = float(analysis.get("last_price") or candles[-1].close)
                context["smc_zones"] = zones_ahead(analysis.get("zones") or [], last, limit=2)

                # The market-structure story: the same read the SMC screens
                # render, reduced to the beats a seat can quote. Every seat
                # argues structure now, not just candles — and the speech
                # bubbles quote the same phases the dashboard shows.
                try:
                    from plugins.MT5TradingPlugin.backend.services.smc_narrative import (
                        build_narrative, evidence_lines,
                    )

                    narrative = build_narrative(candles, analysis)
                    if narrative.get("steps"):
                        context["smc_structure"] = {
                            "bias": analysis.get("bias"),
                            "momentum": analysis.get("momentum"),
                            "range": analysis.get("range"),
                            "equilibrium": analysis.get("equilibrium"),
                            "flow": narrative.get("flow"),
                            "steps": narrative.get("steps"),
                            "evidence": evidence_lines(narrative),
                        }
                except Exception as exc:  # noqa: BLE001 — enrichment only
                    logger.debug(f"[Orchestrator] structure narrative skipped for {symbol}: {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[Orchestrator] zone context skipped for {symbol}: {exc}")

        try:
            from app.signals.release_narrative import latest_release_read

            if release := await latest_release_read(symbol):
                context["economic_release"] = release
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[Orchestrator] release context skipped for {symbol}: {exc}")

        if db is not None:
            try:
                from app.services.scenario_tracker import scenario_narrative, track_symbol

                if states := await track_symbol(db, symbol):
                    context["scenario"] = {
                        "plans": states, "summary": scenario_narrative(states),
                    }
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[Orchestrator] scenario context skipped for {symbol}: {exc}")
                await safe_rollback(db)

    @staticmethod
    async def _resolve_ohlcv(symbol: str, timeframe: str, limit: int) -> List[List[Any]]:
        """Candles for any instrument, from every source the app has.

        The per-branch fetches above each speak to one provider and return
        nothing when it declines. That is how an analysis of a pair that was
        trading normally reached the board with no candles at all — and an
        agent with no candles has nothing to be bullish or bearish about, so it
        says neutral. This asks the shared resolver, which exhausts every feed
        and folds a finer timeframe up before it reports failure.
        """
        try:
            from app.services import candles as candle_source

            return await candle_source.fetch(symbol, timeframe, limit)
        except Exception as exc:  # noqa: BLE001 — a dead resolver is not fatal
            logger.warning(f"[Orchestrator] candle resolver failed for {symbol}: {exc}")
            return []

    @staticmethod
    async def _add_forecast(
        context: Dict[str, Any], symbol: str, timeframe: str
    ) -> None:
        """The /forecast read — Kronos' path distribution — as board evidence.

        The forecast page has, for every pair, a model-based projection with a
        volume gate and a macro bias behind it. The room was analysing the same
        instruments without ever consulting it, so two surfaces of the same app
        could disagree about which way a market was pointing. This is the same
        call ``/forecast`` makes (cached, so a shared symbol costs one
        inference), reduced to what an agent can reason from.

        Never gating: an unavailable forecast is silence, not a hold.
        """
        try:
            from plugins.KronosForecastPlugin.backend.services import forecast_service

            resp = await forecast_service.run_forecast_cached("bitget", symbol, timeframe)
        except Exception as exc:  # noqa: BLE001 — enrichment only
            logger.debug(f"[Orchestrator] forecast context skipped for {symbol}: {exc}")
            return
        if resp is None:
            return

        signal = getattr(resp, "signal", None)
        block: Dict[str, Any] = {
            "engine": getattr(resp, "engine", None),
            "model": getattr(resp, "model_name", None),
            "horizon": f"{getattr(resp, 'pred_len', 0)}×{timeframe}",
            "decision": getattr(resp, "decision", None),
            "anchor_price": getattr(resp, "anchor_price", None),
            "note": getattr(resp, "note", None),
        }
        if signal is not None:
            block.update({
                "direction": getattr(signal, "direction", None),
                "pct_change": getattr(signal, "pct_change", None),
                "confidence": getattr(signal, "confidence", None),
                "rationale": getattr(signal, "rationale", None) or getattr(signal, "note", None),
            })
        forecast = getattr(resp, "forecast", None) or []
        if forecast:
            closes = [float(getattr(c, "close", 0) or 0) for c in forecast]
            block["projected_path"] = {
                "next_close": closes[0] if closes else None,
                "final_close": closes[-1] if closes else None,
                "path_high": max(closes) if closes else None,
                "path_low": min(closes) if closes else None,
            }
        upper = getattr(resp, "upper_band", None) or []
        lower = getattr(resp, "lower_band", None) or []
        if upper and lower:
            block["band"] = {
                "p90_final": float(getattr(upper[-1], "value", 0) or 0),
                "p10_final": float(getattr(lower[-1], "value", 0) or 0),
            }
        volume = getattr(resp, "volume", None)
        if volume is not None:
            block["volume_gate"] = {
                "status": getattr(volume, "status", None),
                "detail": getattr(volume, "detail", None),
            }

        # ── Multi-horizon ensemble: the next timeframe up the ladder ──────
        # A single-horizon read can be right about the entry window and wrong
        # about the swing. Fetching the higher TF (cached like the first) and
        # stating agreement explicitly gives every seat the same cross-check
        # the /forecast page shows, instead of each seat guessing it.
        htf = _HIGHER_TF.get(timeframe)
        if htf:
            try:
                htf_resp = await forecast_service.run_forecast_cached(
                    "bitget", symbol, htf
                )
                htf_signal = getattr(htf_resp, "signal", None) if htf_resp else None
                if htf_signal is not None:
                    htf_dir = getattr(htf_signal, "direction", None) or "flat"
                    block["htf"] = {
                        "timeframe": htf,
                        "direction": htf_dir,
                        "pct_change": getattr(htf_signal, "pct_change", None),
                        "confidence": getattr(htf_signal, "confidence", None),
                    }
                    entry_dir = str(block.get("direction") or "flat")
                    agree = (
                        entry_dir == htf_dir
                        or (entry_dir in {"up", "down"} and htf_dir == "flat")
                    )
                    block["ensemble"] = {
                        "agreement": bool(agree),
                        "entry_tf": timeframe,
                        "htf": htf,
                        "detail": (
                            f"entry {timeframe} says {entry_dir}; "
                            f"{htf} says {htf_dir}"
                        ),
                    }
            except Exception as exc:  # noqa: BLE001 — enrichment only
                logger.debug(
                    f"[Orchestrator] HTF forecast skipped for {symbol} {htf}: {exc}"
                )

        # ── Structure cross-check: the SMC story vs the model's path ───────
        # The seats get both reads; stating whether they agree is cheaper than
        # every seat re-deriving the comparison. Structure runs before the
        # forecast in the context build, so it is already on the board.
        structure = context.get("smc_structure") or {}
        fc_dir = str(block.get("direction") or "flat")
        smc_bias = str(structure.get("bias") or "neutral")
        if smc_bias != "neutral":
            smc_dir = "up" if smc_bias == "bullish" else "down"
            agree = fc_dir in {"flat", smc_dir}
            block["structure_check"] = {
                "smc_bias": smc_bias,
                "forecast_direction": fc_dir,
                "agreement": bool(agree),
                "detail": (
                    f"structure says {smc_bias} ({structure.get('momentum') or 'normal'} "
                    f"momentum); forecast says {fc_dir or 'flat'}"
                ),
            }

        # ── Band-derived trade level candidates ───────────────────────────
        # The p10/p90 band and projected path extremes are exactly where a
        # stop and targets belong; handing them over pre-computed stops every
        # seat from re-deriving (and disagreeing about) them from scratch.
        band = block.get("band") or {}
        path = block.get("projected_path") or {}
        anchor = block.get("anchor_price")
        try:
            anchor_f = float(anchor) if anchor else 0.0
        except (TypeError, ValueError):
            anchor_f = 0.0
        if anchor_f > 0:
            atr_hint = 0.0
            tech = context.get("technical") or {}
            ind = tech.get("indicators") or {}
            try:
                atr_hint = float(ind.get("atr") or 0)
            except (TypeError, ValueError):
                atr_hint = 0.0
            if atr_hint <= 0:
                atr_hint = anchor_f * 0.008  # ~0.8% fallback when no ATR yet

            p90 = band.get("p90_final")
            p10 = band.get("p10_final")
            path_high = path.get("path_high")
            path_low = path.get("path_low")

            def _f(v: Any) -> Optional[float]:
                try:
                    out = float(v)
                    return out if out > 0 else None
                except (TypeError, ValueError):
                    return None

            direction = str(block.get("direction") or "")
            levels: Dict[str, Any] = {
                "basis": "kronos_band",
                "note": (
                    "candidate SL/TPs derived from Kronos' p10/p90 band and "
                    "projected path — evidence to weigh, not orders"
                ),
            }
            if direction in {"up", "long"}:
                sl = _f(p10)
                if sl is not None:
                    levels["stop_candidate"] = round(sl - 0.5 * atr_hint, 8)
                tps = [v for v in (_f(path_high), _f(p90)) if v]
                if tps:
                    levels["target_candidates"] = [round(t, 8) for t in sorted(set(tps))]
            elif direction in {"down", "short"}:
                sl = _f(p90)
                if sl is not None:
                    levels["stop_candidate"] = round(sl + 0.5 * atr_hint, 8)
                tps = [v for v in (_f(path_low), _f(p10)) if v]
                if tps:
                    levels["target_candidates"] = [round(t, 8) for t in sorted(set(tps), reverse=True)]
            if "stop_candidate" in levels or "target_candidates" in levels:
                block["level_candidates"] = levels

        context["kronos_forecast"] = block

    #: What ``technical.action`` / ``technical.confidence`` actually are.
    #:
    #: ``app.signals.technical.analyze`` scores a fixed indicator basket and
    #: emits its own verdict. The local no-AI fallback needs those fields, so
    #: they stay — but the seats were reading them as the desk's answer and
    #: quoting the number back ("technical confidence 0.3176 is below the 0.55
    #: threshold") instead of forming a view. That scorer has never seen the
    #: structure, the forecast or the momentum read, and it returns "hold" at
    #: ~0.3 through most of a healthy trend, so deferring to it is how a
    #: trending market got declined by every seat in turn.
    _TECHNICAL_PROVENANCE = (
        "action/confidence in this block are a MECHANICAL INDICATOR COMPOSITE, "
        "not the desk's verdict and not yours. The scorer that produced them "
        "sees only the indicator basket below — never the candle window, the "
        "structure, the SMC zones, the forecast or the momentum read. It "
        "returns 'hold' at low confidence through most of a healthy trend. "
        "Weigh `indicators` yourself; never adopt this action, and never cite "
        "this confidence as the reason for your own."
    )

    @staticmethod
    def _label_technical(context: Dict[str, Any]) -> None:
        """Mark the pre-baked indicator verdict as what it is."""
        tech = context.get("technical")
        if isinstance(tech, dict) and "action" in tech:
            tech["provenance"] = AgentOrchestrator._TECHNICAL_PROVENANCE

    @staticmethod
    async def _add_cycle(context: Dict[str, Any], symbol: str) -> None:
        """The Bitcoin 1064-day calendar as board evidence.

        Every completed BTC cycle has run ~1064 days from bottom to top and
        ~365 back down, and alts follow BTC — so the season an instrument is
        trading in is context no seat should argue without. This is the same
        snapshot the cycle page renders (cached, so one read serves every
        seat), reduced to the phase, the countdowns and the evidence lines.

        Advisory only: an unavailable calendar is silence, not a hold.
        """
        try:
            from app.services import market_cycle

            if not market_cycle.cycle_applies(symbol):
                return
            snap = await market_cycle.resolve_cycle_snapshot()
            if snap is None or not snap.ok:
                return
            bias = market_cycle.cycle_bias(symbol, snap)
            context["btc_cycle"] = {
                "phase": snap.phase,
                "anchor": snap.anchor,
                "day_of_cycle": snap.day_of_cycle,
                "phase_pct": round(snap.phase_pct, 3),
                "projected_top": snap.projected_top,
                "projected_bottom": snap.projected_bottom,
                "days_to_top": snap.days_to_top,
                "days_to_bottom": snap.days_to_bottom,
                "late_phase": snap.late_phase,
                "price": snap.price,
                "cycle_high": snap.cycle_high,
                "cycle_low": snap.cycle_low,
                "bias": bias.normalized,
                "bias_reason": bias.reason,
                "validation": {
                    "top_hit_rate": snap.validation.get("top_hit_rate"),
                    "bottom_hit_rate": snap.validation.get("bottom_hit_rate"),
                    "tolerance_days": snap.validation.get("tolerance_days"),
                },
                "evidence": market_cycle.evidence_lines(snap),
            }
        except Exception as exc:  # noqa: BLE001 — enrichment only
            logger.debug(f"[Orchestrator] cycle context skipped for {symbol}: {exc}")

    @staticmethod
    async def _add_whales(context: Dict[str, Any], symbol: str) -> None:
        """The curated BTC whale registry as board evidence.

        Where the big wallets are flowing their coins over 7 days — the
        accumulation/distribution read that separates a real move from a
        trap. Advisory only; an unreachable chain is silence.
        """
        try:
            from app.services import market_cycle, whale_watch

            if not market_cycle.cycle_applies(symbol):
                return
            snap = await whale_watch.resolve_whale_snapshot()
            if snap is None:
                return
            context["btc_whales"] = {
                "status": snap.status,
                "score": snap.score,
                "net_flow_7d_btc": snap.net_flow_7d_btc,
                "wallets_read": len(snap.wallets),
                "detail": snap.detail,
                "movers": snap.moves[:3],
                "evidence": whale_watch.evidence_lines(snap),
            }
        except Exception as exc:  # noqa: BLE001 — enrichment only
            logger.debug(f"[Orchestrator] whale context skipped for {symbol}: {exc}")

    @staticmethod
    def _add_momentum(context: Dict[str, Any], ohlcv: Sequence[Sequence[Any]]) -> None:
        """A measured directional read, computed here rather than asked for.

        Everything else in the context is a fact an agent may or may not weigh.
        This is the one that answers the question the board kept ducking on a
        trending day: *is this market going somewhere right now?* It is
        arithmetic — EMA stack, range position, ATR expansion, the run of
        closes — so a strong move cannot be argued away as "unclear", and the
        prompts require an agent calling neutral into ``strong`` to say what
        would change its mind.
        """
        try:
            closes = [float(c[4]) for c in ohlcv if c and c[4] is not None]
            highs = [float(c[2]) for c in ohlcv if c and c[2] is not None]
            lows = [float(c[3]) for c in ohlcv if c and c[3] is not None]
        except (TypeError, ValueError, IndexError):
            return
        if len(closes) < 25:
            return

        def _ema(values: List[float], period: int) -> Optional[float]:
            if len(values) < period:
                return None
            k = 2.0 / (period + 1)
            out = sum(values[:period]) / period
            for v in values[period:]:
                out = v * k + out * (1 - k)
            return out

        last = closes[-1]
        ema20, ema50, ema200 = _ema(closes, 20), _ema(closes, 50), _ema(closes, 200)

        stack = None
        if ema20 and ema50:
            if last > ema20 > ema50 and (ema200 is None or ema50 > ema200):
                stack = "bullish"
            elif last < ema20 < ema50 and (ema200 is None or ema50 < ema200):
                stack = "bearish"
            else:
                stack = "mixed"

        window = closes[-60:]
        hi, lo = max(highs[-60:]), min(lows[-60:])
        span = hi - lo
        range_pos = ((last - lo) / span) if span > 0 else 0.5

        # True range now against its own recent average: an expanding range is
        # the signature of the day a trend actually pays.
        trs = [
            max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
            for i in range(max(1, len(closes) - 30), len(closes))
        ]
        atr_now = sum(trs[-5:]) / max(1, len(trs[-5:])) if trs else 0.0
        atr_avg = sum(trs) / max(1, len(trs)) if trs else 0.0
        expansion = (atr_now / atr_avg) if atr_avg > 0 else 1.0

        change_pct = ((last - window[0]) / window[0] * 100) if window and window[0] else 0.0
        ups = sum(1 for a, b in zip(window, window[1:]) if b > a)
        downs = max(0, len(window) - 1 - ups)

        # How much of the window's own range the move actually travelled. This,
        # not a fixed percentage, is what makes the read work on every
        # instrument: 1% is a huge day in EURUSD and noise in SOLUSDT, but
        # "closed near the top of its range having travelled most of it" means
        # the same thing on both.
        drive = ((last - window[0]) / span) if span > 0 else 0.0

        # How directly it got there. Net travel alone cannot tell a clean run
        # from a market bouncing between two prices that happens to close at
        # the top — both look like "covered the range". Kaufman's efficiency
        # ratio does: net movement over total movement is near 1 for a trend
        # and near 0 for an oscillation.
        path = sum(abs(b - a) for a, b in zip(window, window[1:]))
        efficiency = (abs(last - window[0]) / path) if path > 0 else 0.0

        # Price leads, the EMA stack corroborates. Requiring the stack to agree
        # was the bug: on a fast timeframe price whips across the EMA20 all day,
        # so gold up 3.2% and sitting at 83% of its range was being reported to
        # the board as "sideways" — and a board told the market is sideways
        # holds, which is exactly the entry that was missed.
        # A market that gave back nearly everything it took is chopping,
        # whatever it happens to close at. Nothing below can call that a trend.
        choppy = efficiency < 0.12

        if choppy:
            direction = "sideways"
        elif drive >= 0.35 and range_pos >= 0.55:
            direction = "up"
        elif drive <= -0.35 and range_pos <= 0.45:
            direction = "down"
        elif stack == "bullish" and change_pct > 0:
            direction = "up"
        elif stack == "bearish" and change_pct < 0:
            direction = "down"
        else:
            direction = "sideways"

        # "strong" is deliberately reachable. A market that has travelled half
        # its range and is closing at the edge of it IS moving, whether or not
        # the averages have caught up — and calling that merely "moderate" is
        # what made HOLD feel like the safe answer every time.
        aligned = (direction == "up" and stack == "bullish") or (
            direction == "down" and stack == "bearish"
        )
        edge = range_pos >= 0.7 or range_pos <= 0.3
        if direction != "sideways" and efficiency >= 0.15 and (
            (abs(drive) >= 0.5 and (expansion >= 1.05 or edge))
            or (aligned and abs(drive) >= 0.4)
        ):
            strength = "strong"
        elif direction != "sideways" and (abs(drive) >= 0.2 or (aligned and edge)):
            # Sitting at the edge of the range with the averages agreeing is a
            # market with a lean, even when the net travel was chopped up
            # getting there. "Weak" would invite the board to ignore it.
            strength = "moderate"
        else:
            strength = "weak"

        context["momentum"] = {
            "direction": direction,
            "strength": strength,
            "ema_stack": stack,
            "ema20": ema20, "ema50": ema50, "ema200": ema200,
            "range_high": hi, "range_low": lo,
            "range_position_pct": round(range_pos * 100, 1),
            "change_pct_60_bars": round(change_pct, 3),
            "range_travelled": round(drive, 3),
            "path_efficiency": round(efficiency, 3),
            "atr_expansion": round(expansion, 3),
            "up_bars": ups, "down_bars": downs,
            "note": (
                "Measured, not inferred. A 'strong' reading with a matching EMA "
                "stack is a market that is moving now — treat standing aside as "
                "a decision that needs its own justification, not the default."
            ),
        }
        # ── Entry-quality & volatility regime — the "wait for the right moment" ──
        # The 2026-08-28 wipeout was a strong move chased mid-range with no level.
        # This block tells every agent explicitly whether price is AT a level worth
        # trading from, and what regime they are in, so the signal seat cannot
        # pretend a mid-air tick is an entry.
        AgentOrchestrator._add_entry_quality(context)

    @staticmethod
    def _add_entry_quality(context: Dict[str, Any]) -> None:
        """Volatility regime + is price at a tradeable level? Injected as board fact.

        Not an opinion — distance to the nearest structural level measured in ATRs,
        and ATR expansion measured against its own recent mean. When this says
        `wait_for_level`, a market order mid-range is a chase by definition.
        """
        try:
            mom = context.get("momentum") or {}
            tech = context.get("technical") or {}
            ind = tech.get("indicators") or {}
            price = context.get("current_price")
            if price is None:
                # fallback: last close from candles
                try:
                    price = float(context.get("recent_candles", [])[-1]["close"])
                except Exception:
                    price = None
            atr = None
            try:
                atr = float(ind.get("atr") or 0) or None
            except Exception:
                atr = None
            atr_exp = None
            try:
                atr_exp = float(mom.get("atr_expansion") or 1.0)
            except Exception:
                atr_exp = 1.0
            # regime
            regime = "calm"
            if atr_exp is not None:
                if atr_exp >= 1.35:
                    regime = "expansion"
                elif atr_exp >= 1.20:
                    regime = "elevated"
                elif atr_exp <= 0.85:
                    regime = "compression"
            # level proximity — reuse the same tolerance as local_analysis
            tol = 0
            if price and atr:
                tol = max(atr * 0.6, price * 0.004)
            elif price:
                tol = price * 0.006
            has_level = False
            level_desc = ""
            level_price = None
            # 1) fib golden zone
            try:
                fib = context.get("fib") or {}
                gz = fib.get("golden_zone") or {}
                low, high = gz.get("low"), gz.get("high")
                if low is not None and high is not None and price is not None:
                    low_f, high_f = float(low), float(high)
                    if (low_f - tol) <= price <= (high_f + tol):
                        has_level = True
                        level_desc = f"fib golden zone {low_f:.2f}-{high_f:.2f}"
                        level_price = (low_f + high_f) / 2
            except Exception:
                pass
            # 2) SMC zones / supply-demand
            if not has_level and price is not None:
                try:
                    zonas = context.get("smc_zones") or []
                    if isinstance(zonas, list):
                        for z in zonas:
                            if not isinstance(z, dict):
                                continue
                            zl = z.get("low") or z.get("price") or z.get("level")
                            zh = z.get("high") or z.get("price") or z.get("level")
                            try:
                                zl_f = float(zl) if zl is not None else None
                                zh_f = float(zh) if zh is not None else None
                                if zl_f is None or zh_f is None:
                                    continue
                                lo, hi = min(zl_f, zh_f), max(zl_f, zh_f)
                                if (lo - tol) <= price <= (hi + tol):
                                    has_level = True
                                    level_desc = f"SMC {z.get('type','zone')} {lo:.2f}-{hi:.2f}"
                                    level_price = (lo + hi) / 2
                                    break
                            except Exception:
                                continue
                except Exception:
                    pass
            # 3) sd_channels compact
            if not has_level and price is not None:
                try:
                    sd = context.get("sd_channels") or {}
                    if isinstance(sd, dict):
                        for bucket in ("demand", "supply", "zones"):
                            lst = sd.get(bucket)
                            if isinstance(lst, list):
                                for z in lst:
                                    if not isinstance(z, dict):
                                        continue
                                    zp = z.get("price") or z.get("center") or z.get("level")
                                    try:
                                        zp_f = float(zp) if zp is not None else None
                                        if zp_f and abs(price - zp_f) <= tol:
                                            has_level = True
                                            level_desc = f"SD {bucket} {zp_f:.2f}"
                                            level_price = zp_f
                                            break
                                    except Exception:
                                        continue
                            if has_level:
                                break
                except Exception:
                    pass

            # mid-range?
            bb = None
            try:
                bb = float(ind.get("bb_pct_b")) if ind.get("bb_pct_b") is not None else None
            except Exception:
                bb = None
            range_pos = None
            try:
                range_pos = float(mom.get("range_position_pct")) if mom.get("range_position_pct") is not None else None
            except Exception:
                range_pos = None
            is_mid_range = False
            if bb is not None and 0.28 <= bb <= 0.72:
                if range_pos is not None and 30 <= range_pos <= 70:
                    is_mid_range = True
                elif range_pos is None and not has_level:
                    is_mid_range = True

            # recommendation
            rec = "tradeable"
            note = ""
            if regime in ("expansion", "elevated") and is_mid_range and not has_level:
                rec = "wait_for_level"
                note = f"Volatile {regime} (ATR {atr_exp:.2f}x) mid-range %B {bb:.2f} at {price} with no level — wait for pullback to {level_desc or 'fib 0.5-0.618 / nearest OB/demand'}; market order here is chasing."
            elif is_mid_range and not has_level:
                rec = "wait_for_level"
                note = f"Mid-range %B {bb} range {range_pos}% with no level — no edge. Wait for retest of level."
            elif has_level:
                note = f"At level: {level_desc} (within {tol:.2f}, ~{tol/price*100:.3f}%)"
            elif regime == "expansion" and not has_level:
                rec = "wait_for_retest"
                note = f"{regime} without level — breakout needs retest before entry."

            dist_atr = None
            if has_level and level_price and atr:
                try:
                    dist_atr = round(abs(price - level_price) / atr, 2)
                except Exception:
                    dist_atr = None

            context["volatility"] = {
                "regime": regime,
                "atr_expansion": atr_exp,
                "atr": atr,
                "note": f"Regime {regime}, ATR {atr_exp:.2f}x" + (f" — {note}" if note else ""),
            }
            context["entry_quality"] = {
                "has_level": has_level,
                "level": level_desc,
                "level_price": level_price,
                "distance_atr": dist_atr,
                "is_mid_range": is_mid_range,
                "tolerance": round(tol, 4) if tol else None,
                "recommendation": rec,
                "note": note or ("At level — tradeable" if has_level else "No level — use resting order at level"),
            }
        except Exception as exc:  # noqa: BLE001 — enrichment only
            logger.debug(f"[Orchestrator] entry_quality skipped: {exc}")

    @staticmethod
    async def _gather_context(
        symbol: str,
        timeframe: str = "1h",
        exchange: SupportedExchange = SupportedExchange.BITGET,
        db: Optional[AsyncSession] = None,
    ) -> Dict[str, Any]:
        """Build market context dict for agents.

        For forex/metals symbols (XAUUSD, XAGUSD, EURUSD, etc.) that are not
        available on Bitget, data is fetched from Yahoo Finance via the
        ForexProvider module — giving live, real-time prices.
        """
        context: Dict[str, Any] = {"symbol": symbol, "timeframe": timeframe}

        # ── Branch: FX, metals, indices, energy, softs ───────────────────────
        # Two-tier guard via market_data: is_forex_symbol alone knows only a few
        # majors plus gold, so every cross, index and commodity used to fall
        # through to the crypto branch and fail there.
        window = _analysis_window()
        if market_data.is_universal_symbol(symbol):
            try:
                ohlcv, forex_ticker = await market_data.fetch_ohlcv_universal(
                    symbol, timeframe=timeframe, limit=max(200, window)
                )
                if not ohlcv:
                    # Yahoo rate-limited, or the provider does not know this
                    # spelling. Every other feed is still worth asking before
                    # the board is handed an empty chart.
                    ohlcv = await AgentOrchestrator._resolve_ohlcv(
                        symbol, timeframe, max(200, window)
                    )
                    forex_ticker = forex_ticker or {"source": "candle_resolver"}
                if ohlcv:
                    ta = technical_analyze(ohlcv, timeframe)
                    context["technical"] = ta
                    AgentOrchestrator._add_candles(context, ohlcv, timeframe, window)
                    context["current_price"] = ohlcv[-1][4]
                    context["ticker"] = forex_ticker  # includes buy_volume, sell_volume
                    context["price_source"] = forex_ticker.get("source", "forex_provider")
                    logger.info(
                        f"[Orchestrator] {symbol} — live price "
                        f"{context['current_price']:.4g} via {context['price_source']}"
                    )
                    await AgentOrchestrator._add_structure(context, symbol, ohlcv, db)
                    AgentOrchestrator._add_momentum(context, ohlcv)
                    AgentOrchestrator._label_technical(context)
                else:
                    logger.warning(
                        f"[Orchestrator] no candles for {symbol} {timeframe} "
                        "from any feed — the board is analysing without a chart"
                    )
                    context["technical"] = {
                        "error": "No OHLCV from any feed for this symbol/timeframe"
                    }
            except Exception as e:
                logger.warning(f"[Orchestrator] Forex OHLCV failed for {symbol}: {e}")
                context["technical"] = {"error": str(e)}

            await AgentOrchestrator._add_forecast(context, symbol, timeframe)
            await AgentOrchestrator._add_cycle(context, symbol)
            await AgentOrchestrator._add_whales(context, symbol)

            # Sentiment stub (no exchange needed)
            try:
                base_coin = symbol.replace("USD", "").replace("/", "")
                context["sentiment"] = {
                    "symbol": base_coin,
                    "note": "Forex/metals — use pipeline_signal.sentiment for detail",
                }
            except Exception:
                pass

            # Account state and research, exactly as the crypto branch gets
            # them. This branch used to return here: every FX pair, metal,
            # index and energy contract reached the board with no balance to
            # size against and no calendar to check, so their seats could only
            # describe the chart while a crypto seat could plan a trade.
            await AgentOrchestrator._add_account_and_research(context, symbol, db)
            AgentOrchestrator._add_best_trader_skill(context, symbol)
            return context

        # ── Branch: Crypto symbols via Bitget ─────────────────────────────────
        connector = exchange_manager.get_exchange(exchange)
        if not connector:
            return {"error": f"Exchange {exchange.value} not initialized", "symbol": symbol}

        # OHLCV + TA
        try:
            try:
                ohlcv = await connector.get_ohlcv(
                    symbol=symbol, timeframe=timeframe, limit=max(200, window)
                )
            except Exception as exc:  # noqa: BLE001 — one venue, not the market
                logger.debug(f"[Orchestrator] {symbol} venue OHLCV failed: {exc}")
                ohlcv = []
            if not ohlcv:
                # The configured venue may not list this spelling. Public
                # keyless exchanges usually do, and an empty chart is the one
                # input that guarantees a neutral read.
                ohlcv = await AgentOrchestrator._resolve_ohlcv(
                    symbol, timeframe, max(200, window)
                )
            ta = technical_analyze(ohlcv, timeframe)
            context["technical"] = ta
            # Closed candles for price/movement context — the current bar is
            # weighed against at least the last ~28 closed candles (configurable).
            if ohlcv:
                AgentOrchestrator._add_candles(context, ohlcv, timeframe, window)
                context["current_price"] = ohlcv[-1][4]
                await AgentOrchestrator._add_structure(context, symbol, ohlcv, db)
                AgentOrchestrator._add_momentum(context, ohlcv)
                AgentOrchestrator._label_technical(context)
            else:
                logger.warning(
                    f"[Orchestrator] no candles for {symbol} {timeframe} from "
                    "any feed — the board is analysing without a chart"
                )
        except Exception as e:
            logger.warning(f"[Orchestrator] OHLCV/TA failed for {symbol}: {e}")
            context["technical"] = {"error": str(e)}

        await AgentOrchestrator._add_forecast(context, symbol, timeframe)
        await AgentOrchestrator._add_cycle(context, symbol)
        await AgentOrchestrator._add_whales(context, symbol)

        # Sentiment (basic — detailed sentiment comes from the pipeline context)
        try:
            base_coin = symbol.split("/")[0]
            context["sentiment"] = {"symbol": base_coin, "note": "Use pipeline_signal.sentiment for detailed data"}
        except Exception as e:
            logger.warning(f"[Orchestrator] Sentiment setup failed for {symbol}: {e}")
            context["sentiment"] = {"error": str(e)}

        # Ticker — the crypto connector only for crypto. A gold or FX symbol
        # sent there answers with an ERROR log and no data; the universal
        # resolver serves it from the live MT5 account instead.
        try:
            # NB: no local `from app.services import market_data` here. It is
            # already imported at module scope, and re-importing inside this
            # function makes the name function-local for the *whole* body —
            # which made the universal-symbol checks higher up raise
            # UnboundLocalError and failed every single analysis.
            is_crypto = (
                market_data.classify(market_data.normalize_symbol(symbol))
                == market_data.CRYPTO
            )
            if is_crypto:
                ticker = await connector.get_ticker(symbol)
                context["ticker"] = {
                    "last": ticker.get("last"),
                    "bid": ticker.get("bid"),
                    "ask": ticker.get("ask"),
                    "high": ticker.get("high"),
                    "low": ticker.get("low"),
                    "volume": ticker.get("quoteVolume"),
                    "change_pct": ticker.get("percentage"),
                    "source": "bitget",
                }
            else:
                # No OHLC/volume from a broker quote — say so rather than
                # implying zeros the agent might reason from.
                quote = await market_data.get_quote(symbol, db=db)
                context["ticker"] = {
                    "last": quote.price if quote else None,
                    "bid": quote.bid if quote else None,
                    "ask": quote.ask if quote else None,
                    "high": None,
                    "low": None,
                    "volume": None,
                    "change_pct": quote.change_pct if quote else None,
                    "source": quote.source if quote else "unavailable",
                }
        except Exception:
            await safe_rollback(db)

        await AgentOrchestrator._add_account_and_research(context, symbol, db)
        AgentOrchestrator._add_best_trader_skill(context, symbol)
        return context

    @staticmethod
    async def _add_account_and_research(
        context: Dict[str, Any], symbol: str, db: Optional[AsyncSession]
    ) -> None:
        """Balance to size against, and the research the desk already did.

        Both asset branches call this. Without the account state the risk
        manager sizes against an imagined balance, so "1% of equity" has no
        answer; without the research the board can buy straight into an NFP
        print the research loop flagged fifteen minutes earlier.
        """
        context["accounts"] = await AgentOrchestrator._gather_account_state(db)

        if db is not None:
            try:
                from app.agents.research_context import gather_research

                context["research"] = await gather_research(db, symbol)
            except Exception as exc:  # noqa: BLE001 - enrichment only
                logger.debug(f"[Orchestrator] research context unavailable for {symbol}: {exc}")
                await safe_rollback(db)

    @staticmethod
    def _add_best_trader_skill(context: Dict[str, Any], symbol: str) -> None:
        """Inject the stock best-trader skill for this symbol (A+A) into board context.

        Every Trading Room seat + JARVIS chair sees the same playbook, so the
        board argues from the pair's own stock levels and risk rules instead of
        a generic template. Evolution's Learned block is included, so wins feed
        back without overwriting the stock text.
        """
        try:
            from app.hermes_bridge.skill_registry import get_skill_for_symbol, load_skill_md

            norm = (symbol or "").replace("/", "").strip().upper()
            entry = get_skill_for_symbol(norm)
            if not entry:
                return
            # Load a preview (900 chars) — full SKILL.md stays on /hermes modal
            preview = ""
            try:
                loaded = load_skill_md(entry["name"])
                if loaded and loaded.get("md"):
                    md = loaded["md"]
                    body = md.split("---", 2)[-1] if md.startswith("---") else md
                    preview = body.strip()[:900]
            except Exception:
                preview = entry.get("content_preview") or ""
            context["hermes_skill"] = {
                "symbol": entry.get("symbol") or norm,
                "asset_class": entry.get("asset_class") or "",
                "group": entry.get("group") or "",
                "linked_agents": entry.get("linked_agents", []),
                "jarvis": entry.get("jarvis", {"role": "ceo", "human_name": "JARVIS"}),
                "is_best_trader": True,
                "playbook_preview": preview,
                "path": entry.get("path"),
                "evolved_at": entry.get("evolved_at"),
                "win_rate": entry.get("win_rate"),
                "decisions_reviewed": entry.get("decisions_reviewed", 0),
                "frontmatter": entry.get("frontmatter", {}),
            }
            context["hermes_best_trader"] = context["hermes_skill"]
        except Exception as exc:  # noqa: BLE001 — enrichment only
            logger.debug(f"[Orchestrator] best-trader skill inject skipped for {symbol}: {exc}")

    @staticmethod
    def _build_skill_prompt(context: Dict[str, Any], role: str, symbol: str) -> str:
        """Role-specific best-trader skill block injected per agent (so results are accurate to the agent).

        Each seat sees the same stock playbook but through its own lens:
        market_analyst → structure/fib, signal_generator → entry_quality gate,
        risk_manager → sizing, etc. Includes linked-agent verification + JARVIS
        chair reminder and the evolving Learned block (win_rate). Returned empty
        when no skill exists for this symbol so other pairs never get cross-talk.
        """
        try:
            skill = (context.get("hermes_skill") or context.get("hermes_best_trader")) if isinstance(context, dict) else None
            if not skill or not isinstance(skill, dict):
                return ""
            sym = str(skill.get("symbol") or symbol or "").upper()
            asset = str(skill.get("asset_class") or "")
            group = str(skill.get("group") or "")
            preview = str(skill.get("playbook_preview") or "").strip()[:700]
            linked = skill.get("linked_agents") or []
            jarvis = skill.get("jarvis") or {"role": "ceo", "human_name": "JARVIS"}
            evolved = skill.get("evolved_at")
            win_rate = skill.get("win_rate")
            reviewed = skill.get("decisions_reviewed", 0)
            # Verify this agent is among linked_agents (stock skills link ALL 7)
            is_linked = role in linked if linked else False
            # Role-specific lens (keeps prompts short while making each seat's job distinct)
            role_lens: Dict[str, str] = {
                "market_analyst": "Your lens: structure, fib golden zone (0.5–0.618), SMC OB/FVG, swing high/low, support/resistance, candle window + momentum. Quote the level price the skill names.",
                "sentiment_analyst": "Your lens: sentiment vs price, crowding/squeeze risk, news catalyst risk, BTC-cycle bias if crypto. Flag when sentiment confirms or contradicts structure.",
                "signal_generator": "Your lens: entry_quality gate — every BUY/SELL needs price AT a structural level (fib/OB/FVG). Mid-range between levels = HOLD with resting limit at that level. Validate stop 0.8–2.5×ATR, R:R ≥1:1.5, timeframe alignment. Use Kronos level_candidates only at a level.",
                "risk_manager": "Your lens: size from stop distance vs real equity (1% risk), correlated pairs = one position, max 3 same-direction, daily drawdown 5%, max 10× leverage. Shrink or reject when ATR expansion is high or entry is mid-range.",
                "trade_executor": "Your lens: limit at the level when spread >0.1% or ATR expansion, market only on confirmed break + close + volume. Verify SL/TP are on the right side of the level before confirming.",
                "position_reviewer": "Your lens: hold vs sweep-vs-break, RSI/MACD divergence, multi-timeframe confirmation. Move SL to breakeven after ~1R, trail thereafter. Never widen a stop.",
                "strategy_optimizer": "Your lens: win rate by setup/session/pair, calibration (confidence higher on winners?), regime change detection. With <5 closed trades answer keep with low confidence.",
            }
            lens = role_lens.get(role, f"Your lens: {role} — apply skill levels + risk rules to your decision.")
            header = f"\n\n## Best-Trader Skill — {sym} ({group or asset}) — for {role}"
            identity = (
                f"\nThis skill is linked to **all 7 specialists** ({', '.join(linked) if linked else 'all'}) + **JARVIS chair ({jarvis.get('human_name','JARVIS')} {jarvis.get('role','ceo')})**"
                f" — verified: your role `{role}` {'IS' if is_linked else 'is NOT (still show reasoning)'} listed in `linked_agents`. Apply it as YOUR seat's rules."
            )
            body = f"\n{preview}" if preview else ""
            evo = ""
            if evolved and win_rate is not None:
                try:
                    evo = f"\n\n**Learned (auto, evolved):** win {float(win_rate):.0%} over {reviewed} resolved for {sym} — bias your confidence toward measured outcomes, not the prior."
                except Exception:
                    evo = f"\n\n**Learned (auto):** evolved for {sym}."
            elif evolved:
                evo = f"\n\n**Learned (auto):** evolved for {sym} over {reviewed} resolved."
            return f"{header}\n{lens}{identity}{body}{evo}\n"
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[Orchestrator] skill prompt build skipped for {role} {symbol}: {exc}")
            return ""

    @staticmethod
    async def _build_scalp_prompt(symbol: str) -> str:
        """The Scalp Bot's multi-timeframe bias for this pair, to sharpen entries.

        Best-effort and plugin-guarded: builds a compact candle stack, runs the
        scalp strategy engine's directional read, and returns a short cue. Any
        failure (plugin absent, data gap, type mismatch) returns '' and is
        silently skipped so it can never break the pipeline.
        """
        try:
            from plugins.MT5TradingPlugin.backend.services.scalp_strategy import (
                ScalpStrategyEngine, Candle,
            )
        except Exception:
            return ""

        # MT5 TF codes the engine keys on → the timeframes our providers speak.
        tf_map = {"M5": "5m", "M15": "15m", "H1": "1h"}
        candles_by_tf: Dict[str, Any] = {}
        for tf_code, tf in tf_map.items():
            try:
                ohlcv = None
                connector = exchange_manager.get_exchange(SupportedExchange.BITGET)
                if connector and not market_data.is_universal_symbol(symbol):
                    ohlcv = await connector.get_ohlcv(symbol=symbol, timeframe=tf, limit=200)
                else:
                    ohlcv, _ = await market_data.fetch_ohlcv_universal(symbol, timeframe=tf, limit=200)
                if ohlcv:
                    candles_by_tf[tf_code] = [
                        Candle(time=int(c[0]), open=float(c[1]), high=float(c[2]),
                               low=float(c[3]), close=float(c[4]), volume=float(c[5] or 0))
                        for c in ohlcv
                    ]
            except Exception:
                continue

        if "M5" not in candles_by_tf:
            return ""
        try:
            bias = ScalpStrategyEngine(symbol, primary_tf="M5").compute_bias(candles_by_tf)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[Orchestrator] scalp bias skipped for {symbol}: {exc}")
            return ""
        if not bias or getattr(bias, "direction", "neutral") == "neutral":
            return ""
        return (
            "\n\n## Scalp Bot read (high-frequency)\n"
            f"Multi-timeframe scalp bias: {bias.direction.upper()} "
            f"(confidence {bias.confidence:.0%}). Use as a short-horizon entry-timing "
            "cue — it must still agree with structure and pass risk before acting.\n"
        )

    @staticmethod
    async def _gather_account_state(db: AsyncSession) -> Dict[str, Any]:
        """Live equity, free margin and open exposure across every venue."""
        state: Dict[str, Any] = {"mt5": [], "crypto": None, "sim": None, "policy": None}

        try:
            from app.agents.execution import get_settings, trades_today
            s = await get_settings(db)
            state["policy"] = {
                "execution_enabled": s.execution_enabled,
                # Spelled out for the seats: "dry run" reads to a model like
                # "nothing you decide will happen", which is no longer true and
                # was never a useful thing for a risk manager to believe.
                "dry_run": s.dry_run,
                "routing": (
                    "demo account only — the live account is not traded or managed"
                    if s.dry_run else "demo and live accounts take every trade together"
                ),
                "risk_pct": s.risk_pct,
                "max_open_positions": s.max_open_positions,
                "max_leverage": s.max_leverage,
                "trades_today": trades_today(),
                "max_trades_per_day": s.max_trades_per_day,
                "venues": [
                    v for v, on in
                    (("sim", s.allow_sim), ("crypto", s.allow_crypto), ("mt5", s.allow_mt5)) if on
                ],
            }
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[Orchestrator] room policy unavailable: {exc}")
            await safe_rollback(db)

        try:
            from plugins.MT5TradingPlugin.backend.models import MT5Account
            rows = (await db.execute(select(MT5Account))).scalars().all()
            state["mt5"] = [
                {
                    "account_id": a.id,
                    "name": a.name,
                    "balance": a.balance,
                    "equity": a.equity,
                    "free_margin": getattr(a, "margin_free", None),
                    "currency": a.currency,
                    "leverage": a.leverage,
                    "open_positions": getattr(a, "position_count", 0),
                }
                for a in rows
            ]
        except Exception as exc:  # noqa: BLE001 - plugin-optional
            logger.debug(f"[Orchestrator] MT5 account state unavailable: {exc}")
            await safe_rollback(db)

        try:
            from app.trading.live import LiveTradeEngine
            state["crypto"] = await LiveTradeEngine.get_settings_snapshot(db)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[Orchestrator] crypto account state unavailable: {exc}")
            await safe_rollback(db)

        try:
            from app.models.database import SimAccount
            sim = (await db.execute(select(SimAccount).limit(1))).scalar_one_or_none()
            if sim:
                state["sim"] = {"balance": sim.balance, "equity": getattr(sim, "equity", None)}
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[Orchestrator] sim account state unavailable: {exc}")
            await safe_rollback(db)

        return state

    @staticmethod
    async def _build_knowledge_graph_prompt(db: AsyncSession, role: str, symbol: str) -> str:
        """Build the stored-knowledge + Graphify code-map block for an agent prompt.

        Plugin-optional: returns '' if the AI Market Analyst plugin is absent or
        disabled, so the core keeps working standalone.
        """
        extra = ""
        try:
            from plugins.AiMarketAnalyst.backend.services.ai_router import get_router_settings
            from plugins.AiMarketAnalyst.backend.services import knowledge_service, graphify_service
        except Exception:
            return ""
        try:
            settings = await get_router_settings(db)
            # Stored knowledge (DB) — always injected when present
            rows = await knowledge_service.query_knowledge(db, agent_role=role, symbol=symbol, limit=6)
            extra += knowledge_service.build_knowledge_prompt(rows)
            # Graphify code/knowledge map (runtime) — gated by the toggle
            if settings.graphify_enabled and graphify_service.graph_available():
                base_coin = symbol.split("/")[0]
                extra += graphify_service.build_graph_prompt(base_coin, limit=5)
        except Exception as e:
            logger.debug(f"[Orchestrator] knowledge/graph prompt skipped: {e}")
        return extra

    @staticmethod
    async def _store_decision_knowledge(
        db: AsyncSession,
        symbol: str,
        timeframe: str,
        action: str,
        confidence: Any,
        reasoning: str,
    ) -> None:
        """Persist a concise insight so agents can reference it on future tasks.

        Plugin-optional and best-effort; never blocks the pipeline.
        """
        if not reasoning:
            return
        try:
            from plugins.AiMarketAnalyst.backend.services import knowledge_service
        except Exception:
            return
        try:
            conf = float(confidence or 0)
            snippet = reasoning.strip()[:280]
            await knowledge_service.store_knowledge(
                db,
                content=f"{action.upper()} @ {timeframe} (conf {conf:.2f}) — {snippet}",
                agent_role="signal_generator",
                symbol=symbol,
                kind="outcome",
                title=f"Last {symbol} {timeframe} call",
                weight=1.0 + conf,  # higher-confidence decisions weigh more
                source="orchestrator",
            )
        except Exception as e:
            logger.debug(f"[Orchestrator] store_decision_knowledge skipped: {e}")

        # ── Live vault capture (fire-and-forget) ──────────────────────────────
        # Write every significant agent decision to the Obsidian vault so the
        # Intelligence brain map and Vault page learn from it in real time.
        try:
            from plugins.ObsidianKnowledgePlugin.backend.services.vault_capture import vault_capture
            conf_val = float(confidence or 0)
            if conf_val >= 0.4:  # only capture meaningful decisions
                vault_capture(
                    action_type="agent-decision",
                    symbol=symbol,
                    summary=f"{action.upper()} @ {timeframe} | conf={conf_val:.0%}",
                    detail=reasoning.strip()[:400],
                    tags=["agent-decision", symbol, action.lower(), timeframe],
                    agent_role="signal_generator",
                    confidence=conf_val,
                )
        except Exception:
            pass

    @staticmethod
    async def _run_agent_with_memory(
        db: AsyncSession,
        agent,
        context: Dict[str, Any],
        symbol: str,
        session_id: str = "live",
        live: bool = False,
    ) -> Dict[str, Any]:
        """
        Run a single agent with memory awareness:
        1. Fetch past decisions for this symbol + role
        2. Inject stored knowledge + Graphify map
        3. Try local decision from memory (no LLM call)
        4. If not confident enough, route through connected providers (or OpenAI)

        ``live`` is for runs a person asked for and is waiting on. Both of the
        shortcuts above exist to keep the background scanner off the token
        budget, and both are wrong when someone types /room: the memory
        shortcut answers from past decisions without calling a model at all,
        and the per-symbol cache replays an answer up to an hour old. Together
        they produced a board reading "AI calls: 0" under text that had been
        written by a model — 55 minutes earlier, about a different price.

        Broadcasts start/complete/fail to the trading room so the 3D view and
        agent panels track the pipeline live.
        """
        try:
            await room.agent_started(session_id, agent.role, agent.name, symbol)
        except Exception as exc:  # noqa: BLE001 - telemetry must never break analysis
            logger.debug(f"[Orchestrator] room.agent_started failed: {exc}")

        try:
            # Each agent runs on its OWN session. The provider router writes
            # usage/commits mid-call, and Phase 1 runs two agents at once — a
            # shared session there raced its autoflush and rolled the whole
            # transaction back, which then failed every later agent in the
            # meeting. Isolated sessions keep one bad agent from poisoning the rest.
            async with AsyncSessionLocal() as agent_db:
                decision = await AgentOrchestrator._run_agent_inner(
                    agent_db, agent, context, symbol, live=live,
                )
        except Exception as exc:
            try:
                await room.agent_failed(session_id, agent.role, agent.name, str(exc))
            except Exception:  # noqa: BLE001
                pass
            raise

        try:
            await room.agent_completed(session_id, agent.role, agent.name, symbol, decision)
            # The seat now presents its verdict to the board — this is the
            # event that drives the speech bubbles and the live transcript.
            try:
                await room.agent_speaking(session_id, agent.role, agent.name, symbol, decision)
            except Exception as exc:  # noqa: BLE001 - presentation is cosmetic
                logger.debug(f"[Orchestrator] room.agent_speaking failed: {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[Orchestrator] room.agent_completed failed: {exc}")
        return decision

    @staticmethod
    async def _run_agent_inner(
        db: AsyncSession,
        agent,
        context: Dict[str, Any],
        symbol: str,
        live: bool = False,
    ) -> Dict[str, Any]:
        past = await get_past_decisions(db, symbol, agent.role)
        memory_prompt = build_memory_prompt(past)
        memory_count = len([d for d in past if d.get("outcome")])

        # ── Room identity + operator brief ─────────────────────────────────────
        # The name is what JARVIS calls them and what the user sees on the 3D
        # board; the brief is free text the user writes on the settings page.
        persona = room.persona_for(agent.role)
        memory_prompt += (
            f"\n\n## Your seat at the table\n"
            f"You are {persona['human_name']}, the {persona['title']}.\n"
        )
        if persona.get("tasks"):
            memory_prompt += (
                "\n### Standing instructions from the desk\n"
                f"{persona['tasks']}\n"
                "Follow these alongside your role's rules. Where they conflict with "
                "a hard risk limit, the risk limit wins.\n"
            )

        # Account state so sizing advice is grounded in real equity, not a guess.
        accounts = context.get("accounts") if isinstance(context, dict) else None
        if accounts and agent.role in ("risk_manager", "trade_executor", "signal_generator"):
            memory_prompt += (
                "\n### Live account state\n"
                f"{json.dumps(accounts, default=str)[:2000]}\n"
                "Size every recommendation against this equity and these limits.\n"
            )

        # Inject stored knowledge + Graphify code map (plugin-optional)
        memory_prompt += await AgentOrchestrator._build_knowledge_graph_prompt(db, agent.role, symbol)

        # ── Obsidian vault context (plugin-optional) ───────────────────────────
        # When OBSIDIAN_INJECT_CONTEXT=true, recent vault notes for this symbol
        # are appended to the memory prompt — enriching agent reasoning with
        # human-curated strategy notes and historical signal journals.
        if settings.OBSIDIAN_INJECT_CONTEXT:
            try:
                from plugins.ObsidianKnowledgePlugin.backend.services.vault_reader import VaultReader
                vault_reader = VaultReader()
                vault_context = vault_reader.get_context_for_symbol(symbol)
                if vault_context:
                    memory_prompt += (
                        "\n\n## Obsidian Vault Knowledge\n"
                        "_(Recent notes from the linked knowledge vault)_\n\n"
                        + vault_context
                    )
            except Exception as _vault_exc:
                logger.debug(f"[Orchestrator] Vault context skipped: {_vault_exc}")

        # ── Agent-Reach live research (plugin-optional) ────────────────────────
        # market_analyst/sentiment_analyst only: the other 4 roles already see
        # this indirectly via signal_context["agent_analyses"], so injecting it
        # again downstream would just add latency without new information.
        if settings.AGENT_REACH_INJECT_CONTEXT and agent.role in ("market_analyst", "sentiment_analyst"):
            try:
                from app.services import agent_reach_client
                research_summary = await agent_reach_client.research_summary_for_symbol(
                    symbol, token_budget=settings.AGENT_REACH_CONTEXT_TOKEN_BUDGET,
                )
                if research_summary:
                    memory_prompt += "\n\n## Agent-Reach Live Research\n" + research_summary
            except Exception as _ar_exc:
                logger.debug(f"[Orchestrator] Agent-Reach context skipped: {_ar_exc}")

        # Try local decision first (no LLM call) — never on a run someone is
        # waiting for: they asked the desk, not its filing cabinet.
        local = None if live else try_local_decision(past, agent.role)
        if local is not None:
            local["memory_context_used"] = memory_count
            return await agent.analyze(context, local_decision=local)

        # Scalp Bot bias sharpens the seats that time entries.
        scalp_prompt = context.get("scalp_prompt") if isinstance(context, dict) else None
        if scalp_prompt and agent.role in ("market_analyst", "signal_generator"):
            memory_prompt += scalp_prompt

        # Best-trader skill per agent (A+A+B): role-specific playbook so each AI
        # call is prompted with the symbol's own levels + this seat's lens.
        # This is the user request: "/trading-room agents must use the skills
        # fully and identify the skills of each agent so results are accurate".
        try:
            skill_block = AgentOrchestrator._build_skill_prompt(context, agent.role, symbol)
            if skill_block:
                memory_prompt += skill_block
        except Exception as _sk_exc:
            logger.debug(f"[Orchestrator] skill memory inject skipped for {agent.role} {symbol}: {_sk_exc}")

        # Route through connected providers (db passed) → falls back to OpenAI
        decision = await agent.analyze(
            context, memory_prompt=memory_prompt, db=db, live=live,
        )
        decision["memory_context_used"] = memory_count
        return decision

    @staticmethod
    async def _background_ai_allowed(db: AsyncSession) -> bool:
        """Whether background (scanner) analyses may spend AI tokens.

        In the default 'telegram_only' token mode, the continuous pair-scanner
        does NOT spend tokens — the free daily tier is reserved for
        telegram-signal and manual analyses. Plugin-optional; if the AI Market
        Analyst plugin is absent, background AI is allowed (legacy behaviour).
        """
        try:
            from plugins.AiMarketAnalyst.backend.services.ai_router import get_router_settings
        except Exception:
            return True
        try:
            s = await get_router_settings(db)
            return getattr(s, "agent_token_mode", "telegram_only") != "telegram_only"
        except Exception:
            return True

    @staticmethod
    async def analyze_symbol(
        db: AsyncSession,
        symbol: str,
        timeframe: str = "1h",
        trigger: str = "scanner",
    ) -> Dict[str, Any]:
        """Public entry: dedupe + focus-lock, then run the pipeline once.

        A pair may only be in session once at a time, and while a pair is
        pinned in the room the automated triggers (worker/scanner/signal) work
        that pair alone — other pairs are ignored until focus is cleared.
        """
        norm = _norm_symbol(symbol)
        live = trigger in LIVE_TRIGGERS

        # One meeting per pair at a time — for the automated triggers, which are
        # only ever repeating work that will come round again anyway. A person
        # who typed /room while the worker happened to hold that pair used to
        # get this stub back: no decisions, no levels, "AI calls: 0". Waiting a
        # few seconds for the meeting in progress is not what they asked for
        # either, so their run simply goes ahead alongside it.
        if norm in _inflight_symbols and not live:
            logger.info(f"[Orchestrator] {symbol} already in session — skipping duplicate ({trigger})")
            return {"symbol": symbol, "skipped": True, "reason": "already_in_session",
                    "final_action": "hold", "ai_calls": 0, "decisions": []}

        # Focus lock: pinned pair(s) only, and only for the automated triggers —
        # a pair someone asked for by name is never "not the pair we are on".
        if room.get_focus_symbols() and not live and trigger in ("scanner", "room", "signal") \
                and not room.is_focused(symbol):
            logger.debug(f"[Orchestrator] Focus locked — ignoring {symbol} ({trigger})")
            return {"symbol": symbol, "skipped": True, "reason": "focus_locked",
                    "final_action": "hold", "ai_calls": 0, "decisions": []}

        _inflight_symbols.add(norm)
        try:
            return await AgentOrchestrator._run_full_pipeline(db, symbol, timeframe, trigger)
        finally:
            _inflight_symbols.discard(norm)

    @staticmethod
    async def _run_full_pipeline(
        db: AsyncSession,
        symbol: str,
        timeframe: str = "1h",
        trigger: str = "scanner",
    ) -> Dict[str, Any]:
        # ── Guard: AI agents must be enabled ──
        if not settings.ENABLE_AI_AGENTS:
            return {
                "error": "AI agents are disabled. Enable ENABLE_AI_AGENTS in settings.",
                "symbol": symbol,
                "ai_enabled": False,
            }

        # ── Guard: Token budget — skip background scans in telegram-only mode ──
        # Reserves the free daily/monthly tier for telegram-signal & manual
        # analyses so the continuous pair-scanner never drains it.
        if trigger == "scanner" and not await AgentOrchestrator._background_ai_allowed(db):
            return {
                "symbol": symbol,
                "timeframe": timeframe,
                "ai_enabled": True,
                "token_skipped": True,
                "final_action": "hold",
                "final_confidence": 0,
                "final_reasoning": "AI skipped — telegram-only token mode (background scan preserves free tier)",
                "ai_calls": 0,
                "decisions": [],
            }

        # ── Guard: Circuit breaker — skip entire pipeline if OpenAI is down ──
        from app.agents.base import _circuit_is_open, _circuit_reason
        if _circuit_is_open():
            logger.debug(f"[Orchestrator] Skipping {symbol} — AI circuit breaker open: {_circuit_reason}")
            return {
                "error": f"AI circuit breaker open: {_circuit_reason}",
                "symbol": symbol,
                "ai_enabled": True,
                "circuit_breaker": True,
                "final_action": "hold",
                "final_confidence": 0,
                "final_reasoning": "AI unavailable — circuit breaker tripped",
                "ai_calls": 0,
            }

        session_id = str(uuid.uuid4())[:12]
        logger.info(f"[Orchestrator:{session_id}] Starting analysis for {symbol} ({timeframe})")

        # ── Load active agents from DB ──
        result = await db.execute(select(Agent).where(Agent.is_active == True))
        agents_rows = result.scalars().all()

        if not agents_rows:
            return {"error": "No active agents configured", "symbol": symbol}

        agents_by_role: Dict[str, Any] = {}
        for row in agents_rows:
            # If agent has pair restriction, check
            if row.pairs:
                allowed = [p.strip() for p in row.pairs.split(",")]
                if symbol not in allowed and symbol.replace("/", "") not in allowed:
                    continue
            agents_by_role.setdefault(row.role, []).append(agent_from_db(row))

        # ── Gather market context ──
        context = await AgentOrchestrator._gather_context(symbol, timeframe, db=db)
        if "error" in context and "technical" not in context:
            return {"error": context["error"], "symbol": symbol}

        # Scalp Bot read — computed once here (a few candle fetches) and shared
        # with the analyst + signal seats so entries are timed against the
        # high-frequency bias. Pinned pairs get it because the room keeps
        # returning to them; a pair a person asked for by name gets it because
        # they are waiting on the answer. Only the background scan goes without,
        # which is what keeps the cost bounded.
        if room.is_focused(symbol) or trigger in LIVE_TRIGGERS:
            context["scalp_prompt"] = await AgentOrchestrator._build_scalp_prompt(symbol)

        decisions: List[Dict[str, Any]] = []
        all_errors: List[str] = []

        # Opened only once the pipeline is certain to run — an early bail above
        # would otherwise leave the room showing a meeting that never happened.
        # A run someone typed and is watching. The automated triggers keep every
        # token-saving shortcut; this one gets a fresh read from every seat.
        live = trigger in LIVE_TRIGGERS

        await room.session_started(session_id, symbol, timeframe, trigger)

        # ── Phase 1: Market Analyst + Sentiment Analyst (parallel) ──
        phase1_tasks = []
        phase1_agents = []
        for role in ("market_analyst", "sentiment_analyst"):
            for agent in agents_by_role.get(role, []):
                phase1_tasks.append(
                    AgentOrchestrator._run_agent_with_memory(
                        db, agent, context, symbol, session_id, live=live,
                    )
                )
                phase1_agents.append(agent)

        if phase1_tasks:
            phase1_results = await asyncio.gather(*phase1_tasks, return_exceptions=True)
            for agent, res in zip(phase1_agents, phase1_results):
                if isinstance(res, Exception):
                    all_errors.append(f"{agent.name}: {res}")
                    continue
                decisions.append(res)
                db.add(AgentDecision(
                    agent_id=agent.agent_id,
                    agent_name=agent.name,
                    agent_role=agent.role,
                    symbol=symbol,
                    action=res.get("action", "hold"),
                    confidence=res.get("confidence", 0),
                    reasoning=reasoning_text(res.get("reasoning")),
                    market_data=json.dumps(res, default=str),
                    session_id=session_id,
                    ai_called=res.get("ai_called", True),
                    memory_context_used=res.get("memory_context_used", 0),
                ))

        # Build enriched context for signal generator
        signal_context = {**context}
        signal_context["agent_analyses"] = decisions

        # ── Phase 2: Signal Generator ──
        signal_decision = None
        for agent in agents_by_role.get("signal_generator", []):
            try:
                signal_decision = await AgentOrchestrator._run_agent_with_memory(
                    db, agent, signal_context, symbol, session_id, live=live,
                )
                decisions.append(signal_decision)
                db.add(AgentDecision(
                    agent_id=agent.agent_id,
                    agent_name=agent.name,
                    agent_role=agent.role,
                    symbol=symbol,
                    action=signal_decision.get("action", "hold"),
                    confidence=signal_decision.get("confidence", 0),
                    reasoning=reasoning_text(signal_decision.get("reasoning")),
                    market_data=json.dumps(signal_decision, default=str),
                    session_id=session_id,
                    ai_called=signal_decision.get("ai_called", True),
                    memory_context_used=signal_decision.get("memory_context_used", 0),
                ))
            except Exception as e:
                all_errors.append(f"Signal Generator: {e}")
            break  # use first active signal generator

        # ── Phase 3: Risk Manager (if BUY or SELL) ──
        risk_decision = None
        action = (signal_decision or {}).get("action", "hold")
        if action in ("buy", "sell") and agents_by_role.get("risk_manager"):
            risk_context = {**signal_context, "proposed_trade": signal_decision}
            for agent in agents_by_role["risk_manager"]:
                try:
                    risk_decision = await AgentOrchestrator._run_agent_with_memory(
                        db, agent, risk_context, symbol, session_id, live=live,
                    )
                    decisions.append(risk_decision)
                    db.add(AgentDecision(
                        agent_id=agent.agent_id,
                        agent_name=agent.name,
                        agent_role=agent.role,
                        symbol=symbol,
                        action=risk_decision.get("action", "reject"),
                        confidence=risk_decision.get("confidence", 0),
                        reasoning=reasoning_text(risk_decision.get("reasoning")),
                        market_data=json.dumps(risk_decision, default=str),
                        session_id=session_id,
                        ai_called=risk_decision.get("ai_called", True),
                        memory_context_used=risk_decision.get("memory_context_used", 0),
                    ))
                except Exception as e:
                    all_errors.append(f"Risk Manager: {e}")
                break

        # ── Phase 4: Trade Executor (if approved) ──
        exec_decision = None
        risk_action = (risk_decision or {}).get("action", "reject")
        if risk_action in ("approve", "modify") and agents_by_role.get("trade_executor"):
            exec_context = {
                **signal_context,
                "proposed_trade": signal_decision,
                "risk_review": risk_decision,
            }
            for agent in agents_by_role["trade_executor"]:
                try:
                    exec_decision = await AgentOrchestrator._run_agent_with_memory(
                        db, agent, exec_context, symbol, session_id, live=live,
                    )
                    decisions.append(exec_decision)
                    db.add(AgentDecision(
                        agent_id=agent.agent_id,
                        agent_name=agent.name,
                        agent_role=agent.role,
                        symbol=symbol,
                        action=exec_decision.get("action", "cancel"),
                        confidence=exec_decision.get("confidence", 0),
                        reasoning=reasoning_text(exec_decision.get("reasoning")),
                        market_data=json.dumps(exec_decision, default=str),
                        session_id=session_id,
                        ai_called=exec_decision.get("ai_called", True),
                        memory_context_used=exec_decision.get("memory_context_used", 0),
                    ))
                except Exception as e:
                    all_errors.append(f"Trade Executor: {e}")
                break

        # ── Phase 5: Strategy Optimizer (reviews the historical record) ──
        for agent in agents_by_role.get("strategy_optimizer", []):
            try:
                opt_context = {**signal_context, "pipeline_decisions": decisions}
                opt_decision = await AgentOrchestrator._run_agent_with_memory(
                    db, agent, opt_context, symbol, session_id, live=live,
                )
                decisions.append(opt_decision)
                db.add(AgentDecision(
                    agent_id=agent.agent_id,
                    agent_name=agent.name,
                    agent_role=agent.role,
                    symbol=symbol,
                    action=opt_decision.get("action", "keep"),
                    confidence=opt_decision.get("confidence", 0),
                    reasoning=reasoning_text(opt_decision.get("reasoning")),
                    market_data=json.dumps(opt_decision, default=str),
                    session_id=session_id,
                    ai_called=opt_decision.get("ai_called", True),
                    memory_context_used=opt_decision.get("memory_context_used", 0),
                ))
            except Exception as e:
                all_errors.append(f"Strategy Optimizer: {e}")
            break

        # ── Save signal to DB if actionable ──
        saved_signal = None
        final_action = action
        if risk_action == "reject":
            final_action = "hold"

        if final_action in ("buy", "sell"):
            confidence = signal_decision.get("confidence", 0) if signal_decision else 0
            sig = Signal(
                source=SignalSource.SYSTEM.value,
                symbol=symbol,
                action=SignalAction.BUY.value if final_action == "buy" else SignalAction.SELL.value,
                price=context.get("current_price", 0),
                timeframe=timeframe,
                strength=confidence,
                confidence=confidence,
                status=SignalStatus.PENDING.value,
                raw_data=json.dumps({
                    "session_id": session_id,
                    "agent_decisions": [d.get("reasoning", "") for d in decisions],
                    "risk_review": risk_decision,
                    "execution_plan": exec_decision,
                }, default=str),
                indicators=json.dumps({
                    "sl_pct": signal_decision.get("stop_loss_pct", 2),
                    "tp_pct": signal_decision.get("take_profit_pct", 4),
                    "risk_score": (risk_decision or {}).get("risk_score", 5),
                    "position_size_pct": (risk_decision or {}).get("position_size_pct", 2),
                    "max_leverage": (risk_decision or {}).get("max_leverage", 10),
                }, default=str),
            )
            db.add(sig)
            await db.flush()
            saved_signal = {"id": sig.id, "action": final_action, "symbol": symbol}

            # Update agent decisions with signal_id.
            # scalars() belongs on the Result, not on each Row: iterating the
            # Result yields Rows, and Row.scalars() does not exist — which threw
            # AttributeError and aborted every signal save that got this far,
            # leaving the decisions unlinked from the signal they produced.
            linked = await db.execute(
                select(AgentDecision).where(AgentDecision.session_id == session_id)
            )
            for row in linked.scalars():
                row.signal_id = sig.id

        await db.commit()

        ai_calls = sum(1 for d in decisions if d.get("ai_called", True))
        local_calls = len(decisions) - ai_calls

        # Derive confidence & reasoning from Signal Generator decision
        final_confidence = signal_decision.get("confidence", 0) if signal_decision else 0
        final_reasoning = signal_decision.get("reasoning", "") if signal_decision else ""

        # ── Persist an insight to the agent knowledge store (plugin-optional) ──
        await AgentOrchestrator._store_decision_knowledge(
            db, symbol, timeframe, final_action, final_confidence, final_reasoning
        )

        result_payload = {
            "session_id": session_id,
            "symbol": symbol,
            "timeframe": timeframe,
            "final_action": final_action,
            "final_confidence": final_confidence,
            "final_reasoning": final_reasoning,
            "signal": saved_signal,
            "decisions": decisions,
            "errors": all_errors,
            "agents_used": len(decisions),
            "ai_calls": ai_calls,
            "local_decisions": local_calls,
            "ai_enabled": True,
            # Carried on the result so every downstream surface — the room UI,
            # the Telegram publisher, the journal — can show the same forecast
            # the seats argued from, rather than fetching a later one that may
            # already disagree with the verdict it is printed under.
            "kronos_forecast": context.get("kronos_forecast"),
            "momentum": context.get("momentum"),
            # The season the verdict was made in, and the whale flow behind it.
            # Same reason as the forecast: the /room card and the web brief must
            # quote the calendar the seats actually read.
            "btc_cycle": context.get("btc_cycle"),
            "btc_whales": context.get("btc_whales"),
            # The structure story the seats argued from — quoted in the debate
            # bubbles and printed under the verdict so the room's read and its
            # conclusion travel together.
            "smc_structure": context.get("smc_structure"),
            # Best-trader skill the seats argued from — so /trading-room + /hermes
            # + Telegram all quote the same stock playbook the AI was prompted with.
            "hermes_skill": context.get("hermes_skill"),
            "hermes_best_trader": context.get("hermes_best_trader"),
            # Who asked. The publisher needs it: an answer a person typed is
            # delivered by whoever they typed to, and publishing it again turns
            # one question into eight messages.
            "trigger": trigger,
        }
        # Set before the session is announced: the publisher and the room UI
        # both draw the plan against this price, and reading it a moment later
        # meant the Telegram card quoted a level the chart was not drawn at.
        result_payload["price"] = context.get("current_price", 0)
        # Tag each decision with the skill it was prompted with for traceability
        try:
            skill_tag = (context.get("hermes_skill") or {}).get("symbol")
            if skill_tag:
                for d in decisions:
                    d["skill_used"] = skill_tag
                    d["skill_asset_class"] = (context.get("hermes_skill") or {}).get("asset_class")
        except Exception:
            pass
        await room.session_completed(session_id, result_payload)

        # The chair reads the verdict aloud — the debate view's closing line.
        try:
            await room.chair_speaking(session_id, result_payload)
        except Exception as exc:  # noqa: BLE001 - presentation is cosmetic
            logger.debug(f"[Orchestrator] room.chair_speaking failed: {exc}")

        # ── Execution: gated, sized and dry-run by default (see agents/execution.py)
        try:
            from app.agents import execution
            result_payload["execution"] = await execution.execute_decision(
                db, result_payload, room.consensus_from(decisions)
            )
        except Exception as exc:  # noqa: BLE001 - never let execution break analysis
            logger.warning(f"[Orchestrator:{session_id}] execution step failed: {exc}")

        return result_payload

    @staticmethod
    async def analyze_multiple(
        db: AsyncSession,
        symbols: List[str],
        timeframe: str = "1h",
        trigger: str = "manual",
    ) -> List[Dict[str, Any]]:
        """Run orchestration for multiple symbols sequentially."""
        results = []
        for symbol in symbols:
            try:
                r = await AgentOrchestrator.analyze_symbol(db, symbol, timeframe, trigger=trigger)
                results.append(r)
            except Exception as e:
                logger.error(f"[Orchestrator] Failed for {symbol}: {e}")
                results.append({"symbol": symbol, "error": str(e)})
        return results

    # ────────────────────────────────────────────────────────────
    # Signal Validation — called by the signal pipeline
    # ────────────────────────────────────────────────────────────

    @staticmethod
    async def validate_signal(
        db: AsyncSession,
        symbol: str,
        signal_data: Dict[str, Any],
        timeframe: str = "1h",
    ) -> Dict[str, Any]:
        """
        Validate a signal generated by the main pipeline using AI agents.
        Runs Signal Generator + Risk Manager for confirmation.
        Does NOT create signals in DB — only returns validation result.

        Returns: {approved: bool, confidence: float, reasoning: str, decisions: [...]}
        """
        if not settings.ENABLE_AI_AGENTS:
            from app.agents.custom_agents import are_custom_agents_enabled, custom_validate_trade
            if are_custom_agents_enabled():
                context = await AgentOrchestrator._gather_context(symbol, timeframe, db=db)
                context["pipeline_signal"] = signal_data
                pos_ctx = {"open_positions": 0, "max_positions": 5, "available_balance": 1000, "total_exposure": 0, "max_exposure": 5000, "is_dca": False}
                return await custom_validate_trade(db, symbol, signal_data, pos_ctx, context)
            return {"approved": True, "reason": "AI agents disabled — auto-approved"}

        from app.agents.base import _circuit_is_open
        if _circuit_is_open():
            from app.agents.custom_agents import are_custom_agents_enabled, custom_validate_trade
            if are_custom_agents_enabled():
                context = await AgentOrchestrator._gather_context(symbol, timeframe, db=db)
                context["pipeline_signal"] = signal_data
                pos_ctx = {"open_positions": 0, "max_positions": 5, "available_balance": 1000, "total_exposure": 0, "max_exposure": 5000, "is_dca": False}
                return await custom_validate_trade(db, symbol, signal_data, pos_ctx, context)
            return {"approved": True, "reason": "AI circuit breaker open — auto-approved"}

        session_id = f"val-{str(uuid.uuid4())[:8]}"
        logger.info(f"[Orchestrator:{session_id}] Validating {signal_data.get('action', '?')} signal for {symbol}")

        result = await db.execute(select(Agent).where(Agent.is_active == True))
        agents_rows = result.scalars().all()
        if not agents_rows:
            return {"approved": True, "reason": "No active agents — auto-approved"}

        agents_by_role: Dict[str, Any] = {}
        for row in agents_rows:
            if row.pairs:
                allowed = [p.strip() for p in row.pairs.split(",")]
                if symbol not in allowed and symbol.replace("/", "") not in allowed:
                    continue
            agents_by_role.setdefault(row.role, []).append(agent_from_db(row))

        # Build context from signal data + fresh market data
        context = await AgentOrchestrator._gather_context(symbol, timeframe, db=db)
        context["pipeline_signal"] = signal_data

        decisions: List[Dict[str, Any]] = []

        # Phase 1: Signal Generator validation
        for agent in agents_by_role.get("signal_generator", [])[:1]:
            try:
                decision = await AgentOrchestrator._run_agent_with_memory(
                    db, agent, context, symbol
                )
                decisions.append(decision)
                market_data = AgentOrchestrator._augment_market_data({"validation": True, **decision}, context)
                db.add(AgentDecision(
                    agent_id=agent.agent_id,
                    agent_name=agent.name,
                    agent_role=agent.role,
                    symbol=symbol,
                    action=decision.get("action", "hold"),
                    confidence=decision.get("confidence", 0),
                    reasoning=reasoning_text(decision.get("reasoning")),
                    market_data=json.dumps(market_data, default=str),
                    session_id=session_id,
                    ai_called=decision.get("ai_called", True),
                    memory_context_used=decision.get("memory_context_used", 0),
                ))
            except Exception as e:
                logger.warning(f"[Orchestrator] Signal validation agent error: {e}")

        # Phase 2: Risk Manager validation
        agent_action = decisions[0].get("action", "hold") if decisions else signal_data.get("action", "hold")
        if agent_action in ("buy", "sell"):
            risk_context = {**context, "proposed_trade": decisions[0] if decisions else signal_data}
            for agent in agents_by_role.get("risk_manager", [])[:1]:
                try:
                    risk_decision = await AgentOrchestrator._run_agent_with_memory(
                        db, agent, risk_context, symbol
                    )
                    decisions.append(risk_decision)
                    market_data = AgentOrchestrator._augment_market_data({"validation": True, **risk_decision}, risk_context)
                    db.add(AgentDecision(
                        agent_id=agent.agent_id,
                        agent_name=agent.name,
                        agent_role=agent.role,
                        symbol=symbol,
                        action=risk_decision.get("action", "reject"),
                        confidence=risk_decision.get("confidence", 0),
                        reasoning=reasoning_text(risk_decision.get("reasoning")),
                        market_data=json.dumps(market_data, default=str),
                        session_id=session_id,
                        ai_called=risk_decision.get("ai_called", True),
                        memory_context_used=risk_decision.get("memory_context_used", 0),
                    ))
                except Exception as e:
                    logger.warning(f"[Orchestrator] Risk validation agent error: {e}")

        await db.commit()

        # Determine approval
        approved = True
        reasoning_parts = []

        if decisions:
            sig_decision = decisions[0]
            sig_action = sig_decision.get("action", "hold")
            sig_confidence = sig_decision.get("confidence", 0)

            # Signal Generator disagrees with pipeline
            pipeline_action = signal_data.get("action", "hold")
            if sig_action != pipeline_action and sig_action == "hold":
                approved = False
                reasoning_parts.append(f"Signal Generator recommends HOLD (conf={sig_confidence:.2f})")

            # Risk Manager rejects
            if len(decisions) > 1:
                risk = decisions[1]
                risk_action = risk.get("action", "reject")
                if risk_action == "reject":
                    approved = False
                    reasoning_parts.append(f"Risk Manager rejected: {risk.get('reasoning', 'N/A')[:100]}")
                elif risk_action == "modify":
                    reasoning_parts.append(f"Risk Manager modified: {risk.get('reasoning', 'N/A')[:100]}")

        ai_calls = sum(1 for d in decisions if d.get("ai_called", True))
        return {
            "approved": approved,
            "session_id": session_id,
            "confidence": decisions[0].get("confidence", 0) if decisions else 0,
            "reasoning": " | ".join(reasoning_parts) if reasoning_parts else "Agents approved",
            "decisions": decisions,
            "ai_calls": ai_calls,
            "risk_params": decisions[1] if len(decisions) > 1 else None,
        }

    # ────────────────────────────────────────────────────────────
    # Trade Validation — called before auto-trade execution
    # ────────────────────────────────────────────────────────────

    @staticmethod
    async def validate_trade(
        db: AsyncSession,
        symbol: str,
        signal: Dict[str, Any],
        position_context: Dict[str, Any],
        timeframe: str = "1h",
        auto_trade_ai_provider: str = "orchestrator",
        tradingagents_llm_provider: str = "openai",
        tradingagents_deep_think_llm: str = "gpt-5.4",
        tradingagents_quick_think_llm: str = "gpt-5.4-mini",
        tradingagents_backend_url: Optional[str] = None,
        tradingagents_max_debate_rounds: int = 2,
        tradingagents_max_risk_discuss_rounds: int = 2,
    ) -> Dict[str, Any]:
        """
        Final validation before placing a trade order.
        Runs Risk Manager + Trade Executor with position/balance context.
        Falls back to custom rule-based agents when AI is unavailable.

        Returns: {approved: bool, order_params: {...}, reasoning: str}
        """
        if not settings.ENABLE_AI_AGENTS:
            # Check if custom agents are enabled as fallback
            from app.agents.custom_agents import are_custom_agents_enabled, custom_validate_trade
            if are_custom_agents_enabled():
                context = await AgentOrchestrator._gather_context(symbol, timeframe, db=db)
                context["signal"] = signal
                context["positions"] = position_context
                return await custom_validate_trade(db, symbol, signal, position_context, context)
            return {"approved": True, "reason": "AI agents disabled — auto-approved"}

        from app.agents.base import _circuit_is_open
        if _circuit_is_open():
            # Use custom agents when circuit breaker is open
            from app.agents.custom_agents import are_custom_agents_enabled, custom_validate_trade
            if are_custom_agents_enabled():
                context = await AgentOrchestrator._gather_context(symbol, timeframe, db=db)
                context["signal"] = signal
                context["positions"] = position_context
                return await custom_validate_trade(db, symbol, signal, position_context, context)
            return {"approved": True, "reason": "AI circuit breaker open — auto-approved"}

        provider = str(auto_trade_ai_provider or "orchestrator").strip().lower()
        if provider == "tradingagents":
            ta_session_id = f"trade-ta-{str(uuid.uuid4())[:8]}"
            logger.info(f"[Orchestrator:{ta_session_id}] Validating trade for {symbol} via TradingAgents sidecar")

            try:
                from app.services import tradingagents_client as ta_client
                from app.models.database import TradingAgentsRun

                max_debate_rounds = max(1, min(6, int(tradingagents_max_debate_rounds or 2)))
                max_risk_discuss_rounds = max(1, min(6, int(tradingagents_max_risk_discuss_rounds or 2)))

                ta_payload: Dict[str, Any] = {
                    "ticker": symbol,
                    "llm_provider": tradingagents_llm_provider or None,
                    "deep_think_llm": tradingagents_deep_think_llm or None,
                    "quick_think_llm": tradingagents_quick_think_llm or None,
                    "max_debate_rounds": max_debate_rounds,
                    "max_risk_discuss_rounds": max_risk_discuss_rounds,
                }
                ta_payload = {k: v for k, v in ta_payload.items() if v is not None}

                started_at = time.monotonic()
                snapshot = await ta_client.run_analysis_blocking(ta_payload)

                run_id = snapshot.get("run_id", ta_session_id)
                result = snapshot.get("result") or {}
                raw_decision: Any = (
                    result.get("recommendation")
                    or snapshot.get("decision_summary")
                    or result.get("final_trade_decision")
                    or {}
                )
                if hasattr(raw_decision, "model_dump"):
                    raw_decision = raw_decision.model_dump()
                if isinstance(raw_decision, dict) and not (
                    raw_decision.get("decision")
                    or raw_decision.get("action")
                    or raw_decision.get("signal")
                ):
                    # Recommendation lacked a direct verdict — fall back to the
                    # portfolio manager's full text so parsing still works.
                    raw_decision = {
                        **raw_decision,
                        "decision": (result.get("final_trade_decision") or "")[:4000],
                    }
                if isinstance(raw_decision, str):
                    try:
                        raw_decision = json.loads(raw_decision)
                    except Exception:
                        raw_decision = {"decision": raw_decision}

                parsed = AgentOrchestrator._parse_trade_validation_decision(signal.get("action"), raw_decision)
                decision_action = "approve" if parsed["approved"] else "reject"

                duration_s = round(time.monotonic() - started_at, 1)
                rec = result.get("recommendation") or {}

                # Persist the full pipeline output so the Trading Room can
                # replay every analyst report and debate behind this verdict.
                try:
                    db.add(TradingAgentsRun(
                        run_id=run_id,
                        ticker=symbol,
                        mapped_ticker=result.get("ticker"),
                        trade_date=result.get("trade_date") or snapshot.get("trade_date") or "",
                        source="trade_validation",
                        status="done" if snapshot.get("status") == "done" else "error",
                        decision=(rec.get("action") or "").lower() or None,
                        confidence=parsed["confidence"],
                        reasoning=reasoning_text(parsed.get("reasoning")),
                        result=result or None,
                        config_used={k: v for k, v in ta_payload.items() if k != "api_key"},
                        error=snapshot.get("error"),
                        duration_s=duration_s,
                    ))
                    await db.commit()
                except Exception as persist_err:  # noqa: BLE001
                    logger.warning(f"[Orchestrator:{ta_session_id}] TradingAgents persist failed: {persist_err}")

                decision = {
                    "agent_name": "TradingAgents",
                    "agent_role": "tradingagents",
                    "provider": "tradingagents",
                    "action": decision_action,
                    "confidence": parsed["confidence"],
                    "reasoning": parsed["reasoning"],
                    "raw_decision": raw_decision,
                    "session_id": ta_session_id,
                    "ta_run_id": run_id,
                    "duration_s": duration_s,
                }

                return {
                    "approved": parsed["approved"],
                    "session_id": ta_session_id,
                    "reasoning": parsed["reasoning"],
                    "decisions": [decision],
                    "order_params": None,
                    "provider": "tradingagents",
                }
            except Exception as e:
                logger.warning(f"[Orchestrator:{ta_session_id}] TradingAgents validation failed, falling back to orchestrator: {e}")

        session_id = f"trade-{str(uuid.uuid4())[:8]}"
        logger.info(f"[Orchestrator:{session_id}] Validating trade execution for {symbol}")

        result = await db.execute(select(Agent).where(Agent.is_active == True))
        agents_rows = result.scalars().all()
        if not agents_rows:
            return {"approved": True, "reason": "No active agents — auto-approved"}

        agents_by_role: Dict[str, Any] = {}
        for row in agents_rows:
            if row.pairs:
                allowed = [p.strip() for p in row.pairs.split(",")]
                if symbol not in allowed and symbol.replace("/", "") not in allowed:
                    continue
            agents_by_role.setdefault(row.role, []).append(agent_from_db(row))

        context = await AgentOrchestrator._gather_context(symbol, timeframe, db=db)
        context["signal"] = signal
        context["positions"] = position_context

        decisions: List[Dict[str, Any]] = []

        # Risk Manager
        for agent in agents_by_role.get("risk_manager", [])[:1]:
            try:
                decision = await AgentOrchestrator._run_agent_with_memory(
                    db, agent, context, symbol
                )
                decisions.append(decision)
                market_data = AgentOrchestrator._augment_market_data({"trade_validation": True, **decision}, context)
                db.add(AgentDecision(
                    agent_id=agent.agent_id,
                    agent_name=agent.name,
                    agent_role=agent.role,
                    symbol=symbol,
                    action=decision.get("action", "reject"),
                    confidence=decision.get("confidence", 0),
                    reasoning=reasoning_text(decision.get("reasoning")),
                    market_data=json.dumps(market_data, default=str),
                    session_id=session_id,
                    ai_called=decision.get("ai_called", True),
                    memory_context_used=decision.get("memory_context_used", 0),
                ))
            except Exception as e:
                logger.warning(f"[Orchestrator] Trade Risk Manager error: {e}")

        # Trade Executor
        if decisions and decisions[0].get("action") in ("approve", "modify"):
            exec_context = {**context, "risk_review": decisions[0]}
            for agent in agents_by_role.get("trade_executor", [])[:1]:
                try:
                    exec_decision = await AgentOrchestrator._run_agent_with_memory(
                        db, agent, exec_context, symbol
                    )
                    decisions.append(exec_decision)
                    market_data = AgentOrchestrator._augment_market_data({"trade_validation": True, **exec_decision}, exec_context)
                    db.add(AgentDecision(
                        agent_id=agent.agent_id,
                        agent_name=agent.name,
                        agent_role=agent.role,
                        symbol=symbol,
                        action=exec_decision.get("action", "cancel"),
                        confidence=exec_decision.get("confidence", 0),
                        reasoning=reasoning_text(exec_decision.get("reasoning")),
                        market_data=json.dumps(market_data, default=str),
                        session_id=session_id,
                        ai_called=exec_decision.get("ai_called", True),
                        memory_context_used=exec_decision.get("memory_context_used", 0),
                    ))
                except Exception as e:
                    logger.warning(f"[Orchestrator] Trade Executor error: {e}")

        await db.commit()

        # Determine approval
        approved = True
        reasoning_parts = []

        if decisions:
            risk = decisions[0]
            if risk.get("action") == "reject":
                approved = False
                reasoning_parts.append(f"Risk Manager rejected: {risk.get('reasoning', 'N/A')[:100]}")

            if len(decisions) > 1:
                executor = decisions[1]
                if executor.get("action") == "cancel":
                    approved = False
                    reasoning_parts.append(f"Trade Executor cancelled: {executor.get('reasoning', 'N/A')[:100]}")
                elif executor.get("action") == "wait":
                    approved = False
                    reasoning_parts.append(f"Trade Executor: wait — {executor.get('timing', 'N/A')}")

        return {
            "approved": approved,
            "session_id": session_id,
            "reasoning": " | ".join(reasoning_parts) if reasoning_parts else "Trade approved",
            "decisions": decisions,
            "order_params": decisions[1] if len(decisions) > 1 else None,
        }

    # ────────────────────────────────────────────────────────────
    # Position Monitor — analyzes open positions
    # ────────────────────────────────────────────────────────────

    @staticmethod
    async def analyze_positions(
        db: AsyncSession,
        min_hold_hours: float = 2.0,
    ) -> Dict[str, Any]:
        """
        Analyze open positions that have been held for at least min_hold_hours
        since their last agent review (or since entry if never reviewed).

        Uses the Position Reviewer + Market Analyst agents.
        Returns list of position reviews with HOLD/CLOSE/ADJUST decisions.
        """
        use_custom_agents = False
        if not settings.ENABLE_AI_AGENTS:
            from app.agents.custom_agents import are_custom_agents_enabled
            if not are_custom_agents_enabled():
                return {"skipped": True, "reason": "AI agents disabled and custom agents not enabled"}
            use_custom_agents = True

        from app.agents.base import _circuit_is_open
        if not use_custom_agents and _circuit_is_open():
            from app.agents.custom_agents import are_custom_agents_enabled
            if not are_custom_agents_enabled():
                return {"skipped": True, "reason": "AI circuit breaker open and custom agents not enabled"}
            use_custom_agents = True

        session_id = f"pos-{str(uuid.uuid4())[:8]}"
        logger.info(f"[Orchestrator:{session_id}] Starting position analysis (min_hold={min_hold_hours}h, custom={use_custom_agents})")

        from datetime import timedelta
        now = now_sast()
        review_threshold = now - timedelta(hours=min_hold_hours)

        # Fetch open trades
        result = await db.execute(
            select(Trade).where(
                Trade.exchange == "bitget",
                Trade.status == "open",
                Trade.trade_side == "open",
            )
        )
        open_trades = result.scalars().all()
        if not open_trades:
            return {"skipped": False, "positions_reviewed": 0, "reason": "No open positions"}

        # Filter: only review positions older than threshold
        # Check last agent review via AgentDecision with session_id starting with "pos-"
        trades_to_review = []
        for trade in open_trades:
            # Check last position review for this trade's symbol
            last_review = await db.execute(
                select(AgentDecision.created_at)
                .where(
                    AgentDecision.symbol == trade.symbol,
                    AgentDecision.agent_role == "position_reviewer",
                    AgentDecision.session_id.like("pos-%"),
                )
                .order_by(AgentDecision.created_at.desc())
                .limit(1)
            )
            last_review_time = last_review.scalar_one_or_none()

            # If never reviewed, use trade creation time
            reference_time = last_review_time or trade.created_at
            if reference_time and reference_time < review_threshold:
                trades_to_review.append(trade)

        if not trades_to_review:
            return {
                "skipped": False,
                "positions_reviewed": 0,
                "reason": f"No positions need review (all reviewed within {min_hold_hours}h)",
            }

        # Load active agents
        agent_result = await db.execute(select(Agent).where(Agent.is_active == True))
        agents_rows = agent_result.scalars().all()
        agents_by_role: Dict[str, Any] = {}
        for row in agents_rows:
            agents_by_role.setdefault(row.role, []).append(agent_from_db(row))

        # Get exchange positions for current prices/PnL
        from app.exchanges.bitget import BitgetConnector
        from typing import cast
        connector = cast(
            Optional[BitgetConnector],
            exchange_manager.get_exchange(SupportedExchange.BITGET),
        )

        exchange_positions = []
        if connector:
            try:
                pos_data = await connector.get_futures_positions()
                exchange_positions = [
                    p for p in (pos_data or []) if float(p.get("total", 0)) > 0
                ]
            except Exception as e:
                logger.warning(f"[Orchestrator] Failed to fetch positions: {e}")

        reviews = []
        actions_taken = []

        for trade in trades_to_review:
            symbol = trade.symbol
            bitget_sym = symbol.replace("/", "").upper()

            # Find matching exchange position
            hold_side = "long" if trade.side == "buy" else "short"
            exchange_pos = next(
                (p for p in exchange_positions
                 if p.get("symbol", "").upper().replace("USDT_UMCBL", "USDT") == bitget_sym
                 and (p.get("holdSide", "") or "").lower() == hold_side),
                None,
            )

            current_price = 0.0
            unrealized_pnl = 0.0
            if exchange_pos:
                current_price = float(exchange_pos.get("markPrice", 0) or exchange_pos.get("marketPrice", 0) or 0)
                unrealized_pnl = float(exchange_pos.get("unrealizedPL", 0) or 0)

            if current_price <= 0 and connector:
                try:
                    ticker = await connector.get_ticker(symbol)
                    current_price = float(ticker.get("last", 0) or 0)
                except Exception:
                    pass

            entry_price = trade.price or 0
            hold_duration_hours = (now - trade.created_at).total_seconds() / 3600 if trade.created_at else 0

            pnl_pct = 0.0
            if entry_price > 0 and current_price > 0:
                if hold_side == "long":
                    pnl_pct = ((current_price - entry_price) / entry_price) * 100
                else:
                    pnl_pct = ((entry_price - current_price) / entry_price) * 100

            # Build position context
            position_context = {
                "symbol": symbol,
                "side": trade.side,
                "hold_side": hold_side,
                "entry_price": entry_price,
                "current_price": current_price,
                "unrealized_pnl": unrealized_pnl,
                "pnl_pct": round(pnl_pct, 2),
                "hold_duration_hours": round(hold_duration_hours, 1),
                "stop_loss": trade.stop_loss,
                "take_profit": trade.take_profit,
                "leverage": trade.leverage,
                "amount": trade.amount,
                "trade_id": trade.id,
            }

            # Gather market context — multi-timeframe for reversal detection
            context = await AgentOrchestrator._gather_context(symbol, "1h", db=db)
            context["position"] = position_context

            # Add multi-TF analysis for reversal detection
            multi_tf_data = {}
            for tf in ("5m", "15m", "4h"):
                try:
                    tf_ohlcv = await connector.get_ohlcv(symbol=symbol, timeframe=tf, limit=100) if connector else None
                    if tf_ohlcv and len(tf_ohlcv) >= 20:
                        tf_ta = technical_analyze(tf_ohlcv, tf)
                        multi_tf_data[tf] = {
                            "score": tf_ta.get("score"),
                            "action": tf_ta.get("action"),
                            "indicators": {
                                k: tf_ta.get("indicators", {}).get(k)
                                for k in ("rsi", "macd_histogram", "macd", "macd_signal", "adx", "plus_di", "minus_di", "ema50", "ema200", "stoch_rsi", "price", "bb_pct_b", "bb_upper", "bb_lower", "volume_ratio", "buy_ratio")
                            },
                            "reasons": tf_ta.get("reasons", [])[:5],
                        }
                except Exception as e:
                    logger.debug(f"[Orchestrator] Multi-TF {tf} for {symbol}: {e}")
            if multi_tf_data:
                context["multi_timeframe"] = multi_tf_data

            decisions = []

            if use_custom_agents:
                # ── Custom agents path ──
                from app.agents.custom_agents import custom_market_analyst, custom_position_reviewer
                try:
                    market_dec = await custom_market_analyst(db, context, symbol)
                    decisions.append(market_dec)
                    db.add(AgentDecision(
                        agent_id=0,
                        agent_name=market_dec.get("agent_name", "Custom Market Analyst"),
                        agent_role="market_analyst",
                        symbol=symbol,
                        action=market_dec.get("action", "neutral"),
                        confidence=market_dec.get("confidence", 0),
                        reasoning=reasoning_text(market_dec.get("reasoning")),
                        market_data=json.dumps({"position_review": True, **market_dec}, default=str),
                        session_id=session_id,
                        ai_called=False,
                        memory_context_used=0,
                    ))
                except Exception as e:
                    logger.warning(f"[Orchestrator] Custom Market Analyst error for {symbol}: {e}")

                try:
                    review = await custom_position_reviewer(
                        db, context, symbol, exchange_pos or {"holdSide": hold_side, "openPriceAvg": entry_price}
                    )
                    decisions.append(review)
                    db.add(AgentDecision(
                        agent_id=0,
                        agent_name=review.get("agent_name", "Custom Position Reviewer"),
                        agent_role="position_reviewer",
                        symbol=symbol,
                        action=review.get("action", "hold"),
                        confidence=review.get("confidence", 0),
                        reasoning=reasoning_text(review.get("reasoning")),
                        market_data=json.dumps({"position_review": True, **review}, default=str),
                        session_id=session_id,
                        ai_called=False,
                        memory_context_used=0,
                    ))
                except Exception as e:
                    logger.warning(f"[Orchestrator] Custom Position Reviewer error for {symbol}: {e}")
            else:
                # ── AI agents path ──
                # Market Analyst for current conditions
                for agent in agents_by_role.get("market_analyst", [])[:1]:
                    try:
                        decision = await AgentOrchestrator._run_agent_with_memory(
                            db, agent, context, symbol
                        )
                        decisions.append(decision)
                        db.add(AgentDecision(
                            agent_id=agent.agent_id,
                            agent_name=agent.name,
                            agent_role=agent.role,
                            symbol=symbol,
                            action=decision.get("action", "neutral"),
                            confidence=decision.get("confidence", 0),
                            reasoning=reasoning_text(decision.get("reasoning")),
                            market_data=json.dumps({"position_review": True, **decision}, default=str),
                            session_id=session_id,
                            ai_called=decision.get("ai_called", True),
                            memory_context_used=decision.get("memory_context_used", 0),
                        ))
                    except Exception as e:
                        logger.warning(f"[Orchestrator] Market Analyst error for {symbol}: {e}")

                # Position Reviewer
                review_context = {**context, "market_analysis": decisions[0] if decisions else {}}
                for agent in agents_by_role.get("position_reviewer", [])[:1]:
                    try:
                        review = await AgentOrchestrator._run_agent_with_memory(
                            db, agent, review_context, symbol
                        )
                        decisions.append(review)
                        db.add(AgentDecision(
                            agent_id=agent.agent_id,
                            agent_name=agent.name,
                            agent_role=agent.role,
                            symbol=symbol,
                            action=review.get("action", "hold"),
                            confidence=review.get("confidence", 0),
                            reasoning=reasoning_text(review.get("reasoning")),
                            market_data=json.dumps({"position_review": True, **review}, default=str),
                            session_id=session_id,
                            ai_called=review.get("ai_called", True),
                            memory_context_used=review.get("memory_context_used", 0),
                        ))
                    except Exception as e:
                        logger.warning(f"[Orchestrator] Position Reviewer error for {symbol}: {e}")

            # Process position review decision
            review_decision = decisions[-1] if decisions else {}
            review_action = review_decision.get("action", "hold")

            review_entry = {
                "trade_id": trade.id,
                "symbol": symbol,
                "side": trade.side,
                "hold_duration_hours": round(hold_duration_hours, 1),
                "pnl_pct": round(pnl_pct, 2),
                "unrealized_pnl": round(unrealized_pnl, 4),
                "review_action": review_action,
                "confidence": review_decision.get("confidence", 0),
                "reasoning": review_decision.get("reasoning", ""),
                "urgency": review_decision.get("urgency", "low"),
                "adjusted_sl": review_decision.get("adjusted_sl"),
                "adjusted_tp": review_decision.get("adjusted_tp"),
                "partial_close_pct": review_decision.get("partial_close_pct"),
                "decisions": decisions,
                "session_id": session_id,
            }
            reviews.append(review_entry)

            # If action is close, create a close signal (any urgency — AI decided to close)
            if review_action == "close":
                close_action = SignalAction.SELL if trade.side == "buy" else SignalAction.BUY
                # Ensure close signals always pass the 0.3 confidence threshold
                close_confidence = max(review_decision.get("confidence", 0.5), 0.5)
                close_sig = Signal(
                    source=SignalSource.SYSTEM.value,
                    symbol=symbol,
                    action=close_action.value,
                    price=current_price,
                    timeframe="1h",
                    strength=close_confidence,
                    confidence=close_confidence,
                    status=SignalStatus.PENDING.value,
                    raw_data=json.dumps({
                        "source": "position_review",
                        "session_id": session_id,
                        "trade_id": trade.id,
                        "review_action": review_action,
                        "urgency": review_decision.get("urgency", "low"),
                        "reasoning": review_decision.get("reasoning", ""),
                        "hold_duration_hours": round(hold_duration_hours, 1),
                        "pnl_pct": round(pnl_pct, 2),
                    }, default=str),
                )
                db.add(close_sig)
                actions_taken.append({
                    "symbol": symbol,
                    "action": "close_signal_created",
                    "signal_action": close_action.value,
                })
                logger.info(
                    f"[Orchestrator:{session_id}] Position review → CLOSE signal for {symbol} "
                    f"(PnL={pnl_pct:+.1f}%, held {hold_duration_hours:.1f}h)"
                )

            # If action is adjust, update SL/TP on the trade record AND on exchange
            if review_action == "adjust":
                new_sl = review_decision.get("adjusted_sl")
                new_tp = review_decision.get("adjusted_tp")
                if new_sl and new_sl > 0:
                    trade.stop_loss = new_sl
                if new_tp and new_tp > 0:
                    trade.take_profit = new_tp
                actions_taken.append({
                    "symbol": symbol,
                    "action": "sl_tp_adjusted",
                    "new_sl": new_sl,
                    "new_tp": new_tp,
                })
                logger.info(
                    f"[Orchestrator:{session_id}] Position review → ADJUST {symbol} "
                    f"SL={new_sl} TP={new_tp}"
                )
                # Execute SL/TP replacement on exchange
                if connector and (new_sl or new_tp):
                    try:
                        replace_result = await connector.replace_tpsl_orders(
                            symbol=symbol,
                            hold_side=hold_side,
                            new_sl=float(new_sl) if new_sl else None,
                            new_tp=float(new_tp) if new_tp else None,
                        )
                        actions_taken.append({
                            "symbol": symbol,
                            "action": "exchange_tpsl_replaced",
                            "cancelled": len(replace_result.get("cancelled", [])),
                            "placed": len(replace_result.get("placed", [])),
                        })
                        logger.info(
                            f"[Orchestrator:{session_id}] Exchange TPSL replaced for {symbol}: "
                            f"{replace_result}"
                        )
                    except Exception as e:
                        logger.error(
                            f"[Orchestrator:{session_id}] Failed to replace TPSL on exchange "
                            f"for {symbol}: {e}"
                        )

        await db.commit()

        logger.info(
            f"[Orchestrator:{session_id}] Position analysis complete: "
            f"{len(reviews)} reviewed, {len(actions_taken)} actions taken"
        )

        return {
            "skipped": False,
            "session_id": session_id,
            "positions_reviewed": len(reviews),
            "actions_taken": actions_taken,
            "reviews": reviews,
        }

    # ── Sim Position Analysis ───────────────────────────────────

    @staticmethod
    async def analyze_sim_positions(
        db: AsyncSession,
        min_hold_hours: float = 0.016,
    ) -> Dict[str, Any]:
        """
        Analyze open simulation positions using AI agents.
        Only runs if the sim account has enable_ai=True.
        """
        if not settings.ENABLE_AI_AGENTS:
            return {"skipped": True, "reason": "AI agents disabled"}

        from app.agents.base import _circuit_is_open
        if _circuit_is_open():
            return {"skipped": True, "reason": "AI circuit breaker open"}

        # Check sim account AI setting
        acct_result = await db.execute(select(SimAccount).limit(1))
        sim_account = acct_result.scalar_one_or_none()
        if not sim_account or not getattr(sim_account, "enable_ai", False):
            return {"skipped": True, "reason": "Sim AI disabled"}

        session_id = f"sim-pos-{str(uuid.uuid4())[:8]}"
        logger.info(f"[Orchestrator:{session_id}] Starting sim position analysis")

        from datetime import timedelta
        now = now_sast()
        review_threshold = now - timedelta(hours=min_hold_hours)

        # Fetch open sim positions
        result = await db.execute(
            select(SimPosition).where(SimPosition.status == "open")
        )
        open_positions = result.scalars().all()
        if not open_positions:
            return {"skipped": False, "positions_reviewed": 0, "reason": "No open sim positions"}

        # Filter by last review time
        positions_to_review = []
        for pos in open_positions:
            last_review = await db.execute(
                select(AgentDecision.created_at)
                .where(
                    AgentDecision.symbol == pos.symbol,
                    AgentDecision.agent_role == "position_reviewer",
                    AgentDecision.session_id.like("sim-pos-%"),
                )
                .order_by(AgentDecision.created_at.desc())
                .limit(1)
            )
            last_review_time = last_review.scalar_one_or_none()
            reference_time = last_review_time or pos.created_at
            if reference_time and reference_time < review_threshold:
                positions_to_review.append(pos)

        if not positions_to_review:
            return {"skipped": False, "positions_reviewed": 0, "reason": "All reviewed recently"}

        # Load agents
        agent_result = await db.execute(select(Agent).where(Agent.is_active == True))
        agents_by_role: Dict[str, Any] = {}
        for row in agent_result.scalars().all():
            agents_by_role.setdefault(row.role, []).append(agent_from_db(row))

        # Fetch current prices from exchange
        connector = exchange_manager.get_exchange(SupportedExchange.BITGET)

        reviews = []
        actions_taken = []

        for pos in positions_to_review:
            symbol = pos.symbol
            current_price = pos.current_price or 0.0

            # Try to get live price
            if connector:
                try:
                    ticker = await connector.get_ticker(symbol)
                    current_price = float(ticker.get("last", 0) or 0) or current_price
                except Exception:
                    pass

            entry_price = pos.entry_price or 0
            hold_hours = (now - pos.created_at).total_seconds() / 3600 if pos.created_at else 0
            leverage = pos.leverage or 1

            pnl_pct = 0.0
            if entry_price > 0 and current_price > 0:
                if pos.side == "long":
                    pnl_pct = ((current_price - entry_price) / entry_price) * 100 * leverage
                else:
                    pnl_pct = ((entry_price - current_price) / entry_price) * 100 * leverage

            position_context = {
                "symbol": symbol,
                "side": "buy" if pos.side == "long" else "sell",
                "hold_side": pos.side,
                "entry_price": entry_price,
                "current_price": current_price,
                "pnl_pct": round(pnl_pct, 2),
                "hold_duration_hours": round(hold_hours, 1),
                "stop_loss": pos.stop_loss,
                "take_profit": pos.take_profit,
                "leverage": leverage,
                "amount": pos.amount,
                "mode": "simulation",
            }

            context = await AgentOrchestrator._gather_context(symbol, "1h", db=db)
            context["position"] = position_context

            # Add multi-TF analysis for reversal detection (sim)
            sim_multi_tf = {}
            for tf in ("5m", "15m", "4h"):
                try:
                    tf_ohlcv = await connector.get_ohlcv(symbol=symbol, timeframe=tf, limit=100) if connector else None
                    if tf_ohlcv and len(tf_ohlcv) >= 20:
                        tf_ta = technical_analyze(tf_ohlcv, tf)
                        sim_multi_tf[tf] = {
                            "score": tf_ta.get("score"),
                            "action": tf_ta.get("action"),
                            "indicators": {
                                k: tf_ta.get("indicators", {}).get(k)
                                for k in ("rsi", "macd_histogram", "macd", "macd_signal", "adx", "plus_di", "minus_di", "ema50", "ema200", "stoch_rsi", "price", "bb_pct_b", "bb_upper", "bb_lower", "volume_ratio", "buy_ratio")
                            },
                            "reasons": tf_ta.get("reasons", [])[:5],
                        }
                except Exception as e:
                    logger.debug(f"[Orchestrator] Sim Multi-TF {tf} for {symbol}: {e}")
            if sim_multi_tf:
                context["multi_timeframe"] = sim_multi_tf

            decisions = []

            # Market Analyst
            for agent in agents_by_role.get("market_analyst", [])[:1]:
                try:
                    decision = await AgentOrchestrator._run_agent_with_memory(db, agent, context, symbol)
                    decisions.append(decision)
                    db.add(AgentDecision(
                        agent_id=agent.agent_id, agent_name=agent.name, agent_role=agent.role,
                        symbol=symbol, action=decision.get("action", "neutral"),
                        confidence=decision.get("confidence", 0),
                        reasoning=reasoning_text(decision.get("reasoning")),
                        market_data=json.dumps({"sim_position_review": True, **decision}, default=str),
                        session_id=session_id,
                        ai_called=decision.get("ai_called", True),
                        memory_context_used=decision.get("memory_context_used", 0),
                    ))
                except Exception as e:
                    logger.warning(f"[Orchestrator] Sim Market Analyst error for {symbol}: {e}")

            # Position Reviewer
            review_context = {**context, "market_analysis": decisions[0] if decisions else {}}
            for agent in agents_by_role.get("position_reviewer", [])[:1]:
                try:
                    review = await AgentOrchestrator._run_agent_with_memory(db, agent, review_context, symbol)
                    decisions.append(review)
                    db.add(AgentDecision(
                        agent_id=agent.agent_id, agent_name=agent.name, agent_role=agent.role,
                        symbol=symbol, action=review.get("action", "hold"),
                        confidence=review.get("confidence", 0),
                        reasoning=reasoning_text(review.get("reasoning")),
                        market_data=json.dumps({"sim_position_review": True, **review}, default=str),
                        session_id=session_id,
                        ai_called=review.get("ai_called", True),
                        memory_context_used=review.get("memory_context_used", 0),
                    ))
                except Exception as e:
                    logger.warning(f"[Orchestrator] Sim Position Reviewer error for {symbol}: {e}")

            review_decision = decisions[-1] if decisions else {}
            review_action = review_decision.get("action", "hold")

            reviews.append({
                "position_id": pos.id,
                "symbol": symbol,
                "side": pos.side,
                "hold_duration_hours": round(hold_hours, 1),
                "pnl_pct": round(pnl_pct, 2),
                "review_action": review_action,
                "confidence": review_decision.get("confidence", 0),
                "reasoning": review_decision.get("reasoning", ""),
            })

            # CLOSE: create close signal for sim
            if review_action == "close" and review_decision.get("urgency") in ("high", "medium"):
                close_action = SignalAction.SELL if pos.side == "long" else SignalAction.BUY
                db.add(Signal(
                    source=SignalSource.SYSTEM.value, symbol=symbol,
                    action=close_action.value, price=current_price,
                    timeframe="1h",
                    strength=review_decision.get("confidence", 0.5),
                    confidence=review_decision.get("confidence", 0.5),
                    status=SignalStatus.PENDING.value,
                    raw_data=json.dumps({
                        "source": "sim_position_review", "session_id": session_id,
                        "position_id": pos.id, "review_action": review_action,
                        "reasoning": review_decision.get("reasoning", ""),
                        "pnl_pct": round(pnl_pct, 2),
                    }, default=str),
                ))
                actions_taken.append({"symbol": symbol, "action": "sim_close_signal_created"})
                logger.info(
                    f"[Orchestrator:{session_id}] Sim position → CLOSE {symbol} "
                    f"(PnL={pnl_pct:+.1f}%, held {hold_hours:.1f}h)"
                )

            # ADJUST: update sim position SL/TP
            if review_action == "adjust":
                new_sl = review_decision.get("adjusted_sl")
                new_tp = review_decision.get("adjusted_tp")
                if new_sl and new_sl > 0:
                    pos.stop_loss = new_sl
                if new_tp and new_tp > 0:
                    pos.take_profit = new_tp
                actions_taken.append({"symbol": symbol, "action": "sim_sl_tp_adjusted", "new_sl": new_sl, "new_tp": new_tp})
                logger.info(f"[Orchestrator:{session_id}] Sim position → ADJUST {symbol} SL={new_sl} TP={new_tp}")

        await db.commit()
        logger.info(f"[Orchestrator:{session_id}] Sim position analysis: {len(reviews)} reviewed, {len(actions_taken)} actions")

        return {
            "skipped": False, "session_id": session_id,
            "positions_reviewed": len(reviews),
            "actions_taken": actions_taken, "reviews": reviews,
        }

    # ── MT5 position review ─────────────────────────────────────

    @staticmethod
    async def analyze_mt5_positions(
        db: AsyncSession,
        min_hold_hours: float = 0.5,
    ) -> Dict[str, Any]:
        """Put the Position Reviewer on the trades sitting at the broker.

        The reviewers covered crypto and the sim account only. An MT5 position —
        the venue the room actually places most of its orders on — was managed
        by nothing but the stop it was opened with, so "the agents manage the
        linked account" was true of every account except the linked one.

        Only positions this app opened are reviewed: a trade placed by hand in
        the terminal belongs to the user. The seat's verdict is applied through
        the broker unless the room is in dry run, in which case it is recorded
        and reported like any other blocked order.
        """
        if not settings.ENABLE_AI_AGENTS:
            return {"skipped": True, "reason": "AI agents disabled"}
        from app.agents.base import _circuit_is_open

        if _circuit_is_open():
            return {"skipped": True, "reason": "AI circuit breaker open"}

        try:
            from app.trading.order_tags import is_app_order
            from plugins.MT5TradingPlugin.backend.models import (
                MT5Account, MT5AccountStatus, MT5Position,
            )
            from plugins.MT5TradingPlugin.backend.services.mt5_client import mt5_client
        except Exception as exc:  # noqa: BLE001 — plugin-optional
            return {"skipped": True, "reason": f"MT5 plugin unavailable: {exc}"}

        from datetime import timedelta

        from app.agents.execution import get_settings, mt5_targets
        from app.workers.room_worker import get_focus_timeframe

        room_settings = await get_settings(db)
        # Reviewing is acting: a verdict here closes positions and moves stops.
        # So it is scoped to exactly the accounts the room is allowed to trade —
        # in a dry run the demo alone, which is what "the live account is not
        # managed by the room" has to mean to be worth anything.
        routing = await mt5_targets(db, room_settings)
        accounts = routing["targets"]
        send = bool(room_settings.execution_enabled)
        if not accounts:
            return {"skipped": False, "positions_reviewed": 0,
                    "reason": f"No account to review ({routing['note']})"}

        agents_rows = (await db.execute(
            select(Agent).where(
                Agent.is_active == True,  # noqa: E712 - SQLAlchemy needs the comparison
                Agent.role == "position_reviewer",
            )
        )).scalars().all()
        if not agents_rows:
            return {"skipped": True, "reason": "No position reviewer configured"}
        reviewer = agent_from_db(agents_rows[0])

        session_id = f"mt5-{str(uuid.uuid4())[:8]}"
        cutoff = now_sast() - timedelta(hours=max(0.0, min_hold_hours))
        reviews: List[Dict[str, Any]] = []

        for account in accounts:
            positions = (await db.execute(
                select(MT5Position).where(MT5Position.account_id == account.id)
            )).scalars().all()
            for pos in positions:
                if not is_app_order(getattr(pos, "comment", "")):
                    continue
                opened = getattr(pos, "mt5_time_open", None) or getattr(pos, "created_at", None)
                if opened is not None and opened.tzinfo is None:
                    opened = opened.replace(tzinfo=cutoff.tzinfo)
                if opened is not None and opened > cutoff:
                    continue

                side = getattr(pos.side, "value", str(pos.side))
                entry = float(pos.price_open or 0)
                price = float(pos.price_current or entry or 0)
                if entry <= 0 or price <= 0:
                    continue

                context = await AgentOrchestrator._gather_context(
                    pos.symbol, get_focus_timeframe(), db=db
                )
                is_long = str(side).lower() in ("long", "buy")
                pnl_pct = ((price - entry) / entry * 100) if is_long \
                    else ((entry - price) / entry * 100)
                context["position"] = {
                    "venue": "mt5",
                    "account": f"{account.login}@{account.server}",
                    "ticket": pos.mt5_ticket,
                    "symbol": pos.symbol,
                    "side": side,
                    "volume": pos.volume,
                    "entry": entry,
                    "current_price": price,
                    "stop_loss": pos.sl,
                    "take_profit": pos.tp,
                    "unrealized_pnl_pct": round(pnl_pct, 3),
                }

                try:
                    review = await AgentOrchestrator._run_agent_with_memory(
                        db, reviewer, context, pos.symbol, session_id, live=False,
                    )
                except Exception as exc:  # noqa: BLE001 — one bad review, not the pass
                    logger.warning(f"[Orchestrator] MT5 review failed for {pos.symbol}: {exc}")
                    continue

                db.add(AgentDecision(
                    agent_id=reviewer.agent_id,
                    agent_name=reviewer.name,
                    agent_role=reviewer.role,
                    symbol=pos.symbol,
                    action=review.get("action", "hold"),
                    confidence=review.get("confidence", 0),
                    reasoning=reasoning_text(review.get("reasoning")),
                    market_data=json.dumps({"mt5_position_review": True, **review}, default=str),
                    session_id=session_id,
                    ai_called=review.get("ai_called", True),
                    memory_context_used=review.get("memory_context_used", 0),
                ))

                applied = await AgentOrchestrator._apply_mt5_review(
                    account, pos, review, send=send, client=mt5_client,
                )
                reviews.append({
                    "ticket": pos.mt5_ticket, "symbol": pos.symbol,
                    "action": review.get("action", "hold"),
                    "confidence": review.get("confidence", 0),
                    "reasoning": reasoning_text(review.get("reasoning"))[:400],
                    "applied": applied,
                })

        await db.commit()
        if reviews:
            logger.info(
                f"[Orchestrator:{session_id}] reviewed {len(reviews)} MT5 position(s)"
            )
        return {"skipped": False, "positions_reviewed": len(reviews), "reviews": reviews}

    @staticmethod
    async def _apply_mt5_review(
        account: Any, pos: Any, review: Dict[str, Any], *, send: bool, client: Any,
    ) -> Dict[str, Any]:
        """Turn one reviewer verdict into broker calls, or into a dry-run note.

        A stop is only ever moved in the protective direction here. The seat is
        being asked whether the trade is still good, not for permission to give
        the position more room — widening belongs to :mod:`app.agents.guardian`,
        which pairs it with a matching cut in size.
        """
        action = str(review.get("action") or "hold").lower()
        out: Dict[str, Any] = {"action": action, "applied": send, "sent": []}
        is_long = str(getattr(pos.side, "value", pos.side)).lower() in ("long", "buy")

        def _protective(candidate: Any) -> Optional[float]:
            try:
                level = float(candidate)
            except (TypeError, ValueError):
                return None
            if level <= 0 or pos.sl is None:
                return level if level > 0 else None
            return level if (level > pos.sl if is_long else level < pos.sl) else None

        try:
            if action == "close":
                out["sent"].append("close")
                if send:
                    await client.close_position(
                        login=account.login, server=account.server,
                        password=account.password_encrypted, ticket=int(pos.mt5_ticket),
                    )
            elif action == "adjust":
                new_sl = _protective(review.get("adjusted_sl"))
                new_tp = review.get("adjusted_tp")
                pct = review.get("partial_close_pct")
                if new_sl or new_tp:
                    out["sent"].append(f"modify sl={new_sl} tp={new_tp}")
                    if send:
                        await client.modify_order(
                            login=account.login, server=account.server,
                            password=account.password_encrypted,
                            ticket=int(pos.mt5_ticket),
                            sl=round(new_sl, 5) if new_sl else pos.sl,
                            tp=round(float(new_tp), 5) if new_tp else pos.tp,
                        )
                if pct:
                    volume = max(0.01, round(float(pos.volume or 0) * float(pct) / 100 / 0.01) * 0.01)
                    if volume >= 0.01 and volume < float(pos.volume or 0):
                        out["sent"].append(f"partial close {volume}")
                        if send:
                            await client.close_position(
                                login=account.login, server=account.server,
                                password=account.password_encrypted,
                                ticket=int(pos.mt5_ticket), volume=volume,
                            )
        except Exception as exc:  # noqa: BLE001 — a broker error is reported, not raised
            out["error"] = str(exc)[:200]
            logger.warning(f"[Orchestrator] MT5 review action failed for #{pos.mt5_ticket}: {exc}")
        return out

    # ── Limit Order Optimization ────────────────────────────────

    @staticmethod
    async def analyze_limit_orders(
        db: AsyncSession,
        min_age_minutes: float = 5.0,
    ) -> Dict[str, Any]:
        """
        Analyze pending limit orders and recommend better entry prices
        based on current market conditions & TA.

        Uses Market Analyst to evaluate whether the current limit price
        is still optimal or if a better entry is available.
        Returns list of order reviews with KEEP/ADJUST/CANCEL decisions.
        """
        if not settings.ENABLE_AI_AGENTS:
            return {"skipped": True, "reason": "AI agents disabled"}

        from app.agents.base import _circuit_is_open
        if _circuit_is_open():
            return {"skipped": True, "reason": "AI circuit breaker open"}

        session_id = f"lo-{str(uuid.uuid4())[:8]}"
        logger.info(f"[Orchestrator:{session_id}] Starting limit order optimization")

        from app.exchanges.bitget import BitgetConnector
        from typing import cast
        connector = cast(
            Optional[BitgetConnector],
            exchange_manager.get_exchange(SupportedExchange.BITGET),
        )
        if not connector:
            return {"skipped": True, "reason": "Bitget connector not available"}

        # Fetch all pending limit orders
        try:
            open_orders = await connector.get_futures_open_orders()
        except Exception as e:
            logger.error(f"[Orchestrator:{session_id}] Failed to fetch open orders: {e}")
            return {"skipped": True, "reason": str(e)}

        if not open_orders:
            return {"skipped": False, "orders_reviewed": 0, "reason": "No pending limit orders"}

        # Filter to limit orders only (not market), and respect minimum age
        now = now_sast()
        limit_orders = []
        for order in open_orders:
            order_type = (order.get("orderType") or "").lower()
            if order_type != "limit":
                continue
            create_ts = order.get("cTime") or order.get("createTime")
            if create_ts:
                try:
                    from datetime import datetime, timezone, timedelta
                    created = datetime.fromtimestamp(int(create_ts) / 1000, tz=timezone(timedelta(hours=2)))
                    age_min = (now - created).total_seconds() / 60
                    if age_min < min_age_minutes:
                        continue
                except Exception:
                    pass
            limit_orders.append(order)

        if not limit_orders:
            return {"skipped": False, "orders_reviewed": 0, "reason": "No limit orders old enough to review"}

        # Load market_analyst agent
        agent_result = await db.execute(select(Agent).where(Agent.is_active == True, Agent.role == "market_analyst"))
        analyst_agents = agent_result.scalars().all()
        if not analyst_agents:
            return {"skipped": True, "reason": "No active market_analyst agent"}

        analyst = agent_from_db(analyst_agents[0])

        # Ensure precision cache is loaded
        await connector.get_max_leverage("BTCUSDT")

        reviews = []
        adjustments = []

        for order in limit_orders:
            bitget_sym = (order.get("symbol") or "").upper()
            display_sym = bitget_sym.replace("USDT", "/USDT")
            order_id = order.get("orderId", "")
            side = (order.get("side") or "").lower()
            order_price = float(order.get("price") or 0)
            size = order.get("size") or order.get("baseVolume") or "0"
            sl_price = order.get("presetStopLossPrice") or order.get("stopLoss")
            tp_price = order.get("presetStopSurplusPrice") or order.get("takeProfit")

            if not bitget_sym or order_price <= 0:
                continue

            # Gather market context
            try:
                context = await AgentOrchestrator._gather_context(display_sym, "1h")
            except Exception as e:
                logger.warning(f"[Orchestrator:{session_id}] Context failed for {display_sym}: {e}")
                continue

            current_price = context.get("current_price", 0)
            if current_price <= 0:
                continue

            # Add multi-timeframe context for better entry analysis
            multi_tf = {}
            for tf in ("5m", "15m"):
                try:
                    tf_ohlcv = await connector.get_ohlcv(symbol=display_sym, timeframe=tf, limit=100)
                    if tf_ohlcv and len(tf_ohlcv) >= 20:
                        tf_ta = technical_analyze(tf_ohlcv, tf)
                        multi_tf[tf] = {
                            "score": tf_ta.get("score"),
                            "action": tf_ta.get("action"),
                            "indicators": {
                                k: tf_ta.get("indicators", {}).get(k)
                                for k in ("rsi", "macd_histogram", "bb_lower", "bb_upper", "bb_middle",
                                           "ema50", "ema200", "adx", "stoch_rsi", "volume_ratio",
                                           "ma5", "ma10", "price")
                            },
                        }
                except Exception:
                    pass

            # Build order-specific context for AI
            order_context = {
                **context,
                "multi_timeframe": multi_tf,
                "pending_order": {
                    "order_id": order_id,
                    "symbol": display_sym,
                    "side": side,
                    "order_price": order_price,
                    "current_market_price": current_price,
                    "size": size,
                    "price_distance_pct": round(
                        ((order_price - current_price) / current_price) * 100, 3
                    ),
                    "stop_loss": float(sl_price) if sl_price and float(sl_price) > 0 else None,
                    "take_profit": float(tp_price) if tp_price and float(tp_price) > 0 else None,
                },
                "task": (
                    "Analyze this PENDING LIMIT ORDER and decide if the entry price should be adjusted "
                    "for a better fill based on current market conditions. Consider: support/resistance levels, "
                    "Bollinger Bands, recent price action, volume, momentum indicators. "
                    "If market has moved significantly since the order was placed, recommend a new price. "
                    "Respond with JSON: {\"action\": \"keep|adjust|cancel\", \"confidence\": 0.0-1.0, "
                    "\"new_price\": <float or null>, \"reasoning\": \"...\"}"
                ),
            }

            try:
                decision = await AgentOrchestrator._run_agent_with_memory(
                    db, analyst, order_context, display_sym,
                )
                db.add(AgentDecision(
                    agent_id=analyst.agent_id,
                    agent_name=analyst.name,
                    agent_role=analyst.role,
                    symbol=display_sym,
                    action=decision.get("action", "keep"),
                    confidence=decision.get("confidence", 0),
                    reasoning=reasoning_text(decision.get("reasoning")),
                    market_data=json.dumps({"limit_order_review": True, "order_id": order_id, **decision}, default=str),
                    session_id=session_id,
                    ai_called=decision.get("ai_called", True),
                    memory_context_used=decision.get("memory_context_used", 0),
                ))
            except Exception as e:
                logger.warning(f"[Orchestrator:{session_id}] AI analysis failed for {display_sym} order {order_id}: {e}")
                continue

            action = decision.get("action", "keep")
            new_price = decision.get("new_price")
            confidence = decision.get("confidence", 0)

            review_entry = {
                "order_id": order_id,
                "symbol": display_sym,
                "side": side,
                "current_price": current_price,
                "order_price": order_price,
                "action": action,
                "new_price": new_price,
                "confidence": confidence,
                "reasoning": decision.get("reasoning", ""),
            }
            reviews.append(review_entry)

            # Execute adjustment if AI recommends it with sufficient confidence
            if action == "adjust" and new_price and confidence >= 0.5:
                try:
                    adjusted_price = float(new_price)

                    # Recalculate SL/TP for the new entry price
                    new_sl: Optional[float] = None
                    new_tp: Optional[float] = None
                    try:
                        from app.trading.simulation import SmartStopLoss
                        ohlcv = await connector.get_ohlcv(symbol=display_sym, timeframe="1h", limit=200)
                        hold_side = "long" if side == "buy" else "short"
                        sl_data = SmartStopLoss.calculate(ohlcv, hold_side, adjusted_price)
                        new_sl = sl_data.get("stop_loss")
                        new_tp = sl_data.get("take_profit")
                        logger.info(
                            f"[Orchestrator:{session_id}] Recalculated SL/TP for {display_sym} "
                            f"@ {adjusted_price}: SL={new_sl} TP={new_tp}"
                        )
                    except Exception as sltp_err:
                        logger.warning(
                            f"[Orchestrator:{session_id}] SL/TP recalc failed for {display_sym}, "
                            f"keeping existing: {sltp_err}"
                        )
                        new_sl = float(sl_price) if sl_price and float(sl_price) > 0 else None
                        new_tp = float(tp_price) if tp_price and float(tp_price) > 0 else None

                    result = await connector.modify_limit_order_price(
                        symbol=bitget_sym,
                        order_id=order_id,
                        new_price=adjusted_price,
                        size=str(size),
                        stop_loss=new_sl,
                        take_profit=new_tp,
                    )

                    new_order_id = result.get("orderId") or result.get("order_id") or order_id

                    # Update the Trade DB record with new price + order ID + SL/TP
                    try:
                        trade_result = await db.execute(
                            select(Trade).where(
                                Trade.exchange_order_id == order_id,
                                Trade.status == "open",
                            )
                        )
                        trade_record = trade_result.scalars().first()
                        if trade_record:
                            trade_record.price = adjusted_price
                            trade_record.exchange_order_id = str(new_order_id)
                            if new_sl:
                                trade_record.stop_loss = new_sl
                            if new_tp:
                                trade_record.take_profit = new_tp
                            logger.info(
                                f"[Orchestrator:{session_id}] Trade DB updated: "
                                f"{display_sym} price={adjusted_price} "
                                f"orderId={order_id}→{new_order_id} SL={new_sl} TP={new_tp}"
                            )
                        else:
                            logger.warning(
                                f"[Orchestrator:{session_id}] No Trade record found for "
                                f"order {order_id} — DB not updated"
                            )
                    except Exception as db_err:
                        logger.error(
                            f"[Orchestrator:{session_id}] Failed to update Trade DB "
                            f"for {order_id}: {db_err}"
                        )

                    adjustments.append({
                        "order_id": order_id,
                        "new_order_id": new_order_id,
                        "symbol": display_sym,
                        "old_price": order_price,
                        "new_price": adjusted_price,
                        "new_sl": new_sl,
                        "new_tp": new_tp,
                        "result": result,
                    })
                    logger.info(
                        f"[Orchestrator:{session_id}] Limit order adjusted: {display_sym} "
                        f"{order_price} → {adjusted_price} (conf={confidence:.2f})"
                    )
                except Exception as e:
                    logger.error(
                        f"[Orchestrator:{session_id}] Failed to modify order {order_id}: {e}"
                    )
                    review_entry["error"] = str(e)

            elif action == "cancel" and confidence >= 0.7:
                try:
                    await connector.cancel_futures_order(
                        symbol=bitget_sym,
                        margin_coin="USDT",
                        order_id=order_id,
                    )

                    # Mark Trade DB record as canceled
                    try:
                        trade_result = await db.execute(
                            select(Trade).where(
                                Trade.exchange_order_id == order_id,
                                Trade.status == "open",
                            )
                        )
                        trade_record = trade_result.scalars().first()
                        if trade_record:
                            trade_record.status = "canceled"
                            trade_record.closed_at = now
                            logger.info(
                                f"[Orchestrator:{session_id}] Trade DB marked canceled: "
                                f"{display_sym} order {order_id}"
                            )
                    except Exception as db_err:
                        logger.error(
                            f"[Orchestrator:{session_id}] Failed to cancel Trade DB "
                            f"for {order_id}: {db_err}"
                        )

                    adjustments.append({
                        "order_id": order_id,
                        "symbol": display_sym,
                        "action": "cancelled",
                    })
                    logger.info(
                        f"[Orchestrator:{session_id}] Limit order cancelled: {display_sym} "
                        f"@ {order_price} (conf={confidence:.2f})"
                    )
                except Exception as e:
                    logger.error(f"[Orchestrator:{session_id}] Failed to cancel order {order_id}: {e}")

        await db.commit()

        logger.info(
            f"[Orchestrator:{session_id}] Limit order optimization: "
            f"{len(reviews)} reviewed, {len(adjustments)} adjusted"
        )

        return {
            "skipped": False,
            "session_id": session_id,
            "orders_reviewed": len(reviews),
            "orders_adjusted": len(adjustments),
            "reviews": reviews,
            "adjustments": adjustments,
        }

    # ── Open-position SL/TP AI recalculation ──

    @staticmethod
    async def analyze_open_positions(
        db: AsyncSession,
        min_age_minutes: float = 10.0,
    ) -> Dict[str, Any]:
        """
        AI-powered SL/TP recalculation for open (filled) positions.

        Uses the Market Analyst to evaluate whether the current SL/TP levels
        are still optimal given changing market conditions. If conditions have
        shifted, it recalculates smart SL/TP and replaces them on the exchange.
        """
        if not settings.ENABLE_AI_AGENTS:
            return {"skipped": True, "reason": "AI agents disabled"}

        from app.agents.base import _circuit_is_open
        if _circuit_is_open():
            return {"skipped": True, "reason": "AI circuit breaker open"}

        session_id = f"pos-{str(uuid.uuid4())[:8]}"
        logger.info(f"[Orchestrator:{session_id}] Starting open position SL/TP optimization")

        from app.exchanges.bitget import BitgetConnector
        from app.trading.simulation import SmartStopLoss
        from typing import cast

        connector = cast(
            Optional[BitgetConnector],
            exchange_manager.get_exchange(SupportedExchange.BITGET),
        )
        if not connector:
            return {"skipped": True, "reason": "Bitget connector not available"}

        # Fetch open positions
        try:
            pos_data = await connector.get_futures_positions()
        except Exception as e:
            logger.error(f"[Orchestrator:{session_id}] Failed to fetch positions: {e}")
            return {"skipped": True, "reason": str(e)}

        open_positions = [
            p for p in (pos_data or [])
            if float(p.get("total") or p.get("available") or 0) > 0
        ]
        if not open_positions:
            return {"skipped": False, "positions_reviewed": 0, "reason": "No open positions"}

        # Filter by age — only review positions older than min_age_minutes
        now = now_sast()
        eligible = []
        for pos in open_positions:
            create_ts = pos.get("cTime") or pos.get("createTime")
            if create_ts:
                try:
                    from datetime import datetime, timezone, timedelta
                    created = datetime.fromtimestamp(int(create_ts) / 1000, tz=timezone(timedelta(hours=2)))
                    age_min = (now - created).total_seconds() / 60
                    if age_min < min_age_minutes:
                        continue
                except Exception:
                    pass
            eligible.append(pos)

        if not eligible:
            return {"skipped": False, "positions_reviewed": 0, "reason": "No positions old enough to review"}

        # Load market analyst
        agent_result = await db.execute(
            select(Agent).where(Agent.is_active == True, Agent.role == "market_analyst")
        )
        analyst_agents = agent_result.scalars().all()
        if not analyst_agents:
            return {"skipped": True, "reason": "No active market_analyst agent"}

        analyst = agent_from_db(analyst_agents[0])
        await connector.get_max_leverage("BTCUSDT")  # Warm precision cache

        # Fetch current TPSL orders once — indexed by (symbol, holdSide)
        try:
            all_tpsl = await connector.get_pending_tpsl_orders()
        except Exception:
            all_tpsl = []
        tpsl_map: Dict[tuple, Dict[str, Any]] = {}
        for order in (all_tpsl or []):
            sym = (order.get("symbol") or "").upper()
            hs = (order.get("posSide") or order.get("holdSide") or "").lower()
            pt = (order.get("planType") or "").lower()
            trigger = float(order.get("triggerPrice") or 0)
            key = (sym, hs)
            if key not in tpsl_map:
                tpsl_map[key] = {}
            if pt in ("loss_plan", "pos_loss") and trigger > 0:
                tpsl_map[key]["sl"] = trigger
            elif pt in ("profit_plan", "pos_profit") and trigger > 0:
                tpsl_map[key]["tp"] = trigger

        reviews: List[Dict[str, Any]] = []
        adjustments: List[Dict[str, Any]] = []

        for pos in eligible:
            bitget_sym = (pos.get("symbol") or "").upper()
            display_sym = bitget_sym.replace("USDT", "/USDT")
            hold_side = (pos.get("holdSide") or "").lower()
            entry_price = float(pos.get("openPriceAvg") or 0)
            size = float(pos.get("total") or pos.get("available") or 0)

            if not bitget_sym or entry_price <= 0 or size <= 0:
                continue

            current_sl = tpsl_map.get((bitget_sym, hold_side), {}).get("sl")
            current_tp = tpsl_map.get((bitget_sym, hold_side), {}).get("tp")

            # Also check Trade DB for SL/TP if not found on exchange
            if not current_sl or not current_tp:
                try:
                    side_val = "buy" if hold_side == "long" else "sell"
                    tr = await db.execute(
                        select(Trade).where(
                            Trade.exchange == "bitget",
                            Trade.status == "open",
                            Trade.trade_side == "open",
                            Trade.symbol == display_sym,
                            Trade.side == side_val,
                        ).order_by(Trade.created_at.desc())
                    )
                    trade_rec = tr.scalars().first()
                    if trade_rec:
                        if not current_sl and trade_rec.stop_loss:
                            current_sl = trade_rec.stop_loss
                        if not current_tp and trade_rec.take_profit:
                            current_tp = trade_rec.take_profit
                except Exception:
                    pass

            # Gather market context
            try:
                context = await AgentOrchestrator._gather_context(display_sym, "1h")
            except Exception as e:
                logger.warning(f"[Orchestrator:{session_id}] Context failed for {display_sym}: {e}")
                continue

            current_price = context.get("current_price", 0)
            if current_price <= 0:
                continue

            # Multi-timeframe context
            multi_tf: Dict[str, Any] = {}
            for tf in ("5m", "15m"):
                try:
                    tf_ohlcv = await connector.get_ohlcv(symbol=display_sym, timeframe=tf, limit=100)
                    if tf_ohlcv and len(tf_ohlcv) >= 20:
                        tf_ta = technical_analyze(tf_ohlcv, tf)
                        multi_tf[tf] = {
                            "score": tf_ta.get("score"),
                            "action": tf_ta.get("action"),
                            "indicators": {
                                k: tf_ta.get("indicators", {}).get(k)
                                for k in ("rsi", "macd_histogram", "bb_lower", "bb_upper", "bb_middle",
                                           "ema50", "ema200", "adx", "stoch_rsi", "volume_ratio",
                                           "ma5", "ma10", "price")
                            },
                        }
                except Exception:
                    pass

            # Calculate what SmartStopLoss would suggest NOW
            smart_sl: Optional[float] = None
            smart_tp: Optional[float] = None
            try:
                ohlcv = await connector.get_ohlcv(symbol=display_sym, timeframe="1h", limit=200)
                sl_data = SmartStopLoss.calculate(ohlcv, hold_side, entry_price)
                smart_sl = sl_data.get("stop_loss")
                smart_tp = sl_data.get("take_profit")
            except Exception:
                pass

            # Calculate distance between current and suggested SL/TP
            sl_distance_pct = None
            tp_distance_pct = None
            if current_sl and smart_sl and current_sl > 0:
                sl_distance_pct = round(((smart_sl - current_sl) / current_sl) * 100, 3)
            if current_tp and smart_tp and current_tp > 0:
                tp_distance_pct = round(((smart_tp - current_tp) / current_tp) * 100, 3)

            position_context = {
                **context,
                "multi_timeframe": multi_tf,
                "open_position": {
                    "symbol": display_sym,
                    "side": hold_side,
                    "entry_price": entry_price,
                    "current_price": current_price,
                    "size": size,
                    "unrealized_pnl": float(pos.get("unrealizedPL") or 0),
                    "leverage": pos.get("leverage"),
                    "current_sl": current_sl,
                    "current_tp": current_tp,
                    "suggested_sl": smart_sl,
                    "suggested_tp": smart_tp,
                    "sl_distance_pct": sl_distance_pct,
                    "tp_distance_pct": tp_distance_pct,
                    "pnl_pct": round(
                        ((current_price - entry_price) / entry_price) * 100
                        if hold_side == "long" else
                        ((entry_price - current_price) / entry_price) * 100,
                        3,
                    ) if entry_price > 0 else 0,
                },
                "task": (
                    "Analyze this OPEN POSITION and decide if the SL/TP levels should be adjusted "
                    "based on current market conditions. Compare current SL/TP vs the suggested smart levels. "
                    "Consider: support/resistance shifts, Bollinger Bands, momentum changes, ATR-based levels, "
                    "whether the trade is in profit/loss, and multi-timeframe alignment. "
                    "Respond with JSON: {\"action\": \"keep|adjust\", \"confidence\": 0.0-1.0, "
                    "\"new_sl\": <float or null>, \"new_tp\": <float or null>, \"reasoning\": \"...\"}"
                ),
            }

            try:
                decision = await AgentOrchestrator._run_agent_with_memory(
                    db, analyst, position_context, display_sym,
                )
                db.add(AgentDecision(
                    agent_id=analyst.agent_id,
                    agent_name=analyst.name,
                    agent_role=analyst.role,
                    symbol=display_sym,
                    action=decision.get("action", "keep"),
                    confidence=decision.get("confidence", 0),
                    reasoning=reasoning_text(decision.get("reasoning")),
                    market_data=json.dumps({
                        "position_sltp_review": True,
                        "hold_side": hold_side,
                        **decision,
                    }, default=str),
                    session_id=session_id,
                    ai_called=decision.get("ai_called", True),
                    memory_context_used=decision.get("memory_context_used", 0),
                ))
            except Exception as e:
                logger.warning(
                    f"[Orchestrator:{session_id}] AI position analysis failed for "
                    f"{display_sym} {hold_side}: {e}"
                )
                continue

            action = decision.get("action", "keep")
            new_sl_val = decision.get("new_sl")
            new_tp_val = decision.get("new_tp")
            confidence = decision.get("confidence", 0)

            review_entry: Dict[str, Any] = {
                "symbol": display_sym,
                "side": hold_side,
                "entry_price": entry_price,
                "current_price": current_price,
                "current_sl": current_sl,
                "current_tp": current_tp,
                "suggested_sl": smart_sl,
                "suggested_tp": smart_tp,
                "action": action,
                "ai_new_sl": new_sl_val,
                "ai_new_tp": new_tp_val,
                "confidence": confidence,
                "reasoning": decision.get("reasoning", ""),
            }
            reviews.append(review_entry)

            if action == "adjust" and confidence >= 0.5:
                # Use AI-provided values, fall back to smart calculation
                final_sl = float(new_sl_val) if new_sl_val else smart_sl
                final_tp = float(new_tp_val) if new_tp_val else smart_tp

                # Sanity check: SL must be on the correct side of entry
                if final_sl:
                    if hold_side == "long" and final_sl >= entry_price:
                        final_sl = smart_sl   # Fallback
                    elif hold_side == "short" and final_sl <= entry_price:
                        final_sl = smart_sl

                if not final_sl and not final_tp:
                    logger.info(
                        f"[Orchestrator:{session_id}] No valid new SL/TP for "
                        f"{display_sym} {hold_side}, skipping"
                    )
                    continue

                # Check if there's actually a meaningful change
                sl_changed = final_sl and (not current_sl or abs(final_sl - current_sl) / max(current_sl, 1) > 0.002)
                tp_changed = final_tp and (not current_tp or abs(final_tp - current_tp) / max(current_tp, 1) > 0.002)

                if not sl_changed and not tp_changed:
                    logger.info(
                        f"[Orchestrator:{session_id}] SL/TP change for {display_sym} "
                        f"{hold_side} too small (<0.2%), skipping"
                    )
                    continue

                try:
                    result = await connector.replace_tpsl_orders(
                        symbol=bitget_sym,
                        hold_side=hold_side,
                        new_sl=final_sl if sl_changed else None,
                        new_tp=final_tp if tp_changed else None,
                        margin_coin="USDT",
                    )

                    # Update Trade DB records
                    try:
                        side_val = "buy" if hold_side == "long" else "sell"
                        trade_result = await db.execute(
                            select(Trade).where(
                                Trade.exchange == "bitget",
                                Trade.status == "open",
                                Trade.trade_side == "open",
                                Trade.symbol == display_sym,
                                Trade.side == side_val,
                            )
                        )
                        for t in trade_result.scalars().all():
                            if sl_changed and final_sl:
                                t.stop_loss = final_sl
                            if tp_changed and final_tp:
                                t.take_profit = final_tp
                    except Exception as db_err:
                        logger.error(
                            f"[Orchestrator:{session_id}] Failed to update Trade DB "
                            f"for {display_sym} {hold_side}: {db_err}"
                        )

                    adj_entry: Dict[str, Any] = {
                        "symbol": display_sym,
                        "side": hold_side,
                        "result": result,
                    }
                    if sl_changed:
                        adj_entry["old_sl"] = current_sl
                        adj_entry["new_sl"] = final_sl
                    if tp_changed:
                        adj_entry["old_tp"] = current_tp
                        adj_entry["new_tp"] = final_tp
                    adjustments.append(adj_entry)

                    logger.info(
                        f"[Orchestrator:{session_id}] Position SL/TP adjusted: "
                        f"{display_sym} {hold_side} "
                        f"SL={current_sl}→{final_sl if sl_changed else '(same)'} "
                        f"TP={current_tp}→{final_tp if tp_changed else '(same)'} "
                        f"(conf={confidence:.2f})"
                    )
                except Exception as e:
                    logger.error(
                        f"[Orchestrator:{session_id}] Failed to replace TPSL for "
                        f"{display_sym} {hold_side}: {e}"
                    )
                    review_entry["error"] = str(e)

        await db.commit()

        logger.info(
            f"[Orchestrator:{session_id}] Position SL/TP optimization: "
            f"{len(reviews)} reviewed, {len(adjustments)} adjusted"
        )

        return {
            "skipped": False,
            "session_id": session_id,
            "positions_reviewed": len(reviews),
            "positions_adjusted": len(adjustments),
            "reviews": reviews,
            "adjustments": adjustments,
        }
