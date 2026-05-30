"""
Polymarket Paper Trading Engine v3 — Enhanced with research-backed improvements.

Changes from v2:
- Log-odds Bayesian combination (instead of linear weighted average)
- Multi-persona ensemble (5 diverse analyst prompts)
- Extremizing transform for conservative-bias correction
- Proper Kelly criterion for binary contracts + quarter-Kelly
- YES/NO asymmetry exploitation (optimism tax)
- Category-based edge thresholds
- Calibration logging + Platt scaling + Brier score decomposition
- Cross-market correlation detection
- NegRisk arbitrage detection
"""

import json
import math
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from datetime import datetime, timezone
from pathlib import Path

import requests

GAMMA_API = "https://gamma-api.polymarket.com/markets"
CALIBRATION_FILE = Path(__file__).parent / "calibration.json"
_calibration_cache = {"data": None, "dirty": False}

DEFAULT_CONFIG = {
    "starting_balance": 1000.0,
    "min_edge": 0.05,
    "max_bet_fraction": 0.10,
    "max_deployed_pct": 0.60,
    "markets_to_scan": 40,
    "min_confidence": 0.55,
    "scan_interval_min": 120,
    "max_trades_per_cycle": 3,
    "min_hours_to_resolve": 12,
    # v3 additions
    "kelly_fraction": 0.25,       # quarter-Kelly (research: LLM estimates are noisy)
    "extremize_gamma": 1.3,       # push estimates away from 0.5 (1.0 = no change)
    "no_side_edge_bonus": 0.015,  # 1.5% bonus for NO (optimism tax exploitation)
    "ensemble_passes": 5,         # number of diverse LLM passes (1-5), use all for max diversity
    "use_calibration": True,      # apply Platt scaling if enough data
    "min_calibration_samples": 30, # need this many resolved predictions before applying
    "max_same_day_pct": 0.15,     # max 15% of starting capital in markets resolving same day
    # v4 additions (ROI fixes — see analysis 2026-05)
    # Exit logic: the old -50% price stop-loss realised full losses on noise and
    # gave back almost all take-profit gains. In a binary market an adverse price
    # move WITHOUT new info increases edge, so we hold to settlement and only cut
    # when a fresh analysis says the thesis is actually broken.
    "stop_loss_enabled": False,     # legacy price stop-loss (default OFF — it bled ROI)
    "stop_loss_pct": 0.50,          # only used if stop_loss_enabled
    "thesis_recheck": True,         # re-analyse positions that moved hard against us
    "thesis_recheck_trigger": 0.12, # price must move >=12pp against us to spend a recheck
    "thesis_exit_edge": -0.04,      # exit only if fresh edge on our side < -4% (thesis broken)
    "thesis_recheck_passes": 2,     # cheap ensemble for rechecks (speed)
    "min_entry_price": 0.30,        # refuse to buy any side cheaper than 30c (longshot bleed)
    "max_sports_pct": 0.30,         # cap sports at 30% of starting capital (weakest category)
    "use_learnings": True,          # feed our own losing-pattern stats back into the prompt
}


# ── Math utilities ───────────────────────────────────────────────

def logit(p):
    """Log-odds: log(p / (1-p)). Clamps to avoid infinities."""
    p = max(0.001, min(0.999, p))
    return math.log(p / (1 - p))


def sigmoid(x):
    """Inverse logit: 1 / (1 + exp(-x))."""
    if x > 500:
        return 1.0
    if x < -500:
        return 0.0
    return 1 / (1 + math.exp(-x))


def extremize(p, gamma=1.3):
    """Push probability away from 0.5 by factor gamma in log-odds space.
    gamma > 1 = more extreme, gamma < 1 = more moderate.
    Research: aggregated LLM estimates are systematically too conservative."""
    return sigmoid(gamma * logit(p))


# ── Calibration system ───────────────────────────────────────────

def load_calibration():
    """Load prediction history for calibration tracking (cached in memory)."""
    if _calibration_cache["data"] is not None:
        return _calibration_cache["data"]
    if CALIBRATION_FILE.exists():
        with open(CALIBRATION_FILE) as f:
            _calibration_cache["data"] = json.load(f)
    else:
        _calibration_cache["data"] = {"predictions": [], "platt_a": 1.0, "platt_b": 0.0}
    return _calibration_cache["data"]


def save_calibration(cal):
    _calibration_cache["data"] = cal
    with open(CALIBRATION_FILE, "w") as f:
        json.dump(cal, f, indent=2)


def log_prediction(cal, market_id, question, our_estimate, market_price, side, timestamp=None):
    """Log a prediction for future calibration analysis."""
    cal["predictions"].append({
        "market_id": market_id,
        "question": question[:100],
        "our_estimate": our_estimate,
        "market_price": market_price,
        "side": side,
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "outcome": None,  # filled in when resolved
    })
    save_calibration(cal)


def resolve_prediction(cal, market_id, outcome_yes):
    """Mark every pending prediction on this market with the resolved outcome.

    A single market may have been re-analyzed across multiple scan cycles, so
    there can be several pending predictions to settle.
    """
    mid = str(market_id)
    outcome = 1.0 if outcome_yes else 0.0
    for p in cal["predictions"]:
        if str(p["market_id"]) == mid and p["outcome"] is None:
            p["outcome"] = outcome
    save_calibration(cal)


