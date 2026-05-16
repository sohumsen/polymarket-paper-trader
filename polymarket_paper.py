"""
Polymarket Paper Trading Simulator v3
Uses real market data + Claude CLI for analysis. No accounts needed.
Now powered by engine.py v3 with ensemble, calibration, and advanced bet sizing.
"""

import json
import subprocess
import sys
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

import requests
import engine

GAMMA_API = "https://gamma-api.polymarket.com/markets"
TRADES_FILE = Path(__file__).parent / "trades.json"
CONFIG = dict(engine.DEFAULT_CONFIG)
STARTING_BALANCE = CONFIG["starting_balance"]
MIN_EDGE = CONFIG["min_edge"]
MAX_BET_FRACTION = CONFIG["max_bet_fraction"]
MAX_DEPLOYED_PCT = CONFIG["max_deployed_pct"]
MARKETS_TO_SCAN = CONFIG["markets_to_scan"]
MIN_CONFIDENCE = CONFIG["min_confidence"]


# ── ANSI colors ───────────────────────────────────────────────────

class C:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    GREEN   = "\033[92m"
    RED     = "\033[91m"
    YELLOW  = "\033[93m"
    BLUE    = "\033[94m"
    CYAN    = "\033[96m"
    MAGENTA = "\033[95m"
    WHITE   = "\033[97m"
    GRAY    = "\033[90m"
    BG_BLUE   = "\033[44m"
    BG_GREEN  = "\033[42m"
    BG_RED    = "\033[41m"
    BG_YELLOW = "\033[43m"
    BG_GRAY   = "\033[100m"

    @staticmethod
    def pnl(val):
        if val > 0.005:
            return f"{C.GREEN}+${val:,.2f}{C.RESET}"
        elif val < -0.005:
            return f"{C.RED}-${abs(val):,.2f}{C.RESET}"
        return f"{C.GRAY}$0.00{C.RESET}"

    @staticmethod
    def pnl_pct(val):
        if val > 0.05:
            return f"{C.GREEN}+{val:.1f}%{C.RESET}"
        elif val < -0.05:
            return f"{C.RED}{val:.1f}%{C.RESET}"
        return f"{C.GRAY}0.0%{C.RESET}"

    @staticmethod
    def edge_color(edge):
        if edge >= 0.15:
            return C.GREEN + C.BOLD
        elif edge >= 0.08:
            return C.GREEN
        elif edge >= 0.05:
            return C.YELLOW
        return C.GRAY


def enable_ansi_windows():
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


# ── Spinner ───────────────────────────────────────────────────────

class Spinner:
    FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, text=""):
        self.text = text
        self._stop = threading.Event()
        self._thread = None

    def _spin(self):
        i = 0
        while not self._stop.is_set():
            frame = self.FRAMES[i % len(self.FRAMES)]
            line = f"\r  {C.CYAN}{frame}{C.RESET} {self.text}"
            sys.stdout.write(line)
            sys.stdout.flush()
            i += 1
            self._stop.wait(0.1)

    def start(self):
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join()
        # Clear spinner line
        sys.stdout.write(f"\r{' ' * (len(self.text) + 10)}\r")
        sys.stdout.flush()


# ── Data persistence ──────────────────────────────────────────────

def load_state():
    if TRADES_FILE.exists():
        with open(TRADES_FILE) as f:
            return json.load(f)
    return {"balance": STARTING_BALANCE, "trades": [], "resolved_pnl": 0.0}


def save_state(state):
    with open(TRADES_FILE, "w") as f:
        json.dump(state, f, indent=2)


def deployed_amount(state):
    return sum(t["cost"] for t in state["trades"] if t["status"] == "open")


# ── Polymarket API ────────────────────────────────────────────────

def parse_prices(raw):
    """Safely parse outcomePrices from API (can be string or list)."""
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


def fetch_markets(limit=MARKETS_TO_SCAN):
    """Fetch a diverse mix of markets: high volume + trending + newer."""
    all_markets = {}

    # Batch 1: highest 24h volume (most liquid, but most efficient)
    for order_by in ["volume24hr", "volume1wk", "startDate"]:
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

    if not all_markets:
        print(f"  {C.RED}Error fetching markets.{C.RESET}")
        return []

    # Return up to limit, preferring diversity
    return list(all_markets.values())[:limit]


def fetch_market_by_id(market_id):
    try:
        r = requests.get(f"{GAMMA_API}?id={market_id}", timeout=10)
        r.raise_for_status()
        data = r.json()
        return data[0] if data else None
    except requests.RequestException:
        return None


# ── Claude analysis ───────────────────────────────────────────────

def _parse_last_probability(raw):
    """Extract the LAST decimal probability from Claude's response.

    CoT reasoning contains many numbers. We want the final answer,
    which should be on the last non-empty line.
    """
    if not raw:
        return None
    # Search lines in reverse for a standalone decimal
    lines = [l.strip() for l in raw.strip().split("\n") if l.strip()]
    for line in reversed(lines):
        # Try to match a standalone decimal like 0.72 or .65
        match = re.search(r'\b(0?\.\d+)\b', line)
        if match:
            val = float(match.group(1))
            if 0.01 <= val <= 0.99:
                return val
        # Also try percentage like 72%
        match = re.search(r'\b(\d{1,2})%', line)
        if match:
            val = int(match.group(1)) / 100
            if 0.01 <= val <= 0.99:
                return val
    return None


