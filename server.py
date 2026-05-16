"""
Polymarket Paper Trader — FastAPI Backend
WebSocket for real-time updates + REST for control.
"""

import asyncio
import json
import logging
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

import engine

STATE_FILE = Path(__file__).parent / "trades.json"

# ── Globals ───────────────────────────────────────────────────────

state = {}
config = dict(engine.DEFAULT_CONFIG)
bot_running = False
bot_paused = False
bot_task = None
skip_markets = set()
current_opportunities = []
bot_log = []          # recent log entries [{ts, type, msg, data}]
MAX_LOG = 200

# WebSocket clients
ws_clients: set[WebSocket] = set()


# ── Pydantic Response Models ──────────────────────────────────────

class BrierBin(BaseModel):
    """Calibration reliability diagram bin."""
    avg_forecast: float = Field(..., description="Mean predicted probability in this bin")
    avg_outcome: float = Field(..., description="Mean actual outcome (0 or 1) in this bin")
    count: int = Field(..., description="Number of predictions in this bin")


class BrierScore(BaseModel):
    """Brier score decomposition into reliability, resolution, and uncertainty."""
    n: int = Field(..., description="Number of resolved predictions used")
    brier: float = Field(..., description="Overall Brier score (lower is better; perfect = 0)")
    log_loss: float = Field(..., description="Log loss (cross-entropy)")
    reliability: float = Field(..., description="Calibration error — lower is better")
    resolution: float = Field(..., description="Sharpness of predictions — higher is better")
    uncertainty: float = Field(..., description="Base rate entropy of outcomes")
    market_brier: float = Field(..., description="Brier score of the market's own implied probabilities")
    skill_score: float = Field(..., description="Brier skill score vs. market (positive = beating market)")
    bins: dict[str, BrierBin] = Field(..., description="10 reliability diagram bins (keys '0'–'9')")


class StatsResponse(BaseModel):
    """Aggregate portfolio statistics."""
    balance: float = Field(..., description="Current cash balance in USD")
    open_count: int = Field(..., description="Number of open positions")
    open_cost: float = Field(..., description="Total USD deployed in open positions")
    resolved_pnl: float = Field(..., description="Realized P&L from closed trades")
    total_pnl: float = Field(..., description="Total P&L including unrealized")
    total_pnl_pct: float = Field(..., description="Total P&L as % of starting balance")
    deployed_pct: float = Field(..., description="Fraction of portfolio currently deployed")
    wins: int = Field(..., description="Number of winning resolved trades")
    losses: int = Field(..., description="Number of losing resolved trades")
    win_rate: float = Field(..., description="Win rate (0–1) across resolved trades")
    total_trades: int = Field(..., description="Total resolved trades")
    calibration: Optional[BrierScore] = Field(None, description="Brier score summary if ≥30 resolved trades")
    model_config = ConfigDict(json_schema_extra={"example": {
        "balance": 1123.45, "open_count": 3, "open_cost": 87.60,
        "resolved_pnl": 43.20, "total_pnl": 166.05, "total_pnl_pct": 16.6,
        "deployed_pct": 8.76, "wins": 7, "losses": 2, "win_rate": 0.78,
        "total_trades": 9, "calibration": None
    }})


class PortfolioPosition(BaseModel):
    """An open position with live market price and unrealized P&L."""
    market_id: str = Field(..., description="Polymarket market ID (hex string)")
    question: str = Field(..., description="Market question text")
    side: Literal["YES", "NO"] = Field(..., description="Side held")
    entry_price: float = Field(..., description="Price paid per share at entry (0–1)")
    our_estimate: float = Field(..., description="LLM ensemble probability estimate (0–1)")
    confidence: float = Field(..., description="Model confidence in the estimate (0–1)")
    shares: float = Field(..., description="Number of shares held")
    cost: float = Field(..., description="Total USD cost of the position")
    timestamp: str = Field(..., description="ISO-8601 entry timestamp (UTC)")
    status: str = Field(..., description="'open'")
    end_date: str = Field(..., description="ISO-8601 market resolution date")
    score: float = Field(..., description="Composite opportunity score used for ranking")
    edge: float = Field(..., description="Estimated edge: our_estimate − entry_price")
    category: str = Field(..., description="Market category (e.g. 'Politics', 'Sports')")
    current_price: float = Field(..., description="Live market price for the held side (0–1)")
    unrealized: float = Field(..., description="Unrealized P&L in USD at current price")
    direction: Literal["up", "down", "flat"] = Field(..., description="Price movement direction since entry")
    max_payout: float = Field(..., description="Maximum payout if position resolves in our favour (USD)")
    expected_profit: float = Field(..., description="Expected profit: edge × shares")
    roi_pct: float = Field(..., description="Unrealized ROI as a percentage of cost")
    model_config = ConfigDict(json_schema_extra={"example": {
        "market_id": "0xabc123", "question": "Will the Fed cut rates in June?",
        "side": "YES", "entry_price": 0.42, "our_estimate": 0.58, "confidence": 0.71,
        "shares": 47.6, "cost": 20.0, "timestamp": "2026-04-10T14:00:00Z",
        "status": "open", "end_date": "2026-06-15T00:00:00Z",
        "score": 0.83, "edge": 0.16, "category": "Economics",
        "current_price": 0.49, "unrealized": 3.33, "direction": "up",
        "max_payout": 47.6, "expected_profit": 7.62, "roi_pct": 16.65
    }})