def compute_brier_score(cal):
    """Compute Brier score and decomposition on resolved predictions."""
    resolved = [p for p in cal["predictions"] if p["outcome"] is not None]
    if not resolved:
        return None

    n = len(resolved)
    forecasts = [p["our_estimate"] for p in resolved]
    outcomes = [p["outcome"] for p in resolved]

    # Overall Brier score
    brier = sum((f - o) ** 2 for f, o in zip(forecasts, outcomes)) / n

    # Log loss (more sensitive to confident wrong predictions)
    log_loss = 0
    for f, o in zip(forecasts, outcomes):
        f_clamped = max(0.001, min(0.999, f))
        log_loss += -(o * math.log(f_clamped) + (1 - o) * math.log(1 - f_clamped))
    log_loss /= n

    # Brier decomposition: bin into deciles
    bins = {}
    for f, o in zip(forecasts, outcomes):
        b = min(int(f * 10), 9)  # bin 0-9
        if b not in bins:
            bins[b] = {"forecasts": [], "outcomes": []}
        bins[b]["forecasts"].append(f)
        bins[b]["outcomes"].append(o)

    # Reliability (calibration error)
    reliability = 0
    for b, data in bins.items():
        nk = len(data["forecasts"])
        fk = sum(data["forecasts"]) / nk
        ok = sum(data["outcomes"]) / nk
        reliability += nk * (fk - ok) ** 2
    reliability /= n

    # Resolution (how much forecasts vary from base rate)
    base_rate = sum(outcomes) / n
    resolution = 0
    for b, data in bins.items():
        nk = len(data["forecasts"])
        ok = sum(data["outcomes"]) / nk
        resolution += nk * (ok - base_rate) ** 2
    resolution /= n

    # Uncertainty (inherent, fixed for dataset)
    uncertainty = base_rate * (1 - base_rate)

    # Market Brier for comparison
    market_forecasts = [p["market_price"] for p in resolved]
    market_brier = sum((f - o) ** 2 for f, o in zip(market_forecasts, outcomes)) / n

    return {
        "n": n,
        "brier": round(brier, 4),
        "log_loss": round(log_loss, 4),
        "reliability": round(reliability, 4),    # lower = better calibrated
        "resolution": round(resolution, 4),       # higher = better differentiation
        "uncertainty": round(uncertainty, 4),
        "market_brier": round(market_brier, 4),   # compare: are we beating the market?
        "skill_score": round(1 - brier / max(uncertainty, 0.001), 4),  # >0 = better than naive
        "bins": {str(b): {"avg_forecast": round(sum(d["forecasts"])/len(d["forecasts"]), 3),
                          "avg_outcome": round(sum(d["outcomes"])/len(d["outcomes"]), 3),
                          "count": len(d["forecasts"])}
                 for b, d in sorted(bins.items())},
    }


def fit_platt_scaling(cal):
    """Fit Platt scaling parameters (a, b) on resolved predictions.
    p_calibrated = sigmoid(a * logit(p_raw) + b)
    Uses simple gradient descent."""
    resolved = [p for p in cal["predictions"] if p["outcome"] is not None]
    if len(resolved) < 20:
        return 1.0, 0.0  # identity (no correction)

    forecasts = [p["our_estimate"] for p in resolved]
    outcomes = [p["outcome"] for p in resolved]

    # Initialize
    a, b = 1.0, 0.0
    lr = 0.01

    for _ in range(500):
        grad_a, grad_b = 0, 0
        for f, o in zip(forecasts, outcomes):
            z = a * logit(f) + b
            p = sigmoid(z)
            err = p - o
            grad_a += err * logit(f)
            grad_b += err
        grad_a /= len(forecasts)
        grad_b /= len(forecasts)
        a -= lr * grad_a
        b -= lr * grad_b

    # Clamp to reasonable range
    a = max(0.5, min(2.5, a))
    b = max(-1.0, min(1.0, b))

    cal["platt_a"] = round(a, 4)
    cal["platt_b"] = round(b, 4)
    save_calibration(cal)
    return a, b


def apply_calibration(p, cal, config=None):
    """Apply Platt scaling if we have enough data."""
    config = config or DEFAULT_CONFIG
    if not config.get("use_calibration", True):
        return p
    resolved = [pr for pr in cal["predictions"] if pr["outcome"] is not None]
    if len(resolved) < config.get("min_calibration_samples", 30):
        return p
    a = cal.get("platt_a", 1.0)
    b = cal.get("platt_b", 0.0)
    return sigmoid(a * logit(p) + b)


# ── State management ─────────────────────────────────────────────

def new_state(starting_balance=1000.0):
    return {"balance": starting_balance, "trades": [], "resolved_pnl": 0.0}


def load_state(path):
    p = Path(path)
    if p.exists():
        with open(p) as f:
            return json.load(f)
    return new_state()


def save_state(state, path):
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


def deployed_amount(state):
    return sum(t["cost"] for t in state["trades"] if t["status"] == "open")


def same_day_exposure(state, end_date):
    """Total cost deployed in open trades resolving on the same day as end_date."""
    if not end_date:
        return 0
    target_day = end_date[:10]  # YYYY-MM-DD
    return sum(
        t["cost"] for t in state["trades"]
        if t["status"] == "open" and t.get("end_date", "")[:10] == target_day
    )


def get_stats(state, config=None):
    config = config or DEFAULT_CONFIG
    open_trades = [t for t in state["trades"] if t["status"] == "open"]
    resolved = [t for t in state["trades"] if t["status"] == "resolved"]
    open_cost = sum(t["cost"] for t in open_trades)
    total_value = state["balance"] + open_cost
    starting = config["starting_balance"]
    pnl = total_value - starting
    wins = sum(1 for t in resolved if t.get("won"))
    losses = len(resolved) - wins

    stats = {
        "balance": state["balance"],
        "open_count": len(open_trades),
        "open_cost": open_cost,
        "resolved_pnl": state["resolved_pnl"],
        "total_pnl": round(pnl, 2),
        "total_pnl_pct": round((pnl / starting) * 100, 2) if starting else 0,
        "deployed_pct": round((open_cost / starting) * 100, 1) if starting else 0,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / len(resolved) * 100, 1) if resolved else 0,
        "total_trades": len(state["trades"]),
    }

    # Attach calibration stats if available
    cal = load_calibration()
    brier = compute_brier_score(cal)
    if brier:
        stats["calibration"] = brier

    return stats


# ── Learning feedback (self-generated losing-pattern summary) ────────
# A compact, data-driven "what's been losing us money" block that gets injected
# into the analyst prompts each cycle (Prediction Arena's "Critical learning
# section"). Recomputed from our own resolved trades, so it self-updates.

LEARNINGS = ""           # module-global, read by ask_claude
_MIN_LEARNING_TRADES = 20


def compute_learnings(state):
    """Build a short prompt block summarising our own profitable vs losing patterns.

    Returns "" until we have enough resolved trades to be meaningful.
    """
    resolved = [t for t in state.get("trades", []) if t["status"] == "resolved"]
    if len(resolved) < _MIN_LEARNING_TRADES:
        return ""

    def roi(group):
        cost = sum(t["cost"] for t in group)
        return (sum(t.get("profit", 0) for t in group) / cost * 100) if cost else 0.0

    # Per-category ROI
    cats = {}
    for t in resolved:
        cats.setdefault(t.get("category", "other"), []).append(t)
    cat_lines = sorted(((roi(g), c, len(g)) for c, g in cats.items() if len(g) >= 5))

    # Entry-price buckets (the side we bought)
    def bucket(p):
        return "<30c" if p < 0.30 else "30-50c" if p < 0.50 else "50-70c" if p < 0.70 else ">=70c"
    bk = {}
    for t in resolved:
        bk.setdefault(bucket(t["entry_price"]), []).append(t)

    worst_cat = cat_lines[0] if cat_lines else None
    best_cat = cat_lines[-1] if cat_lines else None
    low_bucket = bk.get("<30c", [])

    parts = ["=== OUR TRACK RECORD (learn from it) ==="]
    if best_cat and worst_cat and best_cat[1] != worst_cat[1]:
        parts.append(
            f"Best category for us: {best_cat[1]} ({best_cat[0]:+.0f}% ROI). "
            f"Worst: {worst_cat[1]} ({worst_cat[0]:+.0f}% ROI) - demand a bigger edge there."
        )
    if low_bucket:
        parts.append(
            f"Cheap longshot buys (<30c) have returned {roi(low_bucket):+.0f}% ROI over "
            f"{len(low_bucket)} trades - avoid low-priced sides unless the edge is huge."
        )
    parts.append(
        "Reminder: an adverse price move is not new information. If your thesis holds, "
        "the cheaper price is a better entry, not a reason to fear the position."
    )
    return "\n".join(parts) + "\n\n"