def _call_claude(prompt, timeout=60):
    try:
        result = subprocess.run(
            ["claude", "--print", prompt],
            capture_output=True, text=True, timeout=timeout
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return None
    except FileNotFoundError:
        print(f"\n  {C.RED}Error: 'claude' CLI not found. Is it installed?{C.RESET}")
        return None


def ask_claude(question, description, price_yes, volume=0, end_date="", momentum=None):
    """Two-pass analysis with independent estimates (no anchoring)."""

    # Build momentum context
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

    # Time horizon
    time_str = ""
    if end_date:
        try:
            end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            days = (end - datetime.now(timezone.utc)).days
            if days > 0:
                time_str = f"Resolves in: {days} days\n"
            else:
                time_str = "Resolves: very soon (possibly today)\n"
        except (ValueError, TypeError):
            pass

    # ── Pass 1: Deep chain-of-thought analysis ──
    prompt1 = (
        f"You are an expert prediction market analyst and calibrated forecaster. "
        f"Your job is to find MISPRICINGS in prediction markets.\n\n"
        f"=== MARKET ===\n"
        f"Question: {question}\n"
        f"Description: {description[:1000]}\n"
        f"Current market price: {price_yes:.1%} YES / {1-price_yes:.1%} NO\n"
        f"24h trading volume: ${volume:,.0f}\n"
        f"{time_str}"
        f"{momentum_str}\n"
        f"=== ANALYSIS FRAMEWORK ===\n"
        f"Think through these steps:\n"
        f"1. QUESTION PARSING: What exactly needs to happen for YES to pay out?\n"
        f"2. BASE RATE: For this type of event, what's the historical frequency?\n"
        f"3. EVIDENCE: What specific facts push the probability up or down?\n"
        f"4. MARKET EFFICIENCY: With ${volume:,.0f} volume, how likely is a mispricing?\n"
        f"5. BLIND SPOTS: What might the market be overlooking or overweighting?\n"
        f"6. CALIBRATION: Am I being overconfident? Extreme probabilities (>90% or <10%) "
        f"are rarely correct unless the evidence is overwhelming.\n\n"
        f"IMPORTANT: Do NOT just agree with the market price. Form your OWN view first, "
        f"then compare. But also don't be contrarian without reason.\n\n"
        f"After your full reasoning, write your final probability estimate on the LAST "
        f"LINE as a decimal between 0.01 and 0.99. ONLY the number on that line."
    )

    raw1 = _call_claude(prompt1)
    if not raw1:
        return None, 0
    est1 = _parse_last_probability(raw1)
    if est1 is None:
        return None, 0

    # ── Pass 2: Independent estimate (NO knowledge of pass 1) ──
    prompt2 = (
        f"Quick probability estimate for a prediction market.\n\n"
        f"Question: \"{question}\"\n"
        f"{time_str}"
        f"{momentum_str}"
        f"What is the probability this resolves YES?\n"
        f"Think briefly, then reply with ONLY a decimal (e.g. 0.65) on the last line."
    )
    raw2 = _call_claude(prompt2, timeout=30)
    est2 = _parse_last_probability(raw2) if raw2 else None

    # ── Combine estimates ──
    if est2 is not None:
        # Weighted average: pass 1 (deep analysis) gets more weight
        final = est1 * 0.65 + est2 * 0.35
        # Confidence = agreement between passes (1.0 = perfect agreement)
        disagreement = abs(est1 - est2)
        if disagreement < 0.03:
            confidence = 0.95   # very strong agreement
        elif disagreement < 0.08:
            confidence = 0.80
        elif disagreement < 0.15:
            confidence = 0.65
        else:
            confidence = 0.45   # major disagreement — don't trust
    else:
        final = est1
        confidence = 0.55       # single pass — moderate confidence

    # High-volume markets: reduce CONFIDENCE (not the estimate!)
    # The estimate should reflect true probability. Confidence reflects
    # how likely we are to actually have edge over an efficient market.
    if volume > 2_000_000:
        confidence *= 0.6
    elif volume > 1_000_000:
        confidence *= 0.7
    elif volume > 500_000:
        confidence *= 0.85

    return round(final, 4), round(confidence, 2)


# ── Trading logic ─────────────────────────────────────────────────

def kelly_size(edge, odds, balance, confidence=1.0):
    """Half-Kelly bet sizing scaled by confidence."""
    if edge <= 0 or odds <= 0:
        return 0
    kelly_fraction = edge / odds
    # Scale by confidence and use half-Kelly for conservatism
    adjusted = kelly_fraction * confidence * 0.5
    capped = min(adjusted, MAX_BET_FRACTION)
    return round(max(balance * capped, 0), 2)


def days_until(date_str, colored=True):
    if not date_str:
        return "unknown"
    try:
        end = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = end - now
        days = delta.days
        if not colored:
            if days < 0: return "ended"
            if days == 0: return f"{delta.seconds // 3600}h"
            return f"{days}d"
        if days < 0:
            return f"{C.RED}ended{C.RESET}"
        if days == 0:
            h = delta.seconds // 3600
            return f"{C.RED}{'< 1h' if h == 0 else f'{h}h'}{C.RESET}"
        if days <= 2:
            return f"{C.RED}{days}d{C.RESET}"
        if days < 7:
            return f"{C.YELLOW}{days}d{C.RESET}"
        if days < 30:
            return f"{C.CYAN}{days}d{C.RESET}"
        return f"{C.CYAN}{days // 30}mo{C.RESET}"
    except (ValueError, TypeError):
        return "unknown"


def opportunity_score(opp):
    """Composite score 0-100: edge * confidence * volume sweet spot."""
    edge = opp.get("edge", 0)
    conf = opp.get("confidence", 0.5)
    vol = opp.get("volume", 0)

    # Edge component (0-40)
    edge_score = min(edge / 0.20, 1.0) * 40

    # Confidence component (0-35)
    conf_score = min(conf / 0.95, 1.0) * 35

    # Volume component (0-25): medium volume = sweet spot
    if vol < 10_000:
        vol_score = 5
    elif vol < 50_000:
        vol_score = 15
    elif vol < 200_000:
        vol_score = 25     # sweet spot
    elif vol < 500_000:
        vol_score = 20
    elif vol < 1_000_000:
        vol_score = 12
    else:
        vol_score = 6       # very efficient

    return round(edge_score + conf_score + vol_score)


def get_momentum(market):
    """Extract price momentum from market data."""
    m = {}
    try:
        v = market.get("oneHourPriceChange")
        if v is not None:
            m["1h"] = float(v)
    except (ValueError, TypeError):
        pass
    try:
        v = market.get("oneWeekPriceChange")
        if v is not None:
            m["1w"] = float(v)
    except (ValueError, TypeError):
        pass
    try:
        v = market.get("oneMonthPriceChange")
        if v is not None:
            m["1m"] = float(v)
    except (ValueError, TypeError):
        pass
    return m if m else None


def analyze_market(market, counter=""):
    """Analyze a single market. Returns opportunity dict or None."""
    question = market.get("question", "")
    description = market.get("description", "")
    prices_raw = market.get("outcomePrices", "")
    volume = market.get("volumeNum", 0) or 0
    end_date = market.get("endDateIso", "")

    price_yes, price_no = parse_prices(prices_raw)
    if price_yes is None or not question:
        return None

    # Skip near-certain markets (no edge possible)
    if price_yes < 0.08 or price_yes > 0.92:
        return None

    # Show spinner while Claude thinks
    short_q = question[:45]
    spinner = Spinner(f"{counter}Analyzing: {short_q}...")
    spinner.start()

    momentum = get_momentum(market)
    estimate, confidence = ask_claude(
        question, description, price_yes, volume, end_date, momentum
    )
    spinner.stop()

    if estimate is None:
        print(f"  {C.GRAY}{counter}{short_q}... (Claude unavailable){C.RESET}")
        return None

    if confidence < MIN_CONFIDENCE:
        print(f"  {C.GRAY}{counter}{short_q}... "
              f"(low conf: {confidence:.0%}){C.RESET}")
        return None

    # Calculate edge for both sides
    edge_yes = estimate - price_yes
    edge_no = (1 - estimate) - price_no

    # Pick best side
    if edge_yes >= edge_no and edge_yes > MIN_EDGE:
        side, edge, entry_price = "YES", edge_yes, price_yes
    elif edge_no > MIN_EDGE:
        side, edge, entry_price = "NO", edge_no, price_no
    else:
        print(f"  {C.GRAY}{counter}{short_q}... "
              f"(no edge: est {estimate:.0%} vs mkt {price_yes:.0%}){C.RESET}")
        return None

    ec = C.edge_color(edge)
    print(f"  {ec}{counter}{short_q}... "
          f"edge {edge:+.0%}  conf {confidence:.0%}{C.RESET}")

    return {
        "market_id": market.get("id"),
        "question": question,
        "side": side,
        "entry_price": entry_price,
        "our_estimate": estimate,
        "confidence": confidence,
        "market_price_yes": price_yes,
        "edge": round(edge, 4),
        "volume": volume,
        "end_date": end_date,
        "momentum": momentum,
    }


# ── CLI interface ─────────────────────────────────────────────────

def print_header(state):
    open_trades = [t for t in state["trades"] if t["status"] == "open"]
    open_count = len(open_trades)
    open_cost = sum(t["cost"] for t in open_trades)
    resolved = [t for t in state["trades"] if t["status"] == "resolved"]
    total_value = state["balance"] + open_cost + state["resolved_pnl"]
    pnl = total_value - STARTING_BALANCE
    pnl_pct = (pnl / STARTING_BALANCE) * 100

    clear_screen()
    print()
    print(f"  {C.BG_BLUE}{C.WHITE}{C.BOLD}                                                          {C.RESET}")
    print(f"  {C.BG_BLUE}{C.WHITE}{C.BOLD}   POLYMARKET PAPER TRADER  v3     powered by Claude CLI   {C.RESET}")
    print(f"  {C.BG_BLUE}{C.WHITE}{C.BOLD}                                                          {C.RESET}")
    print()

    print(f"  {C.BOLD}Cash{C.RESET}       {C.WHITE}${state['balance']:>10,.2f}{C.RESET}")
    print(f"  {C.BOLD}In bets{C.RESET}    {C.CYAN}${open_cost:>10,.2f}{C.RESET}  {C.DIM}({open_count} open){C.RESET}")
    print(f"  {C.BOLD}Realized{C.RESET}   {C.pnl(state['resolved_pnl'])}")
    print(f"  {C.BOLD}Total P&L{C.RESET}  {C.pnl(pnl)}  {C.pnl_pct(pnl_pct)}")

    # Deployment bar
    bar_w = 40
    dep_pct = open_cost / STARTING_BALANCE if STARTING_BALANCE > 0 else 0
    filled = int(bar_w * min(dep_pct, 1.0))
    limit_mark = int(bar_w * MAX_DEPLOYED_PCT)
    bar_chars = []
    for i in range(bar_w):
        if i < filled:
            bar_chars.append(f"{C.CYAN}█")
        elif i == limit_mark:
            bar_chars.append(f"{C.YELLOW}│")
        else:
            bar_chars.append(f"{C.GRAY}░")
    bar = "".join(bar_chars) + C.RESET
    print(f"\n  {C.DIM}Deployed:{C.RESET} {bar} {dep_pct:.0%} {C.DIM}(max {MAX_DEPLOYED_PCT:.0%}){C.RESET}")

    # Quick stats if we have history
    if resolved:
        wins = sum(1 for t in resolved if t.get("won"))
        wr = wins / len(resolved) * 100
        print(f"  {C.DIM}Record:{C.RESET}   {C.GREEN}{wins}W{C.RESET} / "
              f"{C.RED}{len(resolved)-wins}L{C.RESET}  "
              f"({wr:.0f}% win rate)")

    print(f"\n  {C.DIM}{'─' * 58}{C.RESET}")


def cmd_scan(state):
    print(f"\n  {C.CYAN}{C.BOLD}Fetching live markets from Polymarket...{C.RESET}")
    passes = CONFIG.get("ensemble_passes", 3)
    print(f"  {C.DIM}v3 engine: {passes}-persona ensemble | quarter-Kelly | calibrated{C.RESET}")
    markets = engine.fetch_markets(CONFIG["markets_to_scan"])
    if not markets:
        print(f"  {C.RED}No markets found.{C.RESET}")
        return

    # Check deployment limit
    current_deployed = deployed_amount(state)
    max_deploy = STARTING_BALANCE * MAX_DEPLOYED_PCT
    available = max_deploy - current_deployed
    if available < 5:
        print(f"  {C.YELLOW}Deployment limit reached ({current_deployed/STARTING_BALANCE:.0%} deployed). "
              f"Wait for bets to resolve or increase limit.{C.RESET}")
        return

    # Skip markets we already have open positions in
    open_ids = {t["market_id"] for t in state["trades"] if t["status"] == "open"}
    markets = [m for m in markets if m.get("id") not in open_ids]

    total = len(markets)
    if total == 0:
        print(f"  {C.YELLOW}All fetched markets already have open positions.{C.RESET}")
        return

    print(f"  {C.DIM}Scanning {total} markets ({passes}-pass ensemble each)...{C.RESET}\n")

    opportunities = []
    for idx, m in enumerate(markets):
        short_q = m.get("question", "")[:45]
        counter = f"[{idx+1}/{total}] "
        spinner = Spinner(f"{counter}Analyzing: {short_q}...")
        spinner.start()

        opp = engine.analyze_market(m, CONFIG)

        spinner.stop()
        if opp:
            cat = opp.get("category", "other")
            ec = C.edge_color(opp["edge"])
            print(f"  {ec}{counter}{short_q}... "
                  f"edge {opp['edge']:+.0%}  conf {opp['confidence']:.0%}  [{cat}]{C.RESET}")
            opportunities.append(opp)
        else:
            print(f"  {C.GRAY}{counter}{short_q}... (no edge){C.RESET}")

    # Check cross-market correlations
    warnings = engine.check_correlation_consistency(opportunities)
    if warnings:
        print(f"\n  {C.YELLOW}{C.BOLD}Correlation warnings:{C.RESET}")
        for w in warnings:
            print(f"  {C.YELLOW}  {w['warning']}{C.RESET}")

    print(f"\n  {C.DIM}Scanned: {total} | Found: {len(opportunities)} opportunities{C.RESET}")

    if not opportunities:
        print(f"  {C.YELLOW}No opportunities with sufficient edge and confidence.{C.RESET}")
        return

    opportunities.sort(key=lambda x: x.get("score", 0), reverse=True)
    print(f"\n  {C.GREEN}{C.BOLD}{'═' * 58}")
    print(f"  OPPORTUNITIES (sorted by score)")
    print(f"  {'═' * 58}{C.RESET}\n")

    for i, opp in enumerate(opportunities, 1):
        conf = opp.get("confidence", 0.5)
        score = opp.get("score", 0)
        size = engine.kelly_size(opp["edge"], opp["entry_price"],
                                 state["balance"], conf, CONFIG["max_bet_fraction"], CONFIG)
        size = min(size, available)
        ec = C.edge_color(opp["edge"])
        sc = C.GREEN if opp["side"] == "YES" else C.RED
        tl = days_until(opp.get("end_date", ""))
        cat = opp.get("category", "other")

        # Score badge
        if score >= 70:
            sb = f"{C.BG_GREEN}{C.WHITE}{C.BOLD} {score:>2} {C.RESET}"
        elif score >= 50:
            sb = f"{C.BG_YELLOW}{C.WHITE}{C.BOLD} {score:>2} {C.RESET}"
        else:
            sb = f"{C.BG_GRAY}{C.WHITE} {score:>2} {C.RESET}"

        # Confidence mini-bar
        cb = int(conf * 5)
        conf_bar = f"{C.GREEN}{'█' * cb}{C.GRAY}{'░' * (5 - cb)}{C.RESET}"

        # ROI if win
        roi = ((1.0 / opp["entry_price"]) - 1) * 100

        # Momentum indicator
        mom = opp.get("momentum", {})
        mom_str = ""
        if mom:
            h1 = mom.get("1h")
            if h1 is not None:
                arrow = f"{C.GREEN}▲" if h1 > 0 else f"{C.RED}▼" if h1 < 0 else f"{C.GRAY}─"
                mom_str = f" {arrow}{C.RESET}"

        print(f"  {sb} {C.BOLD}{C.WHITE}[{i}]{C.RESET} {opp['question'][:46]}")
        print(f"       Market: {C.CYAN}{opp['market_price_yes']:.0%}{C.RESET}{mom_str}  "
              f"{C.DIM}|{C.RESET}  Claude: {C.MAGENTA}{opp['our_estimate']:.0%}{C.RESET}  "
              f"{C.DIM}|{C.RESET}  {sc}{C.BOLD}{opp['side']}{C.RESET}  "
              f"{C.DIM}[{cat}]{C.RESET}")
        print(f"       Edge: {ec}{opp['edge']:+.0%}{C.RESET}  "
              f"{C.DIM}|{C.RESET}  Conf: {conf_bar} {conf:.0%}  "
              f"{C.DIM}|{C.RESET}  ROI: {C.GREEN}{roi:.0f}%{C.RESET}")
        print(f"       Bet: {C.YELLOW}${size:.2f}{C.RESET}  "
              f"{C.DIM}|{C.RESET}  Vol: {C.DIM}${opp.get('volume',0):,.0f}{C.RESET}  "
              f"{C.DIM}|{C.RESET}  Resolves: {tl}")
        print()

    # Allow multiple trades
    traded = set()

    while True:
        n = len(opportunities)
        remaining = min(state["balance"], max_deploy - deployed_amount(state))
        if remaining < 1:
            print(f"  {C.YELLOW}No more funds available.{C.RESET}")
            break

        if traded:
            print(f"  {C.DIM}Already placed: {', '.join(str(i+1) for i in sorted(traded))}{C.RESET}")

        prompt_text = (f"  {C.BOLD}Trade? {C.RESET}{C.DIM}# / "
                       f"multiple (1,2,3) / 'all' / 'done': {C.RESET}")
        choice = input(prompt_text).strip().lower()

        if choice in ("done", "d", "skip", "s", ""):
            break

        elif choice == "all":
            for idx, opp in enumerate(opportunities):
                if idx in traded:
                    continue
                avail = min(state["balance"], max_deploy - deployed_amount(state))
                if avail < 1:
                    break
                conf = opp.get("confidence", 0.5)
                size = engine.kelly_size(opp["edge"], opp["entry_price"],
                                         state["balance"], conf, CONFIG["max_bet_fraction"], CONFIG)
                size = min(size, avail)
                if size >= 1:
                    _execute_trade(state, opp, size)
                    traded.add(idx)
            break

        else:
            nums = re.split(r'[,\s]+', choice)
            placed_any = False
            for num_str in nums:
                try:
                    idx = int(num_str) - 1
                except ValueError:
                    print(f"  {C.YELLOW}'{num_str}' isn't a number.{C.RESET}")
                    continue

                if idx < 0 or idx >= n:
                    print(f"  {C.YELLOW}#{num_str} out of range (1-{n}).{C.RESET}")
                    continue

                if idx in traded:
                    print(f"  {C.GRAY}#{num_str} already traded.{C.RESET}")
                    continue

                avail = min(state["balance"], max_deploy - deployed_amount(state))
                if avail < 1:
                    print(f"  {C.YELLOW}No more funds.{C.RESET}")
                    break

                opp = opportunities[idx]
                conf = opp.get("confidence", 0.5)
                size = engine.kelly_size(opp["edge"], opp["entry_price"],
                                         state["balance"], conf, CONFIG["max_bet_fraction"], CONFIG)
                size = min(size, avail)

                if size < 1:
                    print(f"  {C.GRAY}#{num_str} bet too small.{C.RESET}")
                    continue

                if len(nums) == 1:
                    if place_trade(state, opp):
                        traded.add(idx)
                        placed_any = True
                else:
                    _execute_trade(state, opp, size)
                    traded.add(idx)
                    placed_any = True

            if not placed_any and len(nums) > 1:
                print(f"  {C.GRAY}No trades placed.{C.RESET}")


def _execute_trade(state, opp, size):
    """Execute a trade without prompting."""
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
    }
    state["balance"] -= round(size, 2)
    state["trades"].append(trade)
    save_state(state)
    print(f"  {C.GREEN}Bought {shares:.1f} sh {opp['side']} @ "
          f"${opp['entry_price']:.2f} for ${size:.2f}{C.RESET}  "
          f"{C.DIM}{opp['question'][:35]}{C.RESET}")


