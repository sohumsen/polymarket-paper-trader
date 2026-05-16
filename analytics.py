"""
Analytics + public PnL export for the Polymarket paper trader.

Reads trades.json + calibration.json, writes machine-readable CSV/JSON and a
human-readable markdown report into public/. Run from the project root:

    python analytics.py

No third-party deps — uses stdlib only so a cron job can run it without
touching the FastAPI process.
"""

from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import mean, pstdev
from typing import Iterable

ROOT = Path(__file__).parent
PUBLIC = ROOT / "public"
STARTING_BALANCE = 1000.0


# ── Loading ──────────────────────────────────────────────────────

def load_trades() -> tuple[list[dict], float]:
    raw = json.loads((ROOT / "trades.json").read_text())
    return raw["trades"], raw["balance"]


def load_calibration() -> dict:
    return json.loads((ROOT / "calibration.json").read_text())


def backfill_calibration_from_trades() -> dict:
    """One-shot: fill calibration outcomes using natural-resolve trades.

    Early-exit and stop-loss trades are skipped because they don't tell us
    how the underlying market actually resolved — only how the price moved
    while we held. Natural resolutions hit the 0.9/0.1 threshold so the
    binary outcome is known.

    Returns counts: {"filled": N, "skipped_no_match": M}.
    """
    trades = json.loads((ROOT / "trades.json").read_text())["trades"]
    cal = load_calibration()

    # market_id -> outcome (1 if YES won, 0 if NO won)
    outcomes: dict[str, int] = {}
    for t in trades:
        if t.get("status") != "resolved":
            continue
        if t.get("exit_reason"):  # early exit or stop-loss
            continue
        mid = str(t["market_id"])
        side = t["side"]
        won = bool(t.get("won"))
        # YES win => outcome=1; NO win => outcome=0
        outcomes[mid] = 1 if (side == "YES") == won else 0

    filled = 0
    for p in cal["predictions"]:
        if p["outcome"] is not None:
            continue
        mid = str(p["market_id"])
        if mid in outcomes:
            p["outcome"] = float(outcomes[mid])
            filled += 1

    (ROOT / "calibration.json").write_text(json.dumps(cal, indent=2))
    return {"filled": filled, "natural_resolve_markets": len(outcomes)}


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ── Core metrics ─────────────────────────────────────────────────

def equity_curve(trades: list[dict]) -> list[tuple[date, float]]:
    """Realized equity by day: $1,000 + cumulative realized P&L.

    Open positions don't move the curve — capital that's deployed but not
    yet resolved is neither a win nor a loss. Only counts trades that have
    actually resolved (natural close or early exit) at the resolution
    timestamp (falling back to entry timestamp if resolved_at isn't logged).
    """
    by_day: dict[date, float] = defaultdict(float)
    for t in trades:
        if t.get("status") != "resolved":
            continue
        when = parse_ts(t.get("resolved_at", t["timestamp"])).date()
        by_day[when] += float(t.get("profit", 0))

    if not by_day:
        return []

    # Always anchor at the first trade's date so the curve starts at $1000
    first_day = min(parse_ts(t["timestamp"]).date() for t in trades)
    days = sorted({first_day, *by_day.keys()})

    curve = []
    running = STARTING_BALANCE
    for d in days:
        running += by_day.get(d, 0.0)
        curve.append((d, round(running, 2)))
    return curve