def update_learnings(state):
    """Recompute and cache the learning block. Call once per scan cycle."""
    global LEARNINGS
    LEARNINGS = compute_learnings(state)
    return LEARNINGS


# ── Polymarket API ───────────────────────────────────────────────

def parse_prices(raw):
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None, None
    if not raw or len(raw) < 2:
        return None, None
    try:
        return float(raw[0]), float(raw[1])
    except (ValueError, TypeError):
        return None, None


def fetch_markets(limit=40):
    all_markets = {}
    for order_by in ["volume24hr", "volume1wk", "volume1mo", "startDate", "endDate"]:
        params = {
            "limit": limit,
            "active": "true",
            "closed": "false",
            "order": order_by,
            "ascending": "false",
        }
        try:
            r = requests.get(GAMMA_API, params=params, timeout=15)
            r.raise_for_status()
            for m in r.json():
                mid = m.get("id")
                if mid and mid not in all_markets:
                    all_markets[mid] = m
        except requests.RequestException:
            pass
    return list(all_markets.values())[:limit]


def fetch_market_by_id(market_id):
    try:
        r = requests.get(f"{GAMMA_API}?id={market_id}", timeout=10)
        r.raise_for_status()
        data = r.json()
        if data:
            return data[0]
        # Market may have resolved and been archived — retry with closed=true
        r2 = requests.get(f"{GAMMA_API}?id={market_id}&closed=true", timeout=10)
        r2.raise_for_status()
        data2 = r2.json()
        return data2[0] if data2 else None
    except requests.RequestException:
        return None


def get_hours_to_resolve(end_date):
    if not end_date:
        return None
    try:
        end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        return (end - datetime.now(timezone.utc)).total_seconds() / 3600
    except (ValueError, TypeError):
        return None


LIVE_EVENT_KEYWORDS = {
    "football", "soccer", "match", "vs.", " vs ", "game", "nba", "nfl", "nhl",
    "nba", "mlb", "premier league", "champions league", "la liga", "serie a",
    "bundesliga", "ligue 1", "eredivisie", "mls", "score", "goal", "half",
    "quarter", "innings", "set", "tennis", "basketball", "baseball", "hockey",
    "rugby", "cricket", "formula 1", "f1", "race", "grand prix", "ufc", "mma",
    "boxing", "fight", "bout", "playoff", "tournament", "championship",
}


def looks_like_live_event(question, description=""):
    text = (question + " " + description).lower()
    return any(kw in text for kw in LIVE_EVENT_KEYWORDS)


def get_momentum(market):
    m = {}
    for key, label in [("oneHourPriceChange", "1h"),
                       ("oneWeekPriceChange", "1w"),
                       ("oneMonthPriceChange", "1m")]:
        try:
            v = market.get(key)
            if v is not None:
                m[label] = float(v)
        except (ValueError, TypeError):
            pass
    return m or None


# ── Category detection ───────────────────────────────────────────

CATEGORY_KEYWORDS = {
    "sports": {"win", "game", "match", "score", "nba", "nfl", "mlb", "nhl", "ufc",
               "premier league", "champions league", "soccer", "football", "tennis",
               "basketball", "baseball", "hockey", "boxing", "fight", "tournament",
               "playoff", "championship", "grand prix", "f1", "race"},
    "politics": {"president", "elect", "vote", "congress", "senate", "governor",
                 "prime minister", "parliament", "democrat", "republican", "party",
                 "poll", "approval", "impeach", "cabinet", "legislation", "bill"},
    "crypto": {"bitcoin", "btc", "ethereum", "eth", "crypto", "token", "defi",
               "blockchain", "solana", "sol", "price above", "price below"},
    "finance": {"gdp", "inflation", "fed", "interest rate", "stock", "s&p",
                "nasdaq", "dow", "treasury", "cpi", "unemployment", "jobs",
                "earnings", "revenue", "ipo"},
    "entertainment": {"oscar", "grammy", "emmy", "movie", "film", "album",
                      "song", "artist", "box office", "netflix", "spotify",
                      "streaming", "celebrity", "award"},
    "world": {"war", "peace", "treaty", "invasion", "sanction", "nato",
              "refugee", "earthquake", "hurricane", "pandemic", "covid",
              "conflict", "ceasefire", "hostage", "missile", "airspace",
              "blockade", "uranium", "nuclear", "iran", "israel", "hezbollah",
              "ukraine", "russia", "gaza", "hamas"},
}

# Category efficiency (higher = more inefficient = easier to find edge = trade more readily).
# Recalibrated 2026-05 from our own realised ROI: sports was our WORST category
# (~0.9% ROI on 70% of volume) yet the old table gave it a 1.1 boost. Geopolitical
# NO bets ("world") were our actual edge, so they keep the highest multiplier.
CATEGORY_EDGE_MULTIPLIER = {
    "finance": 0.6,       # very efficient, need bigger edge
    "crypto": 0.75,       # somewhat efficient
    "sports": 0.7,        # OUR WORST category — demand more edge (was 1.1)
    "politics": 1.0,      # baseline; modestly profitable for us
    "entertainment": 1.3, # quite inefficient
    "world": 1.4,         # geopolitics/conflict — our strongest edge
    "other": 1.0,
}


def detect_category(question, description=""):
    """Detect market category. Returns (category_name, edge_multiplier).

    Matches single-word keywords on WORD BOUNDARIES (not substrings) — the old
    substring match tagged any question containing "whether" as crypto because
    "eth" is a substring of it, which is why Iran/geopolitics markets were
    mislabelled "crypto". Multi-word keywords still match as substrings.
    """
    text = (question + " " + description).lower()
    tokens = set(re.findall(r"[a-z0-9&]+", text))
    best_cat = "other"
    best_score = 0
    for cat, keywords in CATEGORY_KEYWORDS.items():
        score = 0
        for kw in keywords:
            if " " in kw:
                if kw in text:
                    score += 1
            elif kw.strip() in tokens:
                score += 1
        if score > best_score:
            best_score = score
            best_cat = cat
    multiplier = CATEGORY_EDGE_MULTIPLIER.get(best_cat, 1.0)
    return best_cat, multiplier