def place_trade(state, opp):
    """Interactive single trade with preview. Returns True if placed."""
    conf = opp.get("confidence", 0.5)
    max_avail = min(
        state["balance"],
        STARTING_BALANCE * MAX_DEPLOYED_PCT - deployed_amount(state)
    )
    size = engine.kelly_size(opp["edge"], opp["entry_price"],
                             state["balance"], conf, CONFIG["max_bet_fraction"], CONFIG)
    size = min(size, max_avail)

    if size < 1:
        print(f"  {C.YELLOW}Bet too small or deployment limit reached.{C.RESET}")
        return False

    shares = size / opp["entry_price"]
    max_payout = shares * 1.0
    roi = ((max_payout / size) - 1) * 100

    print(f"\n  {C.DIM}{'─' * 54}{C.RESET}")
    print(f"  {C.BOLD}{C.WHITE}Trade Preview{C.RESET}")
    print(f"  {C.DIM}{'─' * 54}{C.RESET}")
    print(f"  Market:    {opp['question'][:44]}")
    sc = C.GREEN if opp["side"] == "YES" else C.RED
    print(f"  Side:      {sc}{C.BOLD}{opp['side']}{C.RESET} @ ${opp['entry_price']:.2f}")
    print(f"  Cost:      {C.YELLOW}${size:.2f}{C.RESET}  ({shares:.1f} shares)")
    print(f"  If win:    {C.GREEN}${max_payout:.2f}{C.RESET}  "
          f"({C.GREEN}+${max_payout - size:.2f}, +{roi:.0f}%{C.RESET})")
    print(f"  If lose:   {C.RED}-${size:.2f}{C.RESET}")
    print(f"  Edge:      {C.edge_color(opp['edge'])}{opp['edge']:+.0%}{C.RESET}  "
          f"Confidence: {conf:.0%}")
    print(f"  {C.DIM}{'─' * 54}{C.RESET}")

    choice = input(f"\n  {C.BOLD}Confirm?{C.RESET} "
                   f"{C.DIM}(y / amount / n): {C.RESET}").strip().lower()
    if choice in ("", "n", "no"):
        print(f"  {C.GRAY}Skipped.{C.RESET}")
        return False
    if choice not in ("y", "yes"):
        try:
            size = min(float(choice), max_avail)
            shares = size / opp["entry_price"]
        except ValueError:
            print(f"  {C.RED}Invalid.{C.RESET}")
            return False

    if size > state["balance"]:
        print(f"  {C.RED}Not enough cash (${state['balance']:.2f}).{C.RESET}")
        return False

    _execute_trade(state, opp, size)
    print(f"\n  {C.BG_GREEN}{C.WHITE}{C.BOLD} TRADE PLACED {C.RESET}")
    return True
    print(f"  {C.DIM}Cash remaining: ${state['balance']:.2f}{C.RESET}")