def summary_metrics(trades: list[dict], balance: float) -> dict:
    resolved = [t for t in trades if t.get("status") == "resolved"]
    open_ = [t for t in trades if t.get("status") == "open"]

    wins = [t for t in resolved if t.get("won")]
    losses = [t for t in resolved if not t.get("won")]
    realized_pnl = sum(float(t.get("profit", 0)) for t in resolved)
    deployed = sum(float(t["cost"]) for t in open_)

    # Daily returns for Sharpe
    curve = equity_curve(trades)
    daily_returns = []
    for i in range(1, len(curve)):
        prev = curve[i - 1][1]
        if prev <= 0:
            continue
        daily_returns.append((curve[i][1] - prev) / prev)
    sharpe = None
    if len(daily_returns) > 1:
        sd = pstdev(daily_returns)
        if sd > 0:
            sharpe = mean(daily_returns) / sd * math.sqrt(252)

    avg_win = mean(float(t["profit"]) for t in wins) if wins else 0.0
    avg_loss = mean(float(t["profit"]) for t in losses) if losses else 0.0

    # Max drawdown on equity curve
    peak = STARTING_BALANCE
    max_dd = 0.0
    for _, eq in curve:
        peak = max(peak, eq)
        dd = (eq - peak) / peak
        max_dd = min(max_dd, dd)

    first_ts = min(parse_ts(t["timestamp"]) for t in trades) if trades else None
    last_ts = max(parse_ts(t["timestamp"]) for t in trades) if trades else None

    return {
        "starting_balance": STARTING_BALANCE,
        "current_balance": round(balance, 2),
        "deployed_in_open": round(deployed, 2),
        "total_equity_realized_plus_cash": round(balance + 0, 2),
        "realized_pnl": round(realized_pnl, 2),
        "realized_return_pct": round(realized_pnl / STARTING_BALANCE * 100, 2),
        "trades_total": len(trades),
        "trades_resolved": len(resolved),
        "trades_open": len(open_),
        "wins": len(wins),
        "losses": len(losses),
        "hit_rate_pct": round(len(wins) / len(resolved) * 100, 1) if resolved else None,
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "expectancy_per_trade": round(realized_pnl / len(resolved), 2) if resolved else None,
        "sharpe_annualized": round(sharpe, 2) if sharpe is not None else None,
        "max_drawdown_pct": round(max_dd * 100, 2),
        "first_trade": first_ts.isoformat() if first_ts else None,
        "last_trade": last_ts.isoformat() if last_ts else None,
        "days_live": (last_ts - first_ts).days if first_ts and last_ts else None,
    }


# ── Breakdowns ───────────────────────────────────────────────────