# ── Multi-persona ensemble ───────────────────────────────────────

def _parse_last_probability(raw):
    if not raw:
        return None
    lines = [l.strip() for l in raw.strip().split("\n") if l.strip()]
    # First pass: look for explicit PROBABILITY: prefix (preferred format)
    for line in reversed(lines):
        match = re.search(r'PROBABILITY:\s*(0?\.\d+)', line, re.IGNORECASE)
        if match:
            val = float(match.group(1))
            if 0.01 <= val <= 0.99:
                return val
    # Fallback: look for bare decimal on a line by itself (or nearly)
    for line in reversed(lines):
        # Skip lines that look like data/stats (contain $, volume, etc.)
        if any(kw in line.lower() for kw in ["volume", "market", "$", "price"]):
            continue
        match = re.search(r'\b(0?\.\d+)\b', line)
        if match:
            val = float(match.group(1))
            if 0.01 <= val <= 0.99:
                return val
        match = re.search(r'\b(\d{1,2})%', line)
        if match:
            val = int(match.group(1)) / 100
            if 0.01 <= val <= 0.99:
                return val
    return None


def _kill_proc_tree(proc):
    """Kill a process and all its children (handles Windows pipe-holding child processes)."""
    try:
        if sys.platform == "win32":
            subprocess.call(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        else:
            import os, signal
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    # Drain any remaining pipe data so the handle is released
    try:
        proc.communicate(timeout=3)
    except Exception:
        pass


def _call_claude(prompt, timeout=60):
    proc = None
    try:
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        proc = subprocess.Popen(
            ["claude", "--print", prompt],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            **kwargs
        )
        stdout, _ = proc.communicate(timeout=timeout)
        return stdout.strip()
    except subprocess.TimeoutExpired:
        _kill_proc_tree(proc)
        return None
    except FileNotFoundError:
        return None
    except Exception:
        if proc:
            _kill_proc_tree(proc)
        return None


# 5 diverse analyst personas for ensemble
PERSONAS = [
    {
        "name": "deep_analyst",
        "weight": 0.30,
        "prompt": (
            "You are an expert prediction market analyst and calibrated forecaster. "
            "Your job is to find MISPRICINGS.\n\n"
            "{context}\n"
            "=== ANALYSIS FRAMEWORK ===\n"
            "1. QUESTION PARSING: What exactly needs to happen for YES?\n"
            "2. LIVE STATUS CHECK: Has this event started or finished?\n"
            "3. BASE RATE: Historical frequency for this type of event?\n"
            "4. EVIDENCE: Specific facts pushing probability up or down?\n"
            "5. MARKET EFFICIENCY: With ${volume:,.0f} volume, how likely is a mispricing?\n"
            "6. BLIND SPOTS: What might the market be overlooking?\n"
            "7. CALIBRATION: Am I being overconfident?\n\n"
            "Form your OWN view first, then compare to market.\n"
            "On the LAST LINE write EXACTLY: PROBABILITY: 0.XX (decimal 0.01-0.99)."
        ),
    },
    {
        "name": "contrarian",
        "weight": 0.20,
        "prompt": (
            "You are a contrarian analyst who specializes in finding where crowds are wrong. "
            "Markets are often biased by recency, narrative, and groupthink.\n\n"
            "{context}\n"
            "Consider: What is the consensus getting wrong? Where are people anchoring too heavily? "
            "What low-probability scenarios are being ignored? What evidence contradicts the market price?\n"
            "Be bold but not reckless. Adjust for base rates.\n"
            "On the LAST LINE write EXACTLY: PROBABILITY: 0.XX (decimal 0.01-0.99)."
        ),
    },
    {
        "name": "base_rate_statistician",
        "weight": 0.25,
        "prompt": (
            "You are a Bayesian statistician focused purely on base rates and reference classes. "
            "Ignore narratives and focus on data.\n\n"
            "{context}\n"
            "Your approach:\n"
            "1. What is the reference class for this event?\n"
            "2. What is the base rate for that class?\n"
            "3. What STRONG evidence justifies deviating from the base rate?\n"
            "4. Apply Bayesian updating conservatively.\n"
            "On the LAST LINE write EXACTLY: PROBABILITY: 0.XX (decimal 0.01-0.99)."
        ),
    },
    {
        "name": "quick_gut",
        "weight": 0.15,
        "prompt": (
            "Quick probability estimate for a prediction market.\n\n"
            "Question: \"{question}\"\n{time_str}{momentum_str}"
            "What is the probability this resolves YES?\n"
            "Think briefly, then on the LAST LINE write EXACTLY: PROBABILITY: 0.XX"
        ),
    },
    {
        "name": "momentum_trader",
        "weight": 0.10,
        "prompt": (
            "You are a momentum-based prediction market trader. "
            "You believe price movements contain information.\n\n"
            "{context}\n"
            "Focus on: recent price direction, volume trends, time to resolution.\n"
            "If the price is moving in a direction with volume, that signal matters.\n"
            "If the market is stale with no movement, trust the current price more.\n"
            "On the LAST LINE write EXACTLY: PROBABILITY: 0.XX (decimal 0.01-0.99)."
        ),
    },
]


def ask_claude(question, description, price_yes, volume=0, end_date="", momentum=None, hours_left=None, config=None):
    """Multi-persona ensemble analysis with log-odds Bayesian combination."""
    config = config or DEFAULT_CONFIG
    num_passes = config.get("ensemble_passes", 3)
    num_passes = max(1, min(5, num_passes))

    # Build shared context
    momentum_str = ""
    if momentum:
        parts = []
        if momentum.get("1h") is not None:
            parts.append(f"1h: {momentum['1h']:+.1%}")
        if momentum.get("1w") is not None:
            parts.append(f"1w: {momentum['1w']:+.1%}")
        if momentum.get("1m") is not None:
            parts.append(f"1mo: {momentum['1m']:+.1%}")
        if parts:
            momentum_str = f"Price momentum: {', '.join(parts)}\n"

    time_str = ""
    if end_date:
        try:
            end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            days = (end - datetime.now(timezone.utc)).days
            if hours_left is not None and hours_left < 48:
                time_str = f"Resolves in: ~{hours_left:.0f} hours\n"
            elif days > 0:
                time_str = f"Resolves in: {days} days\n"
            else:
                time_str = "Resolves: very soon\n"
        except (ValueError, TypeError):
            pass

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    is_sports = looks_like_live_event(question, description)

    live_warning = ""
    if hours_left is not None and hours_left < 48 and is_sports:
        live_warning = (
            f"LIVE EVENT WARNING: This market resolves in ~{hours_left:.0f} hours "
            f"and appears to be about an ongoing or imminent event.\n"
            f"MANDATORY: Find the CURRENT score/status/result as of {today}.\n"
            f"If event is in progress or completed, base estimate on CURRENT STATE.\n"
            f"If you cannot determine current state, output 0.50.\n\n"
        )
    elif is_sports:
        live_warning = (
            f"NOTE: Sports/competitive event market (today: {today}). "
            f"Check whether event has occurred or is in progress.\n\n"
        )

    learning_block = LEARNINGS if config.get("use_learnings", True) else ""

    context = (
        f"Today: {today}\n{live_warning}{learning_block}"
        f"=== MARKET ===\n"
        f"Question: {question}\n"
        f"Description: {description[:1000]}\n"
        f"Current price: {price_yes:.1%} YES / {1-price_yes:.1%} NO\n"
        f"24h volume: ${volume:,.0f}\n"
        f"{time_str}{momentum_str}"
    )

    # Run ensemble passes in parallel
    personas_to_use = PERSONAS[:num_passes]
    total_weight = sum(p["weight"] for p in personas_to_use)

    def run_persona(persona):
        if persona["name"] == "quick_gut":
            prompt = persona["prompt"].format(
                question=question, time_str=time_str, momentum_str=momentum_str
            )
            timeout = 30
        else:
            prompt = persona["prompt"].format(
                context=context, volume=volume, question=question,
                time_str=time_str, momentum_str=momentum_str
            )
            timeout = 60
        raw = _call_claude(prompt, timeout=timeout)
        est = _parse_last_probability(raw) if raw else None
        return est, persona["weight"] / total_weight

    estimates = []
    weights = []
    with ThreadPoolExecutor(max_workers=num_passes) as pool:
        futures = {pool.submit(run_persona, p): p for p in personas_to_use}
        try:
            for fut in as_completed(futures, timeout=90):
                try:
                    est, w = fut.result()
                    if est is not None:
                        estimates.append(est)
                        weights.append(w)
                except Exception:
                    pass
        except FuturesTimeout:
            # Some personas didn't respond in 90 s — use whatever we got
            for fut in futures:
                if fut.done() and not fut.cancelled():
                    try:
                        est, w = fut.result()
                        if est is not None and est not in estimates:
                            estimates.append(est)
                            weights.append(w)
                    except Exception:
                        pass

    if not estimates:
        return None, 0

    # ── Combine in log-odds space (Bayesian combination) ──
    # This is the mathematically correct way to combine independent evidence
    total_log_odds = 0
    total_w = sum(weights)
    for est, w in zip(estimates, weights):
        normalized_w = w / total_w
        total_log_odds += normalized_w * logit(est)

    combined = sigmoid(total_log_odds)

    # ── Apply calibration correction (Platt scaling) first ──
    # Platt scaling should see the raw combined estimate, not the extremized one
    cal = load_calibration()
    combined = apply_calibration(combined, cal, config)

    # ── Extremize: push away from 0.5 ──
    # Once Platt has enough data, consider disabling extremize (set gamma=1.0)
    gamma = config.get("extremize_gamma", 1.3)
    combined = extremize(combined, gamma)

    # ── Confidence from ensemble agreement ──
    if len(estimates) >= 2:
        # Use standard deviation of estimates as disagreement measure
        mean_est = sum(estimates) / len(estimates)
        variance = sum((e - mean_est) ** 2 for e in estimates) / len(estimates)
        std_dev = math.sqrt(variance)

        if std_dev < 0.03:
            confidence = 0.95
        elif std_dev < 0.06:
            confidence = 0.85
        elif std_dev < 0.10:
            confidence = 0.70
        elif std_dev < 0.15:
            confidence = 0.55
        else:
            confidence = 0.40
    else:
        confidence = 0.50

    # Volume noted for downstream min_edge adjustment (not confidence penalty)
    # High volume = efficient market, handled via volume_edge_multiplier in analyze_market

    return round(combined, 4), round(confidence, 2)


# ── Trading logic ────────────────────────────────────────────────

def kelly_size(edge, entry_price, balance, confidence=1.0, max_fraction=0.10, config=None):
    """Kelly criterion for binary all-or-nothing contracts.

    Binary contract: pay p per share, receive $1 if win, $0 if lose.
    Correct Kelly fraction: f* = (q - p) / (1 - p)
    where q = our win probability, p = entry price.

    Then multiply by kelly_fraction (default 0.25 = quarter-Kelly).
    Research: quarter-Kelly optimal for noisy LLM estimates (PolySwarm, 2026).
    """
    config = config or DEFAULT_CONFIG
    if edge <= 0 or entry_price <= 0 or entry_price >= 1:
        return 0

    # Win probability = entry_price + edge (clamped)
    win_prob = max(0.01, min(0.99, entry_price + edge))

    if win_prob <= entry_price:
        return 0

    # Correct Kelly for binary contract: (q - p) / (1 - p)
    kelly = (win_prob - entry_price) / (1 - entry_price)

    # Apply kelly fraction (quarter-Kelly default) and confidence
    frac = config.get("kelly_fraction", 0.25)
    adjusted = kelly * confidence * frac
    capped = min(adjusted, max_fraction)

    return round(max(balance * capped, 0), 2)


def opportunity_score(edge, confidence, volume, category_multiplier=1.0):
    """Composite score 0-100, now category-aware."""
    # Edge component (0-40) — adjusted by category efficiency
    effective_edge = edge * category_multiplier
    edge_score = min(effective_edge / 0.20, 1.0) * 40

    # Confidence component (0-35)
    conf_score = min(confidence / 0.95, 1.0) * 35

    # Volume component (0-25)
    if volume < 10_000:
        vol_score = 5
    elif volume < 50_000:
        vol_score = 15
    elif volume < 200_000:
        vol_score = 25
    elif volume < 500_000:
        vol_score = 20
    elif volume < 1_000_000:
        vol_score = 12
    else:
        vol_score = 6

    return round(edge_score + conf_score + vol_score)


def analyze_market(market, config=None):
    """Analyze a market with all v3 enhancements. Returns opportunity dict or None."""
    config = config or DEFAULT_CONFIG
    question = market.get("question", "")
    description = market.get("description", "")
    prices_raw = market.get("outcomePrices", "")
    volume = market.get("volumeNum", 0) or 0
    end_date = market.get("endDateIso", "")

    price_yes, price_no = parse_prices(prices_raw)
    if price_yes is None or not question:
        return None
    if price_yes < 0.08 or price_yes > 0.92:
        return None

    hours_left = get_hours_to_resolve(end_date)
    min_hours = config.get("min_hours_to_resolve", 12)
    if hours_left is not None and hours_left < min_hours:
        return None

    # Detect category for edge threshold adjustment
    category, cat_multiplier = detect_category(question, description)

    # Ban same-day sports: Claude can't verify live scores
    if category == "sports" and hours_left is not None and hours_left < 24:
        return None

    momentum = get_momentum(market)
    estimate, confidence = ask_claude(
        question, description, price_yes, volume, end_date, momentum, hours_left, config
    )

    if estimate is None:
        return None
    if confidence < config["min_confidence"]:
        return None

    # Calculate edge for both sides
    edge_yes = estimate - price_yes
    edge_no = (1 - estimate) - price_no

    # YES/NO asymmetry: add bonus to NO side (optimism tax exploitation)
    # Research: NO contracts outperform YES at 69/99 price levels
    # Only apply bonus if there's meaningful raw edge first (prevent phantom signals)
    no_bonus = config.get("no_side_edge_bonus", 0.015)
    if edge_no >= 0.02:  # at least 2% raw edge before bonus kicks in
        edge_no += no_bonus

    # Category-adjusted minimum edge (inefficient categories need less edge)
    min_edge = config["min_edge"] / cat_multiplier

    # Volume-based min_edge adjustment: high volume = more efficient = need bigger edge
    if volume > 2_000_000:
        min_edge *= 1.5
    elif volume > 1_000_000:
        min_edge *= 1.3
    elif volume > 500_000:
        min_edge *= 1.15

    if edge_yes >= edge_no and edge_yes > min_edge:
        side, edge, entry_price = "YES", edge_yes, price_yes
    elif edge_no > min_edge:
        side, edge, entry_price = "NO", edge_no, price_no
    else:
        return None

    # Entry-price floor: cheap (longshot) sides bled us dry (<30c buckets ran
    # -77%/-10% ROI). Refuse to buy any side priced below the floor regardless of
    # nominal edge — the edge on longshots is mostly noise/overconfidence.
    min_entry = config.get("min_entry_price", 0.0)
    if entry_price < min_entry:
        return None

    score = opportunity_score(edge, confidence, volume, cat_multiplier)
    roi = ((1.0 / entry_price) - 1) * 100

    # Log prediction for calibration
    cal = load_calibration()
    log_prediction(cal, market.get("id"), question, estimate, price_yes, side)

    return {
        "market_id": market.get("id"),
        "question": question,
        "side": side,
        "entry_price": round(entry_price, 4),
        "our_estimate": estimate,
        "confidence": confidence,
        "market_price_yes": round(price_yes, 4),
        "edge": round(edge, 4),
        "volume": volume,
        "end_date": end_date,
        "momentum": momentum,
        "score": score,
        "roi_pct": round(roi, 1),
        "category": category,
        "cat_multiplier": cat_multiplier,
    }


def execute_trade(state, opp, size, config=None):
    """Place a paper trade. Returns trade dict or None if blocked by risk limits."""
    config = config or DEFAULT_CONFIG

    # Same-day exposure limit
    max_same_day = config.get("max_same_day_pct", 0.15) * config["starting_balance"]
    current_same_day = same_day_exposure(state, opp.get("end_date", ""))
    if current_same_day + size > max_same_day:
        size = max(0, max_same_day - current_same_day)
        if size < 1.0:  # less than $1 not worth it
            return None

    # Sports concentration cap: our weakest category (~0.9% ROI) and ~70% of past
    # volume. Keep it from dominating the book.
    if opp.get("category") == "sports":
        max_sports = config.get("max_sports_pct", 1.0) * config["starting_balance"]
        cur_sports = sum(t["cost"] for t in state["trades"]
                         if t["status"] == "open" and t.get("category") == "sports")
        if cur_sports + size > max_sports:
            size = max(0, max_sports - cur_sports)
            if size < 1.0:
                return None

    shares = size / opp["entry_price"]
    trade = {
        "market_id": opp["market_id"],
        "question": opp["question"],
        "side": opp["side"],
        "entry_price": opp["entry_price"],
        "our_estimate": opp["our_estimate"],
        "confidence": opp.get("confidence", 0.5),
        "shares": round(shares, 4),
        "cost": round(size, 2),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "open",
        "end_date": opp.get("end_date", ""),
        "score": opp.get("score", 0),
        "edge": opp.get("edge", 0),
        "category": opp.get("category", "other"),
    }
    state["balance"] = round(state["balance"] - size, 2)
    state["trades"].append(trade)
    return trade


def check_resolved(state):
    """Settle open positions whose markets have closed, AND backfill calibration
    outcomes for markets we already exited early on."""
    results = []
    cal = load_calibration()
    seen_markets: set[str] = set()

    for t in state["trades"]:
        mid = str(t.get("market_id"))
        if t["status"] == "open":
            m = fetch_market_by_id(mid)
            if not m or not m.get("closed"):
                continue
            p_yes, _ = parse_prices(m.get("outcomePrices", ""))
            if p_yes is None:
                continue
            won = (t["side"] == "YES" and p_yes > 0.9) or \
                  (t["side"] == "NO" and p_yes < 0.1)
            payout = t["shares"] * 1.0 if won else 0
            profit = round(payout - t["cost"], 2)
            if won:
                state["balance"] = round(state["balance"] + payout, 2)
            t["status"] = "resolved"
            t["won"] = won
            t["profit"] = profit
            state["resolved_pnl"] = round(state["resolved_pnl"] + profit, 2)
            results.append({"trade": t, "won": won, "profit": profit})
            resolve_prediction(cal, mid, p_yes > 0.9)
            seen_markets.add(mid)

    # Backfill: markets where we exited early but the market has since closed —
    # we still want the calibration outcome recorded, even though we no longer
    # hold the position.
    pending_mids = {str(p["market_id"]) for p in cal["predictions"]
                    if p["outcome"] is None}
    for mid in pending_mids - seen_markets:
        m = fetch_market_by_id(mid)
        if not m or not m.get("closed"):
            continue
        p_yes, _ = parse_prices(m.get("outcomePrices", ""))
        if p_yes is None:
            continue
        resolve_prediction(cal, mid, p_yes > 0.9)

    if results or pending_mids - seen_markets:
        fit_platt_scaling(cal)

    return results


def get_portfolio_live(state):
    """Get open positions with live prices."""
    positions = []
    for t in state["trades"]:
        if t["status"] != "open":
            continue
        pos = {**t}
        m = fetch_market_by_id(t["market_id"])
        if m:
            p_yes, p_no = parse_prices(m.get("outcomePrices", ""))
            if p_yes is not None:
                cur = p_yes if t["side"] == "YES" else p_no
                pos["current_price"] = round(cur, 4)
                pos["unrealized"] = round((cur - t["entry_price"]) * t["shares"], 2)
                pos["direction"] = "up" if cur > t["entry_price"] + 0.01 else \
                                   "down" if cur < t["entry_price"] - 0.01 else "flat"
        est = t.get("our_estimate", t["entry_price"])
        win_prob = (1 - est) if t["side"] == "NO" else est
        pos["max_payout"] = round(t["shares"] * 1.0, 2)
        pos["expected_profit"] = round((win_prob * t["shares"]) - t["cost"], 2)
        pos["roi_pct"] = round(((1.0 / t["entry_price"]) - 1) * 100, 1)
        positions.append(pos)
    return positions


# ── Position exit logic ──────────────────────────────────────────
# Philosophy (v4): in a binary market an adverse price move with no new
# information INCREASES our edge, so we hold to settlement. We take profit when
# the market converges to our view ("edge eroded"), and we only cut a losing
# position when a FRESH analysis says the thesis is actually broken — not because
# the price wobbled. The old -50% price stop-loss realised full losses on noise
# and is OFF by default (see stop_loss_enabled).

def check_exits(state, config=None):
    """Check open positions for exit signals. Sells at simulated live price.
    Returns list of exit actions taken."""
    config = config or DEFAULT_CONFIG
    exits = []
    for t in state["trades"]:
        if t["status"] != "open":
            continue
        m = fetch_market_by_id(t["market_id"])
        if not m:
            continue
        p_yes, p_no = parse_prices(m.get("outcomePrices", ""))
        if p_yes is None:
            continue

        current_price = p_yes if t["side"] == "YES" else p_no
        entry = t["entry_price"]
        edge_at_entry = t.get("edge", 0)

        # Current implied edge: how much edge remains vs our stored estimate
        if t["side"] == "YES":
            current_edge = t.get("our_estimate", entry) - p_yes
        else:
            current_edge = (1 - t.get("our_estimate", 1 - entry)) - p_no

        should_exit = False
        reason = ""

        # (1) TAKE PROFIT: price converged to our view, <20% of original edge left.
        if edge_at_entry > 0 and current_edge < edge_at_entry * 0.20:
            should_exit = True
            reason = f"edge eroded ({current_edge:.1%} vs {edge_at_entry:.1%} at entry)"

        # (2) THESIS RE-CHECK: only for positions that moved hard against us, with
        # time left to matter. Re-analyse; cut ONLY if the fresh view says we are
        # genuinely on the wrong side. Otherwise hold (and refresh our estimate).
        adverse_move = entry - current_price  # positive = moved against us
        hours_left = get_hours_to_resolve(m.get("endDateIso", ""))
        time_ok = hours_left is None or hours_left > config.get("min_hours_to_resolve", 12)
        if (not should_exit and config.get("thesis_recheck", True)
                and adverse_move >= config.get("thesis_recheck_trigger", 0.12)
                and time_ok):
            fresh = _recheck_estimate(m, p_yes, config)
            if fresh is not None:
                fresh_edge = (fresh - p_yes) if t["side"] == "YES" else ((1 - fresh) - p_no)
                if fresh_edge < config.get("thesis_exit_edge", -0.04):
                    should_exit = True
                    reason = f"thesis broken (fresh edge {fresh_edge:+.1%} on {t['side']})"
                else:
                    # Thesis intact — keep holding, refresh stored estimate so the
                    # take-profit math tracks our current view.
                    t["our_estimate"] = fresh

        # (3) LEGACY price stop-loss — OFF by default (it bled ROI historically).
        if not should_exit and config.get("stop_loss_enabled", False):
            unrealized = (current_price - entry) * t["shares"]
            if unrealized < -t["cost"] * config.get("stop_loss_pct", 0.50):
                should_exit = True
                reason = f"stop-loss triggered (unrealized: ${unrealized:.2f})"

        if should_exit:
            # Sell shares at current price
            proceeds = round(current_price * t["shares"], 2)
            profit = round(proceeds - t["cost"], 2)
            state["balance"] = round(state["balance"] + proceeds, 2)
            t["status"] = "resolved"
            t["won"] = profit > 0
            t["profit"] = profit
            t["exit_reason"] = reason
            state["resolved_pnl"] = round(state["resolved_pnl"] + profit, 2)
            exits.append({"trade": t, "profit": profit, "reason": reason})

    return exits


def _recheck_estimate(market, price_yes, config):
    """Run a cheap fresh ensemble pass on an open market for thesis re-checking.
    Returns a fresh P(YES) estimate, or None if analysis failed."""
    recheck_cfg = dict(config)
    recheck_cfg["ensemble_passes"] = config.get("thesis_recheck_passes", 2)
    question = market.get("question", "")
    description = market.get("description", "")
    volume = market.get("volumeNum", 0) or 0
    end_date = market.get("endDateIso", "")
    momentum = get_momentum(market)
    hours_left = get_hours_to_resolve(end_date)
    estimate, _ = ask_claude(
        question, description, price_yes, volume, end_date,
        momentum, hours_left, recheck_cfg
    )
    return estimate


# ── Cross-market correlation ─────────────────────────────────────

def find_correlated_markets(markets):
    """Detect markets that are semantically related (same underlying event).
    Returns groups of correlated market IDs.

    Uses keyword overlap as a simple proxy for semantic similarity.
    Full implementation would use embeddings, but this catches obvious pairs.
    """
    def extract_keywords(text):
        text = text.lower()
        # Remove common words
        stop = {"will", "the", "a", "an", "in", "on", "at", "to", "for", "of",
                "is", "be", "by", "it", "or", "and", "this", "that", "with"}
        words = set(re.findall(r'\b[a-z]{3,}\b', text)) - stop
        return words

    market_keywords = {}
    for m in markets:
        mid = m.get("id")
        if mid:
            market_keywords[mid] = extract_keywords(
                m.get("question", "") + " " + m.get("description", "")[:200]
            )

    groups = []
    seen = set()
    ids = list(market_keywords.keys())

    for i, id1 in enumerate(ids):
        if id1 in seen:
            continue
        group = [id1]
        kw1 = market_keywords[id1]
        for id2 in ids[i+1:]:
            if id2 in seen:
                continue
            kw2 = market_keywords[id2]
            if len(kw1) > 0 and len(kw2) > 0:
                overlap = len(kw1 & kw2) / min(len(kw1), len(kw2))
                if overlap > 0.5:  # >50% keyword overlap
                    group.append(id2)
                    seen.add(id2)
        if len(group) > 1:
            groups.append(group)
            seen.add(id1)

    return groups


def check_correlation_consistency(opportunities):
    """Flag opportunities where correlated markets have inconsistent estimates.
    Returns list of warnings."""
    warnings = []
    # Group by keywords (simple approach)
    for i, opp1 in enumerate(opportunities):
        for opp2 in opportunities[i+1:]:
            q1 = opp1["question"].lower()
            q2 = opp2["question"].lower()
            # Check for shared entity names
            words1 = set(re.findall(r'\b[A-Z][a-z]+\b', opp1["question"]))
            words2 = set(re.findall(r'\b[A-Z][a-z]+\b', opp2["question"]))
            common = words1 & words2
            if len(common) >= 2:  # share 2+ proper nouns
                # Check if estimates are consistent
                est_diff = abs(opp1["our_estimate"] - opp2["our_estimate"])
                if est_diff > 0.3:
                    warnings.append({
                        "markets": [opp1["market_id"], opp2["market_id"]],
                        "questions": [opp1["question"][:60], opp2["question"][:60]],
                        "estimates": [opp1["our_estimate"], opp2["our_estimate"]],
                        "warning": f"Large estimate gap ({est_diff:.0%}) between related markets",
                    })
    return warnings


# ── NegRisk arbitrage detection ──────────────────────────────────

def detect_negrisk_arbitrage(markets):
    """Detect multi-outcome markets where probabilities don't sum to 1.0.
    This represents risk-free arbitrage opportunity.

    Returns list of arbitrage opportunities with expected profit.
    """
    # Group markets by shared slug/topic (NegRisk markets often share a slug)
    slug_groups = {}
    for m in markets:
        slug = m.get("groupSlug") or m.get("slug", "")
        if slug:
            if slug not in slug_groups:
                slug_groups[slug] = []
            slug_groups[slug].append(m)

    arbitrages = []
    for slug, group in slug_groups.items():
        if len(group) < 2:
            continue

        # Sum YES prices across the group
        total_yes = 0
        valid_markets = []
        for m in group:
            p_yes, _ = parse_prices(m.get("outcomePrices", ""))
            if p_yes is not None:
                total_yes += p_yes
                valid_markets.append({"market": m, "price_yes": p_yes})

        if len(valid_markets) < 2:
            continue

        # If total > 1.0: buy all NO contracts (guaranteed one pays out)
        # If total < 1.0: buy all YES contracts (guaranteed one pays out)
        deviation = total_yes - 1.0

        if abs(deviation) > 0.02:  # >2% deviation = meaningful
            cost_of_arb = len(valid_markets) - total_yes if deviation < 0 else total_yes - 1.0
            arbitrages.append({
                "slug": slug,
                "num_markets": len(valid_markets),
                "total_yes_price": round(total_yes, 4),
                "deviation": round(deviation, 4),
                "direction": "buy_all_YES" if deviation < 0 else "buy_all_NO",
                "arb_profit_pct": round(abs(deviation) / len(valid_markets) * 100, 2),
                "markets": [{
                    "id": vm["market"]["id"],
                    "question": vm["market"].get("question", "")[:60],
                    "price_yes": vm["price_yes"],
                } for vm in valid_markets],
            })

    return sorted(arbitrages, key=lambda x: abs(x["deviation"]), reverse=True)


# ── Calibration report ───────────────────────────────────────────

def get_calibration_report():
    """Generate a full calibration report for display."""
    cal = load_calibration()
    brier = compute_brier_score(cal)
    if not brier:
        return {"status": "insufficient_data", "n": len(cal["predictions"]),
                "resolved": len([p for p in cal["predictions"] if p["outcome"] is not None])}

    report = {
        "status": "ok",
        **brier,
        "platt_a": cal.get("platt_a", 1.0),
        "platt_b": cal.get("platt_b", 0.0),
        "total_predictions": len(cal["predictions"]),
        "beating_market": brier["brier"] < brier["market_brier"],
        "edge_over_market": round(brier["market_brier"] - brier["brier"], 4),
    }

    # Diagnosis
    if brier["reliability"] > 0.02:
        report["diagnosis"] = "Poor calibration — predictions are systematically biased. Platt scaling should help."
    elif brier["resolution"] < 0.01:
        report["diagnosis"] = "Good calibration but low resolution — not differentiating enough between events. Need better features/analysis."
    elif brier["brier"] < brier["market_brier"]:
        report["diagnosis"] = "Outperforming the market! Keep current approach."
    else:
        report["diagnosis"] = "Underperforming market. Consider reducing bet sizes until calibration improves."

    return report


# ── Chart data ───────────────────────────────────────────────────

def get_chart_data(state):
    """Pre-compute chart data for the dashboard."""
    resolved = [t for t in state["trades"] if t["status"] == "resolved"]
    open_trades = [t for t in state["trades"] if t["status"] == "open"]

    # Sort resolved by timestamp
    resolved_sorted = sorted(resolved, key=lambda t: t.get("timestamp", ""))

    # P&L series: cumulative P&L over time
    pnl_series = []
    cumulative = 0
    for t in resolved_sorted:
        profit = t.get("profit", 0)
        cumulative += profit
        pnl_series.append({
            "ts": t.get("timestamp", "")[:16],  # trim to minute
            "pnl": round(profit, 2),
            "cumulative": round(cumulative, 2),
            "question": t.get("question", "")[:40],
        })

    # Trade bars: profit per resolved trade
    trade_bars = []
    for t in resolved_sorted:
        trade_bars.append({
            "question": t.get("question", "")[:25],
            "profit": round(t.get("profit", 0), 2),
            "won": t.get("won", False),
            "side": t.get("side", ""),
        })

    # Allocation: open positions by cost
    allocation = []
    colors = ["#3b82f6", "#a855f7", "#06b6d4", "#22c55e", "#eab308",
              "#ef4444", "#f97316", "#ec4899", "#6366f1", "#14b8a6"]
    for i, t in enumerate(open_trades):
        allocation.append({
            "question": t.get("question", "")[:30],
            "cost": round(t.get("cost", 0), 2),
            "side": t.get("side", ""),
            "color": colors[i % len(colors)],
        })

    # Cash portion for allocation
    cash = state.get("balance", 0)
    total_deployed = sum(a["cost"] for a in allocation)
    allocation.append({
        "question": "Cash",
        "cost": round(cash, 2),
        "side": "",
        "color": "#2a2b38",
    })

    return {
        "pnl_series": pnl_series,
        "trade_bars": trade_bars,
        "allocation": allocation,
        "summary": {
            "total_deployed": round(total_deployed, 2),
            "cash": round(cash, 2),
            "total_pnl": round(cumulative, 2),
            "wins": sum(1 for t in resolved if t.get("won")),
            "losses": sum(1 for t in resolved if not t.get("won")),
        },
    }