def cmd_portfolio(state):
    open_trades = [t for t in state["trades"] if t["status"] == "open"]
    if not open_trades:
        print(f"\n  {C.GRAY}No open positions.{C.RESET}")
        return

    total_cost = 0
    total_unrealized = 0

    print(f"\n  {C.BG_BLUE}{C.WHITE}{C.BOLD} OPEN POSITIONS ({len(open_trades)}) {C.RESET}\n")

    for i, t in enumerate(open_trades, 1):
        m = fetch_market_by_id(t["market_id"])
        cur_price = None
        unrealized = 0
        current_pct = "?"
        arrow = ""

        if m:
            p_yes, p_no = parse_prices(m.get("outcomePrices", ""))
            if p_yes is not None:
                cur_price = p_yes if t["side"] == "YES" else p_no
                current_pct = f"{cur_price:.0%}"
                unrealized = (cur_price - t["entry_price"]) * t["shares"]
                if cur_price > t["entry_price"] + 0.01:
                    arrow = f" {C.GREEN}▲{C.RESET}"
                elif cur_price < t["entry_price"] - 0.01:
                    arrow = f" {C.RED}▼{C.RESET}"
                else:
                    arrow = f" {C.GRAY}─{C.RESET}"

        total_cost += t["cost"]
        total_unrealized += unrealized

        est = t.get("our_estimate", t["entry_price"])
        win_prob = (1 - est) if t["side"] == "NO" else est
        max_payout = t["shares"] * 1.0
        expected = (win_prob * max_payout) - t["cost"]
        roi = ((max_payout / t["cost"]) - 1) * 100 if t["cost"] > 0 else 0

        tl = days_until(t.get("end_date", ""))
        placed = t.get("timestamp", "")[:10]
        sc = C.GREEN if t["side"] == "YES" else C.RED

        print(f"  {C.BOLD}{C.WHITE}[{i}]{C.RESET} {t['question'][:53]}")
        print(f"  {C.DIM}├{C.RESET} {sc}{C.BOLD}{t['side']}{C.RESET} "
              f"{t['shares']:.1f} shares @ ${t['entry_price']:.2f}  "
              f"{C.DIM}|{C.RESET}  Cost: {C.YELLOW}${t['cost']:.2f}{C.RESET}")
        print(f"  {C.DIM}├{C.RESET} Price: {current_pct}{arrow} "
              f"(entry {t['entry_price']:.0%})  "
              f"{C.DIM}|{C.RESET}  Unreal: {C.pnl(unrealized)}")
        print(f"  {C.DIM}├{C.RESET} If win: {C.GREEN}${max_payout:.2f}{C.RESET} "
              f"({C.GREEN}+{roi:.0f}%{C.RESET})  "
              f"{C.DIM}|{C.RESET}  EV: {C.pnl(expected)}")
        print(f"  {C.DIM}└{C.RESET} Resolves: {tl}  "
              f"{C.DIM}|{C.RESET}  {C.DIM}Placed {placed}{C.RESET}")
        print()

    print(f"  {C.DIM}{'─' * 58}{C.RESET}")
    print(f"  {C.BOLD}Invested:{C.RESET}     {C.CYAN}${total_cost:.2f}{C.RESET}   "
          f"{C.DIM}|{C.RESET}   Unrealized: {C.pnl(total_unrealized)}   "
          f"{C.DIM}|{C.RESET}   Value: ${total_cost + total_unrealized:.2f}")
    print()


