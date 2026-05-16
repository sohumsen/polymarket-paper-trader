# Polymarket Paper Trader — Public Track Record

_Generated 2026-05-16T10:16:34.217253+00:00. Starting balance $1000, all trades are paper (no real money)._

## Headline

- **Cash balance:** $468.96
- **Deployed in open positions:** $537.58
- **Realized P&L:** $+6.54 (+0.65%)
- **Hit rate:** 66.4% (73/110 resolved)
- **Expectancy/trade:** $+0.06
- **Avg win / avg loss:** $+13.40 / $-26.25
- **Sharpe (annualized, daily):** 1.96
- **Max drawdown:** -21.88%
- **Live for:** 28 days (2026-04-11 -> 2026-05-09)

## Calibration vs market

- **Our Brier:** 0.2852 vs **Market Brier:** 0.2503  -> skill margin **-0.0348** (trailing the market on calibration)
- **Log loss:** 0.8692  ·  **Base rate:** 0.3  ·  **N resolved predictions:** 30
- **Platt scaling:** a=0.5, b=-0.3276

### Reliability table

| Forecast bin | N | Avg forecast | Realized rate |
|---|---:|---:|---:|
| 0.0-0.1 | 6 | 0.057 | 0.333 |
| 0.1-0.2 | 6 | 0.121 | 0.167 |
| 0.2-0.3 | 3 | 0.25 | 0 |
| 0.3-0.4 | 3 | 0.328 | 0.333 |
| 0.4-0.5 | 4 | 0.429 | 1 |
| 0.5-0.6 | 2 | 0.55 | 0 |
| 0.6-0.7 | 2 | 0.627 | 0 |
| 0.7-0.8 | 1 | 0.765 | 0 |
| 0.8-0.9 | 1 | 0.842 | 1 |
| 0.9-1.0 | 2 | 0.929 | 0 |

## Breakdowns

### By side

| Bucket | N | Hit rate | PnL | ROI on cost |
|---|---:|---:|---:|---:|
| YES | 33 | 60.6% | $+27.36 | +3.78% |
| NO | 77 | 68.8% | $-20.82 | -0.82% |

### By category

| Bucket | N | Hit rate | PnL | ROI on cost |
|---|---:|---:|---:|---:|
| sports | 71 | 66.2% | $+105.34 | +5.68% |
| crypto | 26 | 84.6% | $+8.94 | +0.83% |
| politics | 3 | 66.7% | $-14.05 | -11.05% |
| unknown | 10 | 20.0% | $-93.69 | -49.19% |

### By exit type

| Bucket | N | Hit rate | PnL | ROI on cost |
|---|---:|---:|---:|---:|
| edge_eroded | 60 | 100.0% | $+803.82 | +42.45% |
| natural_resolve | 30 | 43.3% | $-267.08 | -35.74% |
| stop_loss | 20 | 0.0% | $-530.20 | -86.79% |

### By predicted edge

| Bucket | N | Hit rate | PnL | ROI on cost |
|---|---:|---:|---:|---:|
| [0.15, 0.2) | 15 | 80.0% | $+105.96 | +20.51% |
| [0.1, 0.15) | 37 | 73.0% | $+62.12 | +5.25% |
| [-inf, 0.05) | 1 | 100.0% | $+6.12 | +41.83% |
| [0.05, 0.1) | 42 | 61.9% | $-25.36 | -2.74% |
| [0.2, inf) | 13 | 53.8% | $-66.74 | -12.59% |
| unknown | 2 | 0.0% | $-75.56 | -92.70% |

### By position size ($)

| Bucket | N | Hit rate | PnL | ROI on cost |
|---|---:|---:|---:|---:|
| [15, 40) | 54 | 74.1% | $+177.20 | +12.81% |
| [5, 15) | 17 | 52.9% | $+4.98 | +2.93% |
| [-inf, 5) | 8 | 37.5% | $-4.96 | -17.77% |
| [40, 100) | 31 | 67.7% | $-170.68 | -10.22% |

## Equity curve

See [equity.csv](equity.csv). Last 14 points:

| Date | Equity |
|---|---:|
| 2026-04-18 | $969.36 |
| 2026-04-19 | $1011.07 |
| 2026-04-20 | $996.35 |
| 2026-04-21 | $1021.36 |
| 2026-04-22 | $1121.20 |
| 2026-04-23 | $1035.78 |
| 2026-04-24 | $1095.29 |
| 2026-04-25 | $1231.37 |
| 2026-04-26 | $1157.15 |
| 2026-04-27 | $961.97 |
| 2026-05-03 | $984.87 |
| 2026-05-04 | $1005.32 |
| 2026-05-05 | $1007.90 |
| 2026-05-09 | $1006.54 |