class TradeRecord(BaseModel):
    """A resolved (closed) trade record."""
    market_id: str = Field(..., description="Polymarket market ID")
    question: str = Field(..., description="Market question text")
    side: Literal["YES", "NO"] = Field(..., description="Side held")
    entry_price: float = Field(..., description="Price paid per share at entry (0–1)")
    our_estimate: float = Field(..., description="LLM ensemble probability estimate at entry")
    confidence: float = Field(..., description="Model confidence at entry (0–1)")
    shares: float = Field(..., description="Number of shares held")
    cost: float = Field(..., description="Total USD cost")
    timestamp: str = Field(..., description="ISO-8601 entry timestamp")
    status: Literal["open", "resolved"] = Field(..., description="Trade status")
    end_date: str = Field(..., description="ISO-8601 resolution date")
    score: float = Field(..., description="Opportunity score at time of trade")
    edge: float = Field(..., description="Estimated edge at entry")
    category: str = Field(..., description="Market category")
    won: Optional[bool] = Field(None, description="True if trade won (only on resolved trades)")
    profit: Optional[float] = Field(None, description="Net profit/loss in USD (only on resolved trades)")
    model_config = ConfigDict(json_schema_extra={"example": {
        "market_id": "0xdef456", "question": "Will Bitcoin reach $100k by end of 2025?",
        "side": "NO", "entry_price": 0.63, "our_estimate": 0.31, "confidence": 0.68,
        "shares": 15.9, "cost": 10.0, "timestamp": "2026-01-05T09:00:00Z",
        "status": "resolved", "end_date": "2025-12-31T00:00:00Z",
        "score": 0.74, "edge": -0.32, "category": "Crypto",
        "won": True, "profit": 5.9
    }})


class Opportunity(BaseModel):
    """A market opportunity identified by the scanning engine."""
    market_id: str = Field(..., description="Polymarket market ID")
    question: str = Field(..., description="Market question text")
    side: Literal["YES", "NO"] = Field(..., description="Recommended side to buy")
    entry_price: float = Field(..., description="Current market price for the recommended side")
    our_estimate: float = Field(..., description="LLM ensemble probability estimate")
    confidence: float = Field(..., description="Model confidence (0–1)")
    edge: float = Field(..., description="Estimated edge (our_estimate − entry_price)")
    score: float = Field(..., description="Composite score used for ranking (higher = better)")
    category: str = Field(..., description="Market category")
    market_price_yes: float = Field(..., description="Current YES price on the market (0–1)")
    momentum: Optional[Any] = Field(None, description="Price momentum by timeframe, e.g. {'1h': -0.05, '1w': 0.03}")
    volume_24h: Optional[float] = Field(None, description="24-hour trading volume in USD")
    end_date: Optional[str] = Field(None, description="ISO-8601 market resolution date")
    model_config = ConfigDict(json_schema_extra={"example": {
        "market_id": "0xghi789", "question": "Will the S&P 500 close above 5500 in April 2026?",
        "side": "YES", "entry_price": 0.38, "our_estimate": 0.55, "confidence": 0.73,
        "edge": 0.17, "score": 0.91, "category": "Finance",
        "market_price_yes": 0.38, "momentum": 0.03, "volume_24h": 45000,
        "end_date": "2026-04-30T00:00:00Z"
    }})


class BotStatusResponse(BaseModel):
    """Current bot running / paused state."""
    running: bool = Field(..., description="True if the autopilot loop is active")
    paused: bool = Field(..., description="True if the loop is temporarily paused")
    model_config = ConfigDict(json_schema_extra={"example": {"running": True, "paused": False}})


class BotActionResponse(BaseModel):
    """Result of a bot start / pause / stop command."""
    status: Literal["started", "resumed", "already_running", "paused", "not_running", "stopped", "error"] = Field(
        ..., description="Outcome of the command"
    )
    detail: Optional[str] = Field(None, description="Error detail (only present when status='error')")
    model_config = ConfigDict(json_schema_extra={"example": {"status": "started"}})


class SkipResponse(BaseModel):
    """Confirmation that a market has been added to the skip list."""
    status: Literal["skipped"] = Field(..., description="Always 'skipped'")
    market_id: str = Field(..., description="The market ID that was skipped")
    model_config = ConfigDict(json_schema_extra={"example": {"status": "skipped", "market_id": "0xabc123"}})


class ResetResponse(BaseModel):
    """Confirmation that the paper account has been reset."""
    status: Literal["reset"] = Field(..., description="Always 'reset'")
    model_config = ConfigDict(json_schema_extra={"example": {"status": "reset"}})


class LogEvent(BaseModel):
    """A single bot event log entry."""
    ts: str = Field(..., description="ISO-8601 timestamp (UTC)")
    type: str = Field(..., description="Event type (e.g. 'trade_placed', 'resolved', 'cycle_start')")
    msg: str = Field(..., description="Human-readable event message")
    data: Optional[dict[str, Any]] = Field(None, description="Optional structured payload (trade, stats, etc.)")
    model_config = ConfigDict(json_schema_extra={"example": {
        "ts": "2026-04-12T10:30:00Z", "type": "trade_placed",
        "msg": "Bought 47.6 sh YES @ $0.42",
        "data": {"market_id": "0xabc123", "cost": 20.0}
    }})


class NegRiskMarket(BaseModel):
    """Individual market within a NegRisk group."""
    id: str = Field(..., description="Polymarket market ID")
    question: str = Field(..., description="Market question text")
    price_yes: float = Field(..., description="Current YES price (0–1)")


class NegRiskOpportunity(BaseModel):
    """A NegRisk arbitrage opportunity across mutually exclusive outcomes."""
    slug: str = Field(..., description="Polymarket event slug grouping the markets")
    num_markets: int = Field(..., description="Number of mutually exclusive outcome markets")
    total_yes_price: float = Field(..., description="Sum of all YES prices (should equal 1.0 in a fair market)")
    deviation: float = Field(..., description="Deviation from 1.0 (positive = overpriced, negative = underpriced)")
    direction: Literal["buy_all_YES", "buy_all_NO"] = Field(..., description="Recommended arb direction")
    arb_profit_pct: float = Field(..., description="Estimated arbitrage profit as a percentage")
    markets: list[NegRiskMarket] = Field(..., description="Individual markets in this NegRisk group")
    model_config = ConfigDict(json_schema_extra={"example": {
        "slug": "us-election-2026-senate", "num_markets": 3,
        "total_yes_price": 1.08, "deviation": 0.08,
        "direction": "buy_all_NO", "arb_profit_pct": 7.41,
        "markets": [
            {"id": "0xaaa", "question": "Party A wins Senate?", "price_yes": 0.40},
            {"id": "0xbbb", "question": "Party B wins Senate?", "price_yes": 0.38},
            {"id": "0xccc", "question": "Party C wins Senate?", "price_yes": 0.30},
        ]
    }})