def cmd_check_resolved(state):
    open_trades = [t for t in state["trades"] if t["status"] == "open"]
    if not open_trades:
        print(f"\n  {C.GRAY}No open positions to check.{C.RESET}")
        return

    spinner = Spinner(f"Checking {len(open_trades)} positions...")
    spinner.start()

    # Use engine.check_resolved which also updates calibration data
    results = engine.check_resolved(state)

    spinner.stop()

    if not results:
        print(f"  {C.GRAY}No markets have resolved yet. Check back later.{C.RESET}")
        return

    print(f"\n  {C.BOLD}{'═' * 50}{C.RESET}")
    for r in results:
        t = r["trade"]
        won = r["won"]
        profit = r["profit"]
        if won:
            print(f"  {C.BG_GREEN}{C.WHITE}{C.BOLD} WIN  {C.RESET} "
                  f"{t['question'][:38]}  {C.GREEN}+${profit:.2f}{C.RESET}")
        else:
            print(f"  {C.BG_RED}{C.WHITE}{C.BOLD} LOSS {C.RESET} "
                  f"{t['question'][:38]}  {C.RED}-${abs(profit):.2f}{C.RESET}")
    print(f"  {C.BOLD}{'═' * 50}{C.RESET}")

    net = sum(r["profit"] for r in results)
    print(f"\n  {C.BOLD}{len(results)} resolved{C.RESET}  |  Net: {C.pnl(net)}")
    print(f"  {C.DIM}Calibration data updated. Run [8] to see accuracy report.{C.RESET}")
    save_state(state)