def _group_pnl(resolved: list[dict], key_fn) -> dict[str, dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for t in resolved:
        groups[key_fn(t)].append(t)
    out = {}
    for k, ts in groups.items():
        wins = sum(1 for t in ts if t.get("won"))
        pnl = sum(float(t.get("profit", 0)) for t in ts)
        cost = sum(float(t["cost"]) for t in ts)
        out[k] = {
            "n": len(ts),
            "wins": wins,
            "hit_rate_pct": round(wins / len(ts) * 100, 1),
            "pnl": round(pnl, 2),
            "roi_pct": round(pnl / cost * 100, 2) if cost else 0,
        }
    return out


def breakdowns(trades: list[dict]) -> dict:
    resolved = [t for t in trades if t.get("status") == "resolved"]
    return {
        "by_side": _group_pnl(resolved, lambda t: t["side"]),
        "by_category": _group_pnl(resolved, lambda t: t.get("category", "unknown")),
        "by_exit": _group_pnl(
            resolved,
            lambda t: (
                "stop_loss" if (t.get("exit_reason") or "").startswith("stop-loss")
                else "edge_eroded" if (t.get("exit_reason") or "").startswith("edge eroded")
                else "natural_resolve"
            ),
        ),
        "by_edge_bucket": _group_pnl(
            resolved,
            lambda t: _bucket(t.get("edge"), [0.05, 0.10, 0.15, 0.20]),
        ),
        "by_size_bucket": _group_pnl(
            resolved,
            lambda t: _bucket(t["cost"], [5, 15, 40, 100]),
        ),
    }


def _bucket(v, edges: list[float]) -> str:
    if v is None:
        return "unknown"
    v = float(v)
    prev = "-inf"
    for e in edges:
        if v < e:
            return f"[{prev}, {e})"
        prev = str(e)
    return f"[{prev}, inf)"


# ── Calibration ──────────────────────────────────────────────────

def calibration_table(cal: dict) -> tuple[list[dict], dict]:
    """Reliability diagram + headline scores. Independent of engine.py so
    this script keeps working even if the engine is refactored.
    """
    resolved = [p for p in cal["predictions"] if p.get("outcome") is not None]
    if not resolved:
        return [], {"n": 0}

    n = len(resolved)
    forecasts = [float(p["our_estimate"]) for p in resolved]
    outcomes = [int(p["outcome"]) for p in resolved]
    market = [float(p["market_price"]) for p in resolved]

    brier = sum((f - o) ** 2 for f, o in zip(forecasts, outcomes)) / n
    market_brier = sum((f - o) ** 2 for f, o in zip(market, outcomes)) / n
    base_rate = sum(outcomes) / n
    uncertainty = base_rate * (1 - base_rate)

    log_loss = 0.0
    for f, o in zip(forecasts, outcomes):
        f = min(0.999, max(0.001, f))
        log_loss -= o * math.log(f) + (1 - o) * math.log(1 - f)
    log_loss /= n

    # 10 bins
    bins: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for f, o in zip(forecasts, outcomes):
        bins[min(int(f * 10), 9)].append((f, o))
    rows = []
    for b in range(10):
        items = bins.get(b, [])
        if not items:
            continue
        rows.append({
            "bin": f"{b/10:.1f}-{(b+1)/10:.1f}",
            "n": len(items),
            "avg_forecast": round(mean(f for f, _ in items), 3),
            "realized_rate": round(mean(o for _, o in items), 3),
        })

    headline = {
        "n": n,
        "brier": round(brier, 4),
        "market_brier": round(market_brier, 4),
        "skill_vs_market": round(market_brier - brier, 4),  # positive = beating market
        "log_loss": round(log_loss, 4),
        "base_rate": round(base_rate, 3),
        "uncertainty": round(uncertainty, 4),
        "platt_a": cal.get("platt_a"),
        "platt_b": cal.get("platt_b"),
    }
    return rows, headline


# ── Output ───────────────────────────────────────────────────────

def write_csv(path: Path, rows: Iterable[dict], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _render_charts(curve: list[tuple[date, float]], cal_rows: list[dict]) -> None:
    """Render equity curve and reliability diagram as PNGs into public/.

    matplotlib is loaded lazily so analytics.py still runs (with charts
    skipped) on machines that don't have it installed.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return

    # Equity curve
    if curve:
        fig, ax = plt.subplots(figsize=(9, 4.5))
        xs = [d for d, _ in curve]
        ys = [eq for _, eq in curve]
        ax.plot(xs, ys, linewidth=2, color="#2563eb")
        ax.axhline(STARTING_BALANCE, color="#94a3b8", linestyle="--",
                   linewidth=1, label=f"start ${STARTING_BALANCE:.0f}")
        ax.fill_between(xs, ys, STARTING_BALANCE,
                        where=[y >= STARTING_BALANCE for y in ys],
                        alpha=0.15, color="#22c55e")
        ax.fill_between(xs, ys, STARTING_BALANCE,
                        where=[y < STARTING_BALANCE for y in ys],
                        alpha=0.15, color="#ef4444")
        ax.set_title("Realized equity curve (paper)")
        ax.set_ylabel("Equity ($)")
        ax.grid(True, alpha=0.3)
        ax.legend(loc="lower left")
        fig.autofmt_xdate()
        fig.tight_layout()
        fig.savefig(PUBLIC / "equity.png", dpi=130)
        plt.close(fig)

    # Reliability diagram
    if cal_rows:
        fig, ax = plt.subplots(figsize=(6, 6))
        xs = [r["avg_forecast"] for r in cal_rows]
        ys = [r["realized_rate"] for r in cal_rows]
        sizes = [max(40, r["n"] * 25) for r in cal_rows]
        ax.plot([0, 1], [0, 1], color="#94a3b8", linestyle="--",
                linewidth=1, label="perfect calibration")
        ax.scatter(xs, ys, s=sizes, color="#2563eb", alpha=0.7,
                   edgecolors="white", linewidth=1.5)
        for r in cal_rows:
            ax.annotate(f"n={r['n']}",
                        (r["avg_forecast"], r["realized_rate"]),
                        textcoords="offset points", xytext=(8, 4),
                        fontsize=8, color="#475569")
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.set_xlabel("Predicted probability")
        ax.set_ylabel("Realized frequency")
        ax.set_title("Reliability diagram (point size = n)")
        ax.grid(True, alpha=0.3)
        ax.legend(loc="upper left")
        fig.tight_layout()
        fig.savefig(PUBLIC / "reliability.png", dpi=130)
        plt.close(fig)


def export_all() -> dict:
    PUBLIC.mkdir(exist_ok=True)
    trades, balance = load_trades()
    cal = load_calibration()

    metrics = summary_metrics(trades, balance)
    bd = breakdowns(trades)
    cal_rows, cal_headline = calibration_table(cal)
    curve = equity_curve(trades)

    # Trades CSV (only stable columns)
    fields = [
        "timestamp", "market_id", "question", "side", "category",
        "entry_price", "our_estimate", "edge", "confidence",
        "cost", "shares", "status", "won", "profit", "exit_reason",
    ]
    write_csv(PUBLIC / "trades.csv", trades, fields)

    # Equity curve CSV
    with (PUBLIC / "equity.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "equity"])
        for d, eq in curve:
            w.writerow([d.isoformat(), eq])

    # Calibration CSV
    write_csv(PUBLIC / "calibration_bins.csv", cal_rows,
              ["bin", "n", "avg_forecast", "realized_rate"])

    # JSON snapshot for dashboards
    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "metrics": metrics,
        "breakdowns": bd,
        "calibration": cal_headline,
        "calibration_bins": cal_rows,
    }
    (PUBLIC / "metrics.json").write_text(json.dumps(snapshot, indent=2))

    # Markdown report
    (PUBLIC / "diagnostics.md").write_text(render_markdown(snapshot, curve), encoding="utf-8")

    # Charts (best-effort; skipped if matplotlib not installed)
    _render_charts(curve, cal_rows)

    return snapshot


def render_markdown(snap: dict, curve: list[tuple[date, float]]) -> str:
    m = snap["metrics"]
    cal = snap["calibration"]
    bd = snap["breakdowns"]

    lines = [
        "# Polymarket Paper Trader — Public Track Record",
        "",
        f"_Generated {snap['generated_at']}. Starting balance ${m['starting_balance']:.0f}, "
        f"all trades are paper (no real money)._",
        "",
        "## Headline",
        "",
        f"- **Cash balance:** ${m['current_balance']:.2f}",
        f"- **Deployed in open positions:** ${m['deployed_in_open']:.2f}",
        f"- **Realized P&L:** ${m['realized_pnl']:+.2f} ({m['realized_return_pct']:+.2f}%)",
        f"- **Hit rate:** {m['hit_rate_pct']}% ({m['wins']}/{m['trades_resolved']} resolved)",
        f"- **Expectancy/trade:** ${m['expectancy_per_trade']:+.2f}",
        f"- **Avg win / avg loss:** ${m['avg_win']:+.2f} / ${m['avg_loss']:+.2f}",
        f"- **Sharpe (annualized, daily):** {m['sharpe_annualized']}",
        f"- **Max drawdown:** {m['max_drawdown_pct']}%",
        f"- **Live for:** {m['days_live']} days "
        f"({m['first_trade'][:10]} -> {m['last_trade'][:10]})",
        "",
        "## Calibration vs market",
        "",
        f"- **Our Brier:** {cal['brier']} vs **Market Brier:** {cal['market_brier']}  "
        f"-> skill margin **{cal['skill_vs_market']:+.4f}** "
        f"({'beating' if cal['skill_vs_market'] > 0 else 'trailing'} the market on calibration)",
        f"- **Log loss:** {cal['log_loss']}  ·  **Base rate:** {cal['base_rate']}  ·  "
        f"**N resolved predictions:** {cal['n']}",
        f"- **Platt scaling:** a={cal['platt_a']}, b={cal['platt_b']}",
        "",
        "### Reliability table",
        "",
        "| Forecast bin | N | Avg forecast | Realized rate |",
        "|---|---:|---:|---:|",
    ]
    for r in snap["calibration_bins"]:
        lines.append(
            f"| {r['bin']} | {r['n']} | {r['avg_forecast']} | {r['realized_rate']} |"
        )

    def section(title: str, table: dict):
        lines.append("")
        lines.append(f"### {title}")
        lines.append("")
        lines.append("| Bucket | N | Hit rate | PnL | ROI on cost |")
        lines.append("|---|---:|---:|---:|---:|")
        for k, v in sorted(table.items(), key=lambda kv: -kv[1]["pnl"]):
            lines.append(
                f"| {k} | {v['n']} | {v['hit_rate_pct']}% | "
                f"${v['pnl']:+.2f} | {v['roi_pct']:+.2f}% |"
            )

    lines.append("")
    lines.append("## Breakdowns")
    section("By side", bd["by_side"])
    section("By category", bd["by_category"])
    section("By exit type", bd["by_exit"])
    section("By predicted edge", bd["by_edge_bucket"])
    section("By position size ($)", bd["by_size_bucket"])

    lines += [
        "",
        "## Equity curve",
        "",
        "See [equity.csv](equity.csv). Last 14 points:",
        "",
        "| Date | Equity |",
        "|---|---:|",
    ]
    for d, eq in curve[-14:]:
        lines.append(f"| {d.isoformat()} | ${eq:.2f} |")

    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    snap = export_all()
    m = snap["metrics"]
    print(f"Wrote public/ — realized {m['realized_pnl']:+.2f} on "
          f"{m['trades_resolved']} resolved trades, "
          f"{m['deployed_in_open']:.2f} deployed in {m['trades_open']} open.")