class CorrelationWarning(BaseModel):
    """A pair of open opportunities whose probability estimates are logically inconsistent."""
    markets: list[str] = Field(..., description="Two market IDs that are correlated")
    questions: list[str] = Field(..., description="Question text for each market")
    estimates: list[float] = Field(..., description="Our probability estimates for each market")
    warning: str = Field(..., description="Human-readable description of the inconsistency")
    model_config = ConfigDict(json_schema_extra={"example": {
        "markets": ["0xaaa", "0xbbb"],
        "questions": ["Will X happen?", "Will X NOT happen?"],
        "estimates": [0.70, 0.65],
        "warning": "Estimates sum to 1.35 — implies negative probability for alternative outcomes"
    }})


class PnLPoint(BaseModel):
    """A single point in the cumulative P&L time series."""
    ts: str = Field(..., description="ISO-8601 timestamp of the trade resolution")
    pnl: float = Field(..., description="P&L for this single trade in USD")
    cumulative: float = Field(..., description="Running total P&L in USD up to this point")
    question: str = Field(..., description="Market question (truncated to 40 chars)")


class TradeBar(BaseModel):
    """P&L bar for a single resolved trade, used in bar charts."""
    question: str = Field(..., description="Market question (truncated)")
    profit: float = Field(..., description="Profit or loss in USD")
    won: bool = Field(..., description="True if this trade was a win")
    side: str = Field(..., description="Side held (YES or NO)")


class AllocationSlice(BaseModel):
    """A slice of the current portfolio allocation pie chart."""
    question: str = Field(..., description="Market question (truncated)")
    cost: float = Field(..., description="USD deployed in this position")
    side: str = Field(..., description="Side held")
    color: str = Field(..., description="Hex color for the chart slice")


class ChartSummary(BaseModel):
    """Aggregate summary for the charts dashboard."""
    total_deployed: float = Field(..., description="Total USD in open positions")
    cash: float = Field(..., description="Current cash balance")
    total_pnl: float = Field(..., description="Total realized + unrealized P&L")
    wins: int = Field(..., description="Number of winning resolved trades")
    losses: int = Field(..., description="Number of losing resolved trades")


class ChartsResponse(BaseModel):
    """Pre-computed chart data for the dashboard."""
    pnl_series: list[PnLPoint] = Field(..., description="Cumulative P&L time series (chronological)")
    trade_bars: list[TradeBar] = Field(..., description="Per-trade P&L bars (most recent first)")
    allocation: list[AllocationSlice] = Field(..., description="Open position allocation slices")
    summary: ChartSummary = Field(..., description="Aggregate portfolio summary")


class CalibrationReport(BaseModel):
    """Full forecast calibration report with Brier decomposition and Platt scaling parameters."""
    status: Literal["ok", "insufficient_data"] = Field(
        ..., description="'ok' if ≥30 resolved predictions available, else 'insufficient_data'"
    )
    n: int = Field(..., description="Number of resolved predictions analysed")
    brier: float = Field(..., description="Brier score (0 = perfect, 0.25 = random)")
    platt_a: float = Field(..., description="Platt scaling slope parameter (a in sigmoid)")
    platt_b: float = Field(..., description="Platt scaling intercept parameter (b in sigmoid)")
    total_predictions: int = Field(..., description="Total predictions stored in calibration.json")
    beating_market: bool = Field(..., description="True if our Brier score beats the market's implied probabilities")
    edge_over_market: float = Field(..., description="Our skill score relative to market (positive = better)")
    diagnosis: str = Field(..., description="Plain-English calibration diagnosis")
    bins: dict = Field(..., description="Reliability diagram bins (see BrierBin schema)")
    reliability: float = Field(..., description="Reliability component of Brier decomposition")
    resolution: float = Field(..., description="Resolution component of Brier decomposition")
    uncertainty: float = Field(..., description="Uncertainty (base rate entropy)")
    market_brier: float = Field(..., description="Market's Brier score")
    skill_score: float = Field(..., description="Skill score vs. market")
    log_loss: float = Field(..., description="Log loss of our predictions")
    model_config = ConfigDict(json_schema_extra={"example": {
        "status": "ok", "n": 45, "brier": 0.18, "platt_a": 1.12, "platt_b": -0.05,
        "total_predictions": 45, "beating_market": True, "edge_over_market": 0.04,
        "diagnosis": "Well-calibrated. Slightly overconfident in high-probability events.",
        "bins": {}, "reliability": 0.02, "resolution": 0.09, "uncertainty": 0.25,
        "market_brier": 0.22, "skill_score": 0.04, "log_loss": 0.51
    }})


# ── Request Models ────────────────────────────────────────────────