def cmd_history(state):
    resolved = [t for t in state["trades"] if t["status"] == "resolved"]
    if not resolved:
        print(f"\n  {C.GRAY}No resolved trades yet.{C.RESET}")
        return

    wins = sum(1 for t in resolved if t.get("won"))
    losses = len(resolved) - wins
    total_pnl = sum(t.get("profit", 0) for t in resolved)
    wr = (wins / len(resolved) * 100) if resolved else 0
    avg_win = (sum(t["profit"] for t in resolved if t.get("won")) / wins) if wins else 0
    avg_loss = (sum(t["profit"] for t in resolved if not t.get("won")) / losses) if losses else 0

    print(f"\n  {C.BG_BLUE}{C.WHITE}{C.BOLD} TRADE HISTORY {C.RESET}\n")

    # Stats row
    print(f"  {C.GREEN}{C.BOLD}{wins}W{C.RESET} / {C.RED}{C.BOLD}{losses}L{C.RESET}  "
          f"{C.DIM}|{C.RESET}  Win rate: {C.BOLD}{wr:.0f}%{C.RESET}  "
          f"{C.DIM}|{C.RESET}  P&L: {C.pnl(total_pnl)}")
    print(f"  {C.DIM}Avg win: {C.GREEN}+${avg_win:.2f}{C.RESET}  "
          f"{C.DIM}|  Avg loss: {C.RED}${avg_loss:.2f}{C.RESET}")

    # Win rate bar
    bw = 30
    wf = int(bw * (wr / 100))
    bar = f"{C.GREEN}{'█' * wf}{C.RED}{'█' * (bw - wf)}{C.RESET}"
    print(f"\n  {bar} {wr:.0f}%\n")

    # Trades list
    for t in resolved[-15:]:
        if t.get("won"):
            badge = f"{C.GREEN} W {C.RESET}"
        else:
            badge = f"{C.RED} L {C.RESET}"
        pnl_str = C.pnl(t.get("profit", 0))
        print(f"  {badge} {t['question'][:40]}  "
              f"{C.DIM}{t['side']} @${t['entry_price']:.2f}{C.RESET}  "
              f"{pnl_str}")
    print()


def cmd_reset(state):
    confirm = input(f"  {C.YELLOW}Reset everything? Type 'yes': {C.RESET}").strip().lower()
    if confirm == "yes":
        state["balance"] = STARTING_BALANCE
        state["trades"] = []
        state["resolved_pnl"] = 0.0
        save_state(state)
        print(f"  {C.GREEN}Reset to ${STARTING_BALANCE:,.2f}.{C.RESET}")


def auto_check_resolved(state):
    """Silently check for resolved bets on startup."""
    open_trades = [t for t in state["trades"] if t["status"] == "open"]
    if not open_trades:
        return 0
    count = 0
    for t in open_trades:
        m = fetch_market_by_id(t["market_id"])
        if not m or not m.get("closed"):
            continue
        p_yes, _ = parse_prices(m.get("outcomePrices", ""))
        if p_yes is None:
            continue
        won = (t["side"] == "YES" and p_yes > 0.9) or \
              (t["side"] == "NO" and p_yes < 0.1)
        if won:
            payout = t["shares"] * 1.0
            profit = payout - t["cost"]
            state["balance"] += payout
        else:
            profit = -t["cost"]
        t["status"] = "resolved"
        t["won"] = won
        t["profit"] = round(profit, 2)
        state["resolved_pnl"] += profit
        count += 1
    if count:
        save_state(state)
    return count


def cmd_calibration(state):
    """Show calibration report: Brier score, decomposition, Platt params."""
    report = engine.get_calibration_report()

    if report.get("status") == "insufficient_data":
        n_pred = report.get("n", 0)
        n_resolved = report.get("resolved", 0)
        print(f"\n  {C.YELLOW}Not enough resolved data for calibration.{C.RESET}")
        print(f"  {C.DIM}Predictions logged: {n_pred} | Resolved: {n_resolved} (need 1+){C.RESET}")
        return

    print(f"\n  {C.BG_BLUE}{C.WHITE}{C.BOLD} CALIBRATION REPORT {C.RESET}\n")

    # Headline numbers
    brier = report["brier"]
    mkt_brier = report["market_brier"]
    beating = report.get("beating_market", False)

    brier_color = C.GREEN if brier < 0.2 else C.YELLOW if brier < 0.3 else C.RED
    mkt_color = C.GREEN if beating else C.RED

    print(f"  {C.BOLD}Our Brier:{C.RESET}    {brier_color}{brier:.4f}{C.RESET}  "
          f"{C.DIM}(lower = better, <0.1 = excellent){C.RESET}")
    print(f"  {C.BOLD}Market Brier:{C.RESET} {mkt_brier:.4f}  "
          f"{mkt_color}{'(we are BEATING the market!)' if beating else '(market is better)'}{C.RESET}")
    print(f"  {C.BOLD}Log Loss:{C.RESET}     {report['log_loss']:.4f}")
    print(f"  {C.BOLD}Skill Score:{C.RESET}  {report['skill_score']:.4f}  "
          f"{C.DIM}(>0 = better than naive){C.RESET}")

    # Decomposition
    print(f"\n  {C.BOLD}Brier Decomposition:{C.RESET}")
    print(f"  Reliability:  {report['reliability']:.4f}  {C.DIM}(lower = better calibrated){C.RESET}")
    print(f"  Resolution:   {report['resolution']:.4f}  {C.DIM}(higher = better differentiation){C.RESET}")
    print(f"  Uncertainty:  {report['uncertainty']:.4f}  {C.DIM}(inherent, fixed){C.RESET}")

    # Platt params
    a = report.get("platt_a", 1.0)
    b = report.get("platt_b", 0.0)
    platt_active = report.get("n", 0) >= CONFIG.get("min_calibration_samples", 30)
    print(f"\n  {C.BOLD}Platt Scaling:{C.RESET}  a={a:.4f}  b={b:.4f}  "
          f"{'[ACTIVE]' if platt_active else '[not enough data yet]'}")

    # Calibration bins
    bins = report.get("bins", {})
    if bins:
        print(f"\n  {C.BOLD}Calibration by probability bin:{C.RESET}")
        print(f"  {'Bin':>6}  {'Avg Fcst':>9}  {'Avg Outcome':>12}  {'Count':>6}  {'Gap':>6}")
        for b_key in sorted(bins.keys(), key=lambda x: int(x)):
            b_data = bins[b_key]
            gap = abs(b_data["avg_forecast"] - b_data["avg_outcome"])
            gap_color = C.GREEN if gap < 0.05 else C.YELLOW if gap < 0.10 else C.RED
            lo = int(b_key) * 10
            hi = lo + 10
            print(f"  {lo:>2}-{hi:<2}%  {b_data['avg_forecast']:>9.3f}  "
                  f"{b_data['avg_outcome']:>12.3f}  {b_data['count']:>6}  "
                  f"{gap_color}{gap:>5.3f}{C.RESET}")

    # Diagnosis
    print(f"\n  {C.BOLD}Diagnosis:{C.RESET} {report.get('diagnosis', 'N/A')}")
    print(f"  {C.DIM}Based on {report['n']} resolved predictions{C.RESET}\n")


def cmd_negrisk(state):
    """Scan for NegRisk arbitrage opportunities."""
    spinner = Spinner("Scanning for NegRisk arbitrage...")
    spinner.start()
    markets = engine.fetch_markets(CONFIG["markets_to_scan"])
    arbs = engine.detect_negrisk_arbitrage(markets)
    spinner.stop()

    if not arbs:
        print(f"\n  {C.GRAY}No NegRisk arbitrage found in current markets.{C.RESET}")
        return

    print(f"\n  {C.BG_GREEN}{C.WHITE}{C.BOLD} NEGRISK ARBITRAGE ({len(arbs)} found) {C.RESET}\n")
    for arb in arbs[:5]:
        print(f"  {C.BOLD}{arb['slug'][:50]}{C.RESET}")
        print(f"  {C.DIM}Markets: {arb['num_markets']}  |  "
              f"Sum YES: {arb['total_yes_price']:.4f}  |  "
              f"Deviation: {arb['deviation']:+.4f}{C.RESET}")
        print(f"  {C.GREEN}Strategy: {arb['direction']}  |  "
              f"Est. profit: {arb['arb_profit_pct']:.2f}%{C.RESET}")
        for m in arb["markets"][:5]:
            print(f"    {C.DIM}- {m['question']}  (YES: {m['price_yes']:.2f}){C.RESET}")
        print()