class ConfigUpdate(BaseModel):
    """
    Partial update to the runtime trading configuration.
    Only include fields you want to change; omitted fields are left unchanged.
    """
    min_edge: Optional[float] = Field(
        None, ge=0, le=1,
        description="Minimum edge required to place a trade (our_estimate − market_price). E.g. 0.05 = 5%.",
        examples=[0.05]
    )
    min_confidence: Optional[float] = Field(
        None, ge=0, le=1,
        description="Minimum LLM ensemble confidence to consider a trade. E.g. 0.55.",
        examples=[0.55]
    )
    max_bet_fraction: Optional[float] = Field(
        None, ge=0, le=1,
        description="Maximum fraction of portfolio balance per single trade. E.g. 0.10 = 10%.",
        examples=[0.10]
    )
    max_deployed_pct: Optional[float] = Field(
        None, ge=0, le=1,
        description="Maximum fraction of balance that can be deployed in open positions. E.g. 0.60 = 60%.",
        examples=[0.60]
    )
    scan_interval_min: Optional[int] = Field(
        None, ge=1,
        description="Minutes between full market scan cycles. E.g. 120 = scan every 2 hours.",
        examples=[120]
    )
    max_trades_per_cycle: Optional[int] = Field(
        None, ge=0,
        description="Maximum new trades to place per scan cycle.",
        examples=[3]
    )
    markets_to_scan: Optional[int] = Field(
        None, ge=1,
        description="Number of top markets to fetch and analyse each cycle.",
        examples=[40]
    )
    kelly_fraction: Optional[float] = Field(
        None, ge=0, le=1,
        description="Kelly criterion multiplier (0.25 = quarter-Kelly). Lower = more conservative sizing.",
        examples=[0.25]
    )
    extremize_gamma: Optional[float] = Field(
        None, ge=0.5, le=3.0,
        description="Exponent for extremizing LLM probability estimates away from 50%. >1 = sharpen, 1 = no change.",
        examples=[1.3]
    )
    no_side_edge_bonus: Optional[float] = Field(
        None, ge=0, le=0.1,
        description="Additional edge bonus applied when betting the NO side (accounts for liquidity asymmetry).",
        examples=[0.015]
    )
    ensemble_passes: Optional[int] = Field(
        None, ge=1, le=20,
        description="Number of parallel LLM calls per market analysis. More passes = more accurate but slower.",
        examples=[5]
    )
    use_calibration: Optional[bool] = Field(
        None,
        description="Whether to apply Platt scaling to LLM estimates using historical calibration data.",
        examples=[True]
    )


class ManualTrade(BaseModel):
    """Request body for placing a manual trade on an opportunity from the current scan."""
    market_id: str = Field(
        ...,
        description="Polymarket market ID. Must be in the current opportunities list (run a scan first).",
        examples=["0xabc123def456"]
    )
    side: Literal["YES", "NO"] = Field(
        ...,
        description="Which side to buy. 'YES' buys the YES outcome, 'NO' buys the NO outcome.",
        examples=["YES"]
    )
    amount: float = Field(
        ..., gt=0,
        description="USD amount to stake on this trade.",
        examples=[25.0]
    )


# ── Tag definitions ───────────────────────────────────────────────

TAGS_METADATA = [
    {
        "name": "Portfolio",
        "description": (
            "Read portfolio state, live open positions, and resolved trade history. "
            "**Called by:** Dashboard `StatsPanel` (state), `PortfolioTab` (portfolio), "
            "`HistoryTab` (history), `OpportunitiesTab` (opportunities)."
        ),
    },
    {
        "name": "Bot Control",
        "description": (
            "Start, pause, stop, and query the autopilot market-scanning loop. "
            "The loop fetches markets, runs multi-LLM ensemble analysis, and places trades automatically. "
            "**Called by:** Dashboard `ControlBar`."
        ),
    },
    {
        "name": "Configuration",
        "description": (
            "Read or partially update runtime trading parameters (edge thresholds, Kelly fraction, "
            "scan interval, etc.). Changes take effect on the next scan cycle. "
            "**Called by:** Dashboard `SettingsPanel`."
        ),
    },
    {
        "name": "Trading",
        "description": (
            "Manually place trades on identified opportunities or mark a market to be skipped. "
            "Manual trades must reference a market currently in the opportunities list. "
            "**Called by:** Dashboard `OpportunitiesTab`."
        ),
    },
    {
        "name": "Analytics",
        "description": (
            "Advanced analytics: Brier score calibration report, NegRisk arbitrage scanner, "
            "correlation consistency checker, and pre-computed chart data. "
            "**Called by:** Dashboard `AnalyticsTab`."
        ),
    },
    {
        "name": "System",
        "description": (
            "System-level operations: event log retrieval, reload state from disk, "
            "and full paper-account reset. "
            "**Called by:** Dashboard `DevTools` panel."
        ),
    },
]

# ── Helpers ───────────────────────────────────────────────────────

def log_event(event_type, msg="", data=None):
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "type": event_type,
        "msg": msg,
        "data": data,
    }
    bot_log.append(entry)
    if len(bot_log) > MAX_LOG:
        bot_log.pop(0)
    return entry


async def broadcast(event):
    """Send event to all connected WebSocket clients."""
    if not ws_clients:
        return
    dead = []
    msg = json.dumps(event, default=str)
    for ws in list(ws_clients):
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        ws_clients.discard(ws)


async def broadcast_state():
    stats = engine.get_stats(state, config)
    await broadcast({"type": "state_update", "data": stats})


def save():
    engine.save_state(state, STATE_FILE)


# ── Bot autopilot loop ────────────────────────────────────────────

async def bot_loop():
    global bot_running, bot_paused

    while bot_running:
        if bot_paused:
            await asyncio.sleep(2)
            continue

        try:
            await _bot_cycle()
        except asyncio.CancelledError:
            raise  # propagate real cancellation (server shutdown)
        except Exception as exc:
            log.error(f"bot_loop cycle error: {exc}", exc_info=True)
            await broadcast(log_event("info", f"Cycle error ({type(exc).__name__}), retrying in 10s"))
            await asyncio.sleep(10)
            continue

        # Sleep between cycles (check every 2s for pause/stop)
        wait_sec = config["scan_interval_min"] * 60
        elapsed = 0
        while elapsed < wait_sec and bot_running and not bot_paused:
            await asyncio.sleep(2)
            elapsed += 2

    bot_running = False