def cmd_autopilot(state):
    """Fully autonomous mode using engine v3."""
    import time as _time

    interval_min = CONFIG.get("scan_interval_min", 120) // 4  # faster in CLI mode
    max_trades_per_cycle = CONFIG["max_trades_per_cycle"]

    print(f"\n  {C.BG_GREEN}{C.WHITE}{C.BOLD} AUTOPILOT v3 ENGAGED {C.RESET}")
    print(f"  {C.DIM}Scanning every {interval_min}min | Max {max_trades_per_cycle} trades/cycle{C.RESET}")
    print(f"  {C.DIM}{CONFIG['ensemble_passes']}-persona ensemble | quarter-Kelly | calibrated{C.RESET}")
    print(f"  {C.DIM}Press Ctrl+C to stop{C.RESET}\n")

    cycle = 0
    try:
        while True:
            cycle += 1
            now = datetime.now().strftime("%H:%M:%S")
            print(f"\n  {C.BG_BLUE}{C.WHITE}{C.BOLD} CYCLE {cycle} {C.RESET} "
                  f"{C.DIM}{now}{C.RESET}")
            print(f"  {C.DIM}{'─' * 50}{C.RESET}")

            # Step 1: Check resolved (uses engine with calibration updates)
            resolved_results = engine.check_resolved(state)
            if resolved_results:
                save_state(state)
                for r in resolved_results:
                    tag = f"{C.GREEN}WIN" if r["won"] else f"{C.RED}LOSS"
                    print(f"  {tag}{C.RESET}  {r['trade']['question'][:40]}  {C.pnl(r['profit'])}")
                print(f"  {C.DIM}{len(resolved_results)} resolved{C.RESET}")

            # Step 2: Check deployment capacity
            current_deployed = deployed_amount(state)
            max_deploy = STARTING_BALANCE * MAX_DEPLOYED_PCT
            available = max_deploy - current_deployed

            if available < 5 or state["balance"] < 5:
                print(f"  {C.YELLOW}Fully deployed ({current_deployed/STARTING_BALANCE:.0%}). "
                      f"Waiting for resolutions.{C.RESET}")
            else:
                # Step 3: Fetch and analyze with v3 engine
                print(f"  {C.CYAN}Scanning markets (v3 engine)...{C.RESET}")
                markets = engine.fetch_markets(CONFIG["markets_to_scan"])
                open_ids = {t["market_id"] for t in state["trades"]
                            if t["status"] == "open"}
                markets = [m for m in markets if m.get("id") not in open_ids]

                opportunities = []
                for idx, m in enumerate(markets):
                    short_q = m.get("question", "")[:40]
                    print(f"  {C.DIM}[{idx+1}/{len(markets)}] {short_q}...{C.RESET}", end="", flush=True)
                    opp = engine.analyze_market(m, CONFIG)
                    if opp:
                        opportunities.append(opp)
                        print(f"  {C.GREEN}edge {opp['edge']:+.0%}{C.RESET}")
                    else:
                        print(f"  {C.GRAY}skip{C.RESET}")

                if opportunities:
                    opportunities.sort(key=lambda x: x.get("score", 0), reverse=True)
                    trades_placed = 0

                    for opp in opportunities[:max_trades_per_cycle]:
                        avail = min(state["balance"],
                                    max_deploy - deployed_amount(state))
                        if avail < 1:
                            break
                        conf = opp.get("confidence", 0.5)
                        size = engine.kelly_size(opp["edge"], opp["entry_price"],
                                                 state["balance"], conf,
                                                 CONFIG["max_bet_fraction"], CONFIG)
                        size = min(size, avail)
                        if size >= 1:
                            _execute_trade(state, opp, size)
                            trades_placed += 1

                    print(f"\n  {C.GREEN}{trades_placed} trades placed{C.RESET}")
                else:
                    print(f"  {C.GRAY}No opportunities this cycle.{C.RESET}")

            # Summary
            open_count = sum(1 for t in state["trades"] if t["status"] == "open")
            dep = deployed_amount(state)
            total_val = state["balance"] + dep + state["resolved_pnl"]
            pnl = total_val - STARTING_BALANCE
            print(f"\n  {C.BOLD}Status:{C.RESET} ${state['balance']:.2f} cash | "
                  f"{open_count} open | P&L: {C.pnl(pnl)}")

            # Show calibration summary if available
            report = engine.get_calibration_report()
            if report.get("status") == "ok":
                print(f"  {C.DIM}Brier: {report['brier']:.4f} | "
                      f"Market: {report['market_brier']:.4f} | "
                      f"{'Beating market' if report.get('beating_market') else 'Behind market'}{C.RESET}")

            print(f"\n  {C.DIM}Next scan in {interval_min} min... (Ctrl+C to stop){C.RESET}")
            _time.sleep(interval_min * 60)

    except KeyboardInterrupt:
        save_state(state)
        print(f"\n\n  {C.YELLOW}Autopilot stopped. State saved.{C.RESET}")


def main():
    enable_ansi_windows()
    state = load_state()

    # Auto-check resolved on startup
    resolved = auto_check_resolved(state)

    while True:
        print_header(state)

        if resolved > 0:
            print(f"  {C.YELLOW}{C.BOLD}! {resolved} bet(s) resolved since last session{C.RESET}")
            print(f"  {C.DIM}  Check [4] Trade history for details{C.RESET}")
            print(f"  {C.DIM}{'─' * 58}{C.RESET}")
            resolved = 0

        print(f"  {C.BOLD}{C.CYAN}[1]{C.RESET} Scan markets     {C.DIM}v3 ensemble analysis{C.RESET}")
        print(f"  {C.BOLD}{C.CYAN}[2]{C.RESET} Portfolio         {C.DIM}Open positions & P&L{C.RESET}")
        print(f"  {C.BOLD}{C.CYAN}[3]{C.RESET} Check resolved    {C.DIM}Settle finished bets{C.RESET}")
        print(f"  {C.BOLD}{C.CYAN}[4]{C.RESET} Trade history     {C.DIM}Past wins & losses{C.RESET}")
        print(f"  {C.BOLD}{C.MAGENTA}[8]{C.RESET} {C.MAGENTA}Calibration{C.RESET}      {C.DIM}Brier score & accuracy report{C.RESET}")
        print(f"  {C.BOLD}{C.MAGENTA}[9]{C.RESET} {C.MAGENTA}NegRisk arb{C.RESET}     {C.DIM}Risk-free arbitrage scan{C.RESET}")
        print(f"  {C.BOLD}{C.GREEN}[7]{C.RESET} {C.GREEN}Autopilot{C.RESET}        {C.DIM}Fully autonomous v3 trading{C.RESET}")
        print(f"  {C.BOLD}{C.GRAY}[5]{C.RESET} {C.DIM}Reset{C.RESET}")
        print(f"  {C.BOLD}{C.GRAY}[6]{C.RESET} {C.DIM}Quit{C.RESET}")
        print()

        choice = input(f"  {C.BOLD}> {C.RESET}").strip()
        if choice == "1":
            cmd_scan(state)
        elif choice == "2":
            cmd_portfolio(state)
        elif choice == "3":
            cmd_check_resolved(state)
        elif choice == "4":
            cmd_history(state)
        elif choice == "5":
            cmd_reset(state)
        elif choice == "6":
            save_state(state)
            print(f"\n  {C.GREEN}Saved. Goodbye!{C.RESET}\n")
            break
        elif choice == "7":
            cmd_autopilot(state)
        elif choice == "8":
            cmd_calibration(state)
        elif choice == "9":
            cmd_negrisk(state)
        else:
            print(f"  {C.YELLOW}Pick 1-9.{C.RESET}")


if __name__ == "__main__":
    main()