async def _bot_cycle():
    global current_opportunities

    await broadcast(log_event("cycle_start", "Starting scan cycle"))

    # Step 1a: Check resolved
    resolved = await asyncio.to_thread(engine.check_resolved, state)
    if resolved:
        save()
        for r in resolved:
            evt = log_event("resolved", f"{'WIN' if r['won'] else 'LOSS'}: {r['trade']['question'][:50]}", r)
            await broadcast(evt)
        await broadcast_state()

    # Step 1b: Check exits (stop-loss / edge erosion)
    exits = await asyncio.to_thread(engine.check_exits, state, config)
    if exits:
        save()
        for ex in exits:
            evt = log_event("exit", f"EXIT: {ex['trade']['question'][:50]} — {ex['reason']}", ex)
            await broadcast(evt)
        await broadcast_state()

    # Step 2: Check capacity
    dep = engine.deployed_amount(state)
    max_dep = config["starting_balance"] * config["max_deployed_pct"]

    if state["balance"] < 5 or (max_dep - dep) < 5:
        await broadcast(log_event("info", f"Fully deployed ({dep/config['starting_balance']:.0%}). Waiting."))
        return

    # Step 3: Fetch markets
    await broadcast(log_event("scan_start", "Fetching markets..."))
    markets = await asyncio.to_thread(engine.fetch_markets, config["markets_to_scan"])

    open_ids = {t["market_id"] for t in state["trades"] if t["status"] == "open"}
    markets = [m for m in markets if m.get("id") not in open_ids and m.get("id") not in skip_markets]

    await broadcast(log_event("info", f"Analyzing {len(markets)} markets"))

    opps = []
    for idx, m in enumerate(markets):
        if not bot_running or bot_paused:
            break

        q = m.get("question", "")[:50]
        await broadcast(log_event("analyzing", f"[{idx+1}/{len(markets)}] {q}",
                                  {"index": idx + 1, "total": len(markets), "question": q}))

        try:
            opp = await asyncio.wait_for(
                asyncio.to_thread(engine.analyze_market, m, config),
                timeout=90
            )
        except asyncio.TimeoutError:
            await broadcast(log_event("info", f"Timed out (90s), skipping: {q}"))
            continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await broadcast(log_event("info", f"Analysis error ({type(exc).__name__}): {q}"))
            continue

        if opp:
            opps.append(opp)
            await broadcast(log_event("opportunity", f"Edge {opp['edge']:+.0%}: {q}", opp))
        else:
            await broadcast(log_event("no_edge", f"No edge: {q}"))

    current_opportunities = sorted(opps, key=lambda x: x.get("score", 0), reverse=True)

    # Step 4: Auto-trade top picks
    trades_placed = 0
    max_dep = config["starting_balance"] * config["max_deployed_pct"]
    for opp in current_opportunities[:config["max_trades_per_cycle"]]:
        if not bot_running or bot_paused:
            break
        avail = min(state["balance"], max_dep - engine.deployed_amount(state))
        if avail < 1:
            break
        size = engine.kelly_size(
            opp["edge"], opp["entry_price"],
            state["balance"], opp.get("confidence", 0.5),
            config["max_bet_fraction"], config
        )
        size = min(size, avail)
        if size >= 1:
            trade = engine.execute_trade(state, opp, size, config)
            if trade is None:
                await broadcast(log_event("info", f"Skipped (same-day cap): {opp['question'][:50]}"))
                continue
            save()
            trades_placed += 1
            evt = log_event("trade_placed",
                            f"Bought {trade['shares']:.1f} sh {trade['side']} @ ${trade['entry_price']:.2f}",
                            trade)
            await broadcast(evt)
            await broadcast_state()

    await broadcast(log_event("cycle_complete",
                              f"Cycle done. {trades_placed} trades placed.",
                              {"trades_placed": trades_placed,
                               "next_in_sec": config["scan_interval_min"] * 60}))


# ── FastAPI app ───────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app):
    global state, bot_running, bot_task
    state = engine.load_state(STATE_FILE)
    try:
        resolved = await asyncio.to_thread(engine.check_resolved, state)
        if resolved:
            save()
            log.info(f"Auto-resolved {len(resolved)} trades on startup")
    except Exception as e:
        log.warning(f"Startup check_resolved failed (non-fatal): {e}")

    # Auto-start the bot
    bot_running = True
    bot_task = asyncio.create_task(bot_loop())
    log_event("bot", "Bot auto-started on server launch")
    log.info("Bot auto-started")

    yield

    bot_running = False
    save()


app = FastAPI(
    title="Polymarket Paper Trader",
    version="1.0.0",
    description="""
## Polymarket Paper Trading Bot API

Algorithmic paper-trading system for [Polymarket](https://polymarket.com) prediction markets.
Runs entirely in paper-trading mode — no real money is ever deployed.

---

### How it works

1. **Scan** — The autopilot loop fetches the top *N* active Polymarket markets on a configurable interval.
2. **Analyse** — Each market is scored by a multi-pass LLM ensemble (default 5 × Claude calls),
   whose probability estimates are extremized and optionally Platt-calibrated.
3. **Size** — Positions are sized using quarter-Kelly criterion with hard deployment caps.
4. **Trade** — Top-ranked opportunities are auto-traded, or you can place manual trades via the API.
5. **Resolve** — Resolved markets are checked each cycle; P&L is recorded and fed back into calibration.

---

### Real-time updates

Connect to **`WS /ws`** to receive a live event stream.
On connect you receive: `state_update`, `log_history`, `bot_status`.
During operation: `cycle_start`, `analyzing`, `opportunity`, `trade_placed`, `resolved`, `exit`, `cycle_complete`.

---

### Caller map

| Dashboard Component | Endpoints Used |
|---------------------|----------------|
| `StatsPanel` | `GET /api/state` |
| `PortfolioTab` | `GET /api/portfolio` |
| `HistoryTab` | `GET /api/history` |
| `OpportunitiesTab` | `GET /api/opportunities`, `POST /api/trade`, `POST /api/skip/{market_id}` |
| `ControlBar` | `POST /api/bot/start`, `POST /api/bot/pause`, `POST /api/bot/stop`, `GET /api/bot/status` |
| `SettingsPanel` | `GET /api/config`, `POST /api/config` |
| `AnalyticsTab` | `GET /api/calibration`, `GET /api/charts`, `GET /api/negrisk`, `GET /api/correlations` |
| `DevTools` | `GET /api/log`, `POST /api/reload`, `POST /api/reset` |
| WebSocket client | `WS /ws` |

---

### Modules

| File | Responsibility |
|------|---------------|
| `server.py` | FastAPI app, all REST endpoints, WebSocket, bot autopilot loop |
| `engine.py` | Market fetching, LLM ensemble analysis, Kelly sizing, trade execution, calibration, chart data |
| `trades.json` | Persistent portfolio state (balance, open/resolved trades) |
| `calibration.json` | Historical prediction outcomes for Brier/Platt calibration |
""",
    openapi_tags=TAGS_METADATA,
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── WebSocket ─────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """
    Real-time event stream for the React dashboard.

    **On connect**, three bootstrap messages are sent immediately:
    - `state_update` — current portfolio stats
    - `log_history` — last 50 bot log entries
    - `bot_status` — `{running: bool, paused: bool}`

    **During operation**, the following event types are broadcast to all connected clients:

    | type | trigger | data payload |
    |------|---------|--------------|
    | `cycle_start` | Scan cycle begins | — |
    | `scan_start` | Fetching markets from API | — |
    | `analyzing` | Analysing one market | `{index, total, question}` |
    | `opportunity` | Edge found | `Opportunity` object |
    | `no_edge` | No edge found | — |
    | `trade_placed` | Trade executed | `TradeRecord` object |
    | `resolved` | Trade resolved (win/loss) | `{trade, won}` |
    | `exit` | Position exited early | `{trade, reason}` |
    | `cycle_complete` | Cycle finished | `{trades_placed, next_in_sec}` |
    | `state_update` | Portfolio stats changed | `StatsResponse` object |
    | `bot_status` | Bot started/paused/stopped | `{running, paused}` |
    | `config` | Config updated | full config object |
    | `skip` | Market skipped | — |
    | `reset` | Account reset | — |
    | `info` | General info | — |

    **Caller:** All Dashboard components subscribe to this socket.
    """
    await ws.accept()
    ws_clients.add(ws)
    try:
        stats = engine.get_stats(state, config)
        await ws.send_text(json.dumps({"type": "state_update", "data": stats}, default=str))
        await ws.send_text(json.dumps({"type": "log_history", "data": bot_log[-50:]}, default=str))
        await ws.send_text(json.dumps({"type": "bot_status",
                                        "data": {"running": bot_running, "paused": bot_paused}}, default=str))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        ws_clients.discard(ws)


# ── Portfolio endpoints ───────────────────────────────────────────

@app.get(
    "/api/state",
    tags=["Portfolio"],
    summary="Get portfolio summary stats",
    responses={200: {"description": "Aggregate portfolio statistics including balance, P&L, win rate, and optional Brier score summary.", "model": StatsResponse}},
)
def get_state():
    """
    Returns aggregate portfolio statistics.

    Includes current cash balance, open position count and cost, realized and total P&L,
    deployment percentage, win/loss record, and (if ≥30 resolved trades) a Brier score summary.

    **Caller:** Dashboard `StatsPanel` — polled every few seconds; also updated in real-time
    via `state_update` WebSocket events.
    """
    return engine.get_stats(state, config)


@app.get(
    "/api/portfolio",
    tags=["Portfolio"],
    summary="Open positions with live prices",
    responses={200: {"description": "List of all open positions enriched with live market prices and unrealized P&L.", "model": list[PortfolioPosition]}},
)
def get_portfolio():
    """
    Returns all open positions enriched with live Polymarket prices.

    Each position includes the entry price, current live price, unrealized P&L, ROI %,
    price direction (up/down/flat), maximum payout, and expected profit.

    Prices are fetched live from the Polymarket Gamma API on each call — expect ~200–500ms latency.

    **Caller:** Dashboard `PortfolioTab`.
    """
    return engine.get_portfolio_live(state)


@app.get(
    "/api/history",
    tags=["Portfolio"],
    summary="Last 50 resolved trades",
    responses={200: {"description": "Up to 50 most recent resolved trades, chronologically descending.", "model": list[TradeRecord]}},
)
def get_history():
    """
    Returns up to the last 50 resolved (closed) trades.

    Each record includes the entry price, our probability estimate, shares held, cost,
    and resolution outcome (won/lost, profit/loss).

    **Caller:** Dashboard `HistoryTab`.
    """
    resolved = [t for t in state["trades"] if t["status"] == "resolved"]
    return resolved[-50:]


@app.get(
    "/api/opportunities",
    tags=["Portfolio"],
    summary="Current ranked trading opportunities",
    responses={200: {"description": "Markets identified with positive edge in the most recent scan cycle, sorted by composite score descending.", "model": list[Opportunity]}},
)
def get_opportunities():
    """
    Returns the opportunity list from the most recent scan cycle, sorted by score descending.

    Opportunities are populated during each bot scan cycle and cleared at the start of the next.
    An empty list means the bot hasn't completed a scan yet or found no edges.

    Use `POST /api/trade` to manually place a trade on any opportunity in this list.

    **Caller:** Dashboard `OpportunitiesTab`.
    """
    return current_opportunities


# ── Configuration endpoints ───────────────────────────────────────

@app.get(
    "/api/config",
    tags=["Configuration"],
    summary="Get current runtime config",
    responses={200: {"description": "Full config object with all current parameter values."}},
)
def get_config():
    """
    Returns the full runtime configuration object.

    All values can be changed via `POST /api/config`. Changes take effect on the next scan cycle.
    The starting balance is read-only (set at first launch).

    **Caller:** Dashboard `SettingsPanel`.
    """
    return config


@app.post(
    "/api/config",
    tags=["Configuration"],
    summary="Update one or more config parameters",
    responses={
        200: {"description": "Full updated config object after applying the changes."},
    },
)
async def update_config(update: ConfigUpdate):
    """
    Partially updates the runtime configuration.

    Only include the fields you want to change; omitted fields keep their current values.
    The update is broadcast to all WebSocket clients as a `config` event.
    Changes take effect on the **next** scan cycle (the current cycle, if running, is not interrupted).

    **Example:** To tighten the minimum edge to 8% and reduce scan frequency to every 3 hours:
    ```json
    {"min_edge": 0.08, "scan_interval_min": 180}
    ```

    **Caller:** Dashboard `SettingsPanel`.
    """
    for key, val in update.model_dump(exclude_none=True).items():
        if key in config:
            config[key] = val
    await broadcast(log_event("config", "Config updated", config))
    return config


# ── Bot control endpoints ─────────────────────────────────────────

@app.post(
    "/api/bot/start",
    tags=["Bot Control"],
    summary="Start or resume the autopilot bot",
    response_model=BotActionResponse,
    responses={
        200: {"description": "Bot started, resumed from pause, or was already running."},
    },
)
async def start_bot():
    """
    Starts the autopilot scanning loop, or resumes it if paused.

    - If the bot is stopped → starts a new loop task (`status: "started"`)
    - If the bot is paused → clears the pause flag (`status: "resumed"`)
    - If the bot is already running → no-op (`status: "already_running"`)

    The loop immediately begins a new scan cycle on start/resume.

    **Caller:** Dashboard `ControlBar` start button.
    """
    global bot_running, bot_paused, bot_task
    try:
        if bot_running and not bot_paused:
            return {"status": "already_running"}
        if bot_paused:
            bot_paused = False
            log_event("bot", "Bot resumed")
            await broadcast({"type": "bot_status", "data": {"running": True, "paused": False}})
            return {"status": "resumed"}
        bot_running = True
        bot_paused = False
        bot_task = asyncio.create_task(bot_loop())
        log_event("bot", "Bot started")
        await broadcast({"type": "bot_status", "data": {"running": True, "paused": False}})
        return {"status": "started"}
    except Exception as e:
        log.error(f"start_bot error: {traceback.format_exc()}")
        return {"status": "error", "detail": str(e)}


@app.post(
    "/api/bot/pause",
    tags=["Bot Control"],
    summary="Pause the autopilot bot",
    response_model=BotActionResponse,
    responses={
        200: {"description": "Bot paused, or was not running."},
    },
)
async def pause_bot():
    """
    Pauses the autopilot loop without stopping it.

    The current market analysis step (if in progress) will finish, then the loop
    will suspend until `POST /api/bot/start` is called. No new trades are placed while paused.

    - If running → pauses (`status: "paused"`)
    - If not running → no-op (`status: "not_running"`)

    **Caller:** Dashboard `ControlBar` pause button.
    """
    global bot_paused
    try:
        if not bot_running:
            return {"status": "not_running"}
        bot_paused = True
        log_event("bot", "Bot paused")
        await broadcast({"type": "bot_status", "data": {"running": True, "paused": True}})
        return {"status": "paused"}
    except Exception as e:
        log.error(f"pause_bot error: {traceback.format_exc()}")
        return {"status": "error", "detail": str(e)}


@app.post(
    "/api/bot/stop",
    tags=["Bot Control"],
    summary="Stop the autopilot bot",
    response_model=BotActionResponse,
    responses={
        200: {"description": "Bot stopped (or was already stopped)."},
    },
)
async def stop_bot():
    """
    Stops the autopilot loop entirely.

    The loop task will exit after its current sleep tick (≤2 seconds).
    Use `POST /api/bot/start` to restart from scratch.

    Unlike pause, stop clears both `running` and `paused` flags.

    **Caller:** Dashboard `ControlBar` stop button.
    """
    global bot_running, bot_paused
    try:
        bot_running = False
        bot_paused = False
        log_event("bot", "Bot stopped")
        await broadcast({"type": "bot_status", "data": {"running": False, "paused": False}})
        return {"status": "stopped"}
    except Exception as e:
        log.error(f"stop_bot error: {traceback.format_exc()}")
        return {"status": "error", "detail": str(e)}


@app.get(
    "/api/bot/status",
    tags=["Bot Control"],
    summary="Get bot running/paused state",
    response_model=BotStatusResponse,
    responses={200: {"description": "Current bot state flags."}},
)
def bot_status():
    """
    Returns the current running and paused state of the autopilot bot.

    Lightweight polling endpoint. The WebSocket `bot_status` event provides the same
    information in real-time without polling.

    **Caller:** Dashboard `ControlBar` (initial state on load).
    """
    return {"running": bot_running, "paused": bot_paused}


# ── Trading endpoints ─────────────────────────────────────────────

@app.post(
    "/api/trade",
    tags=["Trading"],
    summary="Place a manual trade",
    responses={
        200: {"description": "Trade placed successfully.", "model": TradeRecord},
        400: {"description": "Market not in current opportunities list."},
    },
)
async def manual_trade(req: ManualTrade):
    """
    Places a manual paper trade on a market from the current opportunities list.

    The `market_id` must be present in `GET /api/opportunities` — run a scan first if the list is empty.
    You can override the side: passing `side: "NO"` will buy the NO outcome at `1 − market_price_yes`.

    The trade is sized to exactly `amount` USD regardless of Kelly. Risk limits (same-day cap,
    deployment cap) still apply — the engine may reject the trade if limits are exceeded.

    The trade is broadcast to all WebSocket clients as a `trade_placed` event.

    **Caller:** Dashboard `OpportunitiesTab` trade button.
    """
    opp = next((o for o in current_opportunities if o["market_id"] == req.market_id), None)
    if not opp:
        return {"error": "Market not in current opportunities. Run a scan first."}
    opp_copy = {**opp, "side": req.side}
    if req.side == "NO":
        opp_copy["entry_price"] = round(1 - opp["market_price_yes"], 4)
    trade = engine.execute_trade(state, opp_copy, req.amount, config)
    save()
    await broadcast(log_event("trade_placed", f"Manual: {trade['side']} ${req.amount:.2f}", trade))
    await broadcast_state()
    return trade


@app.post(
    "/api/skip/{market_id}",
    tags=["Trading"],
    summary="Skip a market for this session",
    response_model=SkipResponse,
    responses={200: {"description": "Market added to the session skip list."}},
)
async def skip_market(market_id: str):
    """
    Adds a market to the session-level skip list so the bot ignores it in future scan cycles.

    The skip list is in-memory only — it is cleared on server restart or `POST /api/reset`.

    **Caller:** Dashboard `OpportunitiesTab` skip button.
    """
    skip_markets.add(market_id)
    await broadcast(log_event("skip", f"Skipped market {market_id}"))
    return {"status": "skipped", "market_id": market_id}


# ── System endpoints ──────────────────────────────────────────────

@app.post(
    "/api/reload",
    tags=["System"],
    summary="Reload portfolio state from disk",
    responses={200: {"description": "Updated portfolio stats after reloading trades.json.", "model": StatsResponse}},
)
async def reload_state():
    """
    Reloads the portfolio state from `trades.json` on disk and returns updated stats.

    Useful after manually editing `trades.json` to correct trade records, or to resync
    after an external modification. The WebSocket `state_update` event is broadcast to all clients.

    **Caller:** Dashboard `DevTools` panel.
    """
    global state
    state = engine.load_state(STATE_FILE)
    await broadcast_state()
    log_event("info", "State reloaded from disk")
    return engine.get_stats(state, config)


@app.post(
    "/api/reset",
    tags=["System"],
    summary="Reset the paper account to starting balance",
    response_model=ResetResponse,
    responses={200: {"description": "Account reset confirmed."}},
)
async def reset():
    """
    **Destructive.** Resets the paper account to the configured starting balance.

    Clears all open and resolved trades, empties the skip list and opportunities cache,
    and saves the blank state to disk. This action cannot be undone.

    The reset is broadcast to all WebSocket clients as a `reset` event.

    **Caller:** Dashboard `DevTools` reset button.
    """
    state["balance"] = config["starting_balance"]
    state["trades"] = []
    state["resolved_pnl"] = 0.0
    skip_markets.clear()
    current_opportunities.clear()
    save()
    await broadcast(log_event("reset", "Paper account reset"))
    await broadcast_state()
    return {"status": "reset"}


@app.get(
    "/api/log",
    tags=["System"],
    summary="Get recent bot event log",
    responses={200: {"description": "Up to 100 most recent event log entries, chronologically ascending.", "model": list[LogEvent]}},
)
def get_log():
    """
    Returns up to the last 100 bot event log entries in chronological order.

    Each entry has a timestamp, type, message, and optional structured data payload.
    The in-memory log holds up to 200 entries; the WebSocket `log_history` event delivers
    the last 50 on connect.

    Common event types: `cycle_start`, `scan_start`, `analyzing`, `opportunity`, `no_edge`,
    `trade_placed`, `resolved`, `exit`, `cycle_complete`, `bot`, `config`, `skip`, `reset`, `info`.

    **Caller:** Dashboard `DevTools` log viewer.
    """
    return bot_log[-100:]


# ── Analytics endpoints ───────────────────────────────────────────

@app.get(
    "/api/calibration",
    tags=["Analytics"],
    summary="Full forecast calibration report",
    responses={
        200: {"description": "Brier score decomposition, Platt scaling parameters, and calibration diagnosis.", "model": CalibrationReport},
    },
)
def get_calibration():
    """
    Returns a full calibration report for the LLM ensemble's probability forecasts.

    Requires ≥30 resolved predictions stored in `calibration.json`. If insufficient data,
    returns `{"status": "insufficient_data"}`.

    **Includes:**
    - **Brier score** and decomposition (reliability, resolution, uncertainty)
    - **Log loss**
    - **Platt scaling** parameters (a, b) learned via gradient descent
    - **Skill score** vs. the market's own implied probabilities
    - **Reliability diagram bins** (10 bins from 0–10% to 90–100%)
    - **Plain-English diagnosis** of calibration quality

    **Caller:** Dashboard `AnalyticsTab` calibration panel.
    """
    return engine.get_calibration_report()


@app.get(
    "/api/negrisk",
    tags=["Analytics"],
    summary="Scan for NegRisk arbitrage opportunities",
    responses={200: {"description": "List of NegRisk groups where YES prices deviate significantly from 1.0.", "model": list[NegRiskOpportunity]}},
)
def get_negrisk():
    """
    Scans the current market universe for NegRisk arbitrage opportunities.

    NegRisk markets group mutually exclusive outcomes (e.g. "Which party wins the Senate?").
    In a perfectly priced market, all YES prices sum to exactly 1.0.
    When they deviate significantly, a risk-free (or near-risk-free) arbitrage exists.

    Fetches fresh market data on each call — expect 1–3 seconds latency.
    Returns only groups where `|deviation| > threshold` (typically 5%).

    **Caller:** Dashboard `AnalyticsTab` NegRisk panel.
    """
    markets = engine.fetch_markets(config.get("markets_to_scan", 40))
    return engine.detect_negrisk_arbitrage(markets)


@app.get(
    "/api/correlations",
    tags=["Analytics"],
    summary="Check opportunities for correlation inconsistencies",
    responses={200: {"description": "List of opportunity pairs with logically inconsistent probability estimates. Empty list = no issues detected.", "model": list[CorrelationWarning]}},
)
def get_correlations():
    """
    Checks the current opportunity list for pairs of markets whose probability estimates
    are logically inconsistent (e.g. two complementary events both assigned >50%).

    Operates on the in-memory `current_opportunities` list — run a bot scan cycle first
    to populate it. Returns an empty list if no opportunities are loaded or no
    inconsistencies are detected.

    **Caller:** Dashboard `AnalyticsTab` correlations panel.
    """
    return engine.check_correlation_consistency(current_opportunities)


@app.get(
    "/api/charts",
    tags=["Analytics"],
    summary="Pre-computed chart data for the dashboard",
    responses={200: {"description": "P&L time series, per-trade bars, portfolio allocation slices, and summary totals.", "model": ChartsResponse}},
)
def get_charts():
    """
    Returns pre-computed chart data for all dashboard visualizations.

    **Includes:**
    - `pnl_series` — cumulative P&L over time (one point per resolved trade)
    - `trade_bars` — per-trade profit/loss bar chart data (most recent first)
    - `allocation` — pie/donut chart slices for open position allocation
    - `summary` — totals: deployed, cash, total P&L, wins, losses

    All data is derived from the in-memory portfolio state — no external API calls.

    **Caller:** Dashboard `AnalyticsTab` charts panel.
    """
    return engine.get_chart_data(state)


# ── Static files (serve React build) ─────────────────────────────

dist = Path(__file__).parent / "dashboard" / "dist"
if dist.exists():
    app.mount("/", StaticFiles(directory=str(dist), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
